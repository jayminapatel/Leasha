r"""The keyboard's ring round the current row of a tree or a table, drawn by one filter.

Layer: L5 widget

**Why this exists** (review 2026-10-14, DS-2 follow-up). The sheet's `outline: 0` took away
Qt's dotted focus box on trees, and a sheet rule can only ring one cell - so in every table
and multi-column tree (the accounts list, the roots and archives tables, the sync log, the
activity, browse and result tables, the settings tables) somebody moving with the arrow keys
lost their place. The side lists already drew one ring round their whole line; this is that
ring for every view, without a subclass: `install_row_ring(view)`.

How: an event filter on the view's viewport. On a paint it lets the view paint itself first
(`viewportEvent`, which runs the view's own `paintEvent` and its delegates, so hover and
selection look exactly as before), then draws `paint_kit.paint_focus_ring` round the current
row - only when the view has the keyboard and has a current index. A table whose keyboard
moves cell by cell (`SelectItems` with a selection, the spreadsheet preview) rings the cell
instead, since that is where the keyboard is. When the current row moves, or the view gains or
loses the keyboard, both rows are repainted so no part of an old ring stays behind.

Nothing here reads the store; painting from values in hand.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QModelIndex, QObject, QRect
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView, QTreeView

from app.ui.widgets.paint_kit import paint_focus_ring

__all__ = ["install_row_ring", "has_row_ring", "ring_rect"]

_ATTR = "_leasha_row_ring"


def _header(view: QAbstractItemView) -> Optional[QHeaderView]:
    if isinstance(view, QTreeView):
        return view.header()
    if isinstance(view, QTableView):
        return view.horizontalHeader()
    return None


def _rings_cell(view: QAbstractItemView) -> bool:
    """True for a table whose keyboard moves cell by cell, so the cell is what is current."""
    return (isinstance(view, QTableView)
            and view.selectionBehavior() == QAbstractItemView.SelectionBehavior.SelectItems
            and view.selectionMode() != QAbstractItemView.SelectionMode.NoSelection)


def ring_rect(view: QAbstractItemView, index: QModelIndex) -> QRect:
    """The viewport rectangle the ring takes for `index`'s row (or its cell); empty when unseen."""
    if not index.isValid():
        return QRect()
    if _rings_cell(view):
        return view.visualRect(index)
    header = _header(view)
    if header is None or header.count() == 0:
        return view.visualRect(index)
    shown = [header.logicalIndex(v) for v in range(header.count())
             if not header.isSectionHidden(header.logicalIndex(v))]
    if not shown:
        return QRect()
    cell = view.visualRect(index.siblingAtColumn(shown[0]))
    if cell.isEmpty():
        cell = view.visualRect(index)
    if cell.isEmpty():
        return QRect()
    left = min(header.sectionViewportPosition(c) for c in shown)
    right = max(header.sectionViewportPosition(c) + header.sectionSize(c) for c in shown)
    width = view.viewport().width()
    left, right = max(0, left), min(width, right)
    if right <= left:
        return QRect()
    return QRect(left, cell.top(), right - left, cell.height())


class _RowRing(QObject):
    """The filter `install_row_ring` hangs on a view. Owned by (and dies with) the view."""

    def __init__(self, view: QAbstractItemView, colours: Optional[Callable[[], Optional[dict]]]) -> None:
        super().__init__(view)
        self._view = view
        self._colours = colours
        self._painting = False
        self._watched: Any = None
        self._last = QModelIndex()
        view.installEventFilter(self)
        view.viewport().installEventFilter(self)
        self._watch()

    def _watch(self) -> None:
        """Follow the current index of whichever selection model the view has now."""
        model = self._view.selectionModel()
        if model is None or model is self._watched:
            return
        self._watched = model
        model.currentChanged.connect(self._moved)

    def _moved(self, now: QModelIndex, before: QModelIndex) -> None:
        for index in (before, now):
            rect = ring_rect(self._view, index)
            if not rect.isEmpty():
                self._view.viewport().update(rect)

    def wants_ring(self) -> bool:
        return self._view.hasFocus() and self._view.currentIndex().isValid()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt's name
        if "_view" not in self.__dict__:           # the view is being torn down: nothing to draw
            return False
        kind = event.type()
        if watched is self._view:
            if kind in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
                self._moved(self._view.currentIndex(), QModelIndex())
            return False
        if kind != QEvent.Type.Paint or self._painting or watched is not self._view.viewport():
            return False
        self._watch()
        self._painting = True
        try:
            self._view.viewportEvent(event)        # the view as Qt draws it: hover, selection, delegates
        finally:
            self._painting = False
        if self.wants_ring():
            rect = ring_rect(self._view, self._view.currentIndex())
            if not rect.isEmpty():
                painter = QPainter(self._view.viewport())
                try:
                    paint_focus_ring(painter, rect.adjusted(1, 0, -1, 0),
                                     self._colours() if self._colours else None)
                finally:
                    painter.end()
        return True


def install_row_ring(view: QAbstractItemView,
                     colours: Optional[Callable[[], Optional[dict]]] = None) -> QAbstractItemView:
    """Ring `view`'s current row when it has the keyboard. Once per view; returns the view.

    `colours`, when given, is asked for the palette at each paint (a widget that was handed one,
    as the side lists are); otherwise the theme in force gives `focus_ring`.
    """
    if getattr(view, _ATTR, None) is None:
        setattr(view, _ATTR, _RowRing(view, colours))
    return view


def has_row_ring(view: Any) -> bool:
    """True when `install_row_ring` has been called on `view`."""
    return getattr(view, _ATTR, None) is not None
