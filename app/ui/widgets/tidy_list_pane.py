r"""Every file in every Tidy set of one kind, as one list.

Layer: L5 widget. Order 1q, the owner (2026-10-11): "the tidy screen ... there has to be a list view
too which is missing, auto column width etc like the other screens, should be standard".

The set pane shows one set at a time; with a thousand sets that is a thousand clicks on Next. This is the
other way of looking: the same files, in the table every list page in Leasha uses - `ResultTable`, the
View menu (columns, density, text size), columns that fit what is in them, a click on a heading to
sort, a Preview pane, a filter box - with a tick in the first column for "this can go". It marks and
unmarks through the same `toggled` signal as the tiles and the Copies tab, so the page keeps one set
of marks; it never moves anything.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QVBoxLayout, QWidget,
)

from app.core.row_facts import format_size
from app.ui.view_options import apply_to_table, available_columns, button as view_button, weak_slot
from app.ui.widgets.preview import attach_preview
from app.ui.widgets.result_table import ResultTable
from app.ui.widgets.status_column import fill_rows

__all__ = ["TidyListPane", "TidyRow", "COLUMNS", "PREFS_KEY", "rows_of"]

#: Where the View menu's choices for this list are remembered.
PREFS_KEY = "ui:tidy_list"
_PATH = int(Qt.ItemDataRole.UserRole) + 31
_SET_WORDS = {"copies": "copies", "similar": "similar photos", "large": "large files",
              "leftovers": "leftovers"}

#: `(key, heading, attribute, right-aligned?)` - the shape every list's COLUMNS uses.
COLUMNS: tuple[tuple[str, str, str, bool], ...] = (
    ("cango", "Can go", "cango", False),
    ("name", "Name", "name", False),
    ("folder", "Folder", "folder", False),
    ("size", "Size", "size_text", True),
    ("modified", "Modified", "modified_text", True),
    ("set", "Set", "set_text", False),
    ("type", "Type", "ext", False),
)
SORT_KEYS = {"cango": "marked_n", "size": "size_bytes", "modified": "mtime_ns", "set": "set_order"}


@dataclass
class TidyRow:
    """One file of one set, as the list and the preview pane read it."""

    path: str
    name: str
    folder: str
    ext: str
    size_bytes: int
    mtime_ns: int
    set_index: int
    set_order: int
    set_text: str
    kind: str
    marked: bool = False
    source_kind: str = "file"
    cango: str = ""

    @property
    def marked_n(self) -> int:
        return 0 if self.marked else 1                 # ticked first when sorted ascending

    @property
    def size_text(self) -> str:
        return format_size(self.size_bytes)

    @property
    def modified_text(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self.mtime_ns / 1e9)) if self.mtime_ns else ""


def rows_of(sets: list, marked_of: Any) -> list[TidyRow]:
    """Every file of every set as a row: its set's place and size in the Set column."""
    rows: list[TidyRow] = []
    for index, tset in enumerate(sets):
        count = len(tset.files)
        word = _SET_WORDS.get(getattr(tset, "kind", ""), "files")
        text = f"{count:,} {word} - {format_size(tset.size_bytes)}" if count > 1 else \
            f"{word.rstrip('s')} - {format_size(tset.size_bytes)}"
        for f in tset.files:
            folder, name = os.path.split(f.path)
            rows.append(TidyRow(
                f.path, getattr(f, "name", "") or name, folder, os.path.splitext(name)[1].lstrip(".").lower(),
                int(f.size_bytes or 0), int(f.mtime_ns or 0), index, index, text, getattr(tset, "kind", ""),
                bool(marked_of(f.path))))
    return rows


