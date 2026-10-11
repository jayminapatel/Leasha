r"""The left-hand list every page shares, drawn the way the Contacts page draws its own.

Layer: L5 widget

Order 1p (2026-10-11), the owner: "i like the look of contacts panel with icons etc can
that be standardised ?". The Contacts panel (order 1k, `contacts/sidebar.py`) set the
look: sections under quiet bold headings, an icon on every line - coloured where colour
means something - a faint count at the right, a tooltip, a right-click menu per entry,
and "doors" that open a tool instead of narrowing. **D1**: one widget draws that for
every page; a page only says what its entries are (`SideEntry`).

What a click means is the page's business: `picked` carries the entry and the page
narrows (D2). A *door* (`kind="door"`) emits `door` instead and the highlight stays
where it was. Entries may nest (`parent`), be ticked (`checked`, for a list whose clicks
toggle, as Photos'), and a long section shows its first `shown` entries behind
"More...". Nothing here reads the store.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QMenu, QTreeWidget, QTreeWidgetItem

from app.ui.presenter.side_entry import SideEntry   # Qt-free, so presenters build entries

__all__ = ["SideEntry", "SideList"]

_ENTRY = int(Qt.ItemDataRole.UserRole) + 11
_SECTION = int(Qt.ItemDataRole.UserRole) + 12
_MORE = int(Qt.ItemDataRole.UserRole) + 13


class SideList(QTreeWidget):
    """Sections of entries with icons and counts. See the module docstring."""

    picked = Signal(object)                 # a `SideEntry` of kind filter or all
    door = Signal(object)                   # a `SideEntry` of kind door
    menu_chosen = Signal(object, str)       # the entry, the action named in its menu

    def __init__(self, name: str, *, accessible: str, tip: str = "",
                 shown: Optional[int] = None, open_sections: Iterable[str] = (),
                 parent: Any = None) -> None:
        super().__init__(parent)
        self.setObjectName(name)
        self.setAccessibleName(accessible)
        if tip:
            self.setToolTip(tip)
        self.setHeaderHidden(True)
        self.setColumnCount(2)
        self.setIndentation(12)
        self.setRootIsDecorated(True)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.header().setStretchLastSection(False)
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.setMinimumWidth(180)
        self.itemClicked.connect(self._clicked)
        self.customContextMenuRequested.connect(self._menu)
        self._shown = shown
        self._colours: dict[str, str] = {}
        self._entries: list[SideEntry] = []
        #: What the list was last drawn from, and its rows by key: when only the ticks changed (a click
        #: edits the filter box, the box redraws the list), the rows are restyled in place instead of
        #: rebuilt - a long folder list took a second to rebuild, at every click.
        self._shape: tuple = ()
        self._by_key: dict[str, QTreeWidgetItem] = {}
        self._current = ""
        self._more: set[str] = set()
        #: The sections that open by default; any other starts folded. None: all open.
        self._default_open: Optional[set[str]] = set(open_sections) or None
        #: What the person did to each section since: `{section: expanded}`.
        self._folded: dict[str, bool] = {}
        self.last_menu: Optional[QMenu] = None

    # -- what the page says ----------------------------------------------------------

    def set_entries(self, entries: Iterable[SideEntry]) -> None:
        """Draw these entries, keeping the current one and which sections and folders are open."""
        entries = list(entries)
        shape = tuple((e.key, e.label, e.count, e.parent, e.section, e.icon, e.colour, e.kind, e.tip)
                      for e in entries)
        if self._by_key and shape == self._shape:
            self._entries = entries
            self._restyle(entries)
            return
        opened = {key for key, row in self._by_key.items() if row.isExpanded()}
        for i in range(self.topLevelItemCount()):
            heading = self.topLevelItem(i)
            name = heading.data(0, _SECTION)
            if name is not None:
                self._folded[name] = heading.isExpanded()
        self._entries = list(entries)
        self.blockSignals(True)
        try:
            self.clear()
            sections: dict[str, QTreeWidgetItem] = {}
            items: dict[str, QTreeWidgetItem] = {}
            counted: dict[str, int] = {}
            hidden: dict[str, int] = {}
            for entry in self._entries:
                holder: Any = self
                if entry.parent and entry.parent in items:
                    holder = items[entry.parent]
                elif entry.section:
                    if entry.section not in sections:
                        sections[entry.section] = self._heading(entry.section)
                    holder = sections[entry.section]
                    counted[entry.section] = counted.get(entry.section, 0) + 1
                    if (self._shown is not None and entry.section not in self._more
                            and counted[entry.section] > self._shown):
                        hidden[entry.section] = hidden.get(entry.section, 0) + 1
                        continue
                item = self._item(entry)
                items[entry.key] = item
                if holder is self:
                    self.addTopLevelItem(item)
                else:
                    holder.addChild(item)
            for section, count in hidden.items():
                more = QTreeWidgetItem([f"More... ({count:,})", ""])
                more.setData(0, _MORE, section)
                more.setToolTip(0, f"Show every entry under {section}")
                sections[section].addChild(more)
            for name, heading in sections.items():
                default = self._default_open is None or name in self._default_open
                heading.setExpanded(self._folded.get(name, default))
            for key in opened:
                if key in items:
                    items[key].setExpanded(True)
            self._by_key, self._shape = items, shape
            current = items.get(self._current)
            if current is not None:
                self.setCurrentItem(current)
        finally:
            self.blockSignals(False)

    def _restyle(self, entries: list[SideEntry]) -> None:
        """Only the ticks differ: restyle the rows whose tick changed, and touch nothing else."""
        self.setUpdatesEnabled(False)
        try:
            for entry in entries:
                row = self._by_key.get(entry.key)
                if row is None:
                    continue
                old = row.data(0, _ENTRY)
                if old is None or bool(old.checked) != bool(entry.checked):
                    self._style(row, entry)
                else:
                    row.setData(0, _ENTRY, entry)
        finally:
            self.setUpdatesEnabled(True)

    def entries(self) -> list[SideEntry]:
        return list(self._entries)

    def entry(self, key: str) -> Optional[SideEntry]:
        return next((e for e in self._entries if e.key == key), None)

    def current_key(self) -> str:
        return self._current

    def select(self, key: str) -> None:
        """Highlight an entry without saying so (a restore, not a choice)."""
        self._current = key
        item = self._find(key)
        if item is not None:
            self.blockSignals(True)
            self.setCurrentItem(item)
            self.blockSignals(False)

    def retint(self, colours: dict[str, str]) -> None:
        """Redraw the icons in this palette (the window calls it on a theme change)."""
        self._colours = dict(colours)
        if self._entries:
            self.set_entries(self._entries)

    # -- drawing -------------------------------------------------------------------------

    def _ink(self, name: str, fallback: str) -> str:
        if not self._colours:
            try:
                from app.ui.theme import theme_colours

                self._colours = dict(theme_colours())
            except Exception:                              # noqa: BLE001 - a default ink
                self._colours = {}
        return self._colours.get(name, fallback)

    def _heading(self, text: str) -> QTreeWidgetItem:
        heading = QTreeWidgetItem([text, ""])
        heading.setData(0, _SECTION, text)
        heading.setFlags(Qt.ItemFlag.ItemIsEnabled)
        font = QFont(self.font())
        font.setBold(True)
        heading.setFont(0, font)
        heading.setForeground(0, QBrush(QColor(self._ink("text_faint", "#888888"))))
        self.addTopLevelItem(heading)
        return heading

    def _item(self, entry: SideEntry) -> QTreeWidgetItem:
        count = "" if entry.count is None else f"{entry.count:,}"
        item = QTreeWidgetItem([entry.label, count])
        item.setData(0, _ENTRY, entry)
        item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        item.setForeground(1, QBrush(QColor(self._ink("text_faint", "#888888"))))
        item.setToolTip(0, entry.tip or entry.label)
        self._style(item, entry)
        return item

    def _style(self, item: QTreeWidgetItem, entry: SideEntry) -> None:
        """What depends on the tick: bold and the accent icon when in force, and the words a screen
        reader says. No tick box - the Contacts panel the owner liked has none."""
        from app.ui.widgets.icons import icon

        count = item.text(1)
        item.setData(0, _ENTRY, entry)
        font = QFont(self.font())
        font.setBold(bool(entry.checked))
        item.setFont(0, font)
        if entry.icon:
            item.setIcon(0, icon(entry.icon, self._ink("accent", "#5b4bd5") if entry.checked
                                 else (entry.colour or self._ink("text_dim", "#888888"))))
        label = entry.label if not count else f"{entry.label}, {count}"
        item.setData(0, Qt.ItemDataRole.AccessibleTextRole,
                     f"{entry.label}, chosen" + (f", {count}" if count else "") if entry.checked else label)

    def _find(self, key: str) -> Optional[QTreeWidgetItem]:
        stack = [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
        while stack:
            item = stack.pop()
            entry = item.data(0, _ENTRY)
            if entry is not None and entry.key == key:
                return item
            stack.extend(item.child(i) for i in range(item.childCount()))
        return None

    # -- clicks --------------------------------------------------------------------------

    def _clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        more = item.data(0, _MORE)
        if more:
            self._more.add(str(more))
            self.set_entries(self._entries)
            return
        entry = item.data(0, _ENTRY)
        if entry is None:
            if item.data(0, _SECTION) is not None:
                item.setExpanded(not item.isExpanded())
            return
        if entry.kind == "door":
            # A door, not a list: put the highlight back where it was.
            self.select(self._current)
            self.door.emit(entry)
            return
        self._current = entry.key
        self.picked.emit(entry)

    def _menu(self, point: QPoint) -> None:
        item = self.itemAt(point)
        entry = item.data(0, _ENTRY) if item is not None else None
        if entry is None or not entry.menu:
            return
        menu = QMenu(self)
        self._fill_menu(menu, entry, entry.menu)
        self.last_menu = menu
        menu.popup(self.viewport().mapToGlobal(point))

    def _fill_menu(self, menu: QMenu, entry: SideEntry, items: Any) -> None:
        """One line per `(label, icon, action[, colour])`. An `action` that is itself a tuple of
        such lines is a submenu (Contacts' tag colours); `colour` tints the icon (a tag's own)."""
        from app.ui.widgets.icons import icon

        for line in items:
            label, icon_name, action = line[0], line[1], line[2]
            tint = line[3] if len(line) > 3 and line[3] else self._ink("text_dim", "#888888")
            if isinstance(action, (tuple, list)):
                sub = menu.addMenu(icon(icon_name, tint), label) if icon_name else menu.addMenu(label)
                self._fill_menu(sub, entry, action)
                continue
            act = menu.addAction(icon(icon_name, tint), label) if icon_name else menu.addAction(label)
            act.triggered.connect(lambda _c=False, e=entry, a=action: self.menu_chosen.emit(e, a))
