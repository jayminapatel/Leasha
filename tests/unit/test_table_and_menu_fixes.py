r"""Four things the owner hit in ten minutes of using the window.

Layer: L5

Every one of these was reproduced before it was fixed, which is worth saying
because three of them are the kind that get "fixed" by reasoning and then come
back:

* *"on the mail results when i clicked on top of a column to sort it it
  crashed"* — `ResultTable.ROLE_ROW` was `UserRole + 1`, and so is
  `sortable_item.SORT_ROLE`. Two roles that must differ, chosen as integers in
  two files. Mail's "From" column was sorting by comparing whole `MailRow`
  objects.
* *"the / comands dont work after first use on code windows"* — `set_values`
  lowers the popup to fit a value list and nothing put it back, so after using
  `/type` once the *command* menu returned one row tall for the rest of the
  session. From the outside that is exactly what a broken dropdown looks like.
* *"in the dropdown / command should indicate what it expects as an argument"* —
  it read `/type <type>`, which says a value goes there and nothing about which.
* *"all the result tables should inatially have auto fit column and should be
  adjustable which it remembers"*.

The first two are covered by asserting the *property*, not the symptom: that the
two roles differ, and that the row count comes back. A test that only checked
"Mail does not crash on this fixture" would pass again the day somebody picks
`UserRole + 1` for something else.
"""

from __future__ import annotations

import os

import pytest

from app.ui.view_options import ViewPreferences, parse_prefs, prefs_to_state

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QTableWidget, QTableWidgetItem,
)

from app.ui.widgets.command_popup import CommandPopup, _hint  # noqa: E402
from app.ui.widgets.result_table import ROLE_ROW, ResultTable  # noqa: E402
from app.ui.widgets.sortable_item import (  # noqa: E402
    SORT_ROLE, SortableItem, _sortable,
)


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


class Row:
    """A row object, like `MailRow`: no ordering, and none wanted."""

    def __init__(self, name):
        self.name = name


# --- the sort crash ---------------------------------------------------------

def test_the_row_role_and_the_sort_role_are_different_numbers(qapp):
    """**The bug, stated as the property.**

    They are integers chosen in two files, and nothing but this test connects
    them. `UserRole + 1` for the row object meant `SortableItem.__lt__` read a
    `MailRow` as the value to sort by.
    """
    assert int(ROLE_ROW) != int(SORT_ROLE)


def test_a_sort_value_that_cannot_be_ordered_is_ignored(qapp):
    """Belt and braces: even if the numbers collide again, sorting degrades to
    the text rather than comparing objects."""
    assert _sortable(Row("x")) is None
    assert _sortable(None) is None
    assert _sortable(3) == 3
    assert _sortable("a") == "a"


def test_sorting_a_column_holding_row_objects_uses_the_text(qapp):
    """What Mail actually does: column 0 carries the row object *and* is the
    column people sort by first."""
    table = QTableWidget(2, 1)
    for index, text in enumerate(("beta", "alpha")):
        item = SortableItem(text)
        item.setData(ROLE_ROW, Row(text))
        table.setItem(index, 0, item)

    table.sortItems(0)

    assert [table.item(n, 0).text() for n in range(2)] == ["alpha", "beta"]


def test_a_real_sort_value_still_wins(qapp):
    """The whole reason `SortableItem` exists: "10 KB" after "3 KB"."""
    table = QTableWidget(2, 1)
    for index, (text, value) in enumerate((("10 KB", 10240), ("3 KB", 3072))):
        item = SortableItem(text)
        item.setData(SORT_ROLE, value)
        table.setItem(index, 0, item)

    table.sortItems(0)

    assert [table.item(n, 0).text() for n in range(2)] == ["3 KB", "10 KB"]


def test_the_row_object_survives_a_sort(qapp):
    """Which is why it is on the item rather than in a list beside the table."""
    table = ResultTable(["Name"], sortable=True)
    table.setSortingEnabled(False)
    rows = [Row("beta"), Row("alpha")]
    table.setRowCount(2)
    for index, row in enumerate(rows):
        table.setItem(index, 0, SortableItem(row.name))
    table.set_row_objects(rows)
    table.setSortingEnabled(True)

    table.sortItems(0)

    assert table.row_object(0) is rows[1]


# --- the / menu going one row tall -----------------------------------------

def test_the_command_menu_comes_back_full_height_after_a_value_menu(qapp):
    """**The "stops working after first use" report.**

    `set_values` sizes the popup to the value list. Without restoring it, the
    command menu returned one row tall - a dropdown showing one of thirty-seven
    switches, which is indistinguishable from one that has stopped working.
    """
    popup = CommandPopup()
    full = popup.maxVisibleItems()

    popup.set_values("type", ["pdf"])
    assert popup.maxVisibleItems() < full, "the fixture no longer reproduces it"

    popup.set_prefix("")

    assert popup.maxVisibleItems() == full


