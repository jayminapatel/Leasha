"""UI phase B, a steady Search page: the words and decisions, without a window.

Layer: L5 (presenter), L4 (the sort grammar)

The owner, 2026-10-13: every screen should feel like one world-class app, and
Search is the front door. These are the parts of that order that are decisions
rather than drawing - what the status line says, what a missing word costs, which
list folds mail with its attachments, and how the Sort control reaches the one
sort the typed `/newest` already uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.search.plain_notices import plain_message
from app.search.query import parse_query
from app.ui.presenter import (
    SORT_CHOICES, results_message, semantic_health, sort_in, status_details,
    status_line, to_row, with_sort,
)
from app.ui.view_options import parse_prefs, prefs_to_state


@dataclass
class _Result:
    file_id: int
    chunk_id: int = 0
    path: str = ""
    text: str = ""
    rank: int = 1


@dataclass
class _Response:
    results: list = field(default_factory=list)
    elapsed_ms: float = 212.0
    interim: bool = False
    from_cache: bool = False
    reranked: bool = False
    keyword_count: int = 0
    vector_count: int = 0
    parsed: object = None
    unmatched: tuple = ()
    notices: tuple = ()


# -- 3. the status line in plain words ---------------------------------------------------

def test_the_status_line_counts_documents_not_passages() -> None:
    three_passages_two_files = _Response([_Result(1, 1), _Result(1, 2), _Result(2, 3)])
    assert status_line(three_passages_two_files) == "2 documents"
    assert status_line(_Response([_Result(7)])) == "1 document"


def test_the_status_line_keeps_timing_and_machinery_out_of_the_words() -> None:
    response = _Response([_Result(1)] * 3, from_cache=True, reranked=True)
    line = status_line(response)
    for word in ("ms", "cached", "reranked", "result(s)", "keyword only"):
        assert word not in line, line
    tip = status_details(response)
    assert "212ms" in tip and "cached" in tip and "reranked" in tip


def test_a_search_still_running_says_so_in_plain_words() -> None:
    assert status_line(_Response([], interim=True)) == "Still searching…"
    assert status_line(_Response([_Result(1), _Result(2)], interim=True)) == \
        "2 documents  ·  Still searching…"


def test_the_shown_count_wins_when_the_list_folded_further() -> None:
    """A mail and its two attachments are three files and one row: the line counts the row."""
    response = _Response([_Result(1), _Result(2), _Result(3)])
    assert status_line(response, shown=1) == "1 document"


def test_meaning_search_failing_is_said_without_a_command_line() -> None:
    response = _Response([_Result(1)], keyword_count=5, vector_count=0)
    said = "Meaning-based search found nothing; keyword results are shown."
    assert semantic_health(response) == said
    assert results_message(response)[1] == said
    assert "app.cli" not in said


# -- 4. one row per real item --------------------------------------------------------------

def test_search_folds_a_mail_with_its_attachments_by_default() -> None:
    assert parse_prefs({}, "ui:results").group_by_conversation is True


def test_a_choice_already_saved_is_kept() -> None:
    assert parse_prefs({"ui:results:conversations": "off"}, "ui:results").group_by_conversation is False
    chosen = parse_prefs({}, "ui:results")
    assert parse_prefs(prefs_to_state(chosen, "ui:results"), "ui:results") == chosen


def test_the_mail_list_is_unchanged() -> None:
    """The Mail tab is a table people scan one message a line; this order is about Search."""
    assert parse_prefs({}, "ui:mail").group_by_conversation is False


def test_a_row_carries_the_words_it_was_found_by_so_its_name_can_be_marked() -> None:
    row = to_row(_Result(1, path="C:/work/pump report.pdf", text="the pump"), ["pump"])
    assert row.terms == ("pump",)


# -- 5. a sort control over the one sort there is ----------------------------------------

def test_the_choices_are_the_four_the_owner_named() -> None:
    assert [label for label, _value in SORT_CHOICES] == ["Best match", "Newest", "Oldest", "Name"]


def test_the_sort_is_read_from_what_was_typed() -> None:
    assert sort_in("pump") == ""
    assert sort_in("pump /newest") == "newest"
    assert sort_in("/oldest pump") == "oldest"
    assert sort_in("pump sort:name") == "name"


def test_choosing_a_sort_rewrites_the_one_switch_in_the_box() -> None:
    assert with_sort("pump", "newest") == "pump /newest"
    assert with_sort("pump /newest", "oldest") == "pump /oldest"
    assert with_sort("/latest pump", "") == "pump"
    assert with_sort("pump sort:oldest /type pdf", "name") == "pump /type pdf /sort name"
    for _label, value in SORT_CHOICES:
        assert sort_in(with_sort("boiler /type pdf", value)) == value


def test_name_is_a_sort_the_grammar_knows() -> None:
    assert parse_query("sort:name pump").sort == "name"
    assert not parse_query("sort:name pump").unknown_operators


def test_the_plain_notice_names_the_order_actually_used() -> None:
    """It said "newest first" for /oldest too - found while adding Name."""
    assert "oldest first" in plain_message("NOTICE_SORTED", "Sorted by date, oldest first")
    assert "newest first" in plain_message("NOTICE_SORTED", "Sorted by date, newest first")
    assert "name" in plain_message("NOTICE_SORTED", "Sorted by name, A to Z").lower()