class TidyListPane(QWidget):
    """The picked kind's files as one table. Marks go out as `toggled`; nothing is moved here."""

    toggled = Signal(str, bool)            # path, marked
    keep_only = Signal(str)                # keep this path, mark the rest of its set
    open_set = Signal(int)                 # show this set in Compare
    error = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store: Any = None
        self._rows: list[TidyRow] = []
        self._available: tuple = ()
        self._kept_columns: set = set()
        self._filling = False
        self.last_menu: Optional[QMenu] = None

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by name or folder")
        self.filter.setClearButtonEnabled(True)
        self.filter.setAccessibleName("Filter the list")
        self.filter.setToolTip("Show only the files whose name or folder contains these words")
        self.filter.textChanged.connect(self._apply_filter)
        self.summary = QLabel("")
        self.summary.setObjectName("resultsSummary")

        self.results = ResultTable(
            [h for _k, h, _a, _r in COLUMNS], ranked=True,
            aligns=["right" if right else "left" for *_rest, right in COLUMNS])
        self.results.setAccessibleName("Every file in these sets")
        header = self.results.horizontalHeader()
        header.setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        self.results.itemChanged.connect(self._item_changed)
        # `activated`, which Qt sends for a double-click and for Enter: with `itemDoubleClicked` alone,
        # Enter on a line did nothing (2026-10-12).
        self.results.activated.connect(weak_slot(self, lambda pane, index: pane._double_clicked(index)))
        self.results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.results.customContextMenuRequested.connect(weak_slot(self, lambda pane, point: pane._menu(point)))
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(weak_slot(self, lambda pane, point: (
            pane.view_button.show_menu(pane.results.horizontalHeader().mapToGlobal(point))
            if pane.view_button is not None else None)))

        #: Built when the page gives a store (`bind_store`): the View menu remembers its choices there.
        self.view_button: Any = None
        self.preview, self.split = attach_preview(self.results, self._open_selected, self.error.emit)
        self.top = QHBoxLayout()
        self.top.addWidget(self.filter, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.top)
        layout.addWidget(self.summary)
        layout.addWidget(self.split, 1)

    # -- what the page says --------------------------------------------------------------

    def bind_store(self, store: Any) -> None:
        """Give the View menu somewhere to remember its choices, and add it (with the Preview toggle)
        to the top row. Once."""
        if self.view_button is not None:
            return
        self._store = store
        self.preview.store = store
        self.view_button = view_button(
            self, store, PREFS_KEY, columns=[(key, heading) for key, heading, _a, _r in COLUMNS],
            on_change=self._prefs_changed, table=self.results)
        self.view_button.add_to(self.top)
        self._apply_prefs()

    def set_rows(self, sets: list, marked_of: Any) -> None:
        """Draw every file of `sets`. Ticks are the page's marks."""
        self._rows = rows_of(sets, marked_of)
        self._fill()

    def refresh_marks(self, marked_of: Any) -> None:
        """Redraw only the ticks, keeping the sort and the selection."""
        by_path = {r.path: r for r in self._rows}
        self._filling = True
        try:
            for row_no in range(self.results.rowCount()):
                item = self.results.item(row_no, 0)
                path = str(item.data(_PATH) or "") if item is not None else ""
                if path in by_path:
                    by_path[path].marked = bool(marked_of(path))
                    item.setCheckState(Qt.CheckState.Checked if by_path[path].marked else Qt.CheckState.Unchecked)
        finally:
            self._filling = False
        self._summarise()

    def retint(self, colours: dict) -> None:
        from app.ui.view_options import retint_toggles

        retint_toggles(self, colours)

    def shutdown(self) -> None:
        self.preview.shutdown()

    # -- drawing -------------------------------------------------------------------------

    def _fill(self) -> None:
        self._filling = True
        try:
            self.results.blockSignals(True)
            fill_rows(self.results, self._rows, COLUMNS, SORT_KEYS, first=self._first_cell)
            self.results.blockSignals(False)
        finally:
            self._filling = False
        self._available = available_columns(
            self._rows, [(key, attribute) for key, _h, attribute, _r in COLUMNS],
            always=("cango", "name", "folder", "size", "set"), kept=self._kept_columns)
        if self.view_button is not None:
            self.view_button.available = self._available
        self._apply_prefs()
        self._apply_filter(self.filter.text())
        self._summarise()

    @staticmethod
    def _first_cell(item: Any, row: TidyRow) -> None:
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if row.marked else Qt.CheckState.Unchecked)
        item.setData(_PATH, row.path)
        item.setToolTip(row.path)

    def _apply_prefs(self) -> None:
        if self.view_button is None:
            return
        apply_to_table(self.results, self.view_button.prefs,
                       columns=[(key, heading) for key, heading, _a, _r in COLUMNS],
                       available=self._available or tuple(key for key, *_ in COLUMNS))
        self.preview.apply_preference(self.view_button.prefs, self.results.current_row())

    def _prefs_changed(self, _prefs: Any) -> None:
        self._apply_prefs()

    def _apply_filter(self, text: str) -> None:
        words = [w for w in str(text or "").casefold().split() if w]
        shown = 0
        for row_no in range(self.results.rowCount()):
            row = self.results.row_object(row_no)
            hay = f"{getattr(row, 'name', '')} {getattr(row, 'folder', '')}".casefold()
            hide = bool(words) and not all(w in hay for w in words)
            self.results.setRowHidden(row_no, hide)
            shown += 0 if hide else 1
        self._summarise(shown if words else None)

    def _summarise(self, shown: Optional[int] = None) -> None:
        total = len(self._rows)
        marked = sum(1 for r in self._rows if r.marked)
        sets = len({r.set_index for r in self._rows})
        line = f"{total:,} files in {sets:,} sets - {marked:,} marked to go"
        if shown is not None:
            line = f"Showing {shown:,} of {total:,} files - {marked:,} marked to go"
        self.summary.setText(line if total else "Nothing to tidy here.")

    # -- clicks --------------------------------------------------------------------------

    def _item_changed(self, item: Any) -> None:
        if self._filling or item.column() != 0:
            return
        path = str(item.data(_PATH) or "")
        if path:
            on = item.checkState() == Qt.CheckState.Checked
            row = self.results.row_object(item.row())
            if row is not None:
                row.marked = on
            self.toggled.emit(path, on)

    def _double_clicked(self, item: Any) -> None:
        row = self.results.row_object(item.row())
        if row is not None and row.kind in ("copies", "similar"):
            self.keep_only.emit(row.path)
        elif row is not None:
            self.open_set.emit(row.set_index)

    def _open_selected(self, *_args: Any) -> None:
        row = self.results.current_row()
        if row is not None:
            from app.core.osbridge.launch import show_in_file_manager

            show_in_file_manager(row.path)

    def _menu(self, point: Any) -> None:
        item = self.results.itemAt(point)
        row = self.results.row_object(item.row()) if item is not None else None
        if row is None:
            return
        menu = QMenu(self)
        if row.kind in ("copies", "similar"):
            menu.addAction("Keep this one", lambda p=row.path: self.keep_only.emit(p))
        menu.addAction("Compare this set", lambda i=row.set_index: self.open_set.emit(i))
        menu.addAction("Show in folder", lambda p=row.path: self._reveal(p))
        self.last_menu = menu
        menu.popup(self.results.viewport().mapToGlobal(point))

    @staticmethod
    def _reveal(path: str) -> None:
        from app.core.osbridge.launch import show_in_file_manager

        show_in_file_manager(path)
