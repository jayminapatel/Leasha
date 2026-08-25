r"""A results table that knows which object each row came from.

Layer: L5

**Written because the preview pane could not be attached to anything else.**
`attach_preview` needs two things from a results widget: a `selected` signal
carrying the row object, and `current_row()` so the pane can draw what is
already highlighted the moment it is switched on. `ResultsView` on the search
tab has both. Files, Mail and Code used a bare `QTableWidget`, which has neither
- it knows about cells and strings, and the row object with the path on it was
thrown away as soon as the text had been read out of it.

So the preview shipped on one tab out of four. The owner's instruction is
explicit and is a standing rule, not a request about this feature: *"preview
pane should be in every search type"*, and *"any feature added which helps the
other search areas has to be applied to others for consistency"*.

**It also absorbs the table boilerplate**, which was copied between views and
had already drifted - one hid its vertical header before setting the labels and
one after, one enabled sorting and one did not, for reasons that were real but
were nowhere written down. Both reasons now live here, next to the switch that
selects between them.

Filling stays in the views. What a Mail row looks like is not something this
should know, and the alternative was a widget taking a formatter, a sort-key
extractor and a tooltip callback - a configuration language for one table.
`set_row_objects` is the whole contract: fill the cells however you like, then
say which object each row came from.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QAbstractItemView, QTableWidget, QWidget

__all__ = ["ResultTable", "ROLE_ROW"]

#: Where the row object is kept. **On the item, not in a list beside the table.**
#: Mail's table is sortable, and after a header click visual row 3 is not
#: `rows[3]` - a positional list would preview a different message from the one
#: highlighted, which is the kind of bug nobody reports because they assume they
#: misclicked. Qt moves item data when it sorts; it does not move a list.
#:
#: **`+ 100`, and the number matters.** `UserRole` itself is taken - both tables
#: keep `file_id` there - and `UserRole + 1` is `sortable_item.SORT_ROLE`, which
#: is where `SortableItem.__lt__` looks for the value a column sorts on. Landing
#: on it meant Mail's "From" column sorted by comparing whole row *objects*:
#: reported as *"on the mail results when i clicked on top of a column to sort
#: it it crashed"*.
#:
#: Two roles that must not be equal, defined in two files, is exactly the sort
#: of thing that goes wrong silently - so `test_result_table.py` asserts they
#: differ, and `SortableItem` now ignores a sort value it cannot compare.
ROLE_ROW = Qt.ItemDataRole.UserRole + 100


class ResultTable(QTableWidget):
    """A table whose rows remember the objects they were built from."""

    #: The row under the cursor changed. The preview pane listens; nothing else
    #: does, and this widget does not know the preview exists.
    selected = pyqtSignal(object)

    def __init__(self, headings: Sequence[str], *, sortable: bool = False,
                 alternating: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(0, len(headings), parent)
        self.setHorizontalHeaderLabels(list(headings))
        self.verticalHeader().hide()
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # **Sorting is per table and is a real decision.** Mail rows have no
        # rank to destroy and "biggest attachment" is a question a header click
        # answers for free; search and filename results are ordered by match
        # quality, and a click that reorders them silently throws that away.
        self.setSortingEnabled(sortable)
        self.setAlternatingRowColors(alternating)

        self.currentCellChanged.connect(
            lambda row, _c, _pr, _pc: self.selected.emit(self.row_object(row)))

    # -- the contract the preview pane needs ---------------------------------

    def set_row_objects(self, rows: Sequence[Any]) -> None:
        """Say which object each table row was built from.

        Call it after filling and **before** re-enabling sorting: the data is
        attached in table order, and a sort that happens first has already moved
        the cells out from under it.
        """
        for index, row in enumerate(rows):
            item = self.item(index, 0)
            if item is not None:
                item.setData(ROLE_ROW, row)

        # A fresh result set with a row still highlighted from the last one
        # would otherwise preview whatever is now at that index. Qt keeps the
        # current index across a `setRowCount`, so this is not hypothetical.
        self.selected.emit(self.row_object(self.currentRow()))

    def row_object(self, row: int) -> Any:
        """The object behind a row index, or None.

        Never raises: this is on the selection path, and an index left over from
        a longer result set must cost a preview, never the window.
        """
        item = self.item(row, 0) if row >= 0 else None
        return item.data(ROLE_ROW) if item is not None else None

    def current_row(self) -> Any:
        """The highlighted row's object, or None.

        Exists so the pane can draw what is *already* selected when it is
        switched on. Without it the pane opens empty beside a highlighted row,
        which reads as a broken preview rather than as nothing having been
        selected since it appeared.
        """
        return self.row_object(self.currentRow())
