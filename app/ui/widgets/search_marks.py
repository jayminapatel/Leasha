r"""The searched words, highlighted in a preview, with F3 and Shift+F3.

Layer: L5 widget

Order 0y section 4b. Leasha finds the message; the person still has to find the
sentence. The words they searched for are painted in the preview's text the
moment it is shown, and F3 / Shift+F3 move to the next and the previous one -
without opening a find box or typing the word a second time.

**One implementation, for a file and for a message.** It is attached to the
preview pane's text area, so a `.txt` file and an email are highlighted by the
same code with the same keys. Which characters count as a searched word is
`presenter.snippets.term_spans` - the rule the result snippets already use, so
the list and the pane agree.

**An overlay, like the find box.** `setExtraSelections` paints over the document
without touching it, in the find box's own colour. The two share the one overlay
a text area has, so while the find box is open and has something typed in it,
the highlight and F3 are its; when it is cleared or closed the searched words
are painted again.

**No I/O.** The text is already in the widget and the words are already in
hand; this is arithmetic over both.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import QTextEdit

from app.ui.presenter.snippets import term_spans

__all__ = ["SearchMarks"]


class SearchMarks(QObject):
    """Paints the searched words in `view` and steps between them."""

    #: What `position()` says has changed - new words, or a step.
    changed = Signal()

    def __init__(self, host: Any, view: Any, find_bar: Any = None) -> None:
        super().__init__(host)
        self._view = view
        self._bar = find_bar
        #: `(start, end)` in the document's own (UTF-16) positions, in order.
        self._spans: list[tuple[int, int]] = []
        #: The match F3 last went to, or -1 before the first press.
        self._index = -1

        # Window-wide while the pane is showing, like the pane's Ctrl+F: the
        # person is usually still in the results list when they press it. Qt
        # ignores a shortcut whose widget is hidden, so the panes on the other
        # pages do not compete for the key.
        self.next_key = QShortcut(QKeySequence(Qt.Key.Key_F3), host)
        self.next_key.activated.connect(self.next)
        self.previous_key = QShortcut(QKeySequence("Shift+F3"), host)
        self.previous_key.activated.connect(self.previous)

        if find_bar is not None:
            # The find box clears the overlay when it is emptied or closed;
            # the searched words go back on it. Connected after the bar's own
            # handlers, so this runs once they have finished.
            find_bar.dismissed.connect(self.repaint)
            find_bar.box.textChanged.connect(
                lambda text: None if text else self.repaint())

    # -- what is highlighted ----------------------------------------------------

    @property
    def count(self) -> int:
        """How many searched words were found in the text."""
        return len(self._spans)

    def show(self, terms: Optional[Sequence[str]]) -> None:
        """Highlight `terms` in the text now in the view. **Never raises.**"""
        self._index = -1
        try:
            words = [str(term) for term in (terms or ()) if str(term or "").strip()]
            self._spans = term_spans(self._view.toPlainText(), words, utf16=True)
        except Exception:                        # noqa: BLE001 - decoration
            self._spans = []
        self.repaint()
        self.changed.emit()

    def clear(self) -> None:
        """Forget the words - the document under them is about to change."""
        self._spans, self._index = [], -1
        self.repaint()
        self.changed.emit()

    def repaint(self) -> None:
        """Put the highlights on the view's overlay. **Never raises.**"""
        try:
            # 2026-10-05, the UI review: the same mark the results list
            # paints (`mark` and `mark_text`). This was the system's selection
            # blue, so one search showed its words in two colours.
            from app.ui.theme import theme_colours

            colours = theme_colours()
            colour, ink = QColor(colours["mark"]), QColor(colours["mark_text"])
            selections = []
            for start, end in self._spans:
                mark = QTextEdit.ExtraSelection()
                cursor = QTextCursor(self._view.document())
                cursor.setPosition(start)
                cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
                mark.cursor = cursor
                mark.format.setBackground(colour)
                mark.format.setForeground(ink)
                selections.append(mark)
            self._view.setExtraSelections(selections)
        except Exception:                        # noqa: BLE001 - decoration
            return

    def position(self) -> str:
        """`3 matches` before the first F3, `1 of 3` after it, `""` for none."""
        if not self._spans:
            return ""
        if self._index < 0:
            total = len(self._spans)
            return f"{total:,} match{'es' if total != 1 else ''}"
        return f"{self._index + 1:,} of {len(self._spans):,}"

    # -- F3 and Shift+F3 ----------------------------------------------------------

    def next(self) -> None:
        self._step(+1)

    def previous(self) -> None:
        self._step(-1)

    def _finding(self) -> bool:
        """The find box is open with something typed: F3 belongs to it."""
        bar = self._bar
        try:
            return bar is not None and not bar.isHidden() and bool(bar.box.text())
        except Exception:                        # noqa: BLE001 - a keystroke
            return False

    def _step(self, direction: int) -> None:
        """Select the next (or previous) searched word, wrapping. **Never raises.**

        Wrapping and silent, as the find box is: a key that stops dead at the
        last match makes somebody think there are no more.
        """
        if self._finding():
            if direction > 0:
                self._bar.next_match()
            else:
                self._bar.previous_match()
            return
        if not self._spans:
            return
        try:
            if self._index < 0:
                self._index = 0 if direction > 0 else len(self._spans) - 1
            else:
                self._index = (self._index + direction) % len(self._spans)
            start, end = self._spans[self._index]
            cursor = self._view.textCursor()
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            self._view.setTextCursor(cursor)
            self._view.ensureCursorVisible()
        except Exception:                        # noqa: BLE001 - a keystroke
            return
        self.changed.emit()
