"""The bar over the list when more than one contact is picked.

Layer: L5

Order 1k 6h, 6s, 6t, D4. Add to or remove from a group, add or remove a tag, keep the picked
contacts in another account (the 6t shortcut for those that live in only one) or take an account
away, set a company, favourite, delete to the bin and export. In the Bin it offers Restore and
Empty the bin instead. The bar decides nothing and writes nothing: it emits `action(name, arg)`
and the page does the work on a worker, asking first where D4 says to.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QMenu, QToolButton, QWidget

from app.ui.contacts.parts import tool_button
from app.ui.widgets import menus
from app.ui.widgets.flow_layout import FlowLayout

__all__ = ["BulkBar"]


def _bar_button(glyph: str, words: str, tip: str, *, menu: bool = False) -> QToolButton:
    b = tool_button(glyph, words, tip)
    b.setText(words)
    b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    if menu:
        b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menus.attach(b)
    return b


class BulkBar(QWidget):
    action = Signal(str, object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("contactBulkBar")
        self.setAccessibleName("Actions for the picked contacts")
        self._rows: list = []
        self._groups: list[str] = []
        self._tags: list[str] = []
        self._accounts: list = []
        self.count = QLabel("")
        self.count.setAccessibleName("How many contacts are picked")
        self.add_group = _bar_button("users", "Add to group", "Put the picked contacts in a group", menu=True)
        self.remove_group = _bar_button("circle-minus", "Take out of group",
                                        "Take the picked contacts out of a group", menu=True)
        self.add_tag = _bar_button("tag", "Add tag", "Give the picked contacts a tag", menu=True)
        self.remove_tag = _bar_button("circle-x", "Remove tag", "Take a tag off the picked contacts", menu=True)
        self.add_account = _bar_button(
            "copy-plus", "Keep in account", "Keep the picked contacts in another account as well - "
            "for contacts that live in only one", menu=True)
        self.remove_account = _bar_button(
            "unlink", "Take out of account", "Delete the picked contacts from one account (asks first)",
            menu=True)
        self.company = _bar_button("building-2", "Set company", "Give every picked contact the same company")
        self.favourite = _bar_button("star", "Favourite", "Make the picked contacts favourites, or all "
                                     "of them not favourites if they already are")
        self.export = _bar_button("download", "Export", "Export the picked contacts")
        self.delete = _bar_button("trash-2", "Delete", "Move the picked contacts to the bin")
        self.restore = _bar_button("archive-restore", "Restore", "Put the picked contacts back from the bin")
        self.empty_bin = _bar_button("trash", "Empty the bin", "Delete everything in the bin for good")
        for b, name in ((self.company, "company"), (self.favourite, "favourite"), (self.export, "export"),
                        (self.delete, "delete"), (self.restore, "restore"), (self.empty_bin, "empty_bin")):
            b.clicked.connect(lambda _c=False, n=name: self.action.emit(n, None))
        for b in (self.add_group, self.remove_group, self.add_tag, self.remove_tag, self.add_account,
                  self.remove_account):
            b.menu().aboutToShow.connect(self._fill)
        # A flow, not a row: eleven buttons side by side are wider than the list they sit over,
        # and a bar that wide pushes the editor out of the window.
        row = FlowLayout(self, spacing=4)
        row.setContentsMargins(8, 4, 8, 4)
        row.addWidget(self.count)
        for b in (self.add_group, self.remove_group, self.add_tag, self.remove_tag, self.add_account,
                  self.remove_account, self.company, self.favourite, self.export, self.delete, self.restore,
                  self.empty_bin):
            row.addWidget(b)
        self.set_mode(bin_view=False)

    def set_mode(self, *, bin_view: bool) -> None:
        for b in (self.add_group, self.remove_group, self.add_tag, self.remove_tag, self.add_account,
                  self.remove_account, self.company, self.favourite, self.export, self.delete):
            b.setVisible(not bin_view)
        self.restore.setVisible(bin_view)
        self.empty_bin.setVisible(bin_view)

    def set_choices(self, groups: list[str], tags: list[str], accounts: list) -> None:
        self._groups, self._tags, self._accounts = groups, tags, accounts

    def set_rows(self, rows: list) -> None:
        self._rows = rows
        n = len(rows)
        self.count.setText(f"{n} picked" if n else "")

    # ------------------------------------------------------------------ menus
    def _fill(self) -> None:
        """Each drop-down's lines, built as it opens by `menus.refill` (review DS-6)."""
        menu = self.sender()
        if not isinstance(menu, QMenu):
            return
        picked_groups = sorted({g for r in self._rows for g in r.groups}, key=str.casefold)
        picked_tags = sorted({t for r in self._rows for t in r.tags}, key=str.casefold)
        picked_accounts = {a for r in self._rows for a in r.accounts}
        if menu is self.add_group.menu():
            items = self._items("group_add", "users", self._groups, new="New group…")
        elif menu is self.remove_group.menu():
            items = self._items("group_remove", "users", picked_groups, empty="None of them is in a group")
        elif menu is self.add_tag.menu():
            items = self._items("tag_add", "tag", self._tags, new="New tag…")
        elif menu is self.remove_tag.menu():
            items = self._items("tag_remove", "tag", picked_tags, empty="None of them has a tag")
        elif menu is self.add_account.menu():
            items = self._accounts_items("account_add", self._accounts, "No account can keep contacts yet")
        else:
            shown = [a for a in self._accounts if a.id in picked_accounts]
            items = self._accounts_items("account_remove", shown, "None of them is kept in an account")
        menus.refill(menu, items)

    def _accounts_items(self, name: str, accounts: list, empty: str) -> list:
        items = [menus.Item(a.name or a.address, lambda i=a.id: self.action.emit(name, i), icon="contact",
                            tint=a.colour or "") for a in accounts]
        return items or [menus.Item(empty, None, icon="info", enabled=False)]

    def _items(self, name: str, glyph: str, names: list[str], *, new: str = "", empty: str = "") -> list:
        # The "New…" line, then a separator, then the names: two groups, as the builder parts them.
        items = [menus.Item(new, lambda: self.action.emit(name, None), icon="plus")] if new else []
        items += [menus.Item(n, lambda v=n: self.action.emit(name, v), icon=glyph, group="remember")
                  for n in names]
        if not names and empty:
            items.append(menus.Item(empty, None, icon="info", enabled=False))
        return items
