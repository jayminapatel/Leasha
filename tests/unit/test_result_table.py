r"""The results widget that lets the preview pane exist on the other tabs.

Layer: L5

`attach_preview` asks a results widget for two things: a `selected` signal
carrying the row object, and `current_row()` so the pane can draw what is
already highlighted the moment it is switched on. `ResultsView` on the search
tab had both, which is why the preview shipped there and nowhere else.

The property that costs the most to get wrong is the one about sorting. Mail's
table sorts on a header click, and after one visual row 3 is not `rows[3]` - a
positional lookup previews a different message from the one highlighted, which
nobody reports because they assume they misclicked.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QTableWidgetItem  # noqa: E402

from app.ui.widgets.result_table import ResultTable  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


class Row:
    def __init__(self, name):
        self.name = name

    def __repr__(self):
        return f"Row({self.name!r})"


def _filled(rows, **kwargs):
    table = ResultTable(["Name", "Size"], **kwargs)
    table.setSortingEnabled(False)
    table.setRowCount(len(rows))
    for index, row in enumerate(rows):
        table.setItem(index, 0, QTableWidgetItem(row.name))
        table.setItem(index, 1, QTableWidgetItem("1 KB"))
    table.set_row_objects(rows)
    return table


def test_a_row_remembers_the_object_it_was_built_from(qapp):
    rows = [Row("a"), Row("b")]
    table = _filled(rows)

    assert table.row_object(1) is rows[1]


def test_the_current_row_is_what_the_pane_draws_when_it_opens(qapp):
    rows = [Row("a"), Row("b")]
    table = _filled(rows)

    table.setCurrentCell(1, 0)

    assert table.current_row() is rows[1]


def test_selecting_a_row_announces_the_object(qapp):
    rows = [Row("a"), Row("b")]
    table = _filled(rows)
    seen = []
    table.selected.connect(seen.append)

    table.setCurrentCell(1, 0)

    assert seen and seen[-1] is rows[1]


def test_the_object_follows_the_row_through_a_sort(qapp):
    """**The bug a list beside the table would have.**

    Qt moves item data when it sorts. It does not move a list, so after a
    header click the pane would preview whatever happened to be at that index.
    """
    rows = [Row("b"), Row("a")]
    table = _filled(rows, sortable=True)
    table.setSortingEnabled(True)

    table.sortItems(0)                      # now "a" is on top

    assert table.item(0, 0).text() == "a"
    assert table.row_object(0) is rows[1], "the object stayed where the row was"


def test_a_selection_left_over_from_a_longer_result_set_is_not_previewed(qapp):
    """Qt keeps the current index across `setRowCount`, so a row highlighted at
    position 4 of a five-row result survives into a two-row one."""
    table = _filled([Row(str(n)) for n in range(5)])
    table.setCurrentCell(4, 0)

    shorter = [Row("x"), Row("y")]
    table.setRowCount(len(shorter))
    for index, row in enumerate(shorter):
        table.setItem(index, 0, QTableWidgetItem(row.name))
    table.set_row_objects(shorter)

    assert table.current_row() in (None, *shorter)


def test_a_fresh_result_set_announces_the_change(qapp):
    """Without this the pane keeps drawing the previous selection's file beside
    a table that no longer contains it."""
    table = _filled([Row("a")])
    seen = []
    table.selected.connect(seen.append)

    table.set_row_objects([])

    assert seen, "the pane was never told the results had been replaced"


def test_an_index_with_no_row_behind_it_is_none_rather_than_an_error(qapp):
    """This runs on the selection path. An empty table must cost a preview,
    never the window."""
    table = ResultTable(["Name"])

    assert table.row_object(0) is None
    assert table.row_object(-1) is None
    assert table.current_row() is None
