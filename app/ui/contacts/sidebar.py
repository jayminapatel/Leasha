"""The sidebar: every way to narrow the list, each with its icon and its count.

Layer: L5

Order 1k 6c, 6m.4, 6s, 6t. All, Favourites, each group, each tag (in its colour), each account
(in its colour, with its own kind of glyph), "Changed in two places", "Possible duplicates",
"Deleted elsewhere", "Single account", the saved smart groups and the Bin. The entries come
from `logic.build_entries`; this only draws them and says which was picked. "Possible
duplicates" is not a filter - picking it asks for the Duplicates tool and the list stays where it
was.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import QAbstractItemView, QMenu, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from app.ui.contacts.logic import TAG_COLOURS, Entry
from app.ui.qtsip import open_menu
from app.ui.widgets.buttons import action_button
from app.ui.widgets.icons import icon

__all__ = ["ContactSidebar"]

ENTRY_ROLE = Qt.ItemDataRole.UserRole + 1


class ContactSidebar(QWidget):
    #: An `Entry` was picked (never the duplicates door).
    picked = Signal(object)
    #: The sidebar asks for a tool by its openers name.
    opener_requested = Signal(str)
    smart_new = Signal()
    smart_edit = Signal(int)
    smart_delete = Signal(int)
    tag_colour = Signal(int, str)
    tag_delete = Signal(int)
    group_rename = Signal(int)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._colours: dict[str, str] = {}
        self._entries: list[Entry] = []
        self._current = "all"
        self.tree = QTreeWidget()
        self.tree.setObjectName("contactSidebar")
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(2)
        self.tree.setIndentation(10)
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.setAccessibleName("Contact lists")
        self.tree.setToolTip("Narrow the contacts: everyone, favourites, a group, a tag or an account")
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, self.tree.header().ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, self.tree.header().ResizeMode.ResizeToContents)
        self.tree.currentItemChanged.connect(self._changed)
        self.tree.customContextMenuRequested.connect(self._menu)
        self.new_smart = action_button("New smart group", "plus", "secondary")
        self.new_smart.setToolTip("Save a filter such as 'no phone number' as a list of its own")
        self.new_smart.clicked.connect(self.smart_new.emit)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.new_smart)
        self.setMinimumWidth(170)

    def retint(self, colours: dict[str, str]) -> None:
        self._colours = dict(colours)
        if self._entries:
            self.set_entries(self._entries)

    def current_key(self) -> str:
        return self._current

    def entry_for(self, key: str) -> Optional[Entry]:
        return next((e for e in self._entries if e.key == key), None)

    def set_entries(self, entries: list[Entry]) -> None:
        self._entries = list(entries)
        dim = self._colours.get("text_dim", "#888888")
        faint = QBrush(QColor(self._colours.get("text_faint", "#888888")))
        self.tree.blockSignals(True)
        self.tree.clear()
        section: Optional[QTreeWidgetItem] = None
        last = ""
        keep: Optional[QTreeWidgetItem] = None
        for e in entries:
            if e.section != last:
                last = e.section
                section = QTreeWidgetItem([e.section])
                section.setFlags(Qt.ItemFlag.ItemIsEnabled)
                font = QFont(self.tree.font())
                font.setBold(True)
                section.setFont(0, font)
                section.setForeground(0, faint)
                self.tree.addTopLevelItem(section)
                section.setExpanded(True)
            item = QTreeWidgetItem(["" if e.count is None else str(e.count)])
            item.setText(0, e.label)
            item.setText(1, "" if e.count is None else str(e.count))
            item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            item.setForeground(1, faint)
            item.setIcon(0, icon(e.icon, e.colour or dim))
            item.setData(0, ENTRY_ROLE, e)
            item.setToolTip(0, e.tip or e.label)
            item.setData(0, Qt.ItemDataRole.AccessibleTextRole,
                         e.label if e.count is None else f"{e.label}, {e.count}")
            section.addChild(item)               # type: ignore[union-attr]
            if e.key == self._current:
                keep = item
        self.tree.setCurrentItem(keep or self._first())
        if keep is None:
            self._current = "all"
        self.tree.blockSignals(False)

    def _first(self) -> Optional[QTreeWidgetItem]:
        return self.tree.topLevelItem(0).child(0) if self.tree.topLevelItemCount() else None

    def _item(self, key: str) -> Optional[QTreeWidgetItem]:
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            for j in range(top.childCount()):
                e = top.child(j).data(0, ENTRY_ROLE)
                if e is not None and e.key == key:
                    return top.child(j)
        return None

    def select_key(self, key: str, *, emit: bool = True) -> None:
        item = self._item(key)
        if item is None:
            return
        if emit:
            self.tree.setCurrentItem(item)               # `_changed` records the choice
            return
        self.tree.blockSignals(True)
        self.tree.setCurrentItem(item)
        self._current = key
        self.tree.blockSignals(False)

    def _changed(self, item: Optional[QTreeWidgetItem], _before: Any) -> None:
        entry = item.data(0, ENTRY_ROLE) if item is not None else None
        if entry is None:
            return
        if entry.kind == "duplicates":
            # A door, not a list: ask for the tool and put the highlight back where it was.
            self.select_key(self._current, emit=False)
            self.opener_requested.emit("duplicates")
            return
        self._current = entry.key
        self.picked.emit(entry)

    def _menu(self, pos: QPoint) -> None:
        item = self.tree.itemAt(pos)
        entry = item.data(0, ENTRY_ROLE) if item is not None else None
        if entry is None:
            return
        menu = QMenu(self)
        if entry.kind == "smart":
            menu.addAction(icon("pencil", self._colours.get("text_dim", "#888888")), "Edit this smart group…",
                           lambda: self.smart_edit.emit(int(entry.arg)))
            menu.addAction(icon("trash-2", self._colours.get("danger", "#a32020")), "Delete this smart group",
                           lambda: self.smart_delete.emit(int(entry.arg)))
        elif entry.kind == "tag":
            colours = menu.addMenu(icon("palette", self._colours.get("text_dim", "#888888")), "Colour")
            for hexed in TAG_COLOURS:
                act = colours.addAction(icon("tag", hexed), hexed)
                act.triggered.connect(lambda _c=False, h=hexed: self.tag_colour.emit(int(entry.arg), h))
            menu.addAction(icon("trash-2", self._colours.get("danger", "#a32020")), "Delete this tag",
                           lambda: self.tag_delete.emit(int(entry.arg)))
        elif entry.kind == "group":
            menu.addAction(icon("pencil", self._colours.get("text_dim", "#888888")), "Rename this group…",
                           lambda: self.group_rename.emit(int(entry.arg)))
        if menu.isEmpty():
            return
        open_menu(menu, self.tree.viewport().mapToGlobal(pos))
