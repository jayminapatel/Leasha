r"""Every table sorts, and every heading sits over its column.

Layer: L5. The tables order, 2026-08-28.

**The owner's report, verbatim intent:** *Mail sorts on header click; this
should be global on all lists no matter where — and headers are centre-aligned
while their columns are not.*

Two rules carry this file.

**Sorting must not throw the ranking away.** `files_view` refused a header
click for exactly that reason and was right to: a search result list is
ordered by match quality, and an irrecoverable reorder is worse than no
reorder. So the ranking is *kept* — in a hidden column — and a third click on
the same header puts it back. The refusal was correct; what was missing was a
way back.

**A sort must never move what an action acts on.** After a click, visual row 3
is not `rows[3]`. Mail learned this the hard way and worked by id; the three
tables that just became sortable have to be proved to do the same, which is
what half of these tests do.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt                                     # noqa: E402
from PyQt6.QtWidgets import QApplication, QTableWidgetItem      # noqa: E402

from app.ui.widgets.result_table import (                       # noqa: E402
    RELEVANCE, ROLE_RANK, ROLE_ROW, ResultTable, align_headers, alignment_for,
)
from app.ui.widgets.sortable_item import SORT_ROLE, SortableItem  # noqa: E402

_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
_LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


class Row:
    """A row whose *display* order and *real* order disagree — the trap."""

    def __init__(self, name, size_text, size_bytes, when_text, when_ns):
        self.name = name
        self.size = size_text
        self.size_bytes = size_bytes
        self.when = when_text
        self.when_ns = when_ns

    def __repr__(self):
        return f"Row({self.name!r})"


#: **The classic trap, built in on purpose.** Sorted as text, "10 KB" comes
#: before "3 KB" and "3 weeks ago" before "yesterday" — both backwards. A
#: fixture whose display and chronological orders agreed would pass with the
#: sort roles deleted, which is the failure this file exists to prevent.
ROWS = [
    Row("beta", "3 KB", 3_000, "yesterday", 900),
    Row("alpha", "10 KB", 10_000, "3 weeks ago", 100),
    Row("gamma", "1.2 MB", 1_200_000, "just now", 999),
]


def _table(**kwargs):
    table = ResultTable(["Name", "Size", "When"],
                        aligns=["left", "right", "right"], **kwargs)
    _fill(table, ROWS)
    return table


def _fill(table, rows):
    table.setRowCount(len(rows))
    for index, row in enumerate(rows):
        table.setItem(index, 0, SortableItem(row.name))
        size = SortableItem(row.size)
        size.setData(SORT_ROLE, row.size_bytes)
        table.setItem(index, 1, size)
        when = SortableItem(row.when)
        when.setData(SORT_ROLE, row.when_ns)
        table.setItem(index, 2, when)
    table.set_row_objects(rows)
    return table


def _names(table):
    return [table.item(row, 0).text() for row in range(table.rowCount())]


def _click(table, section):
    """A header click, the way Qt delivers one: it sorts, then it tells us."""
    header = table.horizontalHeader()
    order = (Qt.SortOrder.DescendingOrder
             if (header.sortIndicatorSection() == section
                 and header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder)
             else Qt.SortOrder.AscendingOrder)
    table.sortItems(section, order)
    header.setSortIndicator(section, order)
    header.sectionClicked.emit(section)


# ---------------------------------------------------------------------------
# Sorting by what a column means, not by what it says
# ---------------------------------------------------------------------------

def test_a_size_column_sorts_by_bytes_and_not_by_its_text(qapp):
    """3 KB, 10 KB, 1.2 MB. As text that order is 1.2 MB, 10 KB, 3 KB."""
    table = _table()
    _click(table, 1)
    assert _names(table) == ["beta", "alpha", "gamma"]


def test_a_date_column_sorts_by_the_real_date(qapp):
    """"3 weeks ago", "yesterday", "just now" - alphabetically that is exactly
    backwards, which is why a fixture that agrees with itself proves nothing."""
    table = _table()
    _click(table, 2)
    assert _names(table) == ["alpha", "beta", "gamma"]


def test_a_second_click_reverses_it(qapp):
    table = _table()
    _click(table, 1)
    _click(table, 1)
    assert _names(table) == ["gamma", "alpha", "beta"]


def test_a_text_column_still_sorts_as_text(qapp):
    table = _table()
    _click(table, 0)
    assert _names(table) == ["alpha", "beta", "gamma"]


def test_sorting_is_on_by_default_now(qapp):
    """The reversal this order is about: a table used to have to ask."""
    assert ResultTable(["A"]).isSortingEnabled()


# ---------------------------------------------------------------------------
# §1b — the ranked-view reconciliation
# ---------------------------------------------------------------------------

def test_a_ranked_table_starts_in_the_order_the_engine_gave_it(qapp):
    table = _table(ranked=True)
    assert _names(table) == ["beta", "alpha", "gamma"]
    assert table.sort_order == RELEVANCE


def test_a_third_click_returns_a_ranked_list_to_relevance(qapp):
    r"""**The whole of §1b.** `files_view` refused sorting because a click
    discarded the ranking irrecoverably. It is recoverable now, so the
    refusal is not needed - and the order it comes back to is the engine's,
    not something that merely looks like it.
    """
    table = _table(ranked=True)
    original = _names(table)

    _click(table, 0)                                  # ascending by name
    assert _names(table) == ["alpha", "beta", "gamma"]
    _click(table, 0)                                  # descending
    assert _names(table) == ["gamma", "beta", "alpha"]
    _click(table, 0)                                  # back to relevance

    assert _names(table) == original
    assert table.sort_order == RELEVANCE


def test_the_cycle_works_when_qt_drives_it_rather_than_the_helper(qapp):
    r"""**The outcome, not the mechanism.** `_click` above imitates what Qt
    does on a header click, and an imitation that drifts would let every test
    in this section pass over a feature nobody can actually use. This one
    puts the pointer on the header and presses it, three times.

    This project has shipped a mechanism that worked beside an outcome that
    did not - twice - and both times the test asserted the mechanism.
    """
    from PyQt6.QtCore import QPoint
    from PyQt6.QtTest import QTest

    table = _table(ranked=True)
    table.resize(400, 200)
    table.show()
    header = table.horizontalHeader()
    original = _names(table)

    def press(section):
        x = header.sectionPosition(section) + header.sectionSize(section) // 2
        QTest.mouseClick(header.viewport(), Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier,
                         QPoint(x, header.height() // 2))
        qapp.processEvents()

    press(0)
    assert _names(table) == ["alpha", "beta", "gamma"]
    press(0)
    assert _names(table) == ["gamma", "beta", "alpha"]
    press(0)
    assert _names(table) == original
    table.hide()


def test_the_relevance_column_is_never_shown(qapp):
    """It is machinery. A blank column at the end of every results table would
    be the feature announcing its own implementation."""
    table = _table(ranked=True)
    assert table.isColumnHidden(table.columnCount() - 1)


def test_an_unranked_table_has_no_relevance_to_return_to(qapp):
    """Mail has no rank to destroy - its own comment says so - so a third
    click there is just a third click."""
    table = _table()
    assert not table.ranked
    for _ in range(3):
        _click(table, 0)
    assert table.sort_order != RELEVANCE


def test_the_header_says_what_the_third_click_does(qapp):
    r"""**An interaction nobody could guess needs saying out loud.** Sorting
    and reversing are learned behaviour everywhere; a third click that undoes
    both is not, and a cycle nobody knows about is a cycle nobody uses."""
    from app.ui.widgets.result_table import RELEVANCE_HINT

    hint = _table(ranked=True).horizontalHeader().toolTip()
    assert hint == RELEVANCE_HINT
    assert "third" in hint.lower()
    for jargon in ("relevance", "rank", "_", "()"):
        assert jargon not in hint.lower(), f"the hint says {jargon!r} to somebody"


def test_the_three_roles_are_three_different_numbers(qapp):
    r"""Roles are integers chosen in different files. `ROLE_ROW` once landed
    on `SORT_ROLE` and Mail's From column sorted by comparing whole row
    objects - reported as *"it crashed"*. A third role is a third chance."""
    assert len({ROLE_ROW, ROLE_RANK, SORT_ROLE}) == 3


# ---------------------------------------------------------------------------
# §1c — acting by id, not by position
# ---------------------------------------------------------------------------

def test_the_row_object_follows_its_row_through_a_sort(qapp):
    r"""**The bug class sorting introduces, closed at the door.** After a
    header click, visual row 3 is not `rows[3]`; a table that answered
    positionally would open the wrong file, and nobody reports that because
    they assume they misclicked."""
    table = _table(ranked=True)
    _click(table, 0)

    for index in range(table.rowCount()):
        name = table.item(index, 0).text()
        assert table.row_object(index).name == name


def test_the_current_row_is_the_highlighted_one_after_a_sort(qapp):
    table = _table(ranked=True)
    table.setCurrentCell(0, 0)
    wanted = table.current_row()
    _click(table, 0)

    # The same object, wherever the sort put it.
    moved = [table.row_object(row) for row in range(table.rowCount())]
    assert wanted in moved
    table.setCurrentCell(moved.index(wanted), 0)
    assert table.current_row() is wanted


@pytest.mark.parametrize("module_name", [
    "app.ui.files_view", "app.ui.widgets.code_results", "app.ui.mail_view",
])
def test_no_newly_sortable_view_reads_a_row_out_of_a_python_list(module_name):
    r"""The audit §1c asks for, as a guard rather than as a one-off reading.

    `self._rows[row]` is the shape of the defect: a list index that was a
    table row before somebody clicked a header. All three of these answer
    through `ROLE_ROW` or by `file_id`, and a future edit that reintroduces
    the pattern should have to argue with a test.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    source = (root / module_name.replace(".", "/")).with_suffix(".py").read_text(
        encoding="utf-8")
    for pattern in ("_rows[row]", "_rows[index]", "rows[self.results.currentRow()]"):
        assert pattern not in source, f"{module_name} indexes rows by position"


