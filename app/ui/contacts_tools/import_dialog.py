"""Import contacts (order 1k 6i).

Layer: L5

Three steps, and nothing is added until the third is confirmed:

1. **Choose** the files: vCard (`.vcf`, versions 2.1, 3.0 and 4.0), CSV or Excel from anywhere,
   and the `.vcf` files that are already in the index (a contacts export saved from an Outlook
   archive, say), offered as a one-time import.
2. **Map the columns**, only for a CSV whose layout is not Google, Outlook, Apple or Leasha's own:
   one drop-down per column, filled from `importers.suggest_mapping`.
3. **Preview** every card the files hold. A card that matches someone already in the book by
   email or phone (`hub.find_by_email`, `hub.find_by_phone`), or that appears twice in the files,
   says so and offers *Add as new*, *Merge into* the existing contact, or *Skip* (untick it).
   Nothing is in the book until **Add these contacts**.

The commit is ONE journal batch (`actions.commit_import`) with source "import" and the file's name
as the receipt, so **Undo this import** takes the whole import back in one step. The source files are
only ever opened for reading (rule 10).
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QStackedWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from app.contacts.card import display_name
from app.ui.contacts_tools import actions
from app.ui.contacts_tools._common import ToolDialog, btn, ink, make_table, named, plural, summary_line
from app.ui.widgets.icons import icon

__all__ = ["ImportDialog", "MAPPING_TARGETS", "find_indexed_vcards"]

#: What a CSV column can be read as: (words, importers' mapping target). One list, so the
#: drop-down and the importer cannot drift apart.
MAPPING_TARGETS = (
    ("(leave this column out)", ""),
    ("Full name", "name"), ("First name", "given"), ("Last name", "family"), ("Middle name", "additional"),
    ("Title (Mr, Dr)", "prefix"), ("Suffix", "suffix"), ("Nickname", "nickname"),
    ("Company", "company"), ("Department", "department"), ("Job title", "title"), ("Notes", "notes"),
    ("Email", "email"), ("Email (work)", "email:work"), ("Email (home)", "email:home"),
    ("Phone", "phone"), ("Phone (mobile)", "phone:mobile"), ("Phone (work)", "phone:work"),
    ("Phone (home)", "phone:home"),
    ("Website", "url"), ("Birthday", "date:birthday"), ("Anniversary", "date:anniversary"),
    ("Street", "address.street"), ("City", "address.city"), ("County or state", "address.region"),
    ("Postcode", "address.postcode"), ("Country", "address.country"),
    ("Group", "group"), ("Favourite (yes / no)", "favourite"),
)

_FILTER = "Contact files (*.vcf *.csv *.xlsx *.txt);;vCard (*.vcf);;Spreadsheet (*.csv *.xlsx);;All files (*)"


def find_indexed_vcards(hub: Any, limit: int = 200) -> list[str]:
    """The `.vcf` files the index already knows that are still on disk. Reads; a worker's job."""
    try:
        rows = hub.conn.execute(
            "SELECT path FROM files WHERE lower(ext) IN ('.vcf', 'vcf') AND status != 'SKIPPED' "
            "ORDER BY path LIMIT ?", (limit * 2,)).fetchall()
    except Exception:                                          # noqa: BLE001 - no files table: nothing to offer
        return []
    return [r[0] for r in rows if os.path.isfile(r[0])][:limit]


def read_account_choices(ctx: Any) -> tuple[list[tuple[int, str]], Optional[int]]:
    """Accounts new contacts may be kept in (those that take contacts back) and the default."""
    store = getattr(ctx, "accounts", None)
    if store is None:
        return [], None
    accounts = [(a.id, a.name or a.address) for a in store.list() if a.writes_contacts]
    default = store.default_account()
    return accounts, (default.id if default is not None and default.writes_contacts else None)


