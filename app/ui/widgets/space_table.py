r"""The Space Report as tables you can sort and open, not a document to read.

Layer: L5

Order 202626270602 (0n) section 3a: "Table + a few plain numbers; sortable;
row -> reveals the copies with their sources." The Reports page used to show
the Space Report as rendered Markdown; this replaces that *on screen* for the
Space Report only. Export still writes the same document - the document and
these tables are two views of one `SpaceFindings`, so they cannot disagree.

**What goes in each row, and what each column sorts on, is decided in
`app/ui/presenter/space_rows.py`** (plain data, tested without a display).
This module only draws it: a headline of a few plain numbers, then one tab
per question - Duplicates and Similar photos have rows that open to show each
copy and the source it lives on, By source and The only copy are flat.

**Sorting is on the real value, not the words** - `SortableTreeItem` reads
`SORT_ROLE`, so "10 MB" sorts after "3 MB" and "35%" after "4%". A click on a
heading sorts the top-level rows; the copies inside an open row stay with it.

Nothing here touches the store: `set_findings` takes what the Reports page's
worker already read.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QHeaderView,
    QLabel,
    QTabWidget,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)

from app.ui.presenter.space_rows import SpaceRow, SpaceTable, space_headline, space_tables
from app.ui.widgets.result_table import align_headers, alignment_for
from app.ui.widgets.sortable_item import SORT_ROLE, SortableTreeItem

__all__ = ["SpaceTables"]


def _item(row: SpaceRow, aligns: tuple[str, ...]) -> SortableTreeItem:
    item = SortableTreeItem()
    for column, text in enumerate(row.cells):
        item.setText(column, text)
        item.setTextAlignment(column, alignment_for(aligns[column]))
        if row.tooltip:
            item.setToolTip(column, row.tooltip)
        value = row.sort[column] if column < len(row.sort) else None
        if value is not None:
            item.setData(column, SORT_ROLE, value)
    for child in row.children:
        item.addChild(_item(child, aligns))
    return item


def _tree(table: SpaceTable) -> QTreeWidget:
    tree = QTreeWidget()
    tree.setObjectName(f"space-{table.key}")
    tree.setAccessibleName(f"{table.title} table")
    tree.setToolTip(table.hint)
    tree.setColumnCount(len(table.headers))
    tree.setHeaderLabels(list(table.headers))
    tree.setAlternatingRowColors(True)
    tree.setUniformRowHeights(True)
    tree.setRootIsDecorated(any(row.children for row in table.rows))
    tree.setSelectionBehavior(QTreeWidget.SelectionBehavior.SelectRows)
    header = tree.header()
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    for column in range(1, len(table.headers)):
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
    header.setStretchLastSection(False)
    align_headers(tree, list(table.aligns))

    # Fill with sorting off - Qt re-sorts on every insert otherwise - then
    # turn it on with the table's opening order already chosen. `-1` keeps
    # the order the report gave (offline drives first, for "The only copy").
    for row in table.rows:
        tree.addTopLevelItem(_item(row, table.aligns))
    order = (Qt.SortOrder.DescendingOrder if table.descending
             else Qt.SortOrder.AscendingOrder)
    header.setSortIndicator(table.sort_column, order)
    tree.setSortingEnabled(True)
    return tree


class SpaceTables(QWidget):
    """A headline and one tab per question the Space Report answers."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._shown: Any = None
        self.trees: dict[str, QTreeWidget] = {}

        self.headline = QLabel("")
        self.headline.setWordWrap(True)
        self.headline.setObjectName("resultsSummary")
        self.headline.setAccessibleName("Space Report summary")

        self.tabs = QTabWidget()
        self.tabs.setAccessibleName("Space Report sections")
        self.tabs.setToolTip(
            "Each tab answers one question. Click a column heading to sort; "
            "open a row to see every copy and where it lives.")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.headline)
        layout.addWidget(self.tabs, 1)

    def set_findings(self, findings: Any) -> None:
        """Show `findings`. Building is cheap (the report names a couple of
        dozen groups), and a repeat call for the same findings is a no-op so
        switching between reports does not throw away a sort or an opened row."""
        if findings is self._shown:
            return
        self._shown = findings
        keep = self.tabs.currentIndex()
        while self.tabs.count():
            page = self.tabs.widget(0)
            self.tabs.removeTab(0)
            page.deleteLater()
        self.trees.clear()
        self.headline.setText(space_headline(findings))
        for table in space_tables(findings):
            page = QWidget()
            column = QVBoxLayout(page)
            if table.rows:
                tree = _tree(table)
                self.trees[table.key] = tree
                column.addWidget(tree)
            else:
                note = QLabel(table.empty)
                note.setWordWrap(True)
                note.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
                column.addWidget(note, 1)
            index = self.tabs.addTab(page, table.title)
            self.tabs.setTabToolTip(index, table.hint)
        if 0 <= keep < self.tabs.count():
            self.tabs.setCurrentIndex(keep)
