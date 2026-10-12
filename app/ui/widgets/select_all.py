"""Select all / Select none for any list that has ticks or a selection.

Layer: L5 widget. The owner (2026-10-12): "in all the lists - merge, changed in two places and the rest - there
should be an option to select all". One pair of buttons, worded and drawn the same everywhere, that works on a
table, a tree or a list: where its rows carry a tick box it ticks (or unticks) them, and where they do not it
selects (or clears) them. The view is told once, not once per row, so ten thousand rows are as quick as ten.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QListWidget, QTableWidget, QTreeWidget, QTreeWidgetItem, QWidget,
)

from app.ui.widgets.buttons import action_button

__all__ = ["SelectBar", "set_all"]


def _tick_tree(item: QTreeWidgetItem, state: Qt.CheckState) -> int:
    n = 0
    for i in range(item.childCount()):
        n += _tick_tree(item.child(i), state)
    if item.flags() & Qt.ItemFlag.ItemIsUserCheckable and not item.childCount():
        item.setCheckState(0, state)
        n += 1
    elif item.flags() & Qt.ItemFlag.ItemIsUserCheckable:
        item.setCheckState(0, state)
    return n


def _has_tick(item: Any) -> bool:
    """Qt lets every table and list item be ticked; only one that was given a state shows a box."""
    return bool(item.flags() & Qt.ItemFlag.ItemIsUserCheckable) and item.data(Qt.ItemDataRole.CheckStateRole) is not None


def set_all(view: Any, on: bool) -> int:
    """Tick (or untick) every checkable row of `view`, or select (or clear) the rows if none has a tick box.
    Returns how many rows were touched. Other signals are held back until the end."""
    state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
    n = 0
    view.setUpdatesEnabled(False)
    was = view.blockSignals(True)
    try:
        if isinstance(view, QTableWidget):
            for row in range(view.rowCount()):
                item = view.item(row, 0)
                if item is not None and _has_tick(item) and not view.isRowHidden(row):
                    item.setCheckState(state)
                    n += 1
        elif isinstance(view, QTreeWidget):
            for i in range(view.topLevelItemCount()):
                n += _tick_tree(view.topLevelItem(i), state)
        elif isinstance(view, QListWidget):
            for i in range(view.count()):
                item = view.item(i)
                if _has_tick(item):
                    item.setCheckState(state)
                    n += 1
    finally:
        view.blockSignals(was)
        view.setUpdatesEnabled(True)
    if n:
        _announce(view)
        return n
    if isinstance(view, QAbstractItemView):
        view.selectAll() if on else view.clearSelection()
        return len(view.selectionModel().selectedRows()) if view.selectionModel() else 0
    return 0


def _announce(view: Any) -> None:
    """Tell whoever listens that rows changed, once: the view's own changed signal, with nothing in it."""
    model = view.model()
    if model is not None and model.rowCount():
        model.dataChanged.emit(model.index(0, 0), model.index(model.rowCount() - 1, 0))
    view.viewport().update()


class SelectBar(QWidget):
    """The two buttons. `changed` fires once after either, with how many rows it touched."""

    changed = Signal(int)

    def __init__(self, view: Any, noun: str = "list", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.view = view
        self.setAccessibleName(f"Select all or none: {noun}")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.all_button = self._button("Select all", "list-checks", f"Select every row in the {noun}", noun)
        self.none_button = self._button("Select none", "list-x", f"Clear every row in the {noun}", noun)
        self.all_button.clicked.connect(lambda: self.changed.emit(set_all(self.view, True)))
        self.none_button.clicked.connect(lambda: self.changed.emit(set_all(self.view, False)))
        row.addWidget(self.all_button)
        row.addWidget(self.none_button)
        row.addStretch(1)

    @staticmethod
    def _button(text: str, glyph: str, tip: str, noun: str) -> Any:
        b = action_button(text, glyph, "secondary")
        b.setToolTip(tip)
        b.setAccessibleName(f"{text} ({noun})")
        return b
