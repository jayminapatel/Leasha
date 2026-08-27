r"""The first ten seconds, and the four things that were already right.

Layer: L5. **Most of §2e turned out to be already built**, and a test that
pins something true is worth more than a change that makes it true twice: the
next person to touch the tab order or the Enter handler finds out here rather
than from a user.

The one sub-item that is genuinely new - offering back what somebody searched
for before - is pure and tested here; attaching it to the box is a separate,
visual job recorded in the order's note.
"""

from __future__ import annotations

import pytest

from app.ui.first_contact import RECENT_LIMIT, greeting, recent, rows_for


def _Store(queries):
    """The rows `workers.recent_searches_async` hands over.

    **A list, not a store.** The fetch is a database read and lives on a
    worker; what is tested here is the rule that turns rows into a list.
    """
    return [{"query": text} for text in queries]


# --------------------------------------------------------------------------
# What an empty box offers back
# --------------------------------------------------------------------------

def test_the_last_few_searches_come_back_newest_first():
    store = _Store(["volcano homework", "invoice from dave", "holiday rota"])
    assert recent(store) == ("volcano homework", "invoice from dave",
                             "holiday rota")


def test_near_identical_refinements_collapse():
    """**Somebody refining a query leaves four near-identical rows.** A list of
    those is worse than no list at all - it fills the space that should hold
    four different days of work with one afternoon's typing."""
    store = _Store(["volcano", "Volcano", "VOLCANO", "homework"])
    assert recent(store) == ("volcano", "homework")


def test_the_first_spelling_is_the_one_shown():
    """Deduped case-insensitively but returned **as typed**. Showing somebody
    their own words back in a case they did not use reads as a correction."""
    assert recent(_Store(["Leeds Safety", "leeds safety"])) == ("Leeds Safety",)


def test_slash_commands_are_never_offered_back():
    """**A slash command is a mechanism, not a memory.** Somebody who typed
    `/newest` was steering, not asking, and offering it back teaches them the
    box's syntax instead of their own work."""
    store = _Store(["/newest", "/type pdf", "the audit findings"])
    assert recent(store) == ("the audit findings",)


def test_the_list_is_short_by_design():
    """Six, because this is a glance under a cursor, not a history page."""
    store = _Store([f"search number {n}" for n in range(50)])
    assert len(recent(store)) == RECENT_LIMIT


def test_blank_and_whitespace_rows_are_skipped():
    assert recent(_Store(["", "   ", "real one"])) == ("real one",)


def test_no_history_is_a_box_with_no_list():
    """A worker that failed hands over nothing, and that must be a box with no
    dropdown rather than a window that fails to open."""
    assert recent(None) == ()
    assert recent([]) == ()
    assert recent([{}, {"query": None}]) == ()


def test_switched_off_returns_nothing_whatever_the_rows_say():
    """Off means off. Somebody who switched this off is telling you they do
    not want their searches remembered at them."""
    assert recent(_Store(["volcano homework"]), enabled=False) == ()


def test_the_preference_is_read_once_here_not_in_the_view():
    """The same reason `SearchPolicy` exists: a view gets one call and has no
    rule of its own."""
    from types import SimpleNamespace

    store = _Store(["volcano homework"])
    assert rows_for(store, SimpleNamespace(search_offer_recent=False)) == ()
    assert rows_for(store, SimpleNamespace(search_offer_recent=True)) == (
        "volcano homework",)
    assert rows_for(store, None) == ("volcano homework",)


# --------------------------------------------------------------------------
# The line under an empty box
# --------------------------------------------------------------------------

def test_nothing_indexed_and_nothing_searched_are_different_problems():
    """**Telling somebody the wrong one sends them to the wrong screen.** An
    empty index needs the Indexing tab; a full one needs an example."""
    assert "Indexing tab" in greeting(0)
    assert "Indexing tab" not in greeting(12_345)
    assert "12,345" in greeting(12_345)


def test_an_uncounted_index_says_nothing_at_all():
    """`None` means nobody has counted yet. Guessing between the two states
    above is worse than staying quiet for a moment."""
    assert greeting(None) == ""


# --------------------------------------------------------------------------
# The three §2e items that were already true
# --------------------------------------------------------------------------

def test_search_is_the_tab_people_land_on():
    """§2e's "Search is the default tab on launch" - already true, because it
    is added first and nothing selects another. Pinned so that adding a tab
    above it is a failing test rather than a surprise."""
    from app.ui import shell

    source = (shell.__file__ or "")
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    first = text.index("(self.search_view, \"Search\"")
    for other in ("self.files_view", "self.mail_view", "self.code_view",
                  "self.indexing_view", "self.settings_view"):
        assert text.index(f"({other},") > first, (
            f"{other} is now added before Search, so Search is no longer the "
            f"tab somebody lands on")


def test_pressing_enter_on_a_result_opens_it():
    """§2e's "Enter opens the document" - already true. Qt's `activated`
    fires for Enter and for a double click, which is why one connection
    covers both and why this is easy to remove by accident."""
    from app.ui import results_view

    with open(results_view.__file__ or "", encoding="utf-8") as handle:
        text = handle.read()
    assert "self._list.activated.connect" in text


def test_the_search_box_already_has_its_placeholder():
    r"""§2e asks for "a plain example sentence" in the box. There is already a
    placeholder, argued where it is set, and this order's own principle 4 says
    **existing labels and descriptions never change**.

    So the item is met by what is there, and the principle decides the rest.
    This test exists to stop the placeholder being quietly emptied - not to
    pin its wording, which is free to be improved by somebody who means to.
    """
    from app.ui.widgets import search_bar

    with open(search_bar.__file__ or "", encoding="utf-8") as handle:
        text = handle.read()
    assert "box.setPlaceholderText(" in text


@pytest.mark.parametrize("field", ["folder", "when"])
def test_a_result_row_already_says_where_and_when(field):
    """§2e's "filename, folder, when" - the delegate draws all three today.
    Only the thumbnail is missing, and that is recorded in the order."""
    from app.ui.presenter import ResultGroup

    assert field in {f.name for f in ResultGroup.__dataclass_fields__.values()}