# ---------------------------------------------------------------------------
# §1d — a refresh does not throw the sort away
# ---------------------------------------------------------------------------

def test_repopulating_keeps_the_sort_somebody_chose(qapp):
    r"""**The mid-typing case.** Files and Code refresh per keystroke; a
    refresh that silently returned the list to relevance would reorder it
    under the pointer of somebody who had just sorted it."""
    table = _table(ranked=True)
    _click(table, 0)
    assert _names(table) == ["alpha", "beta", "gamma"]

    _fill(table, ROWS)                                # a new result set
    assert _names(table) == ["alpha", "beta", "gamma"]


def test_repopulating_a_relevance_ordered_table_stays_in_relevance(qapp):
    table = _table(ranked=True)
    _fill(table, list(reversed(ROWS)))
    assert _names(table) == ["gamma", "alpha", "beta"]


def test_sorting_is_off_while_the_rows_go_in(qapp):
    r"""Not tidiness: with sorting on, Qt re-sorts after **every** `setItem`,
    which is `O(n log n)` per cell and turns five hundred rows into a visible
    freeze. `mail_view` had bracketed it by hand and three other tables were
    about to be made sortable without inheriting the lesson - so it moved into
    `setRowCount`, where no view can forget it.
    """
    table = _table(ranked=True)
    table.setRowCount(2)
    assert not table.isSortingEnabled(), "still sorting while being filled"
    table.set_row_objects([])
    assert table.isSortingEnabled(), "sorting never came back on"


