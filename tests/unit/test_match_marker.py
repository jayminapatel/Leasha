r"""How a result matched, and the date that was never there.

Layer: L5. Adoptions §2 — *results subtly distinguish HOW they matched: a
meaning-only match shows a plain-words marker instead of pretending words
matched.*

**And a bug found on the way in.** `to_row` never copied `ext` or `mtime_ns`
off the `SearchResult`, though the field comment said it did — so
`ResultGroup.when`, built from `rows[0].mtime_ns`, was `format_when(0)`, which
is the empty string. **Every document result showed no date at all**, for the
life of the feature. Mail hid it: a message takes its date from `sent_at` in
the details map, so the Mail tab looked right while the other three did not.
"""

from __future__ import annotations

import time

import pytest

from app.search.engine import SearchResult
from app.ui.presenter import (
    MEANING_MARKER, group_results, group_subtitle, match_marker, to_row,
)

_NOW = time.time_ns()


@pytest.fixture(autouse=True)
def _now_is_now(monkeypatch):
    """`_NOW` above is taken when the module is *imported* - at collection, which
    in a full run is minutes before this file's tests execute - and the tests
    assert a document touched "just now". They passed on a fast run and read "3
    min ago" on a slow one. Refreshed for each test instead (2026-09-20)."""
    monkeypatch.setitem(globals(), "_NOW", time.time_ns())


def _row(sources=(0,), **changes):
    fields = dict(chunk_id=1, file_id=1, path="C:/work/report.pdf", rank=1,
                  score=0.5, text="the safety report", sources=sources,
                  ext="pdf", mtime_ns=_NOW)
    fields.update(changes)
    return to_row(SearchResult(**fields), ("safety",))


# --------------------------------------------------------------------------
# §2 — the marker
# --------------------------------------------------------------------------

def test_a_meaning_only_match_is_marked():
    r"""**The row that reads as a mistake.** A result with none of the typed
    words in it looks like a bug to anybody who does not know the search
    understands meaning - so that is the one row that gets a marker."""
    assert match_marker(_row(sources=(1,))) == MEANING_MARKER


def test_a_keyword_hit_keeps_todays_highlight_and_says_nothing_extra():
    """A badge on every row makes the one that matters invisible."""
    assert match_marker(_row(sources=(0,))) == ""


def test_a_hit_both_lanes_found_is_not_a_meaning_only_hit():
    assert match_marker(_row(sources=(0, 1))) == ""


def test_the_marker_is_a_word_not_a_colour():
    r"""**The accessibility rule, and it is not decoration.** Colour is never
    the only signal - the same rule the focus ring follows, for the same
    reason: a marker that is only a hue is a marker several people on any
    given day cannot see.
    """
    assert MEANING_MARKER.strip() and any(c.isalpha() for c in MEANING_MARKER)
    assert MEANING_MARKER == MEANING_MARKER.lower()


def test_it_reads_the_lanes_not_the_explain_sentence():
    r"""**Carried as data, not parsed back out of a message.** The rule this
    codebase set for notices - the UI never reads a message string to decide
    anything - applies to a row deciding whether to show a marker."""
    row = _row(sources=(1,))
    assert row.sources == (1,)
    assert match_marker(row) == MEANING_MARKER
    # Even with the sentence removed, the decision still holds.
    row.explain = ""
    assert match_marker(row) == MEANING_MARKER


def test_the_switch_removes_it():
    """Rides `explain_results`: this is the shortest possible answer to *why
    is this here*, and a separate switch for one word would be a preference
    nobody could tell apart from the other."""
    from app.search.policy import SEARCH, for_surface

    row = _row(sources=(1,))
    assert match_marker(row, for_surface(SEARCH)) == MEANING_MARKER
    assert match_marker(
        row, for_surface(SEARCH).with_overrides(explain_results=False)) == ""


def test_it_appears_on_the_row_a_person_actually_reads():
    subtitle = group_subtitle(group_results([_row(sources=(1,))])[0])
    assert MEANING_MARKER in subtitle
    assert MEANING_MARKER not in group_subtitle(
        group_results([_row(sources=(0,))])[0])


def test_a_row_with_nothing_to_read_is_not_an_error():
    assert match_marker(None) == ""
    assert match_marker(object()) == ""


# --------------------------------------------------------------------------
# The date that was never there
# --------------------------------------------------------------------------

def test_the_row_carries_the_date_and_the_extension():
    r"""**The regression this file exists for.** `to_row` built a `ResultRow`
    without `ext` or `mtime_ns` while the field comment claimed both went
    "straight through", so every document result rendered with a blank date.
    """
    row = _row()
    assert row.mtime_ns == _NOW
    assert row.ext == "pdf"


def test_every_document_result_shows_when_it_was_last_touched():
    group = group_results([_row()])[0]
    assert group.when, "a result with no date is a result nobody can place"
    assert group.when == "just now"


def test_an_older_document_says_how_old():
    old = _row(mtime_ns=_NOW - 400 * 86_400 * 1_000_000_000)
    assert group_results([old])[0].when not in ("", "just now")


def test_a_file_with_no_date_still_renders():
    """Unstattable files are a real population - archives, network shares -
    and an empty date is the honest answer for them, not a crash."""
    assert group_results([_row(mtime_ns=0)])[0].when == ""


@pytest.mark.parametrize("extension", ["pdf", "docx", "xlsx"])
def test_the_kind_comes_from_the_result_rather_than_the_filename(extension):
    """It used to fall back to parsing the name every time, because the
    extension never arrived. That worked by accident and stopped working for
    a file whose name has no extension at all."""
    row = _row(path="C:/work/no-extension-here", ext=extension)
    assert group_results([row])[0].kind == extension
