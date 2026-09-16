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
    known = {"ext", "folder", "sender", "repo", "shows", "place", "on", "who"}
    unknown = [c.source for c in COMMANDS if c.source and c.source not in known]

    assert unknown == []


# --- what is offered --------------------------------------------------------

def test_fixed_values_need_no_store():
    """`/has` has exactly two answers and they are true of an empty index."""
    assert value_suggestions(None, "has") == ["attachment", "no-attachment"]


def test_a_date_offers_the_spellings_that_are_easier_to_pick_than_to_recall():
    """The spellings first, then the way out of the menu.

    `custom…` is the order's 3b - typing a date by hand has always worked and
    nothing said so, which made the list read as the only way in.
    """
    from app.ui.presenter import CUSTOM_ROW

    assert value_suggestions(None, "after") == [*RELATIVE_DATES, CUSTOM_ROW]


def test_the_way_out_is_last_and_not_offered_mid_word():
    """It is the way out rather than an answer, and somebody who has typed
    three characters is answering."""
    from app.ui.presenter import CUSTOM_ROW

    assert value_suggestions(None, "after")[-1] == CUSTOM_ROW
    assert CUSTOM_ROW not in value_suggestions(None, "after", "3")


def test_only_dates_offer_a_way_out():
    """`/type` has no free-form spelling to fall back to - every value it takes
    is either in the index or in the grammar."""
    from app.ui.presenter import CUSTOM_ROW

    assert CUSTOM_ROW not in value_suggestions(None, "type")
    assert CUSTOM_ROW not in value_suggestions(None, "has")


def test_index_values_are_offered_for_the_filters_that_have_them():
    """**Leading, not alone.** `/type` also carries the kind words the parser
    expands, so this asserts the front of the list rather than the whole of it -
    see `test_the_kind_words_come_after_the_index`."""
    store = FakeStore({"ext": ["pdf", "docx", "msg"]})

    assert value_suggestions(store, "type")[:3] == ["pdf", "docx", "msg"]


def test_an_alias_resolves_to_its_command():
    """`/kind` and `/ext` are `type`. The parser accepts all three, so the menu
    must too - a spelling that works in the box and not in the menu is the
    drift the shared catalogue exists to prevent."""
    store = FakeStore({"ext": ["pdf"]})

    assert value_suggestions(store, "kind")[0] == "pdf"
    assert value_suggestions(store, "ext") == value_suggestions(store, "type")


def test_fixed_values_lead_when_there_is_no_index_to_read(monkeypatch):
    """`/size` has useful shorthands and nothing in the index to read, so its
    shorthands are the whole list and lead it.

    **This used to say fixed values come first, full stop, and that rule did
    not survive `/type` gaining both.** Frequency beats novelty once there is
    real data: `>1mb` is worth showing because nobody would guess it, but
    `excel` is not worth showing above `pdf` when there are four thousand PDFs.
    So the rule is now conditional, and `test_the_kind_words_come_after_the_index`
    is the other half of it.
    """
    command = command_for("size")

    assert command.values, "size lost its shorthands"
    assert not command.source, "size gained a source; this test now proves nothing"
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
    menu to raise into the handler that opened it.

    This asserted an empty list, from when `/type` had nothing but the index to
    offer. It now keeps its kind words - which is the rule the test below
    already stated for `/has`, applied to the filter people use most. A menu
    that empties itself because the index is busy reads as the filter
    disappearing.
    """
    found = value_suggestions(FakeStore(raises=True), "type")

    assert "excel" in found, "a locked index took the grammar's own values with it"
    assert isinstance(found, list)


def test_fixed_values_survive_a_store_that_cannot_answer():
    """The half that needs no database still works when the database does not.
    A menu that empties itself because the index is busy reads as the filter
    disappearing."""
    assert value_suggestions(FakeStore(raises=True), "has") == [
        "attachment", "no-attachment"]


def test_the_number_offered_is_bounded():
    """No unbounded work behind a keystroke - the first non-negotiable.

    The ceiling is now per source. `ext` is the one bounded column - a machine
    has perhaps eighty file types and never more - and the general cap was
    truncating a list that fits on a screen, by frequency, so the formats
    somebody had just switched on were the first to be cut. Senders and folders
    have no ceiling at all and keep the general one, which is what it was
    protecting against.
    """
    from app.ui.presenter import VALUE_LIMITS

    store = FakeStore({"ext": [f"ext{n}" for n in range(500)]})
    ceiling = VALUE_LIMITS.get("ext", VALUE_LIMIT)

    assert len(value_suggestions(store, "type")) <= ceiling
    assert ceiling < 500, "the ceiling stopped being a ceiling"

    senders = FakeStore({"sender": [f"p{n}@x" for n in range(500)]})
    assert len(value_suggestions(senders, "from")) <= VALUE_LIMIT


def test_the_kind_words_come_after_the_index():
    """The other half of the ordering rule. `/type excel` has parsed since
    Layer 4 and was never offered, because `files.ext` has no row saying
    "excel" - but it must not outrank a type somebody actually has."""
    store = FakeStore({"ext": ["pdf", "docx"]})
    found = value_suggestions(store, "type")

    assert found[:2] == ["pdf", "docx"]
    assert "excel" in found, "a filter that parses is still hidden from the menu"
    assert found.index("excel") > found.index("docx")


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
