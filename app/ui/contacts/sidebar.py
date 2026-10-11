"""The sidebar: every way to narrow the list, each with its icon and its count.

Layer: L5

Order 1k 6c, 6m.4, 6s, 6t; moved onto the shared `SideList` by order 1p. All, Favourites, each
group, each tag (in its colour), each account (in its colour, with its own kind of glyph),
"Changed in two places", "Possible duplicates", "Deleted elsewhere", "Single account", the saved
smart groups and the Bin. The entries come from `logic.build_entries`; `SideList` draws them and
this says which was picked. "Possible duplicates" is not a filter - it is a door: picking it asks
for the Duplicates tool and the list stays where it was.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget

from app.ui.contacts.logic import TAG_COLOURS, Entry
from app.ui.presenter.side_entry import SideEntry
from app.ui.widgets.buttons import action_button
from app.ui.widgets.side_list import SideList

__all__ = ["ContactSidebar"]

_MENUS = {
    "smart": (("Edit this smart group…", "pencil", "edit"), ("Delete this smart group", "trash-2", "delete")),
    "group": (("Rename this group…", "pencil", "rename"),),
    "tag": (("Colour", "palette", tuple((hexed, "tag", f"colour:{hexed}", hexed) for hexed in TAG_COLOURS)),
            ("Delete this tag", "trash-2", "delete")),
}


def side_entry(e: Entry) -> SideEntry:
    """A Contacts `Entry` as the shared list draws it. `value` keeps the `Entry` itself."""
    kind = "all" if e.kind == "all" else "door" if e.kind == "duplicates" else "filter"
    return SideEntry(e.key, e.label, section=e.section, icon=e.icon, colour=e.colour, count=e.count,
                     tip=e.tip or e.label, kind=kind, value=e, menu=_MENUS.get(e.kind, ()))


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
        self._entries: list[Entry] = []
        self._current = "all"
        self.tree = SideList("contactSidebar", accessible="Contact lists",
                             tip="Narrow the contacts: everyone, favourites, a group, a tag or an account")
        self.tree.picked.connect(lambda se: self._pick(se.value))
        self.tree.door.connect(lambda _se: self.opener_requested.emit("duplicates"))
        self.tree.menu_chosen.connect(self._menu_chosen)
        self.tree.currentItemChanged.connect(self._arrowed)
        self.new_smart = action_button("New smart group", "plus", "secondary")
        self.new_smart.setToolTip("Save a filter such as 'no phone number' as a list of its own")
        self.new_smart.clicked.connect(self.smart_new.emit)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.new_smart)
        self.setMinimumWidth(170)

    def retint(self, colours: dict[str, str]) -> None:
        self.tree.retint(colours)

    def current_key(self) -> str:
        return self._current

    def entry_for(self, key: str) -> Optional[Entry]:
        return next((e for e in self._entries if e.key == key), None)

    def set_entries(self, entries: list[Entry]) -> None:
        self._entries = list(entries)
        if self.entry_for(self._current) is None:
            self._current = "all"
        self.tree.select(self._current)                    # what `set_entries` keeps highlighted
        self.tree.set_entries([side_entry(e) for e in self._entries])

    def select_key(self, key: str, *, emit: bool = True) -> None:
        entry = self.entry_for(key)
        if entry is None:
            return
        if emit and entry.kind == "duplicates":            # a door, not a list
            self.opener_requested.emit("duplicates")
            return
        self.tree.select(key)
        if emit:
            self._pick(entry)
        else:
            self._current = key

    def _pick(self, entry: Entry) -> None:
        self._current = entry.key
        self.picked.emit(entry)

    def _arrowed(self, item: Any, _before: Any) -> None:
        """An arrow key moves the highlight and picks, as the list always did; a click does its
        own picking, and a programmatic highlight is silent (`SideList.select` blocks signals)."""
        if item is None or not self.tree.hasFocus() or QApplication.mouseButtons() != Qt.MouseButton.NoButton:
            return
        self.tree._clicked(item, 0)

    def _menu_chosen(self, side: SideEntry, action: str) -> None:
        entry: Entry = side.value
        arg = int(entry.arg) if entry.arg is not None else 0
        if action == "edit":
            self.smart_edit.emit(arg)
        elif action == "delete":
            (self.smart_delete if entry.kind == "smart" else self.tag_delete).emit(arg)
        elif action == "rename":
            self.group_rename.emit(arg)
        elif action.startswith("colour:"):
            self.tag_colour.emit(arg, action.split(":", 1)[1])