def test_the_menu_is_sized_to_the_catalogue_not_to_qt_s_default(qapp):
    """Qt shows seven; there are more switches than that, and the ones below
    the fold are invisible on the keystroke whose purpose is discovery."""
    popup = CommandPopup()

    assert popup.maxVisibleItems() >= 11


def test_a_value_list_is_capped_rather_than_sized_to_itself(qapp):
    """Forty values is a menu taller than the window."""
    popup = CommandPopup()
    popup.set_values("type", [f"ext{n}" for n in range(40)])

    assert popup.maxVisibleItems() <= 12


# --- what the menu says it expects -----------------------------------------

def test_a_row_says_what_the_switch_expects(qapp):
    """Not `<type>`, which restates the name. The values, which is the
    question the menu is open to answer."""
    popup = CommandPopup()
    first = popup._model.item(0).text()

    assert "/type" in first
    # **The wording is not pinned, only that there is some.** The hint was
    # "pdf, docx, xlsx, ..." and is now "an extension, or a kind: excel, ...";
    # both answer the question, and asserting the literal made an improvement
    # to the hint look like a regression.
    hint = first.split("<", 1)[-1].split(">", 1)[0]
    assert len(hint.split()) >= 3, f"the row does not say what it expects: {first!r}"


def test_a_switch_that_takes_no_value_says_so(qapp):
    """An empty column beside `/history` is a question left unanswered."""
    from app.search.gitquery import git_command_for

    assert _hint(git_command_for("history")) == "(no value)"


def test_a_long_hint_is_cut_at_a_comma_rather_than_mid_word(qapp):
    from app.search.commands import command_for

    hint = _hint(command_for("type"))

    assert hint.endswith("…>")
    # Cut at a separator, not mid-word: the character before the ellipsis is a
    # space following a comma or a colon, never half of "powerpoin…".
    assert hint[:-2].rstrip().endswith((",", ":")), hint


def test_every_row_still_carries_its_description(qapp):
    popup = CommandPopup()
    rows = [popup._model.item(n).text() for n in range(popup._model.rowCount())]

    assert all(len(row.split()) >= 3 for row in rows)


# --- column widths ----------------------------------------------------------

def test_nothing_dragged_means_fit_to_contents():
    """The starting state, and the reason it is the starting state: Qt's own
    default gives every column the same arbitrary width."""
    assert ViewPreferences().widths == ()


def test_a_dragged_width_is_remembered():
    prefs = ViewPreferences().with_width("name", 180)

    assert dict(prefs.widths)["name"] == 180


def test_dragging_the_same_column_twice_keeps_one_value():
    prefs = ViewPreferences().with_width("name", 180).with_width("name", 220)

    assert prefs.widths == (("name", 220),)


def test_a_width_of_zero_forgets_the_column():
    """So "fit" is expressible as a width rather than as a special case."""
    prefs = ViewPreferences().with_width("name", 180).with_width("name", 0)

    assert prefs.widths == ()


def test_widths_survive_a_round_trip_through_the_store():
    prefs = ViewPreferences().with_width("name", 180).with_width("repo", 90)
    state = prefs_to_state(prefs, "ui:code")

    assert parse_prefs(state, "ui:code").widths == prefs.widths


def test_a_malformed_width_is_a_default_not_an_exception():
    """These are read while the window is being built. A bad row in a settings
    table must never be the reason the application will not open."""
    found = parse_prefs({"ui:code:widths": "name=abc,,repo=90,x=,=5"}, "ui:code")

    assert found.widths == (("repo", 90),)


def test_the_menu_offers_a_way_back_to_fitted(qapp):
    """A column dragged to nothing stays at nothing across restarts, and the
    handle to pull it back is one pixel wide."""
    from PyQt6.QtWidgets import QWidget

    from app.ui.view_options import build_menu

    seen: list = []
    # `build_menu` takes a *parent* and returns the menu it built. The parent
    # is held in a local: a QWidget that goes out of scope takes its children
    # with it, and the menu is one - the first version of this test lost the
    # menu to garbage collection and reported it as a missing action.
    parent = QWidget()
    menu = build_menu(parent, ViewPreferences().with_width("name", 4),
                      columns=[("name", "Name")], available=("name",),
                      on_change=seen.append)

    labels = [action.text() for action in menu.actions()]
    assert any("Fit columns" in label for label in labels)

    for action in menu.actions():
        if "Fit columns" in action.text():
            action.trigger()

    assert seen and seen[-1].widths == ()
