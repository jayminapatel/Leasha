"""What the window shows when a search has quietly done a worse job.

Layer: L5

UI-1 from the thread-merge handover, and the half of the owner's standing rule
that was still missing: *"For all things it should not fail silently it should
notify in some way."*

The backend detects the degradation, `SearchResponse.notices` carries it, and
the CLI prints it. The window - the only interface the owner actually uses -
said nothing at all. A search returning sixty keyword hits and no vector hits
looks identical to one that worked, so the person gets worse results and
concludes the corpus is thin rather than that half the engine is dead.

**The decision lives in `presenter.notice_line`, which imports no Qt.** That is
deliberate and it is the whole reason this file can exist: `QtWidgets` needs a
display, and the last time UI logic was verified by reading rather than running
it, `QPdfView()` shipped without its parent argument and crashed the window on
startup. Everything deciding *what* is shown is tested here; the widget only
draws the string it is handed.
"""

from __future__ import annotations

import pytest

from app.search.engine import (
    NOTICE_NO_VECTORS,
    NOTICE_RERANK_UNAVAILABLE,
    NOTICE_UNMATCHED_TERMS,
    Notice,
    SearchResponse,
)
from app.ui.presenter import notice_line


def test_a_healthy_search_shows_nothing():
    """Empty, so the bar hides. A permanent banner is furniture within a week."""
    assert notice_line([]) == ""
    assert notice_line(None) == ""


def test_a_notice_is_shown_with_a_warning_mark():
    line = notice_line([Notice(NOTICE_NO_VECTORS, "Meaning-based search is off.")])

    assert "Meaning-based search is off." in line
    assert line.startswith("⚠"), "it must read as a warning, not as chrome"


def test_several_notices_share_one_line():
    """One line that wraps, not one line each.

    A hard break makes a single notice and two notices look like different
    kinds of thing, which is a distinction the reader does not need.
    """
    line = notice_line([
        Notice(NOTICE_UNMATCHED_TERMS, "Not in the index: petrrabigh."),
        Notice(NOTICE_NO_VECTORS, "Meaning-based search returned nothing."),
    ])

    assert "petrrabigh" in line and "returned nothing" in line
    assert "\n" not in line


def test_the_order_the_engine_produced_them_is_kept():
    """Cause before consequence: the unmatched word explains the thin results,
    so it reads first."""
    line = notice_line([
        Notice(NOTICE_UNMATCHED_TERMS, "FIRST"),
        Notice(NOTICE_NO_VECTORS, "SECOND"),
    ])

    assert line.index("FIRST") < line.index("SECOND")


@pytest.mark.parametrize("message", ["", "   ", "\n\t "])
def test_a_notice_with_no_message_is_not_drawn_as_an_empty_warning(message):
    """An empty warning triangle is worse than no warning at all."""
    assert notice_line([Notice(NOTICE_NO_VECTORS, message)]) == ""


def test_a_response_straight_from_the_engine_works(tmp_path):
    """End to end through the real contract, not a hand-built list.

    `SearchResponse.notices` is what the view will actually pass, and a field
    renamed on one side and not the other is exactly the drift the two-thread
    contract used to catch.
    """
    response = SearchResponse(
        notices=(Notice(NOTICE_RERANK_UNAVAILABLE, "Results were not reranked."),))

    assert "not reranked" in notice_line(response.notices)


def test_it_reads_message_and_nothing_else():
    """**Never `code`.** The wording is the backend's and is free to change;
    branching belongs on the code, and `app/ui/` has a test asserting the UI
    does not parse error strings to decide anything."""
    line = notice_line([Notice("NOTICE_SOMETHING_NEW", "a message")])

    assert "a message" in line
    assert "NOTICE_SOMETHING_NEW" not in line, "the code leaked into the window"


def test_an_object_that_is_not_a_notice_does_not_crash_the_window():
    """Defensive, because this runs on the paint path.

    A malformed entry must cost its own line, never the results behind it.
    """
    class Odd:
        pass

    assert notice_line([Odd()]) == ""
    assert "fine" in notice_line([Odd(), Notice("X", "fine")])