class ImportDialog(ToolDialog):
    def __init__(self, ctx: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(ctx, parent, title="Import contacts", glyph="upload",
                         subtitle="Read contacts from a vCard, CSV or Excel file. You see everything, and the "
                                  "duplicates it found, before anything is added.",
                         size=(1000, 680), name="contacts-import")
        self.hub = ctx.hub
        self.rows: list[actions.ImportRow] = []
        self.result: Optional[actions.ImportResult] = None
        self.last_batch: Optional[str] = None
        self.source_label = ""
        self._pending: list[Path] = []
        self._previews: list[tuple[str, Any]] = []
        self._errors: list[str] = []
        self._mapping_path: Optional[Path] = None
        self._map_combos: dict[str, QComboBox] = {}
        self._row_combos: dict[int, QComboBox] = {}
        self.accounts: list[tuple[int, str]] = []
        self.default_account: Optional[int] = None

        self.stack = QStackedWidget()
        self.body.addWidget(self.stack, 1)
        self.stack.addWidget(self._choose_page())
        self.stack.addWidget(self._mapping_page())
        self.stack.addWidget(self._preview_page())

        self.read_button = btn("Read the files", "eye", "primary", "Read the chosen files and show what they hold. "
                               "Nothing is added yet", "Read the files")
        self.map_button = btn("Use this mapping", "check", "primary", "Read the file with these column choices",
                              "Use this mapping")
        self.add_button = btn("Add these contacts", "check", "primary",
                              "Add the ticked contacts to your book, in one step you can undo", "Add these contacts")
        self.back_button = btn("Back", "rotate-ccw", "secondary", "Go back to choosing files", "Back")
        self.undo_button = btn("Undo this import", "undo-2", "secondary", "Take back everything this import added or changed",
                               "Undo this import")
        self.read_button.clicked.connect(self.read_chosen)
        self.map_button.clicked.connect(self.apply_mapping)
        self.add_button.clicked.connect(self.commit)
        self.back_button.clicked.connect(self.go_back)
        self.undo_button.clicked.connect(self.undo)
        self.add_footer_buttons(self.read_button, self.map_button, self.add_button, self.back_button, self.undo_button)
        self._page(0)
        hub, ctx_ = self.hub, ctx
        self.job(lambda: (find_indexed_vcards(hub), read_account_choices(ctx_)), self._offered)

    # -- pages ---------------------------------------------------------------
    def _choose_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.choose_button = btn("Choose files…", "folder-open", "secondary",
                                 "Pick one or more vCard, CSV or Excel files", "Choose files")
        self.choose_button.clicked.connect(self.choose_files)
        row = QHBoxLayout()
        row.addWidget(self.choose_button)
        row.addStretch(1)
        layout.addLayout(row)
        self.file_list = QListWidget()
        named(self.file_list, "Files to import", "The files that will be read. Untick one to leave it out")
        layout.addWidget(self.file_list, 1)
        box = QGroupBox("vCard files already in your index (one-time import)")
        named(box, "Indexed vCard files", "Contacts files Leasha has already found while indexing")
        bl = QVBoxLayout(box)
        self.indexed_list = QListWidget()
        named(self.indexed_list, "vCard files in your index", "Tick any you would like to import once")
        bl.addWidget(self.indexed_list)
        layout.addWidget(box, 1)
        return page

    def _mapping_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.map_intro = QLabel("")
        self.map_intro.setWordWrap(True)
        named(self.map_intro, "Mapping help", "Explains the column step")
        layout.addWidget(self.map_intro)
        self.map_table = make_table(self, ["Column in your file", "Becomes"], "Column mapping",
                                    "Choose what each column of the file holds", stretch=1)
        layout.addWidget(self.map_table, 1)
        return page

    def _preview_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        named(self.summary, "Import summary", "How many contacts will be added, merged or skipped")
        layout.addWidget(self.summary)
        options = QHBoxLayout()
        self.only_decisions = QCheckBox("Show only contacts that need a decision")
        named(self.only_decisions, "Only decisions", "Hide contacts that are new and have nothing to check")
        self.only_decisions.toggled.connect(self._filter_rows)
        keep_label = QLabel("Keep new contacts in:")
        named(keep_label, "Keep in label", "Which account new contacts are saved to")
        self.keep_combo = QComboBox()
        self.keep_combo.addItem("Leasha only", [])
        named(self.keep_combo, "Keep new contacts in", "New contacts are sent to this account at the next sync. "
                                                       "Leasha only keeps them on this computer")
        options.addWidget(self.only_decisions)
        options.addStretch(1)
        options.addWidget(keep_label)
        options.addWidget(self.keep_combo)
        layout.addLayout(options)
        self.table = make_table(self, ["Import", "Row", "Contact", "What to do", "Why", "Check"],
                                "Contacts to import", "Every contact in the files, with the duplicates found", stretch=2)
        self.table.itemChanged.connect(lambda _i: self._update_summary())
        layout.addWidget(self.table, 1)
        return page

    def _page(self, index: int) -> None:
        self.stack.setCurrentIndex(index)
        self.read_button.setVisible(index == 0)
        self.map_button.setVisible(index == 1)
        self.add_button.setVisible(index == 2)
        self.back_button.setVisible(index in (1, 2))
        self.undo_button.setVisible(False)

    def go_back(self) -> None:
        self._page(0)
        self.add_button.setEnabled(True)
        self.say("")

    # -- choosing ------------------------------------------------------------
    def _offered(self, payload: Any) -> None:
        found, (accounts, default) = payload
        for path in found:
            item = QListWidgetItem(path)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.indexed_list.addItem(item)
        self.accounts, self.default_account = accounts, default
        for acc_id, name in accounts:
            self.keep_combo.addItem(name, [acc_id])
            if acc_id == default:
                self.keep_combo.setCurrentIndex(self.keep_combo.count() - 1)

    def choose_files(self) -> None:
        paths, _f = QFileDialog.getOpenFileNames(self, "Choose contacts files", "", _FILTER)
        self.add_files(paths)

    def add_files(self, paths: list[str]) -> None:
        have = {self.file_list.item(i).text() for i in range(self.file_list.count())}
        for path in paths:
            if path and path not in have:
                item = QListWidgetItem(path)
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Checked)
                self.file_list.addItem(item)

    def chosen(self) -> list[Path]:
        out = []
        for widget in (self.file_list, self.indexed_list):
            for i in range(widget.count()):
                item = widget.item(i)
                if item.checkState() == Qt.CheckState.Checked:
                    out.append(Path(item.text()))
        return out

    # -- reading -------------------------------------------------------------
    def read_chosen(self) -> None:
        self.load_files(self.chosen())

    def load_files(self, paths: list) -> None:
        """Read the files (a worker each), ask for a mapping where a CSV needs one, then preview."""
        if not paths:
            self.say("Choose at least one file first.")
            return
        self._pending = [Path(p) for p in paths]
        self._previews, self._errors = [], []
        self.read_button.setEnabled(False)
        self._next_file()

    def _next_file(self, mapping: Optional[dict] = None, path: Optional[Path] = None) -> None:
        if path is None:
            if not self._pending:
                self._build_preview()
                return
            path = self._pending.pop(0)
        from app.contacts import importers                       # noqa: PLC0415

        self.busy(True, f"Reading {path.name}…")
        self.job(lambda: importers.import_file(path, mapping),
                 lambda preview, p=path, m=mapping: self._read(p, preview, m),
                 lambda err, p=path: self._read_failed(p, err))

    def _read_failed(self, path: Path, error: Any) -> None:
        self._errors.append(f"{path.name}: {getattr(error, 'message', error)}")
        self._next_file()

    def _read(self, path: Path, preview: Any, mapping: Optional[dict]) -> None:
        if preview.needs_mapping and mapping is None:
            self._show_mapping(path, preview)
            return
        if preview.needs_mapping:
            self._errors.append(f"{path.name}: the columns could not be read with that mapping")
        else:
            self._previews.append((path.name, preview))
        self._next_file()

    # -- mapping -------------------------------------------------------------
    def _show_mapping(self, path: Path, preview: Any) -> None:
        self.busy(False)
        self._mapping_path = path
        self.map_intro.setText(f"Leasha does not recognise the columns of {path.name}. Say what each one holds; "
                               "columns left out are ignored. Nothing is added yet.")
        guess = dict(preview.mapping or {})
        self.map_table.setRowCount(len(preview.headers))
        self._map_combos = {}
        for r, header in enumerate(preview.headers):
            self.map_table.setItem(r, 0, QTableWidgetItem(header))
            combo = QComboBox()
            for words, value in MAPPING_TARGETS:
                combo.addItem(words, value)
            want = guess.get(header, "")
            combo.addItem(f"Other (kept as '{header}')", f"custom:{header}")
            if want and combo.findData(want) < 0:
                combo.addItem(want, want)
            combo.setCurrentIndex(max(0, combo.findData(want)))
            named(combo, f"Column {header}", f"What the column '{header}' holds")
            self.map_table.setCellWidget(r, 1, combo)
            self._map_combos[header] = combo
        self.map_table.resizeColumnToContents(0)
        self._page(1)
        self.read_button.setEnabled(True)
        self.say("")

    def current_mapping(self) -> dict[str, str]:
        return {h: (c.currentData() or "") for h, c in self._map_combos.items()}

    def apply_mapping(self) -> None:
        if self._mapping_path is None:
            return
        path, mapping = self._mapping_path, self.current_mapping()
        self._next_file(mapping, path)

    # -- the preview ---------------------------------------------------------
    def _build_preview(self) -> None:
        if not self._previews:
            self.busy(False, "; ".join(self._errors) or "Those files hold no contacts.")
            self.read_button.setEnabled(True)
            self._page(0)
            return
        contacts, sources = [], []
        for name, preview in self._previews:
            for item in preview.contacts:
                contacts.append(item)
                sources.append(name)
        if not contacts:
            self.busy(False, "Those files hold no contacts.")
            self.read_button.setEnabled(True)
            self._page(0)
            return
        combined = SimpleNamespace(contacts=contacts)
        hub = self.hub
        self.busy(True, "Looking for people who are already in your book…")

        def build() -> list:
            rows = actions.build_import_rows(hub, combined)
            for row, name in zip(rows, sources):
                row.source = name
            return rows

        self.job(build, self._rows_ready)

    def _rows_ready(self, rows: list) -> None:
        self.rows = rows
        self.source_label = ", ".join(dict.fromkeys(name for name, _p in self._previews))
        skipped = sum(len(p.skipped_rows) for _n, p in self._previews)
        note = []
        if skipped:
            note.append(f"{plural(skipped, 'empty row')} left out")
        if self._errors:
            note.append("could not read " + "; ".join(self._errors))
        self.busy(False, f"Read {plural(len(rows), 'contact')} from {self.source_label}."
                  + (f" ({'; '.join(note)})" if note else ""))
        self._fill_table()
        self._page(2)
        self.read_button.setEnabled(True)

    def _fill_table(self) -> None:
        table = self.table
        table.blockSignals(True)
        table.setRowCount(len(self.rows))
        self._row_combos = {}
        for r, row in enumerate(self.rows):
            check = QTableWidgetItem("")
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            check.setCheckState(Qt.CheckState.Checked if row.choice != "skip" else Qt.CheckState.Unchecked)
            check.setToolTip("Untick to skip this contact")
            table.setItem(r, 0, check)
            table.setItem(r, 1, QTableWidgetItem(str(row.source_row)))
            table.setItem(r, 2, QTableWidgetItem(summary_line(row.card) or "(empty)"))
            if row.matches or row.same_as is not None:
                combo = QComboBox()
                combo.addItem("Add as new", ("new", None))
                for cid, name in zip(row.matches, row.match_names):
                    combo.addItem(f"Merge into existing: {name}", ("merge", cid))
                if row.same_as is not None:
                    combo.addItem(f"Merge into the earlier copy (row {self.rows[row.same_as].source_row})",
                                  ("merge", None))
                want = ("merge", row.target) if row.choice == "merge" else ("new", None)
                combo.setCurrentIndex(max(0, combo.findData(want)))
                named(combo, f"What to do with row {row.source_row}", "Add as a new contact, or merge into the one it matches")
                combo.currentIndexChanged.connect(lambda _i: self._update_summary())
                table.setCellWidget(r, 3, combo)
                self._row_combos[r] = combo
            else:
                table.setItem(r, 3, QTableWidgetItem("Add as new"))
            table.setItem(r, 4, QTableWidgetItem("; ".join(row.reasons)))
            table.setItem(r, 5, QTableWidgetItem("; ".join(row.warnings)))
        table.blockSignals(False)
        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(True)
        self._filter_rows()
        self._update_summary()

    def _filter_rows(self) -> None:
        only = self.only_decisions.isChecked()
        for r, row in enumerate(self.rows):
            self.table.setRowHidden(r, only and not (row.matches or row.same_as is not None or row.warnings))

    def collect_rows(self) -> list[actions.ImportRow]:
        """The rows with the person's choices read back from the table."""
        for r, row in enumerate(self.rows):
            item = self.table.item(r, 0)
            if item is None or item.checkState() != Qt.CheckState.Checked:
                row.choice, row.target = "skip", None
                continue
            combo = self._row_combos.get(r)
            choice, target = combo.currentData() if combo is not None else ("new", None)
            row.choice, row.target = choice, target
        return self.rows

    def set_choice(self, position: int, choice: str, target: Optional[int] = None) -> None:
        """Set one row's action as the table would: 'new', 'merge' (into `target`) or 'skip'."""
        item = self.table.item(position, 0)
        if choice == "skip":
            item.setCheckState(Qt.CheckState.Unchecked)
            return
        item.setCheckState(Qt.CheckState.Checked)
        combo = self._row_combos.get(position)
        if combo is not None:
            combo.setCurrentIndex(max(0, combo.findData((choice, target))))

    def _update_summary(self) -> None:
        rows = self.collect_rows()
        new = sum(1 for r in rows if r.choice == "new")
        merge = sum(1 for r in rows if r.choice == "merge")
        skip = sum(1 for r in rows if r.choice == "skip")
        self.summary.setText(f"{plural(new, 'new contact')} will be added, {plural(merge, 'contact')} merged into "
                             f"existing ones, {plural(skip, 'contact')} skipped. Nothing is added until you press "
                             "Add these contacts.")
        self.add_button.setEnabled(bool(new or merge) and self.result is None)

    # -- commit and undo -----------------------------------------------------
    def commit(self) -> None:
        rows = self.collect_rows()
        if not any(r.choice != "skip" for r in rows):
            return
        self.busy(True, "Adding…")
        self.add_button.setEnabled(False)
        hub, name = self.hub, self.source_label
        kept = list(self.keep_combo.currentData() or [])
        self.job(lambda: actions.commit_import(hub, rows, name, kept_in=kept), self._committed)

    def _committed(self, result: actions.ImportResult) -> None:
        self.result = result
        self.last_batch = result.batch_id
        self.undo_button.setVisible(bool(result.batch_id))
        self.busy(False, f"Added {plural(result.added, 'contact')} and merged {plural(result.merged, 'contact')} into "
                  f"existing ones. Skipped {result.skipped}. Undo this import takes all of it back.")
        self.refresh_page()

    def undo(self) -> None:
        batch = self.last_batch
        if not batch:
            return
        self.busy(True, "Taking the import back…")
        hub = self.hub
        self.job(lambda: (hub.undo_batch(batch), actions.forget_scan(hub))[0], self._undone)

    def _undone(self, result: Any) -> None:
        self.last_batch = None
        self.result = None
        self.undo_button.setVisible(False)
        self.busy(False, f"Import undone: {plural(result.undone, 'change')} put back.")
        self.add_button.setEnabled(True)
        self.refresh_page()
