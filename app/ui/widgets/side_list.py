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
from app.ui.widgets.paint_kit import paint_focus_ring, set_weight

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
        self._structure: tuple = ()
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
            self._let_go_if_untucked()
            return
        # The same rows with other words or numbers (a count moved, a tick changed, a label was reworded): update
        # the rows where they are. Taking the list apart and putting it together again for that made the whole
        # tree flash at every click on Browse (the owner, 2026-10-12) - and cost a rebuild each time.
        structure = tuple((e.key, e.parent, e.section, e.kind) for e in entries)
        if self._by_key and structure == self._structure:
            self._entries = entries
            self._update_in_place(entries)
            self._shape = shape
            self._let_go_if_untucked()
            return
        bar = self.verticalScrollBar().value()
        self.setUpdatesEnabled(False)                  # a rebuild is one repaint, not a flicker of half a tree
        try:
            self._rebuild(entries, shape, structure)
        finally:
            self.setUpdatesEnabled(True)
        self.verticalScrollBar().setValue(bar)
        self._let_go_if_untucked()

    def _let_go_if_untucked(self) -> None:
        """A filter taken away by a second click lets go of the highlight too.

        The click that removed it left Qt's selection on the row, so the list looked as if the
        filter were still in force when the box no longer held it (2026-10-12). Only an entry that
        says it is ticked or not (`checked` is not None) is let go; a list that does not tick -
        Contacts' lists, Browse's years - keeps its highlight as the only mark of what is chosen.
        """
        entry = self.entry(self._current) if self._current else None
        if entry is None or entry.checked is not False:
            return
        self._current = ""
        self.blockSignals(True)
        try:
            self.clearSelection()
            self.setCurrentItem(None)
        finally:
            self.blockSignals(False)

    def _rebuild(self, entries: list[SideEntry], shape: tuple, structure: tuple) -> None:
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
            self._by_key, self._shape, self._structure = items, shape, structure
            current = items.get(self._current)
            if current is not None:
                self.setCurrentItem(current)
        finally:
            self.blockSignals(False)

    def _update_in_place(self, entries: list[SideEntry]) -> None:
        """Same rows, new words, numbers, tips or ticks: change each row where it stands. Nothing is cleared, so
        nothing flashes, and what is open, selected or scrolled stays so."""
        self.setUpdatesEnabled(False)
        try:
            for entry in entries:
                row = self._by_key.get(entry.key)
                if row is None:
                    continue
                count = "" if entry.count is None else f"{entry.count:,}"
                if row.text(0) != entry.label:
                    row.setText(0, entry.label)
                if row.text(1) != count:
                    row.setText(1, count)
                tip = entry.tip or entry.label
                if row.toolTip(0) != tip:
                    row.setToolTip(0, tip)
                row.setForeground(1, QBrush(QColor(self._ink("text_faint", "#888888"))))
                self._style(row, entry)
        finally:
            self.setUpdatesEnabled(True)

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
            self._shape = ()                               # a new palette: draw every row again, not just the ticks
            self.set_entries(self._entries)

    # -- drawing -------------------------------------------------------------------------

    def _ink(self, name: str, fallback: Optional[str] = None) -> str:
        """`name` from the palette this list was given, else from the theme in force.

        No `fallback` means the theme's own token, never a colour written here: the accent fell
        back to a hex of its own, so a palette that did not name it ticked rows in a colour no
        theme has (2026-10-12). `theme_colours()` is never empty.
        """
        if not self._colours:
            try:
                from app.ui.theme import theme_colours

                self._colours = dict(theme_colours())
            except Exception:                              # noqa: BLE001 - a default ink
                self._colours = {}
        if name in self._colours or fallback is not None:
            return self._colours.get(name, fallback)
        from app.ui.theme import theme_colours

        return theme_colours()[name]

    def _heading(self, text: str) -> QTreeWidgetItem:
        heading = QTreeWidgetItem([text, ""])
        heading.setData(0, _SECTION, text)
        heading.setFlags(Qt.ItemFlag.ItemIsEnabled)
        heading.setFont(0, set_weight(QFont(self.font()), "bold"))
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
        item.setFont(0, set_weight(QFont(self.font()), "bold" if entry.checked else "regular"))
        if entry.icon:
            item.setIcon(0, icon(entry.icon, self._ink("accent") if entry.checked
                                 else (entry.colour or self._ink("text_dim", "#888888"))))
        label = entry.label if not count else f"{entry.label}, {count}"
        item.setData(0, Qt.ItemDataRole.AccessibleTextRole,
                     f"{entry.label}, chosen" + (f", {count}" if count else "") if entry.checked else label)

    def drawRow(self, painter: Any, option: Any, index: Any) -> None:  # noqa: N802 - Qt's name
        """The row as Qt draws it, then the keyboard's ring round the whole current line.

        The sheet's `outline: 0` took away the native dotted box and nothing replaced it, so
        moving down a side list with the arrow keys showed no place at all (review 2026-10-14,
        DS-2). A sheet rule would ring each of the two columns separately; this is one ring
        round the row, in the width and colour every painted list uses (`paint_kit`).
        """
        super().drawRow(painter, option, index)
        current = self.currentIndex()
        if (self.hasFocus() and current.isValid() and current.row() == index.row()
                and current.parent() == index.parent()):
            paint_focus_ring(painter, option.rect.adjusted(1, 0, -1, 0), self._colours or None)

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

    def keyPressEvent(self, event: Any) -> None:            # noqa: N802 - Qt's name
        """Enter or Return on the current line is a click on it; every other key is the tree's own.

        The list heard only `itemClicked`, so from the keyboard nothing in any side list could be
        chosen (2026-10-12). A key handler rather than `itemActivated`: Qt sends that for a
        double-click as well, which would answer one double-click twice. The arrows only move -
        a door opens a dialog, and walking past one must not open it.
        """
        if (event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and not event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier):
            item = self.currentItem()
            if item is not None:
                self._clicked(item, 0)
                event.accept()
                return
        super().keyPressEvent(event)

    def _menu(self, point: QPoint) -> None:
        """Right-click on an entry: its page's lines, built by `menus.build` in the page's own order."""
        from app.ui.widgets import menus

        item = self.itemAt(point)               # the viewport's point, as Qt gives it
        entry = item.data(0, _ENTRY) if item is not None else None
        if entry is None or not entry.menu:
            return
        menu = menus.build(self, self._menu_items(entry, entry.menu), grouped=False)
        menus.show(menu, self, point, wait=False, keep=self)

    def _menu_items(self, entry: SideEntry, items: Any) -> list:
        """One line per `(label, icon, action[, colour])`. An `action` that is itself a tuple of
        such lines is a submenu (Contacts' tag colours); `colour` tints the icon (a tag's own)."""
        from app.ui.widgets.menus import Item

        lines = []
        for line in items:
            label, icon_name, action = line[0], line[1], line[2]
            tint = line[3] if len(line) > 3 and line[3] else self._ink("text_dim", "#888888")
            if isinstance(action, (tuple, list)):
                lines.append(Item(label, icon=icon_name, tint=tint, sub=self._menu_items(entry, action)))
                continue
            lines.append(Item(label, lambda e=entry, a=action: self.menu_chosen.emit(e, a),
                              icon=icon_name, tint=tint))
        return lines
