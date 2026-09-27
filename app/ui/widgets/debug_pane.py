r"""The last few hundred log lines, inside the window.

Layer: L5

**This is what replaces the console.** `leasha.cmd` launched the window with
`python.exe`, the console-subsystem binary, so Windows attached a terminal to
every session - an empty black rectangle sitting behind the application for as
long as it ran. `pythonw.exe` removes it, and removing it takes the running
commentary with it: `sys.stderr` is `None` under pythonw, so the lines that used
to scroll past have nowhere to go.

The file log has always had all of it. But *"open the logs folder and find
today's file"* is not something anybody does while wondering whether the
application has hung, which is precisely when the question gets asked - so the
console was doing real work, and it could not simply be deleted.

So the lines are kept in a ring in memory (`core.logging._recent`) and shown
here. Not a log viewer: no search, no filtering, no severity picker. The
question it answers is *"is anything happening"*, and everything past that is
what the log file and `app.cli` are for.

**Polled, not pushed.** A loguru sink emitting a Qt signal would mean the
logging system reaching into the UI thread from whichever thread happened to log
- during indexing, that is four worker threads at once. A timer reading a deque
is duller and cannot deadlock. It only runs while the pane is visible, so the
cost is zero on every tab except this one.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.logging import recent_lines
from app.ui.log_lines import colour_token, is_actionable, path_in

__all__ = ["DebugPane", "SHOWN_LINES", "REFRESH_MS"]

#: How many lines are on screen. The ring holds more; this is what fits.
SHOWN_LINES = 50

#: A second is slow enough to be free and fast enough to look live. Indexing
#: logs a checkpoint every two seconds at most, so nothing is missed by it.
REFRESH_MS = 1_000


class DebugPane(QGroupBox):
    """A scrolling view of the last `SHOWN_LINES` log lines.

    Its own group box rather than something Settings wraps: the title is part
    of what it is, and assembling the frame in the view cost `settings_view.py`
    three of the lines its 250-line guard allows.

    **Workspace §1a/§1b: coloured by level, and clickable where it helps.**
    Warnings and errors are painted from theme tokens - never a hex value,
    because a hardcoded red disappears on the dark theme - and the level word
    stays in the line, because colour is never the only signal. An error that
    names a file can be clicked to go to it; nothing else can, so an
    underline never promises something that does not happen.
    """

    #: Somebody clicked a log line that names a file. The window routes it.
    file_chosen = pyqtSignal(str)
    #: Somebody asked for the log in its own window. Workspace §1c.
    pop_out = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None, *,
                 poppable: bool = True) -> None:
        super().__init__("Recent activity", parent)

        caption = QLabel(
            "The most recent log lines. The full log is in the logs folder - "
            "this is here so a window with no console can still say what it is "
            "doing.")
        caption.setWordWrap(True)
        caption.setObjectName("settingsHint")

        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        # Selectable and copyable: the first thing anybody does with a line that
        # looks wrong is send it to somebody else.
        self.view.setTextInteractionFlags(
            self.view.textInteractionFlags())
        self.view.setMaximumBlockCount(SHOWN_LINES + 10)
        self.view.setPlaceholderText("Nothing logged yet.")
        self.view.setMinimumHeight(160)

        self.follow = QCheckBox("Scroll to the newest line")
        self.follow.setChecked(True)
        self.follow.setToolTip(
            "Turn this off to read something that has scrolled past without new "
            "lines dragging you back down.")

        self.copy_button = QPushButton("Copy")
        self.copy_button.setToolTip("Copy every line shown here to the clipboard.")
        self.copy_button.clicked.connect(self._copy)

        # §1c. **On the pane rather than in Settings**, because the button
        # belongs beside the thing it pops out - and because `settings_view.py`
        # is at 249 of the 250 lines its guard allows, which is the rule that
        # keeps decisions out of views working exactly as intended.
        self.pop_button = QPushButton("Open in its own window")
        self.pop_button.setToolTip(
            "Opens this log as a separate window you can keep on top and "
            "watch while you work in another application. The log stays "
            "here as well.")
        self.pop_button.clicked.connect(self.pop_out)
        self.pop_button.setVisible(bool(poppable))

        controls = QHBoxLayout()
        controls.addWidget(self.follow)
        controls.addStretch(1)
        controls.addWidget(self.pop_button)
        controls.addWidget(self.copy_button)

        # **The style's own margins, as every other card has** (order 0x
        # section 9, review finding 13). They were zeroed here, which in a
        # `QGroupBox` - a bordered card since the redesign - put the caption
        # and the log's box hard against the card's edge, beside "Index
        # storage" and "Environment", whose contents sit inset.
        layout = QVBoxLayout(self)
        layout.addWidget(caption)
        layout.addWidget(self.view, stretch=1)
        layout.addLayout(controls)

        # **Colour needs the palette, and the palette changes.** The pane
        # is repainted from scratch when the theme does, because the tokens
        # are resolved into the HTML at draw time - see `set_palette`.
        self._palette: dict = {}
        self.view.mouseDoubleClickEvent = self._maybe_open       # type: ignore[method-assign]

        self._shown: list[str] = []
        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)

    # -- the loop ------------------------------------------------------------

    def showEvent(self, event: Any) -> None:            # noqa: N802 - Qt's name
        """Start polling only once somebody is looking at it."""
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event: Any) -> None:            # noqa: N802 - Qt's name
        self._timer.stop()
        super().hideEvent(event)

    def refresh(self) -> None:
        """Redraw, **but only when the lines have actually changed.**

        A `setPlainText` on every tick would drop the selection of anybody
        mid-copy and fight the scrollbar of anybody reading, once a second, for
        as long as the pane is open.
        """
        lines = recent_lines(SHOWN_LINES)
        if lines == self._shown:
            return
        self._shown = lines

        bar = self.view.verticalScrollBar()
        at_bottom = bar.value() >= bar.maximum() - 4

        self._draw(lines)

        # Follow the tail only when asked, and only when they were already at
        # the bottom - yanking somebody back down mid-read is the behaviour that
        # makes a live log useless for reading.
        if self.follow.isChecked() and at_bottom:
            bar.setValue(bar.maximum())

    # -- §1a: colour by level, from tokens ------------------------------------

    def set_palette(self, palette: Any) -> None:
        """The theme's colours, pushed in. Redraws what is on screen.

        Pushed rather than read: this widget does not know which theme is on,
        and `MainWindow._apply_theme` is the one place that does.
        """
        self._palette = dict(palette or {})
        if self._shown:
            self._draw(self._shown)

    def _draw(self, lines: Any) -> None:
        r"""Paint the lines, warnings and errors in the theme's own colours.

        **HTML rather than a syntax highlighter**, because the whole view is
        rebuilt when it changes anyway - a `QSyntaxHighlighter` would re-run
        per block on every one of those rebuilds for no gain.

        With no palette the lines are drawn plain, which is what a widget
        built in a test sees, and is correct: no colour is better than a
        wrong one.
        """
        if not self._palette:
            self.view.setPlainText("\n".join(lines))
            return

        from html import escape

        painted = []
        for line in lines:
            colour = self._palette.get(colour_token(line), "")
            text = escape(str(line)).replace(" ", "&nbsp;")
            if is_actionable(line):
                # Underlined *and* coloured, because underline alone reads as
                # a hyperlink and colour alone is not a signal at all.
                painted.append(
                    f'<span style="color:{colour};text-decoration:underline">'
                    f"{text}</span>")
            elif colour:
                painted.append(f'<span style="color:{colour}">{text}</span>')
            else:
                painted.append(text)
        self.view.clear()
        self.view.appendHtml("<br>".join(painted))

    # -- §1b: a log you can act on --------------------------------------------

    def _maybe_open(self, event: Any) -> None:
        """Double-click a line that names a file and go to it. Never raises.

        **Double-click, not single.** A single click is how somebody places
        the cursor to select a line and copy it, which is the first thing
        anybody does with a line that looks wrong - and taking that away to
        add a jump would be a poor trade.
        """
        try:
            QPlainTextEdit.mouseDoubleClickEvent(self.view, event)
            cursor = self.view.cursorForPosition(event.pos())
            line = cursor.block().text()
            found = path_in(line) if is_actionable(line) else None
            if found:
                self.file_chosen.emit(found)
        except Exception:                        # noqa: BLE001 - see docstring
            return

    def _copy(self) -> None:
        from PyQt6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText("\n".join(self._shown))
