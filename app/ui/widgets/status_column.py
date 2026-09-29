r"""The Status column, and the fill loop the Files and Mail tables share.

Layer: L5

The owner: *"in the results it must show the single word status ... and this
should be visible in the results"*. Every results list now carries a Status
column whose cell is one word from `app.core.file_state` and whose tooltip is
that word's plain sentence. The word is computed before it gets here - by the
presenter from what the row already carries, or by the search worker from one
batched store read - so nothing on this path touches a store.

**Why the fill loop moved here.** `files_view.py` and `mail_view.py` were each
one or two lines under the 250-line guard, and both had the same loop: one
`SortableItem` per cell, a sort value for the columns that read as formatted
text, the row's id on the first cell. The Status tooltip is one more thing that
loop has to do, and doing it twice in two views at their guard is how the two
drift apart. It is one function now, with each view's first-cell rules passed
in.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, Sequence

from app.core.file_state import explain
from app.ui.widgets.sortable_item import SORT_ROLE, SortableItem

__all__ = ["STATUS_KEY", "STATUS_HEADING", "STATUS_COLUMN", "fill_rows", "status_tip"]

#: The column's key in every list's `COLUMNS` and in the View menu's
#: preferences. The same key the Code list already used for the raw store
#: status, so a Code column somebody had turned off stays off.
STATUS_KEY = "status"
STATUS_HEADING = "Status"
#: `(key, heading, attribute, right-aligned?)` - the shape every table's
#: `COLUMNS` uses. Each row type calls its word `status`.
STATUS_COLUMN: tuple[str, str, str, bool] = (STATUS_KEY, STATUS_HEADING, "status", False)


def status_tip(word: Any) -> str:
    """The Status cell's tooltip: the word's sentence, or `""` for a blank."""
    return explain(word)


def fill_rows(
    table: Any,
    rows: Sequence[Any],
    columns: Sequence[tuple[str, str, str, bool]],
    sort_keys: Mapping[str, str],
    *,
    first: Optional[Callable[[Any, Any], None]] = None,
) -> None:
    """Replace `table`'s rows with `rows`. UI thread, no I/O.

    `columns` is the list's `COLUMNS`; `sort_keys` maps a column key to the
    row attribute it sorts on when its text would sort wrongly ("10 KB" before
    "3 KB"). `first(item, row)` sets the first cell's own roles - the row's id,
    its tooltip - which differ between lists. The row objects are attached
    last, in table order, which is what `ResultTable` needs to re-enable a sort
    the person chose.
    """
    table.setRowCount(len(rows))
    for index, row in enumerate(rows):
        for column, (key, _heading, attribute, _right) in enumerate(columns):
            # The alignment comes from the column spec the table was built
            # with, which is also what points the heading the same way.
            item = SortableItem(getattr(row, attribute, ""))
            sort_by = sort_keys.get(key)
            if sort_by:
                item.setData(SORT_ROLE, getattr(row, sort_by, 0))
            if key == STATUS_KEY:
                tip = status_tip(getattr(row, attribute, ""))
                if tip:
                    item.setToolTip(tip)
            if column == 0 and first is not None:
                first(item, row)
            table.setItem(index, column, item)
    table.set_row_objects(rows)
