r"""Ctrl+F inside a preview. Workspace §2b.

Layer: L5

**The last metre of every search**, which is the order's phrase and is exactly
right: Leasha finds the document, and then somebody still has to find the
sentence. Without this the answer to "where in here?" is Ctrl+A, Ctrl+C, and
paste it into something that does have a find box.

**Attached to a `QTextEdit`, not built around one.** `attach(bar, view)` is the
whole contract, so the same bar works over the in-app preview pane and over a
pop-out window without either of them knowing the other exists — §2b asks for
both, and two implementations would be two ways for the same keystroke to
behave.

**Highlighting is a selection format, not a document edit.** `setExtraSelections`
paints over the document without touching it, so nothing here can alter the
text being previewed — §6's view-only rule, kept by construction rather than by
care.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QTextCursor, QTextDocument
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget,
)

__all__ = ["FindBar", "attach_find", "match_count", "summary"]


def match_count(text: Any, needle: Any) -> int:
    """How many times `needle` appears in `text`, case-insensitively.

    Counted here rather than by walking the document, so the number can be
    checked without a widget - and so the count and the highlighting cannot
    disagree, which they would if one counted words and the other characters.
    """
    body = str(text or "")
    wanted = str(needle or "")
    if not body or not wanted:
        return 0
    return body.lower().count(wanted.lower())


def summary(index: int, total: int, *, searching: bool = False) -> str:
    r"""`2 of 7`, or `no matches`, or `""` when nothing has been typed.

    **Words, not a bare number.** `0/0` beside an empty box reads as a broken
    counter; nothing at all reads as a box waiting to be typed in, which is
    what it is.

    **`searching` decides the empty case, not the index.** The first version
    keyed off `index >= 0` - which is false for a fresh search - so typing a
    word that is not in the document said *nothing at all*, and a find box
    that goes silent is one somebody presses again harder. Somebody who typed
    is owed an answer.
    """
    if total <= 0:
        return "no matches" if searching else ""
    return f"{max(1, index + 1)} of {total}"


class FindBar(QWidget):
    """A find box, next/previous, and a count. Hidden until Ctrl+F."""

    #: Somebody pressed Escape or Close. The host hides it and takes focus back.
    dismissed = pyqtSignal()
    #: Shown or hidden, including by an ancestor - `attach_find` keeps its
    #: Escape shortcut enabled exactly while this is True.
    visibilityChanged = pyqtSignal(bool)                     # noqa: N815

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._view: Any = None
        self._index = -1
        self._total = 0

        self.box = QLineEdit()
        self.box.setPlaceholderText("Find in this document")
        self.box.setClearButtonEnabled(True)
        self.box.setAccessibleName("Find in this document")
        self.box.textChanged.connect(self._retype)
        self.box.returnPressed.connect(self.next_match)

        self.previous_button = QPushButton("Previous")
        self.previous_button.setToolTip(
            "Go to the match before this one (Shift+Enter).")
        self.previous_button.clicked.connect(self.previous_match)

        self.next_button = QPushButton("Next")
        self.next_button.setToolTip("Go to the next match (Enter).")
        self.next_button.clicked.connect(self.next_match)

        self.count = QLabel("")
        self.count.setObjectName("resultMeta")
        # **Read out, not just seen.** A count that only exists as pixels is a
        # count somebody using a screen reader does not have - the same rule
        # the results list needed.
        self.count.setAccessibleName("Matches")

        self.close_button = QPushButton("Close")
        self.close_button.setToolTip("Hide the find box (Escape).")
        self.close_button.clicked.connect(self.dismissed)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.box, stretch=1)
        layout.addWidget(self.count)
        layout.addWidget(self.previous_button)
        layout.addWidget(self.next_button)
        layout.addWidget(self.close_button)

    # -- what it is searching -------------------------------------------------

    def attach(self, view: Any) -> None:
        """Point the bar at a `QTextEdit`-like widget. Clears any highlight."""
        self.clear()
        self._view = view

    def clear(self) -> None:
        """Forget the search and take the highlighting off. Never raises."""
        self._index, self._total = -1, 0
        self.count.setText("")
        try:
            if self._view is not None:
                self._view.setExtraSelections([])
        except Exception:                        # noqa: BLE001 - decoration
            return

    def focus(self) -> None:
        """What Ctrl+F does: show, take focus, select what is there.

        Selected rather than cleared, so a second Ctrl+F over the same word
        replaces it by typing and keeps it by pressing Enter.
        """
        self.show()
        self.box.setFocus()
        self.box.selectAll()

    # -- searching ------------------------------------------------------------

    def _retype(self, needle: str) -> None:
        self._index = -1
        self._highlight(needle)
        if needle:
            self.next_match()

    def _text(self) -> str:
        try:
            return self._view.toPlainText() if self._view is not None else ""
        except Exception:                        # noqa: BLE001 - a count
            return ""

    def _highlight(self, needle: str) -> None:
        r"""Paint every match. **Never raises**, and never edits.

        `setExtraSelections` is an overlay: the document is not modified, so a
        preview cannot be changed by looking for something in it.
        """
        self._total = match_count(self._text(), needle)
        self.count.setText(
            summary(self._index, self._total, searching=bool(needle)))
        if self._view is None:
            return
        try:
            from PyQt6.QtWidgets import QTextEdit

            selections = []
            if needle:
                # The theme's own highlight, so this reads the same way the
                # search snippets do and survives a theme change.
                colour = QColor(self._view.palette().color(
                    self._view.palette().ColorRole.Highlight))
                cursor = QTextCursor(self._view.document())
                while True:
                    cursor = self._view.document().find(needle, cursor)
                    if cursor.isNull():
                        break
                    found = QTextEdit.ExtraSelection()
                    found.cursor = cursor
                    found.format.setBackground(colour)
                    selections.append(found)
            self._view.setExtraSelections(selections)
        except Exception:                        # noqa: BLE001 - decoration
            return

    def next_match(self) -> None:
        self._step(backwards=False)

    def previous_match(self) -> None:
        self._step(backwards=True)

    def _step(self, *, backwards: bool) -> None:
        r"""Move to the next match, wrapping. **Never raises.**

        **Wrapping, and silently.** A find that stops dead at the last match
        makes somebody think there are no more; every find box anybody has
        used wraps, and announcing it would be a dialog in the middle of a
        keystroke.
        """
        needle = self.box.text()
        if self._view is None or not needle or self._total <= 0:
            self.count.setText(
                summary(self._index, self._total, searching=bool(needle)))
            return
        try:
            flags = (QTextDocument.FindFlag.FindBackward if backwards
                     else QTextDocument.FindFlag(0))
            if not self._view.find(needle, flags):
                # Off the end: go back to the top (or the bottom) and retry.
                cursor = self._view.textCursor()
                cursor.movePosition(QTextCursor.MoveOperation.End if backwards
                                    else QTextCursor.MoveOperation.Start)
                self._view.setTextCursor(cursor)
                self._view.find(needle, flags)
            self._index = ((self._index - 1) if backwards
                           else (self._index + 1)) % max(1, self._total)
            self.count.setText(
                summary(self._index, self._total, searching=True))
        except Exception:                        # noqa: BLE001 - see docstring
            return

    # -- Qt -------------------------------------------------------------------

    def showEvent(self, event: Any) -> None:                # noqa: N802 - Qt's name
        super().showEvent(event)
        self.visibilityChanged.emit(True)

    def hideEvent(self, event: Any) -> None:                # noqa: N802 - Qt's name
        super().hideEvent(event)
        self.visibilityChanged.emit(False)

    def keyPressEvent(self, event: Any) -> None:            # noqa: N802 - Qt's name
        if event.key() == Qt.Key.Key_Escape:
            self.dismissed.emit()
            return
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() & Qt.KeyboardModifier.ShiftModifier):
            self.previous_match()
            return
        super().keyPressEvent(event)


def attach_find(host: Any, view: Any, *, window_escape: bool = True) -> FindBar:
    r"""A find bar over `view`, with Ctrl+F and Escape wired to `host`.

    Returns the bar, hidden. The caller puts it in a layout - where it sits is
    a decision about that window, and this has no opinion.

    **`window_escape=False` for a bar that lives inside the main window.** The
    main window already owns Escape (it empties the search box), and two
    enabled shortcuts for one key are ambiguous: Qt fires neither and hands the
    key to the box, so Escape emptied the search and left the find bar open.
    Such a host closes the bar from that one Escape instead - see
    `MainWindow._clear_search` - so a single press closes the bar and only the
    next one empties the box. A window of its own (the pop-out) keeps this
    shortcut: it has no other Escape to collide with.
    """
    from PyQt6.QtGui import QShortcut, QKeySequence

    bar = FindBar(host)
    bar.attach(view)
    bar.hide()

    def dismiss() -> None:
        bar.clear()
        bar.hide()
        try:
            view.setFocus()
        except Exception:                        # noqa: BLE001 - focus
            return

    bar.dismissed.connect(dismiss)
    shortcut = QShortcut(QKeySequence.StandardKey.Find, host)
    shortcut.activated.connect(bar.focus)
    # Escape from the *document* closes it too, which is what everybody
    # expects and what makes the bar feel like part of the window rather than
    # a widget parked in it.
    #
    # **Only while the bar is showing.** A window-context Escape that is always
    # armed collides with the window's own Escape (clear the search box), and
    # Qt answers two identical shortcuts by firing neither - so with the
    # preview pane open, Escape did nothing anywhere in the window.
    if window_escape:
        away = QShortcut(QKeySequence(Qt.Key.Key_Escape), host)
        away.setEnabled(False)
        bar.visibilityChanged.connect(away.setEnabled)
        away.activated.connect(dismiss)
    return bar
