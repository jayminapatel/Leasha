r"""Browse as a table: the same timeline, one line each, in the table every list page in Leasha uses.

Layer: L5 widget. The owner (2026-10-12): "on browse the main views should be tabular or thumbnails and should be
switchable ... the view options are not there on browse ... make it consistent" with the Photos tab.

`TimelineList` is the thumbnail view (photographs as pictures, the rest as rows, under the day they are from).
This is the other way of looking at the same entries: `ResultTable` with the standard View menu - the columns,
how tightly packed, the text size - columns that fit what is in them, a click on a heading to sort, and a
Preview pane. It is fed the pages the timeline fetches, so paging, folding and the kind chosen in the picker are the
page's, not this widget's; it draws what it is given and says what was chosen.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHeaderView, QVBoxLayout, QWidget

from app.core.row_facts import format_size
from app.ui.view_options import apply_to_table, available_columns, button as view_button, weak_slot
from app.ui.widgets.preview import attach_preview
from app.ui.widgets.result_table import ResultTable
from app.ui.widgets.status_column import fill_rows

__all__ = ["BrowsePane", "BrowseRow", "COLUMNS", "PREFS_KEY", "rows_of"]

#: Where the View menu's choices for this table are remembered.
PREFS_KEY = "ui:browse_table"

#: `(key, heading, attribute, right-aligned?)` - the shape every list's COLUMNS uses.
COLUMNS: tuple[tuple[str, str, str, bool], ...] = (
    ("date", "Date", "when_text", False),
    ("name", "Name", "name", False),
    ("kind", "Kind", "kind_text", False),
    ("where", "Where", "where", False),
    ("size", "Size", "size_text", True),
    ("basis", "Date from", "basis_text", False),
)
SORT_KEYS = {"date": "when_ns", "size": "size_bytes"}


@dataclass
class BrowseRow:
    """One line of the timeline, as the table and the preview pane read it."""

    fold: Any
    path: str
    name: str
    ext: str
    folder: str
    when_ns: int
    size_bytes: int
    when_text: str
    kind_text: str
    where: str
    basis_text: str
    source_kind: str = "file"

    @property
    def size_text(self) -> str:
        return format_size(self.size_bytes) if self.size_bytes else ""


def rows_of(folds: list) -> list[BrowseRow]:
    """The timeline's entries as rows. A folded group is its newest member, with "(+N)" after the name."""
    from app.reports.timeline_words import KIND_WORDS, basis_words

    rows: list[BrowseRow] = []
    for fold in folds:
        e = fold.head
        more = len(getattr(fold, "older", ()) or ())
        name = (e.name or os.path.basename(e.path)) + (f"  (+{more:,})" if more else "")
        where = e.source_name or os.path.dirname(e.real_path or e.path)
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.when_ns / 1e9)) if e.when_ns else ""
        rows.append(BrowseRow(fold, e.real_path or e.path, name, e.ext or "", os.path.dirname(e.path), int(e.when_ns or 0),
                              int(e.size_bytes or 0), when, KIND_WORDS.get(e.kind, e.kind), where,
                              basis_words(e.basis), getattr(e, "source_kind", "file") or "file"))
    return rows


class BrowsePane(QWidget):
    """The timeline as a table. It opens and offers a menu through signals; it changes nothing."""

    opened = Signal(object)              # a TimelineEntry
    menu_requested = Signal(object, object)       # a Fold, a global point
    near_end = Signal()
    error = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._rows: list[BrowseRow] = []
        self._available: tuple = ()
        self._kept_columns: set = set()
        self.view_button: Any = None

        self.results = ResultTable(
            [h for _k, h, _a, _r in COLUMNS], ranked=True,
            aligns=["right" if right else "left" for *_rest, right in COLUMNS])
        self.results.setAccessibleName("Everything from this time, as a table")
        header = self.results.horizontalHeader()
        header.setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        # `activated`, which Qt sends for a double-click and for Enter: with `itemDoubleClicked` alone,
        # Enter on a line did nothing (2026-10-12).
        self.results.activated.connect(weak_slot(self, lambda pane, index: pane._open_row(index.row())))
        self.results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.results.customContextMenuRequested.connect(weak_slot(self, lambda pane, point: pane._menu(point)))
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(weak_slot(self, lambda pane, point: (
            pane.view_button.show_menu(pane.results.horizontalHeader().mapToGlobal(point))
            if pane.view_button is not None else None)))
        self.results.verticalScrollBar().valueChanged.connect(weak_slot(self, lambda pane, _v: pane._scrolled()))
        self.preview, self.split = attach_preview(self.results, self._open_selected, self.error.emit)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.split, 1)

    # -- what the page says -------------------------------------------------------------------------

    def bind_store(self, store: Any, into: Any) -> Any:
        """Give the View menu somewhere to remember its choices and put it in `into` (a layout). Once.
        Returns the button."""
        if self.view_button is not None:
            return self.view_button
        self.preview.store = store
        self.view_button = view_button(
            self, store, PREFS_KEY, columns=[(key, heading) for key, heading, _a, _r in COLUMNS],
            on_change=lambda _prefs: self._apply_prefs(), table=self.results)
        self.view_button.add_to(into)
        self._apply_prefs()
        return self.view_button

    def clear(self) -> None:
        self._rows = []
        self.results.setRowCount(0)

    def set_folds(self, folds: list) -> None:
        """Show these entries (all the pages so far - the table is refilled, which is cheap at a page of 200)."""
        self._rows = rows_of(folds)
        self.results.blockSignals(True)
        try:
            fill_rows(self.results, self._rows, COLUMNS, SORT_KEYS, first=self._first_cell)
        finally:
            self.results.blockSignals(False)
        self._available = available_columns(
            self._rows, [(key, attribute) for key, _h, attribute, _r in COLUMNS],
            always=("date", "name", "kind", "where"), kept=self._kept_columns)
        if self.view_button is not None:
            self.view_button.available = self._available
        self._apply_prefs()

    def row_count(self) -> int:
        return len(self._rows)

    def retint(self, colours: dict) -> None:
        from app.ui.view_options import retint_toggles

        retint_toggles(self, colours)

    def shutdown(self) -> None:
        self.preview.shutdown()

    # -- drawing --------------------------------------------------------------------------------------

    @staticmethod
    def _first_cell(item: Any, row: BrowseRow) -> None:
        item.setToolTip(row.path)

    def _apply_prefs(self) -> None:
        if self.view_button is None:
            return
        apply_to_table(self.results, self.view_button.prefs,
                       columns=[(key, heading) for key, heading, _a, _r in COLUMNS],
                       available=self._available or tuple(key for key, *_ in COLUMNS))
        self.preview.apply_preference(self.view_button.prefs, self.results.current_row())

    # -- clicks ---------------------------------------------------------------------------------------

    def _scrolled(self) -> None:
        bar = self.results.verticalScrollBar()
        if bar.maximum() and bar.value() >= bar.maximum() - max(4, bar.pageStep()):
            self.near_end.emit()

    def _open_row(self, row_no: int) -> None:
        row = self.results.row_object(row_no)
        if row is not None:
            self.opened.emit(row.fold.head)

    def _open_selected(self, *_args: Any) -> None:
        row = self.results.current_row()
        if row is not None:
            self.opened.emit(row.fold.head)

    def _menu(self, point: Any) -> None:
        item = self.results.itemAt(point)
        row = self.results.row_object(item.row()) if item is not None else None
        if row is not None:
            self.menu_requested.emit(row.fold, self.results.viewport().mapToGlobal(point))
