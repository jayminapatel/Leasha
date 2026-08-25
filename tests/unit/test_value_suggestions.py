r"""What the `/` menu offers once a filter has been chosen.

Layer: L4/L5 (the decision; the drawing is Qt and is not here)

The menu answered "which filters exist" and stopped. The harder question is the
next one - *what do I put here* - and it was left to guesswork. A guessed value
returns nothing, and **a filter that returns nothing is indistinguishable from a
filter that does not work**, so the menu was quietly teaching people that the
feature was broken.

So the values come from the index: the extensions actually present, the people
who actually sent mail, the repositories actually found. Everything about *what*
is offered is decided in `presenter.value_suggestions`, which imports no Qt, and
this is where it is checked.

The properties that matter are the two that would be invisible if wrong: that a
store which cannot answer costs the suggestions and nothing else, and that
nothing here reaches the database with an unbounded query.
"""

from __future__ import annotations

import pytest

from app.search.commands import COMMANDS, RELATIVE_DATES, command_for
from app.ui.presenter import VALUE_LIMIT, value_suggestions


class FakeStore:
    """Records how it was asked, which is half of what is under test."""

    def __init__(self, values=None, raises=False):
        self._values = values or {}
        self._raises = raises
        self.calls = []

    def distinct_values(self, kind, *, prefix="", limit=40):
        self.calls.append((kind, prefix, limit))
        if self._raises:
            raise RuntimeError("database is locked")
        return list(self._values.get(kind, []))


# --- the catalogue ----------------------------------------------------------

def test_every_command_has_an_icon():
    """The list is scanned by shape before it is read. A row without one is a
    row that reads as a rendering fault."""
    assert all(command.icon.strip() for command in COMMANDS)


def test_every_command_can_offer_something():
    """**Each filter has either fixed values or a source, and most have one.**

    A command with neither leaves somebody at a colon and a blank, which is the
    state this whole feature exists to end. `/to`, `/subject` and `/name` are
    the honest exceptions - they match free text, and there is no list of
    "things people write in subjects".
    """
    silent = [c.name for c in COMMANDS if not c.values and not c.source]

    assert set(silent) <= {"to", "subject", "name"}, (
        f"{silent} offer nothing after the colon and could")


def test_a_source_names_something_the_store_actually_answers():
    """A source the store does not know is a menu that is always empty - and
    empty reads as broken, not as unconfigured."""
    known = {"ext", "folder", "sender", "repo"}
    unknown = [c.source for c in COMMANDS if c.source and c.source not in known]

    assert unknown == []


# --- what is offered --------------------------------------------------------

def test_fixed_values_need_no_store():
    """`/has` has exactly two answers and they are true of an empty index."""
    assert value_suggestions(None, "has") == ["attachment", "no-attachment"]


def test_a_date_offers_the_spellings_that_are_easier_to_pick_than_to_recall():
    assert value_suggestions(None, "after") == list(RELATIVE_DATES)


def test_index_values_are_offered_for_the_filters_that_have_them():
    store = FakeStore({"ext": ["pdf", "docx", "msg"]})

    assert value_suggestions(store, "type") == ["pdf", "docx", "msg"]


def test_an_alias_resolves_to_its_command():
    """`/kind` and `/ext` are `type`. The parser accepts all three, so the menu
    must too - a spelling that works in the box and not in the menu is the
    drift the shared catalogue exists to prevent."""
    store = FakeStore({"ext": ["pdf"]})

    assert value_suggestions(store, "kind") == ["pdf"]
    assert value_suggestions(store, "ext") == ["pdf"]


def test_fixed_values_come_before_the_index_s(monkeypatch):
    """Both, in that order. `/size` has useful shorthands *and* nothing in the
    index to read; a command with both should show the shorthands first because
    they are the ones somebody would not have guessed."""
    command = command_for("size")

    assert command.values, "size lost its shorthands"
    assert value_suggestions(None, "size")[0].startswith((">", "<"))


def test_the_prefix_narrows_what_is_offered():
    assert value_suggestions(None, "has", "no") == ["no-attachment"]
    assert value_suggestions(None, "after", "yes") == ["yesterday"]


def test_the_prefix_is_passed_to_the_store_rather_than_filtered_afterwards():
    """**Otherwise the LIMIT is applied to the wrong set.** Fetching the first
    forty extensions and *then* filtering for "doc" returns nothing on a corpus
    whose forty commonest types do not include it - which looks exactly like a
    file type that is not indexed."""
    store = FakeStore({"ext": ["docx"]})

    value_suggestions(store, "type", "doc")

    assert store.calls and store.calls[0][1] == "doc"


def test_a_duplicate_is_not_offered_twice():
    """A fixed value that the index also holds is one row, not two."""
    store = FakeStore({"ext": ["attachment"]})
    found = value_suggestions(store, "has")

    assert found.count("attachment") == 1


def test_an_unknown_command_offers_nothing_rather_than_guessing():
    assert value_suggestions(FakeStore(), "wibble") == []


# --- it must never be the cause of a failure --------------------------------

def test_a_store_that_cannot_answer_costs_the_suggestions_and_nothing_else():
    """**It runs behind a keystroke.** Mid-index, locked, or closed while the
    window is shutting down are all normal, and none of them is a reason for a
    menu to raise into the handler that opened it."""
    assert value_suggestions(FakeStore(raises=True), "type") == []


def test_fixed_values_survive_a_store_that_cannot_answer():
    """The half that needs no database still works when the database does not.
    A menu that empties itself because the index is busy reads as the filter
    disappearing."""
    assert value_suggestions(FakeStore(raises=True), "has") == [
        "attachment", "no-attachment"]


def test_the_number_offered_is_bounded():
    """No unbounded work behind a keystroke - the first non-negotiable - and no
    dropdown taller than the screen either."""
    store = FakeStore({"ext": [f"ext{n}" for n in range(500)]})

    assert len(value_suggestions(store, "type")) <= VALUE_LIMIT


def test_the_limit_is_pushed_down_to_the_query():
    """Trimming in Python still made the database build the whole list."""
    store = FakeStore({"ext": []})

    value_suggestions(store, "type", limit=7)

    assert store.calls[0][2] == 7


@pytest.mark.parametrize("name", [c.name for c in COMMANDS])
def test_no_command_raises_for_an_empty_index(name):
    """Every one of them, on a store that answers nothing. The day somebody
    adds a filter with a typo in its source, this is what says so."""
    assert isinstance(value_suggestions(FakeStore(), name), list)
