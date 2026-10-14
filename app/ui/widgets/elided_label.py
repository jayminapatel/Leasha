r"""A one-line label that elides its text instead of growing or wrapping.

Layer: L5

The owner, 2026-10-14: *"the sizes of boxes change on content etc it is
disturbing for the eye"*. A `QLabel` asks for the width of its text, and a
word-wrapped one for the height of however many lines that makes - so a long
file name pushed its neighbours aside, and a long sentence made its box taller
and moved everything below it. This label asks for neither: its width is the
layout's to give (`Ignored`), its height is one line of its font, and text that
does not fit ends in "…".

**`text()` is still the whole text.** Only what is painted is shortened, so
code and tests that read a label's words read them unchanged. When the words
are cut, the whole text becomes the tooltip (unless the caller gave the label a
tooltip of its own) and the accessible name, so nothing is lost to a mouse or a
screen reader.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget

__all__ = ["ElidedLabel"]


class ElidedLabel(QLabel):
    """One line, a fixed height, and "…" when the words do not fit."""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None, *,
                 mode: Qt.TextElideMode = Qt.TextElideMode.ElideRight,
                 tip: bool = True) -> None:
        super().__init__(parent)
        self._full = ""
        self._mode = mode
        #: Whether a cut text puts the whole text in the tooltip.
        self._tip = tip
        self._tipped = False
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(0)
        self.setText(text)

    # -- the text ---------------------------------------------------------

    def setText(self, text: Any) -> None:            # noqa: N802 - Qt's name
        """Keep the whole text; paint as much as fits."""
        text = "" if text is None else str(text)
        if text == self._full and self._full:
            return
        self._full = text
        self._elide()

    def text(self) -> str:                           # noqa: D102 - Qt's name
        return self._full

    def setWordWrap(self, on: bool) -> None:         # noqa: N802 - Qt's name
        """One line by design: wrapping is what made boxes grow."""
        super().setWordWrap(False)

    # -- the size ---------------------------------------------------------

    def sizeHint(self) -> QSize:                     # noqa: N802 - Qt's name
        # As wide as the layout was told it must be (a fixed column), and
        # otherwise nothing: never the width of the words.
        margins = self.contentsMargins()
        return QSize(self.minimumWidth(),
                     self.fontMetrics().height() + margins.top() + margins.bottom())

    def minimumSizeHint(self) -> QSize:              # noqa: N802 - Qt's name
        return self.sizeHint()

    def resizeEvent(self, event: Any) -> None:       # noqa: N802 - Qt's name
        super().resizeEvent(event)
        self._elide()

    def changeEvent(self, event: Any) -> None:       # noqa: N802 - Qt's name
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self.updateGeometry()
            self._elide()

    def _elide(self) -> None:
        width = self.contentsRect().width()
        shown = (self.fontMetrics().elidedText(self._full, self._mode, width)
                 if width > 0 else self._full)
        if super().text() != shown:
            super().setText(shown)
        cut = shown != self._full
        if self._tip and (cut or self._tipped):
            self.setToolTip(self._full if cut else "")
            self._tipped = cut
        self.setAccessibleName(self._full if cut else "")
