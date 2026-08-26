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

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.logging import recent_lines

__all__ = ["DebugPane", "SHOWN_LINES", "REFRESH_MS"]

#: How many lines are on screen. The ring holds more; this is what fits.
SHOWN_LINES = 50

#: A second is slow enough to be free and fast enough to look live. Indexing
#: logs a checkpoint every two seconds at most, so nothing is missed by it.
REFRESH_MS = 1_000


class DebugPane(QWidget):
    """A scrolling view of the last `SHOWN_LINES` log lines."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

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

        controls = QHBoxLayout()
        controls.addWidget(self.follow)
        controls.addStretch(1)
        controls.addWidget(self.copy_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(caption)
        layout.addWidget(self.view, stretch=1)
        layout.addLayout(controls)

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

        self.view.setPlainText("\n".join(lines))

        # Follow the tail only when asked, and only when they were already at
        # the bottom - yanking somebody back down mid-read is the behaviour that
        # makes a live log useless for reading.
        if self.follow.isChecked() and at_bottom:
            bar.setValue(bar.maximum())

    def _copy(self) -> None:
        from PyQt6.QtWidgets import QApplication

        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText("\n".join(self._shown))
