r"""A layout that lines its widgets up like words, wrapping onto the next line.

Layer: L5 widget

**Why this exists.** Qt's own row layout (`QHBoxLayout`) never wraps: when a
row of controls is wider than the space it is given, it squeezes every control
below the size it needs, and what the person sees is text cut to "Who...ear",
buttons shrunk to nothing, or two boxes drawn on top of each other. The
timeline's thirteen month buttons and its "Or any dates" row did all three on
a narrow window (grabbed 2026-09-27, order 0x section 9).

This is the flow layout from Qt's own documentation examples, in Python: each
widget keeps the width it asks for, and when the next one would not fit on the
line it starts a new line - the way text wraps in a paragraph. It answers
"how tall do I need to be at this width?" (`heightForWidth`), which is what
lets the layouts around it give it a second line when it needs one instead of
cutting it off.

Nothing here reads data, paints anything, or knows what the widgets are; it
only places them. Right-to-left and vertical flow are deliberately not
supported - nothing in Leasha needs them.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QPoint, QRect, QSize, Qt
from PyQt6.QtWidgets import QLayout, QLayoutItem, QWidget

__all__ = ["FlowLayout"]


class FlowLayout(QLayout):
    """Widgets left to right, wrapping to a new line when the next will not fit."""

    def __init__(self, parent: Optional[QWidget] = None, *, spacing: int = 6,
                 centred: bool = False, height_for_width: bool = True) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._spacing = spacing
        #: Each line centred in the width rather than starting at the left, for
        #: a page that is itself centred - the Search page's suggested searches
        #: sit under a centred box, and a left-aligned second line there reads
        #: as a mistake. Off by default, so the timeline is exactly as it was.
        self._centred = centred
        #: Whether Qt is told this layout's height depends on its width. True
        #: (the default, and the timeline's) is the textbook arrangement. False
        #: is for a page that sizes the layout's widget itself - the Search
        #: page does, because telling Qt made **every resize of the whole
        #: window about 1 ms slower** (measured 2026-09-27: 2.0 to 3.0 ms
        #: median), with Qt re-asking every layout above it, for a row of four
        #: buttons whose height changes only when a line is added or taken away.
        self._height_for_width = height_for_width
        #: `(width, height)` from the last `heightForWidth`, or None.
        self._hfw: Optional[tuple[int, int]] = None
        self.setContentsMargins(0, 0, 0, 0)

    # -- the QLayout contract: Qt calls these, never this module's own code ----

    def addItem(self, item: QLayoutItem) -> None:            # noqa: N802 - Qt's name
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> Optional[QLayoutItem]:   # noqa: N802 - Qt's name
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> Optional[QLayoutItem]:   # noqa: N802 - Qt's name
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:          # noqa: N802 - Qt's name
        # It never asks for more room than its lines need, in either direction.
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:                      # noqa: N802 - Qt's name
        return self._height_for_width

    def heightForWidth(self, width: int) -> int:              # noqa: N802 - Qt's name
        # Qt asks this several times per resize with the same width; the answer
        # only changes when the width or the widgets do (`invalidate`).
        cached = self._hfw
        if cached is not None and cached[0] == width:
            return cached[1]
        height = self._arrange(QRect(0, 0, width, 0), place=False)
        self._hfw = (width, height)
        return height

    def invalidate(self) -> None:
        self._hfw = None
        super().invalidate()

    def setGeometry(self, rect: QRect) -> None:               # noqa: N802 - Qt's name
        super().setGeometry(rect)
        self._arrange(rect, place=True)

    def sizeHint(self) -> QSize:                              # noqa: N802 - Qt's name
        # Everything on one line: the width it would like, given the room.
        width = sum(item.sizeHint().width() for item in self._items)
        width += self._spacing * max(0, len(self._items) - 1)
        height = max((item.sizeHint().height() for item in self._items), default=0)
        margins = self.contentsMargins()
        return QSize(width + margins.left() + margins.right(),
                     height + margins.top() + margins.bottom())

    def minimumSize(self) -> QSize:                           # noqa: N802 - Qt's name
        # The narrowest it can go is its widest single widget: everything else
        # can move to a line of its own.
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    # -- the one piece of real work ------------------------------------------------

    def _arrange(self, rect: QRect, *, place: bool) -> int:
        """Walk the widgets as if writing a line of text; return the height used.

        With `place` False it only measures (for `heightForWidth`); with it
        True it also moves each widget to where it belongs (and, when `centred`,
        shifts each line right by half the room it leaves). Each widget is
        centred on its line, so a label beside a taller box sits level with the
        box's text rather than at its top edge.
        """
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        lines: list[list[tuple[QLayoutItem, QSize]]] = [[]]
        x = area.x()
        for item in self._items:
            widget = item.widget()
            if widget is not None and widget.isHidden():
                continue
            hint = item.sizeHint()
            if lines[-1] and x + hint.width() > area.right() + 1:
                # It does not fit on this line: start the next one.
                lines.append([])
                x = area.x()
            lines[-1].append((item, hint))
            x += hint.width() + self._spacing
        y = area.y()
        for number, line in enumerate(lines):
            height = max((hint.height() for _item, hint in line), default=0)
            if place:
                x = area.x()
                if self._centred:
                    used = sum(hint.width() for _item, hint in line)
                    used += self._spacing * max(0, len(line) - 1)
                    x += max(0, (area.width() - used) // 2)
                for item, hint in line:
                    item.setGeometry(QRect(QPoint(x, y + (height - hint.height()) // 2), hint))
                    x += hint.width() + self._spacing
            y += height + (self._spacing if number < len(lines) - 1 else 0)
        return y - rect.y() + margins.bottom()
