"""The merge dialog and its preview (order 1k 6g, 6o).

Layer: L5

Two or more contacts side by side, one column each. For every single-valued field (a company,
a job title, notes) a drop-down picks which contact it comes from; for every repeatable value
(an email, a phone, an address) a box says whether to keep it. A live **Result** card shows the
contact as it will be, and **What each account will be sent** shows, for each account in the
result's "Kept in" set, the card as that account would hold it (its provider's capabilities when
the host has them, else the full card; D10). Nothing is written until **Confirm merge**.

After a merge the **Split again** button calls `hub.undo_batch` on the merge's batch and the
contacts come back as they were. The card shown in Result is exactly the card `actions.commit_merge`
writes: both come from `MergeModel.result()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QLabel, QSplitter, QTabWidget, QTableWidgetItem, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from app.contacts.card import SCALARS, Card, display_name
from app.ui.contacts_tools import actions
from app.ui.contacts_tools._common import (
    SCALAR_LABELS, CardList, ToolDialog, btn, card_rows, ink, kind_icon, make_table, named, plural,
    scalar_icon, KIND_LABELS,
)
from app.ui.widgets.icons import icon

__all__ = ["MergeDialog"]


@dataclass
class _Loaded:
    ids: list[int]
    cards: list[Card]
    kept_in: list[int]
    accounts: list[tuple[int, str, str, Any]]          # id, name, address, capabilities


def load_for_merge(ctx: Any, ids: Sequence[int]) -> _Loaded:
    """Read the cards, the accounts they are kept in and what each account can hold. A worker's job."""
    hub = ctx.hub
    have, cards = [], []
    for cid in ids:
        card = hub.get_card(cid)
        if card is not None:
            have.append(cid)
            cards.append(card)
    kept: list[int] = []
    for cid in have:
        for acc in hub.kept_in(cid):
            if acc not in kept:
                kept.append(acc)
    accounts = []
    store = getattr(ctx, "accounts", None)
    for acc_id in kept:
        account = store.get(acc_id) if store is not None else None
        if account is None:
            continue
        accounts.append((acc_id, account.name or account.address, account.address, actions.caps_for(ctx, account)))
    return _Loaded(have, cards, kept, accounts)