def test_a_table_that_was_told_not_to_sort_is_not_switched_on_by_a_refresh(qapp):
    """`setRowCount` remembers what it turned off, rather than assuming."""
    table = ResultTable(["A"], sortable=False)
    table.setRowCount(1)
    table.setItem(0, 0, QTableWidgetItem("x"))
    table.set_row_objects(["x"])
    assert not table.isSortingEnabled()


# ---------------------------------------------------------------------------
# §2 — the heading sits over its column
# ---------------------------------------------------------------------------

def test_each_heading_is_aligned_like_its_column(qapp):
    table = _table()
    for column, wanted in enumerate([_LEFT, _RIGHT, _RIGHT]):
        assert table.horizontalHeaderItem(column).textAlignment() == wanted
        assert table.item(0, column).textAlignment() == wanted


def test_a_view_declares_alignment_once_instead_of_per_cell(qapp):
    r"""The cells are aligned by the table, from the spec, so no population
    loop has to remember - which is how `code_results` came to declare its
    Size column right-aligned and render it left for the life of the table.
    """
    table = ResultTable(["A", "B"], aligns=["left", "right"])
    table.setRowCount(1)
    table.setItem(0, 1, QTableWidgetItem("42"))       # no flag set by hand
    assert table.item(0, 1).textAlignment() == _RIGHT


def test_a_cell_that_asked_for_an_alignment_keeps_it(qapp):
    """So a view can be migrated one column at a time without the screen
    changing under it."""
    table = ResultTable(["A", "B"], aligns=["left", "right"])
    table.setRowCount(1)
    item = QTableWidgetItem("42")
    item.setTextAlignment(_LEFT)
    table.setItem(0, 1, item)
    assert table.item(0, 1).textAlignment() == _LEFT


def test_a_plain_table_can_be_corrected_too(qapp):
    """§2b: the tables that are not `ResultTable` get the same treatment."""
    from PyQt6.QtWidgets import QTableWidget

    plain = QTableWidget(0, 2)
    plain.setHorizontalHeaderLabels(["One", "Two"])
    align_headers(plain)
    assert plain.horizontalHeaderItem(0).textAlignment() == _LEFT


def test_a_tree_can_be_corrected_too(qapp):
    from PyQt6.QtWidgets import QTreeWidget

    tree = QTreeWidget()
    tree.setColumnCount(2)
    tree.setHeaderLabels(["Folder", "How"])
    align_headers(tree)
    assert tree.headerItem().textAlignment(0) == _LEFT


def test_an_unknown_alignment_word_costs_the_column_and_not_the_view(qapp):
    assert alignment_for("sideways") == _LEFT
    assert alignment_for(None) == _LEFT


