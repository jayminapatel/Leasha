r"""A table cell that sorts on its value rather than on its text.

Layer: L5

**Storing the real value is not enough on its own**, and that is the whole
reason this exists. `QTableWidgetItem.__lt__` compares `DisplayRole` and nothing
else, so a size column sorted "10 KB" before "3 KB" and a date column sorted
alphabetically - the exact failures the code storing `sent_at` and `size_bytes`
was written to prevent. The values were set, correct, and unread for the life of
the table, with a comment above them explaining the bug they were not fixing.

Any column can opt in by setting `SORT_ROLE` on its item; columns that do not
sort exactly as they did before.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QTableWidgetItem, QTreeWidgetItem

__all__ = ["SORT_ROLE", "SortableItem", "SortableTreeItem"]

#: The role holding the value a column should actually sort on.
SORT_ROLE = Qt.ItemDataRole.UserRole + 1

#: What a column may sort on. **Anything else is ignored**, and that is a fix
#: rather than a nicety: `ResultTable` once stored the whole row object at this
#: number, so Mail's "From" column sorted by comparing `MailRow` instances.
#: Roles are integers picked in different files, and a collision cannot be
#: prevented by care alone - so the comparison refuses what it cannot order and
#: falls back to the text, which is what the column showed anyway.
_ORDERABLE = (int, float, str, bool)


def _sortable(value: object) -> object:
    """`value` if a column can be ordered by it, else None."""
    return value if isinstance(value, _ORDERABLE) else None


class SortableItem(QTableWidgetItem):
    """A cell that sorts on `SORT_ROLE` when it has one."""

    def __lt__(self, other: QTableWidgetItem) -> bool:      # noqa: D105 - Qt's hook
        mine = _sortable(self.data(SORT_ROLE))
        theirs = _sortable(
            other.data(SORT_ROLE) if isinstance(other, QTableWidgetItem) else None)
        if mine is None or theirs is None:
            # A column that stores a sort value for some rows and not others
            # still orders sensibly instead of raising in the middle of a sort
            # somebody just clicked.
            return super().__lt__(other)
        try:
            return bool(mine < theirs)
        except TypeError:
            # Mixed types in one column - a missing date beside a real one.
            # Not worth an exception inside a sort.
            return str(mine) < str(theirs)


class SortableTreeItem(QTreeWidgetItem):
    """The same rule for a tree row. Qt gives the two no common base.

    `QTreeWidgetItem` and `QTableWidgetItem` are unrelated classes with the same
    `__lt__` flaw, so the Code tree would sort "9 KB" above "10 KB" exactly as
    the tables did before `SortableItem`. The comparison reads the sort column
    Qt is currently sorting by, which is the one difference: a tree item holds
    every column, where a table item is a single cell.
    """

    def __lt__(self, other: object) -> bool:                 # noqa: D105 - Qt's hook
        tree = self.treeWidget()
        column = tree.sortColumn() if tree is not None else 0
        mine = _sortable(self.data(column, SORT_ROLE))
        theirs = _sortable(other.data(column, SORT_ROLE)
                           if isinstance(other, QTreeWidgetItem) else None)
        if mine is None or theirs is None:
            return super().__lt__(other)
        try:
            return bool(mine < theirs)
        except TypeError:
            return str(mine) < str(theirs)
