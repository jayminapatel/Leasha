"""The account list (10o): one row each, drag to reorder.

Layer: L5

A `QTreeWidget` of plain items (no widgets inside rows, so a drag simply moves
them). The icons are Lucide glyphs from `icons.py` and a colour swatch - never a
provider's logo. Dates are written in the format chosen in Settings > Window.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QTreeWidget, QTreeWidgetItem, QWidget

from app.ui.accounts import logic
from app.ui.later import later
from app.ui.theme import theme_colours
from app.ui.widgets.icons import icon_size
from app.ui.widgets.row_ring import install_row_ring

__all__ = ["AccountList", "COLUMNS"]

COLUMNS = ("Account", "Type", "Address", "Status", "Doing", "Last sync", "Next sync", "Notes")
_ID = Qt.ItemDataRole.UserRole


def _swatch(colour: str) -> QIcon:
    pix = QPixmap(QSize(14, 14))
    # An account with no usable colour gets the theme's faint grey, not a literal one.
    pix.fill(QColor(colour) if QColor(colour).isValid() else QColor(theme_colours()["text_faint"]))
    return QIcon(pix)


class AccountList(QTreeWidget):
    #: The account ids in their new order, after a drag.
    reordered = Signal(list)
    #: The account id selected, or 0 for none.
    account_selected = Signal(int)
    #: A double-click (or Enter) on a row: the account id whose settings the person wants to edit.
    account_opened = Signal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        install_row_ring(self)
        self.setObjectName("accountList")
        self.setAccessibleName("Accounts")
        self.setHeaderLabels(list(COLUMNS))
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setAlternatingRowColors(True)
        self.setIconSize(icon_size("list"))
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDragEnabled(True)
        self.setDropIndicatorShown(True)
        self.setToolTip("Drag an account up or down to change the order. The order is also the priority "
                        "when 'the account higher in the list wins'.")
        self.itemSelectionChanged.connect(lambda: self.account_selected.emit(self.current_id()))
        self.itemActivated.connect(lambda *_a: self.account_opened.emit(self.current_id()))
        self._colours: dict[str, str] = {}

    def current_id(self) -> int:
        item = self.currentItem()
        return int(item.data(0, _ID)) if item is not None and item.isSelected() else 0

    def ids(self) -> list[int]:
        return [int(self.topLevelItem(i).data(0, _ID)) for i in range(self.topLevelItemCount())]

    def select(self, account_id: int) -> None:
        for i in range(self.topLevelItemCount()):
            if int(self.topLevelItem(i).data(0, _ID)) == account_id:
                self.setCurrentItem(self.topLevelItem(i))
                return

    def set_rows(self, statuses: list[Any], colours: Optional[dict] = None,
                 doing: Optional[dict[int, str]] = None) -> None:
        from app.ui.widgets.icons import icon  # noqa: PLC0415

        c = colours or theme_colours()
        ink = c.get("text_dim", "#888888")
        warn_ink = c.get("danger", "#a32020")
        keep = self.current_id()
        blocked = self.blockSignals(True)
        self.clear()
        for st in statuses:
            v = logic.row_view(st)
            line = (doing or {}).get(v.account_id, "")
            item = QTreeWidgetItem([v.name, v.type_text, v.address, v.word, line, v.last, v.next, v.note])
            item.setToolTip(4, line or "Open the Sync window for the detail")
            item.setData(0, _ID, v.account_id)
            item.setIcon(0, _swatch(v.colour))
            item.setIcon(1, icon(v.glyph, ink))
            item.setIcon(3, icon(v.word_icon, warn_ink if v.word in ("Needs sign-in", "Failed") else ink))
            if v.warn:
                item.setIcon(7, icon("triangle-alert", warn_ink))
            chips = ", ".join(t for _g, t in logic.kind_chips(st.account))
            item.setToolTip(1, chips)
            item.setToolTip(3, v.note or v.word)
            item.setToolTip(7, v.note)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsDropEnabled)
            self.addTopLevelItem(item)
        self.blockSignals(blocked)
        for col in range(len(COLUMNS)):
            self.resizeColumnToContents(col)
        rows = max(3, min(self.topLevelItemCount(), 8))
        self.setMinimumHeight(self.header().height() + rows * 26 + 8)
        if keep:
            blocked = self.blockSignals(True)
            self.select(keep)
            self.blockSignals(blocked)

    def dropEvent(self, event: Any) -> None:               # noqa: N802 - Qt's naming
        super().dropEvent(event)
        # Read the order once Qt has finished moving the item.
        later(self, 0, lambda: self.reordered.emit(self.ids()))
