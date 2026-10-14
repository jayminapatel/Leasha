"""Clean-up tools (order 1k 6p).

Layer: L5

Four tabs, each a preview of every change before anything is written:

1. **Phone numbers** into one format, international or national, the person's choice.
2. **Name capitals**: all-lower and ALL-CAPS names are fixed; particles ("van", "de") and names
   with their own capitals are left alone unless the person ticks the box.
3. **Company from email**: a blank company filled from a work email's domain; free-mail
   domains (gmail.com and the rest) never give a company.
4. **Finders**: lists to act on - no name, no email or phone, only a name, the same value held
   twice in one contact.

The person ticks the changes to accept (all of them by default, or one at a time) and presses
**Apply the ticked changes**. Everything applied is one journal batch; **Undo this clean-up**
puts it all back in one step. The logic is `app/contacts/cleanup.py`; applying is
`actions.apply_changes`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from app.contacts import cleanup
from app.contacts.card import display_name
from app.ui.contacts_tools import actions
from app.ui.contacts_tools._common import ToolDialog, btn, ink, make_table, named, plural
from app.ui.later import later
from app.ui.widgets.icons import icon
from app.ui.theme import SPACE_S

__all__ = ["CleanupDialog", "compute", "FINDERS"]

#: Rows made per turn of the event loop: the window answers between turns, however long the list.
_CHUNK = 400
#: Column widths come from this many rows, not all of them (measuring ten thousand rows is the freeze).
_FIT_SAMPLE = 300

FINDERS = (
    ("no_name", "Contacts with no name"),
    ("no_contact", "No email or phone number"),
    ("only_name", "Nothing but a name"),
    ("copies", "The same value held twice"),
)


@dataclass
class Computed:
    kind: str
    changes: list = field(default_factory=list)       # cleanup.Change or cleanup.Finding
    names: dict = field(default_factory=dict)


def compute(hub: Any, kind: str, ids: Optional[list[int]] = None, *, style: str = "international",
            force: bool = False, finder: str = "no_name") -> Computed:
    """Read the contacts in scope and run one tool over them. Reads only; a worker's job."""
    if ids is None:
        items = hub.all_cards()
    else:
        items = []
        for cid in ids:
            card = hub.get_card(cid)
            if card is not None:
                items.append((cid, card))
    names = {cid: display_name(card) or f"Contact {cid}" for cid, card in items}
    if kind == "phone":
        found = cleanup.normalise_phones(items, style, hub.region)
    elif kind == "names":
        found = cleanup.fix_name_case(items, force)
    elif kind == "company":
        found = cleanup.company_from_domain(items)
    elif kind == "finders":
        fn = {"no_name": cleanup.no_name, "no_contact": cleanup.no_email_or_phone,
              "only_name": cleanup.only_a_name}.get(finder)
        found = fn(items) if fn else cleanup.exact_field_copies(items, hub.region)
    else:
        raise ValueError(f"Unknown clean-up tool {kind!r}.")
    return Computed(kind, list(found), names)


class _Tab(QWidget):
    """One tab: its options, its preview table and its select buttons."""

    def __init__(self, dialog: "CleanupDialog", kind: str, headers: list[str], name: str, tip: str) -> None:
        super().__init__()
        self.kind = kind
        self.dialog = dialog
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, SPACE_S, 0, 0)
        self.options = QHBoxLayout()
        self.layout_.addLayout(self.options)
        self.count = QLabel("")
        named(self.count, f"{name} count", "How many changes are listed")
        self.layout_.addWidget(self.count)
        self.table = make_table(dialog, headers, name, tip, stretch=len(headers) - 1, select="multi")
        self.layout_.addWidget(self.table, 1)
        self.computed: Optional[Computed] = None
        self.token = 0                    # the newest read of this tab; an older answer is dropped

    def any_ticked(self) -> bool:
        """Is anything ticked? Stops at the first, so a long list costs one look."""
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                return True
        return False

    def ticked(self) -> list:
        out = []
        if self.computed is None:
            return out
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                out.append(self.computed.changes[item.data(Qt.ItemDataRole.UserRole)])
        return out

    def set_all(self, on: bool) -> None:
        """Tick or untick every row in one go: the table is told once, not once per row."""
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        self.table.blockSignals(True)
        self.table.setUpdatesEnabled(False)
        try:
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 0)
                if item is not None:
                    item.setCheckState(state)
        finally:
            self.table.setUpdatesEnabled(True)
            self.table.blockSignals(False)


