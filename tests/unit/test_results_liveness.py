"""The list must not blank while somebody is reading it.

Layer: L5

Reported as *"the mail tab searches as you type, this is good; the other two
tabs are not the same"*. Mail feels better for one reason: **it never blanks.**
It keeps what it has until something replaces it.

The other two threw results away on the way *to* a query. Typing `in` en route
to `invoice` emptied the Files table and then refilled it, and an interim search
that momentarily found nothing cleared the results pane. Both read as the tab
losing your work rather than doing it.

These are source-level checks, because Qt widgets cannot be built without a
display - crude, but they catch exactly the mistake that causes this: clearing
first and asking questions later.
"""

from __future__ import annotations

from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "app" / "ui"


def source(name: str) -> str:
    return (UI / name).read_text(encoding="utf-8")


def test_files_clears_only_when_the_box_is_empty():
    """An empty box is a real request to show nothing. One character on the way
    to a word is not."""
    body = source("files_view.py").split("def _run")[1].split("\n    def ")[0]
    assert "if not text and not ext:" in body


def test_files_keeps_its_rows_while_a_query_is_too_short():
    """It says what it is waiting for and leaves the previous rows alone."""
    body = source("files_view.py").split("def _run")[1].split("\n    def ")[0]
    short = body.split("MIN_NAME_CHARS and not ext")[1]
    assert "setRowCount(0)" not in short.split("return")[0]


def test_files_still_refuses_a_query_too_short_to_mean_anything():
    """Not blanking is not the same as searching for one character - that
    matches nearly everything and the answer is useless."""
    assert "MIN_NAME_CHARS" in source("files_view.py")


def test_a_filter_alone_is_a_complete_request():
    """`/type pdf` means "every PDF". Demanding characters of name as well would
    refuse the simplest thing the dropdown offers."""
    body = source("files_view.py").split("def _run")[1].split("\n    def ")[0]
    assert "and not ext" in body


def test_search_keeps_results_when_a_later_search_finds_nothing():
    """An interim keyword pass can legitimately find nothing while the full
    hybrid pass is still running. Emptying the pane in between is a flicker that
    reads as the search failing."""
    body = source("search_view.py").split("def _on_results")[1].split("\n    def ")[0]
    assert "_shown_anything" in body


def test_search_does_blank_before_it_has_ever_found_anything():
    """The first empty answer of a session is genuinely empty and must say so -
    otherwise the pane sits blank with no explanation at all."""
    body = source("search_view.py").split("def _on_results")[1].split("\n    def ")[0]
    assert "self.results.clear(summary)" in body


def test_emptying_the_box_resets_the_flag():
    """Otherwise a cleared box followed by a fruitless search would refuse to
    blank, leaving results on screen that match nothing on screen."""
    text = source("search_view.py")
    assert "self._shown_anything = False" in text
    assert text.count("_shown_anything = False") >= 2, "init and reset"


def test_the_scroll_position_survives_a_redraw():
    """Toggling a preference or expanding a row otherwise jumps the list to the
    top, losing the place of somebody who had scrolled to the eighth result."""
    body = source("results_view.py").split("def _redraw")[1].split("\n    def ")[0]
    assert "verticalScrollBar" in body
    assert "setValue" in body


@pytest.mark.parametrize("name", ["search_view.py", "files_view.py", "mail_view.py"])
def test_every_searching_view_stops_its_timer_on_close(name):
    """A timer firing during teardown starts a query against a store that is
    being closed - which arrived as a traceback telling the owner to send the
    log file. Nothing was wrong; the work should not have begun."""
    assert "def shutdown(" in source(name)