def test_align_headers_never_raises_on_something_that_is_not_a_table(qapp):
    """It decides where text sits. Nothing about it is worth a traceback."""
    align_headers(object())
    align_headers(None)


# ---------------------------------------------------------------------------
# Every table in the application, walked
# ---------------------------------------------------------------------------

def test_no_heading_anywhere_is_centred_over_a_column_that_is_not(qapp):
    r"""**The suffix-consistency test shape, applied to alignment**, so a
    column added next year cannot quietly regress it. Qt's default is a
    *centred* heading over *left-aligned* cells, which is the mismatch the
    owner reported - and it was in every table in the application except
    none.
    """
    from app.ui.files_view import COLUMNS as FILES
    from app.ui.mail_view import COLUMNS as MAIL
    from app.ui.widgets.code_results import COLUMNS as CODE

    centred = Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
    for columns in (FILES, MAIL, CODE):
        table = ResultTable(
            [heading for _k, heading, _a, _r in columns],
            aligns=["right" if right else "left" for *_rest, right in columns])
        for column, (_k, heading, _a, right) in enumerate(columns):
            item = table.horizontalHeaderItem(column)
            wanted = _RIGHT if right else _LEFT
            assert item.textAlignment() == wanted, (
                f"{heading!r} is aligned {item.textAlignment()}, "
                f"its column is {wanted}")
            assert item.textAlignment() != centred


# ---------------------------------------------------------------------------
# 2026-09-30 - switching sorting back on costs one sort, or none
# ---------------------------------------------------------------------------

def _count_comparisons(monkeypatch) -> list:
    """How many times Qt asks a cell whether it sorts before another."""
    calls = [0]
    real = SortableItem.__lt__

    def counted(self, other):
        calls[0] += 1
        return real(self, other)

    monkeypatch.setattr(SortableItem, "__lt__", counted)
    return calls


def _many(count: int = 60) -> list:
    # Names deliberately out of order, so a sort by name has work to do.
    return [Row(f"file {(n * 37) % count:03d}", f"{n} KB", n * 1_000, "then", n)
            for n in range(count)]


def test_a_ranked_list_is_filled_without_sorting_at_all(qapp, monkeypatch):
    r"""The rows arrive in the engine's order and are numbered as they stand,
    so there is nothing to sort. It used to sort twice per fill - once by
    whatever the header pointed at, once by the hidden rank column - and every
    comparison is a call into Python: about a third of a 2,000-row draw."""
    rows = _many()
    calls = _count_comparisons(monkeypatch)
    table = ResultTable(["Name", "Size", "When"], ranked=True,
                        aligns=["left", "right", "right"])

    _fill(table, rows)
    _fill(table, rows)                                  # and a redraw

    assert calls[0] == 0, f"{calls[0]} comparisons to fill a list already in order"
    assert _names(table) == [row.name for row in rows]
    assert table.isSortingEnabled()
    assert not table.horizontalHeader().isSortIndicatorShown()
    assert table.sort_order == RELEVANCE


def test_a_chosen_sort_is_reapplied_with_one_sort_not_two(qapp, monkeypatch):
    rows = _many()
    table = ResultTable(["Name", "Size", "When"], ranked=True,
                        aligns=["left", "right", "right"])
    _fill(table, rows)
    _click(table, 0)                                    # somebody sorts by name
    calls = _count_comparisons(monkeypatch)
    sorts: list = []                                    # the model says so once per sort
    table.model().layoutChanged.connect(lambda *_a: sorts.append(1))

    _fill(table, rows)                                  # a new result set

    assert _names(table) == sorted(row.name for row in rows)
    assert calls[0] > 0 and len(sorts) == 1, (calls[0], len(sorts))


def test_qt_sorts_the_moment_sorting_is_switched_on(qapp, monkeypatch):
    """The Qt behaviour the two tests above rest on, pinned so an upgrade that
    changes it is noticed: enabling sorting sorts by the header's indicator,
    and an indicator pointing at no column compares nothing."""
    from PyQt6.QtWidgets import QTableWidget

    calls = _count_comparisons(monkeypatch)

    def plain(section):
        table = QTableWidget(3, 1)
        for index, text in enumerate(["b", "c", "a"]):
            table.setItem(index, 0, SortableItem(text))
        table.horizontalHeader().setSortIndicator(section, Qt.SortOrder.AscendingOrder)
        assert calls[0] == 0, "pointing the indicator sorted, with sorting off"
        table.setSortingEnabled(True)
        return [table.item(row, 0).text() for row in range(3)]

    assert plain(0) == ["a", "b", "c"] and calls[0] > 0
    calls[0] = 0
    assert plain(-1) == ["b", "c", "a"] and calls[0] == 0