class MergeDialog(ToolDialog):
    def __init__(self, ctx: Any, ids: Sequence[int], parent: Optional[QWidget] = None) -> None:
        super().__init__(ctx, parent, title="Merge contacts", glyph="merge",
                         subtitle="Choose what the one contact keeps. The result is shown as you choose, and nothing is "
                                  "written until you confirm.",
                         size=(1100, 680), name="contacts-merge")
        self.hub = ctx.hub
        self.ids = [int(i) for i in ids]
        self.model: Optional[actions.MergeModel] = None
        self.loaded: Optional[_Loaded] = None
        self.batch: Optional[str] = None
        self.merged = False
        self._combos: dict[str, QComboBox] = {}
        self._options: dict[str, list] = {}
        self._checks: dict[str, QCheckBox] = {}

        top = QHBoxLayout()
        label = QLabel("Keep as the main contact:")
        named(label, "Main contact label", "The contact whose place in the list the merged one takes")
        self.survivor_combo = QComboBox()
        named(self.survivor_combo, "Main contact", "Which of these contacts the merged contact is stored as")
        self.survivor_combo.setToolTip("Which of these contacts the merged contact is stored as")
        self.survivor_combo.currentIndexChanged.connect(self._survivor_changed)
        top.addWidget(label)
        top.addWidget(self.survivor_combo, 1)
        self.body.addLayout(top)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.table = make_table(self, ["Field"], "Contacts side by side",
                                "One column for each contact, one row for each field", select="rows")
        split.addWidget(self.table)
        right = QTabWidget()
        named(right, "Preview", "The merged contact, and what each account will be sent")
        self.result_list = CardList("Result", "The contact as it will be after the merge")
        right.addTab(self.result_list, icon("id-card", ink()), "Result")
        sent = QWidget()
        sl = QVBoxLayout(sent)
        sl.setContentsMargins(0, 6, 0, 0)
        self.sent_note = QLabel("")
        self.sent_note.setWordWrap(True)
        named(self.sent_note, "Accounts note", "Where the merged contact is kept")
        self.sent_tree = QTreeWidget()
        self.sent_tree.setHeaderHidden(True)
        named(self.sent_tree, "What each account will be sent",
              "For each account the contact is kept in, the card as that account will hold it")
        sl.addWidget(self.sent_note)
        sl.addWidget(self.sent_tree, 1)
        right.addTab(sent, icon("cloud", ink()), "What each account will be sent")
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        self.body.addWidget(split, 1)

        self.confirm_button = btn("Confirm merge", "merge", "primary",
                                  "Write the merged contact. You can split it again afterwards", "Confirm merge")
        self.split_button = btn("Split again", "split", "secondary",
                                "Undo this merge: the contacts come back as they were", "Split again")
        self.confirm_button.setEnabled(False)
        self.split_button.hide()
        self.confirm_button.clicked.connect(self.confirm)
        self.split_button.clicked.connect(self.split_again)
        self.add_footer_buttons(self.confirm_button, self.split_button)

        if len(self.ids) < 2:
            self.say("Select two or more contacts to merge.")
            return
        self.busy(True, "Reading the contacts…")
        ctx_, ids_ = ctx, list(self.ids)
        self.job(lambda: load_for_merge(ctx_, ids_), self._loaded)

    # -- loading -------------------------------------------------------------
    def _loaded(self, loaded: _Loaded) -> None:
        self.busy(False)
        if len(loaded.cards) < 2:
            self.say("Those contacts are no longer in the book, so there is nothing to merge.")
            return
        self.loaded = loaded
        self.ids = loaded.ids
        self.model = actions.MergeModel(loaded.ids, loaded.cards, self.hub.region)
        self.survivor_combo.blockSignals(True)
        for cid, card in zip(loaded.ids, loaded.cards):
            self.survivor_combo.addItem(display_name(card) or f"Contact {cid}")
        self.survivor_combo.blockSignals(False)
        self._build_table()
        self.confirm_button.setEnabled(True)
        self._refresh_result()

    def _names(self) -> list[str]:
        assert self.loaded is not None
        return [display_name(c) or f"Contact {i}" for i, c in zip(self.loaded.ids, self.loaded.cards)]

    def _build_table(self) -> None:
        assert self.model is not None
        model, names = self.model, self._names()
        cols = ["Field", *names, "Use in the result"]
        self.table.setColumnCount(len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        rows = 0
        self.table.setRowCount(0)

        def add_row() -> int:
            nonlocal rows
            self.table.insertRow(rows)
            rows += 1
            return rows - 1

        last = len(cols) - 1
        # single-valued fields: pick a source
        for key in SCALARS:
            values = [(n, c.get(key).strip()) for n, c in enumerate(model.cards) if c.get(key).strip()]
            if not values:
                continue
            r = add_row()
            self._label_cell(r, SCALAR_LABELS[key], scalar_icon(key))
            for n, c in enumerate(model.cards):
                text = c.get(key).strip()
                self.table.setItem(r, n + 1, QTableWidgetItem(text.replace("\n", " / ")))
            distinct: list[tuple[str, int]] = []
            for n, v in values:
                if v not in [d[0] for d in distinct]:
                    distinct.append((v, n))
            if len(distinct) == 1:
                self.table.setItem(r, last, QTableWidgetItem("Same in all"))
                continue
            combo = QComboBox()
            options: list[Optional[int]] = []
            if key == "notes":
                combo.addItem("Both, one after the other")
                options.append(None)
            for v, n in distinct:
                combo.addItem(f"{v.splitlines()[0][:40]}  ({names[n]})")
                options.append(n)
            named(combo, f"Use for {SCALAR_LABELS[key]}", f"Which contact's {SCALAR_LABELS[key].lower()} the result keeps")
            combo.setToolTip(f"Which contact's {SCALAR_LABELS[key].lower()} the result keeps")
            combo.currentIndexChanged.connect(lambda i, k=f"s:{key}", o=options: self._picked(k, o[i]))
            self._combos[f"s:{key}"] = combo
            self._options[f"s:{key}"] = options
            self.table.setCellWidget(r, last, combo)
        # repeatable values: keep or drop
        for key, f, held in model.repeatable_keys():
            r = add_row()
            label = KIND_LABELS.get(f.kind, f.kind) + (f" ({f.label})" if f.label else "")
            self._label_cell(r, label, kind_icon(f.kind, f.label))
            text = f.value.strip() or ", ".join(v for v in f.data.values() if v)
            for n in range(len(model.cards)):
                self.table.setItem(r, n + 1, QTableWidgetItem(text.replace("\n", ", ") if n in held else ""))
            check = QCheckBox("Keep")
            check.setChecked(True)
            named(check, f"Keep {KIND_LABELS.get(f.kind, f.kind)} {text}", f"Keep this {KIND_LABELS.get(f.kind, f.kind).lower()} in the result")
            check.setToolTip(f"Keep this {KIND_LABELS.get(f.kind, f.kind).lower()} in the result")
            check.toggled.connect(lambda on, k=key: self._kept(k, on))
            self._checks[key] = check
            self.table.setCellWidget(r, last, check)
        # groups and favourite
        groups = [", ".join(c.groups) for c in model.cards]
        if any(groups):
            r = add_row()
            self._label_cell(r, "Groups", "tag")
            for n, g in enumerate(groups):
                self.table.setItem(r, n + 1, QTableWidgetItem(g))
            self.table.setItem(r, last, QTableWidgetItem("All kept"))
        if any(c.favourite for c in model.cards):
            r = add_row()
            self._label_cell(r, "Favourite", "star")
            for n, c in enumerate(model.cards):
                self.table.setItem(r, n + 1, QTableWidgetItem("Yes" if c.favourite else ""))
            self.table.setItem(r, last, QTableWidgetItem("Yes"))
        # photo
        shas = [(n, c.photo.sha) for n, c in enumerate(model.cards) if c.photo is not None]
        if shas:
            r = add_row()
            self._label_cell(r, "Photo", "image")
            for n, c in enumerate(model.cards):
                self.table.setItem(r, n + 1, QTableWidgetItem("Photo" if c.photo is not None else ""))
            distinct_photos = list(dict.fromkeys(s for _, s in shas))
            if len(distinct_photos) > 1:
                combo = QComboBox()
                options = [n for n, _s in shas]
                for n in options:
                    combo.addItem(f"Photo of {names[n]}")
                named(combo, "Use for photo", "Which contact's photo the result keeps")
                combo.setToolTip("Which contact's photo the result keeps")
                combo.currentIndexChanged.connect(lambda i, o=options: self._picked("photo", o[i]))
                self._combos["photo"] = combo
                self._options["photo"] = options
                self.table.setCellWidget(r, last, combo)
            else:
                self.table.setItem(r, last, QTableWidgetItem("Same in all"))
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setStretchLastSection(True)

    def _label_cell(self, row: int, text: str, glyph: str) -> None:
        item = QTableWidgetItem(icon(glyph, ink()), text)
        self.table.setItem(row, 0, item)

    # -- choices -------------------------------------------------------------
    def _picked(self, key: str, index: Optional[int]) -> None:
        if self.model is not None:
            self.model.choose(key, index)
            self._refresh_result()

    def _kept(self, key: str, on: bool) -> None:
        if self.model is not None:
            self.model.keep(key, on)
            self._refresh_result()

    def _survivor_changed(self, index: int) -> None:
        if self.model is not None and index >= 0:
            self.model.survivor_index = index
            self._refresh_result()

    def set_choice(self, key: str, index: Optional[int]) -> None:
        """Choose, as the drop-down would, which contact (by column position, 0 first) a field
        comes from. `key` is `s:company`, `s:notes` or `photo`; `None` is 'both' for notes."""
        combo, options = self._combos.get(key), self._options.get(key)
        if combo is None or options is None or index not in options:
            self._picked(key, index)
            return
        combo.setCurrentIndex(options.index(index))

    def set_keep(self, key: str, on: bool) -> None:
        check = self._checks.get(key)
        if check is not None:
            check.setChecked(on)
        else:
            self._kept(key, on)

    def check_keys(self) -> dict[str, str]:
        """{field key: the value's words}, for each repeatable value in the table."""
        return {k: c.accessibleName() for k, c in self._checks.items()}

    # -- the preview ---------------------------------------------------------
    def result_card(self) -> Optional[Card]:
        return self.model.result() if self.model is not None else None

    def _refresh_result(self) -> None:
        card = self.result_card()
        if card is None or self.loaded is None:
            return
        self.result_list.set_card(card)
        self._refresh_sent(card)

    def _refresh_sent(self, card: Card) -> None:
        assert self.loaded is not None
        self.sent_tree.clear()
        accounts = self.loaded.accounts
        if not accounts:
            self.sent_note.setText("This contact is not kept in any account, so nothing will be sent anywhere. "
                                   "It stays in Leasha only.")
            return
        self.sent_note.setText("After you confirm, the next sync sends each account this version. "
                               "An account only stores what it can hold, so some fields may not go.")
        for acc_id, name, address, caps in accounts:
            top = QTreeWidgetItem([f"{name}  ({address})" if address and address != name else name])
            top.setIcon(0, icon("cloud", ink()))
            top.setData(0, Qt.ItemDataRole.UserRole, acc_id)
            sent = actions.projection(card, caps)
            for glyph, text in card_rows(sent):
                child = QTreeWidgetItem([text])
                child.setIcon(0, icon(glyph, ink()))
                top.addChild(child)
            left_out = actions.unsent(card, caps)
            if left_out:
                note = QTreeWidgetItem([f"Not sent to this account: {'; '.join(left_out)}"])
                note.setIcon(0, icon("ban", ink("text_faint")))
                top.addChild(note)
            self.sent_tree.addTopLevelItem(top)
            top.setExpanded(True)

    def sent_texts(self) -> dict[int, list[str]]:
        """{account id: the lines it will be sent}, for tests and the screen reader."""
        out: dict[int, list[str]] = {}
        for i in range(self.sent_tree.topLevelItemCount()):
            top = self.sent_tree.topLevelItem(i)
            out[top.data(0, Qt.ItemDataRole.UserRole)] = [top.child(j).text(0) for j in range(top.childCount())]
        return out

    # -- writing -------------------------------------------------------------
    def confirm(self) -> None:
        if self.model is None or self.merged:
            return
        self.busy(True, "Merging…")
        self.confirm_button.setEnabled(False)
        hub, model = self.hub, self.model
        self.job(lambda: actions.commit_merge(hub, model), self._merged)

    def _merged(self, batch: str) -> None:
        self.batch = batch
        self.merged = True
        name = display_name(self.model.result()) if self.model else "the contact"
        self.busy(False, f"Merged into {name}. Changed your mind? Press Split again.")
        self.split_button.show()
        self.table.setEnabled(False)
        self.refresh_page()

    def split_again(self) -> None:
        if not self.batch:
            return
        self.busy(True, "Splitting…")
        hub, batch = self.hub, self.batch
        self.job(lambda: hub.undo_batch(batch), self._split)

    def _split(self, result: Any) -> None:
        self.batch = None
        self.merged = False
        self.split_button.hide()
        self.table.setEnabled(True)
        self.confirm_button.setEnabled(True)
        actions.forget_scan(self.hub)
        self.busy(False, f"Split again: {plural(result.undone, 'change')} undone. The contacts are as they were.")
        self.refresh_page()