class CleanupDialog(ToolDialog):
    def __init__(self, ctx: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(ctx, parent, title="Clean up contacts", glyph="wand-sparkles",
                         subtitle="Each tool shows every change first. Tick the ones you want, apply them, and undo the "
                                  "whole clean-up in one step if you change your mind.",
                         size=(980, 660), name="contacts-cleanup")
        self.hub = ctx.hub
        self.last_batch: Optional[str] = None
        self._token = 0

        scope_row = QHBoxLayout()
        scope_label = QLabel("Clean up:")
        named(scope_label, "Scope label", "Which contacts to look at")
        self.scope = QComboBox()
        named(self.scope, "Which contacts", "Clean up the whole book, the contacts in view, or the ones you selected")
        self.scope.setToolTip("Clean up the whole book, the contacts in view, or the ones you selected")
        self.scope.addItem("The whole book", None)
        visible = list(getattr(ctx, "visible_ids", []) or [])
        selected = list(getattr(ctx, "selected_ids", []) or [])
        if visible:
            self.scope.addItem(f"The contacts in view ({len(visible)})", visible)
        if selected:
            self.scope.addItem(f"The selected contacts ({len(selected)})", selected)
        self.scope.currentIndexChanged.connect(lambda _i: self.reload())
        scope_row.addWidget(scope_label)
        scope_row.addWidget(self.scope, 1)
        self.body.addLayout(scope_row)

        self.tabs = QTabWidget()
        named(self.tabs, "Clean-up tools", "Switch between the clean-up tools")
        self.body.addWidget(self.tabs, 1)
        headers = ["", "Contact", "Field", "Before", "After"]

        # 1. phone numbers
        self.phone = _Tab(self, "phone", headers, "Phone number changes", "Every phone number that would be rewritten")
        self.style_combo = QComboBox()
        self.style_combo.addItem("International  (+44 20 7946 0018)", "international")
        self.style_combo.addItem("National  (020 7946 0018)", "national")
        named(self.style_combo, "Phone number format", "How phone numbers are written. National is used only for numbers "
                                                       "in your own country")
        self.style_combo.setToolTip("How phone numbers are written. National is used only for numbers "
                                                       "in your own country")
        self.style_combo.currentIndexChanged.connect(lambda _i: self.reload("phone"))
        style_label = QLabel("Write numbers as:")
        named(style_label, "Format label", "How phone numbers are written")
        self.phone.options.addWidget(style_label)
        self.phone.options.addWidget(self.style_combo, 1)
        self.tabs.addTab(self.phone, icon("phone", ink()), "Phone numbers")

        # 2. names
        self.names = _Tab(self, "names", headers, "Name capital changes", "Every name that would be re-capitalised")
        self.force_box = QCheckBox("Capitalise every word, even ones that already have capitals (McDonald becomes Mcdonald)")
        named(self.force_box, "Capitalise everything", "Also change names that already have their own capitals, and words "
                                                       "like van and de")
        self.force_box.setToolTip("Also change names that already have their own capitals, and words "
                                                       "like van and de")
        self.force_box.toggled.connect(lambda _on: self.reload("names"))
        self.names.options.addWidget(self.force_box, 1)
        self.tabs.addTab(self.names, icon("user", ink()), "Name capitals")

        # 3. company
        self.company = _Tab(self, "company", headers, "Company changes", "Every blank company that would be filled in")
        note = QLabel("Only blank companies are filled, from a work email's domain. gmail.com, outlook.com and other free "
                      "mail never give a company.")
        note.setWordWrap(True)
        named(note, "Company note", "How the company is guessed")
        self.company.options.addWidget(note, 1)
        self.tabs.addTab(self.company, icon("building-2", ink()), "Company from email")

        # 4. finders
        self.finders = _Tab(self, "finders", ["", "Contact", "What was found"], "Finder results",
                            "Contacts that need a look", )
        self.finder_combo = QComboBox()
        for key, text in FINDERS:
            self.finder_combo.addItem(text, key)
        named(self.finder_combo, "What to look for", "Which kind of contact to list")
        self.finder_combo.setToolTip("Which kind of contact to list")
        self.finder_combo.currentIndexChanged.connect(lambda _i: self.reload("finders"))
        look = QLabel("Find:")
        named(look, "Find label", "What to look for")
        self.finders.options.addWidget(look)
        self.finders.options.addWidget(self.finder_combo, 1)
        self.bin_button = btn("Move to the bin", "trash-2", "danger",
                              "Send the ticked contacts to the bin. You can undo this, and the bin keeps them 30 days",
                              "Move to the bin")
        self.copies_button = btn("Remove the extra copies", "eraser", "secondary",
                                 "In each ticked contact, keep one copy of every repeated value", "Remove the extra copies")
        self.bin_button.clicked.connect(self.bin_ticked)
        self.copies_button.clicked.connect(self.remove_copies_ticked)
        self.finders.layout_.addLayout(_row(self.bin_button, self.copies_button))
        self.tabs.addTab(self.finders, icon("scan-search", ink()), "Finders")
        self.tabs.currentChanged.connect(lambda _i: self._tab_shown())

        self._tabs = {"phone": self.phone, "names": self.names, "company": self.company, "finders": self.finders}
        for tab in (self.phone, self.names, self.company):
            row = _row(self._small("Select all", "list-checks", tab, True), self._small("Select none", "list-x", tab, False))
            tab.layout_.addLayout(row)
        row = _row(self._small("Select all", "list-checks", self.finders, True),
                   self._small("Select none", "list-x", self.finders, False))
        self.finders.layout_.addLayout(row)

        self.apply_button = btn("Apply the ticked changes", "check", "primary",
                                "Write every ticked change. They are saved together, so one Undo takes them all back",
                                "Apply the ticked changes")
        self.undo_button = btn("Undo this clean-up", "undo-2", "secondary", "Put back everything the last clean-up changed",
                               "Undo this clean-up")
        self.undo_button.hide()
        self.apply_button.clicked.connect(self.apply_ticked)
        self.undo_button.clicked.connect(self.undo)
        self.add_footer_buttons(self.apply_button, self.undo_button)
        for tab in self._tabs.values():
            tab.table.itemChanged.connect(lambda _item: self._sync())
        self.reload()

    def _tab_shown(self) -> None:
        if self.current().computed is None:
            self.reload()
        else:
            self._sync()

    def _small(self, text: str, glyph: str, tab: _Tab, on: bool):
        b = btn(text, glyph, "secondary", f"{text}: every row in this list", f"{text} ({tab.table.accessibleName()})")
        b.clicked.connect(lambda: (tab.set_all(on), self._sync()))
        return b

    # -- loading -------------------------------------------------------------
    def current(self) -> _Tab:
        return self.tabs.currentWidget()

    def scope_ids(self) -> Optional[list[int]]:
        return self.scope.currentData()

    def reload(self, kind: Optional[str] = None) -> None:
        """Recompute one tab (or the visible one) on a worker."""
        tab = self._tabs[kind] if kind else self.current()
        self._token += 1
        tab.token = token = self._token
        self.busy(True, "Looking through your contacts…")
        hub, ids = self.hub, self.scope_ids()
        style = self.style_combo.currentData()
        force = self.force_box.isChecked()
        finder = self.finder_combo.currentData()
        self.job(lambda: compute(hub, tab.kind, ids, style=style, force=force, finder=finder),
                 lambda result, t=tab, n=token: self._computed(t, result, n))

    def _computed(self, tab: _Tab, result: Computed, token: int) -> None:
        if token != tab.token:
            return                                          # a newer read of this tab is on its way
        self.busy(False)
        tab.computed = result
        table = tab.table
        table.blockSignals(True)                            # no handler runs per row: see `_fill_rows`
        table.setRowCount(len(result.changes))
        self.apply_button.setEnabled(False)
        self._fill_rows(tab, 0, token)

    def _fill_rows(self, tab: _Tab, start: int, token: int) -> None:
        """Make the rows a few hundred at a time, so the window never waits on a long list.

        2026-10-11, the owner: Clean up froze "not responding" with many entries. Each `setItem` fired
        the table's change handler, which counted every ticked row again - rows squared, a minute of
        a held window for three thousand. Now no handler runs while rows are made, the column widths
        come from a sample, and the buttons are worked out once at the end."""
        try:
            result, table = tab.computed, tab.table
            if token != tab.token or result is None:
                return
            n = len(result.changes)
            end = min(start + _CHUNK, n)
            table.setUpdatesEnabled(False)
            for row in range(start, end):
                self._make_row(tab, result, row)
            table.setUpdatesEnabled(True)
            if end < n:
                tab.count.setText(f"Listing {end:,} of {n:,} changes...")
                later(self, 0, lambda: self._fill_rows(tab, end, token))
                return
            table.blockSignals(False)
            _fit_columns(table)
            tab.count.setText(f"{plural(n, 'change' if tab.kind != 'finders' else 'contact')} found."
                              if n else "Nothing to do here.")
            self._sync()
        except RuntimeError:
            return                                          # the window was closed while it was listing

    @staticmethod
    def _make_row(tab: _Tab, result: Computed, row: int) -> None:
        change, table = result.changes[row], tab.table
        check = QTableWidgetItem("")
        check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
        check.setCheckState(Qt.CheckState.Checked)
        check.setData(Qt.ItemDataRole.UserRole, row)
        table.setItem(row, 0, check)
        table.setItem(row, 1, QTableWidgetItem(result.names.get(change.id, str(change.id))))
        if tab.kind == "finders":
            table.setItem(row, 2, QTableWidgetItem(change.reason))
        else:
            label = {"phone": "Phone"}.get(change.field, change.field[2:].capitalize() if change.field.startswith("s:")
                                           else change.field)
            table.setItem(row, 2, QTableWidgetItem(label))
            table.setItem(row, 3, QTableWidgetItem(change.before))
            table.setItem(row, 4, QTableWidgetItem(change.after))

    def _sync(self) -> None:
        tab = self.current()
        on_changes = tab.kind != "finders"
        ticked = tab.any_ticked()
        self.apply_button.setEnabled(on_changes and ticked)
        is_finder = tab.kind == "finders"
        self.bin_button.setEnabled(is_finder and ticked)
        copies = is_finder and self.finder_combo.currentData() == "copies"
        self.copies_button.setVisible(copies)
        self.copies_button.setEnabled(copies and ticked)

    # -- acting --------------------------------------------------------------
    def apply_ticked(self) -> None:
        tab = self.current()
        changes = tab.ticked()
        if tab.kind == "finders" or not changes:
            return
        self._write(lambda hub, batch: actions.apply_changes(hub, changes, batch_id=batch), "Applying…")

    def bin_ticked(self) -> None:
        ids = [c.id for c in self.finders.ticked()]
        if ids:
            self._write(lambda hub, batch: actions.bin_contacts(hub, ids, batch_id=batch), "Moving to the bin…")

    def remove_copies_ticked(self) -> None:
        ids = [c.id for c in self.finders.ticked()]
        if ids:
            self._write(lambda hub, batch: actions.remove_copies(hub, ids, batch_id=batch), "Removing the extra copies…")

    def _write(self, fn: Any, text: str) -> None:
        self.busy(True, text)
        self.apply_button.setEnabled(False)
        hub = self.hub
        self.job(lambda: fn(hub, actions.new_batch()), self._applied)

    def _applied(self, result: Any) -> None:
        self.last_batch = result.batch_id
        self.undo_button.setVisible(bool(result.batch_id))
        self.busy(False, f"Changed {plural(result.changes, 'value')} in {plural(result.contacts, 'contact')}. "
                  "Undo this clean-up puts them all back." if result.batch_id else "Nothing needed changing.")
        self.refresh_page()
        self._stale()
        self.reload()

    def _stale(self) -> None:
        for tab in self._tabs.values():
            if tab is not self.current():
                tab.computed = None

    def undo(self) -> None:
        batch = self.last_batch
        if not batch:
            return
        self.busy(True, "Putting everything back…")
        hub = self.hub
        self.job(lambda: hub.undo_batch(batch), self._undone)

    def _undone(self, result: Any) -> None:
        self.last_batch = None
        self.undo_button.hide()
        self.busy(False, f"Undone: {plural(result.undone, 'contact')} put back.")
        self.refresh_page()
        self._stale()
        self.reload()


def _fit_columns(table: Any) -> None:
    """Widths from the first rows and the headings, each capped: the same fit as `resizeColumnsToContents`
    for a short list, and the same cost for any list."""
    metrics = QFontMetrics(table.font())
    for column in range(table.columnCount() - 1):           # the last column stretches
        head = table.horizontalHeaderItem(column)
        widest = metrics.horizontalAdvance(head.text()) if head is not None else 0
        for row in range(min(table.rowCount(), _FIT_SAMPLE)):
            item = table.item(row, column)
            if item is not None:
                widest = max(widest, metrics.horizontalAdvance(item.text()))
        table.setColumnWidth(column, 36 if column == 0 else min(widest + 28, 460))


def _row(*buttons: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(SPACE_S)
    for b in buttons:
        row.addWidget(b)
    row.addStretch(1)
    return row
