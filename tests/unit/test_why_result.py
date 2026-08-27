r"""Asking a result why it is on the page.

Layer: L5. Adoptions §1 — *the best idea of the five documents*.

**The constraint is the whole feature: state facts, never scores.** Every line
is read from something already recorded — which retriever found it, whether
the text contains the typed words, the freshness the blend used, the usage
log, the fold. Not one of them invents a number. *"87% relevant"* is a
sentence nobody can check and everybody would believe, and it is exactly what
this must not produce.

An empty answer is legitimate: a plain keyword hit with nothing else to say
about it says nothing rather than padding.
"""

from __future__ import annotations

import pathlib
import tempfile
import time

import pytest

from app.search.engine import SearchResult
from app.search.query import parse_query
from app.ui.presenter import RECENT_ENOUGH, explain_for, why_result

_NOW = time.time_ns()


def _result(**changes) -> SearchResult:
    fields = dict(chunk_id=1, file_id=1, path="C:/work/essay.docx", rank=1,
                  score=0.5, text="My homework about volcanoes and magma",
                  sources=(0,), mtime_ns=_NOW)
    fields.update(changes)
    return SearchResult(**fields)


def _lines(result, query="volcanoes", **kwargs):
    return why_result(result, parse_query(query), **kwargs)


# --------------------------------------------------------------------------
# What it says, and what it refuses to say
# --------------------------------------------------------------------------

def test_it_names_the_words_that_are_actually_in_it():
    """**Read off the text, not inferred from the score.** That is the
    difference between a fact and a guess, and a guess in an explanation is
    worse than no explanation."""
    lines = _lines(_result(), "volcanoes magma")
    assert any("volcanoes" in line and "magma" in line for line in lines)


def test_a_word_that_is_not_there_is_not_claimed():
    lines = _lines(_result(), "volcanoes sandwiches")
    assert not any("sandwiches" in line for line in lines)


def test_the_words_are_shown_as_they_were_typed():
    """`SearchEngine` handed back as `searchengine` reads as a correction of
    something that was not wrong."""
    result = _result(text="class SearchEngine: pass")
    assert any("SearchEngine" in line for line in _lines(result, "SearchEngine"))


def test_a_meaning_only_match_says_so():
    r"""**Said out loud, because it looks like a mistake otherwise.** A row
    with none of the typed words in it reads as a bug to somebody who does
    not know the search understands meaning."""
    result = _result(text="Lava and eruptions in Sicily", sources=(1,))
    assert any("meaning" in line for line in _lines(result, "volcanoes"))


def test_both_lanes_agreeing_is_named_as_the_strongest_signal():
    result = _result(sources=(0, 1))
    assert any("strongest" in line for line in _lines(result))


def test_a_definition_says_it_is_the_definition():
    result = _result(text="class SearchEngine: pass", declares=True)
    lines = _lines(result, "SearchEngine")
    assert any("defined" in line for line in lines)


def test_recency_is_mentioned_only_when_it_did_something():
    """Below one half-life the blend contributed almost nothing, and saying
    so would be noise dressed as an explanation."""
    recent = _lines(_result(recency=0.9))
    stale = _lines(_result(recency=0.05))
    assert any("Recent" in line for line in recent)
    assert not any("Recent" in line for line in stale)
    assert RECENT_ENOUGH == 0.5


def test_the_recency_line_carries_the_date_rather_than_a_number():
    """A date is checkable. A freshness score is a number somebody would
    believe and could never verify."""
    line = next(line for line in _lines(_result(recency=0.9))
                if "Recent" in line)
    assert "0.9" not in line and "%" not in line


def test_opening_it_before_is_a_fact_about_them():
    """"You have opened this before" is checkable; "popular" is not."""
    assert any("opened this before" in line
               for line in _lines(_result(), opens=1))
    assert any("opened this 4 times" in line
               for line in _lines(_result(), opens=4))
    assert not any("opened" in line for line in _lines(_result(), opens=0))


def test_a_folded_row_says_what_was_folded_into_it():
    from app.search.folding import COPIES, Fold

    fold = Fold(head=_result(), older=(_result(), _result()), reason=COPIES)
    assert any("2 other copies" in line
               for line in _lines(_result(), fold=fold))


def test_nothing_worth_saying_says_nothing():
    """A plain keyword hit with no other signal pads no lines. **An
    explanation that always speaks is an explanation nobody reads.**"""
    result = _result(text="something else entirely", sources=(0,))
    assert why_result(result, parse_query("volcanoes")) == (
        "Your words are in this file, though not in the part shown here.",)


@pytest.mark.parametrize("line_check", ["%", "score", "0.", "rank "])
def test_no_line_ever_contains_a_score(line_check):
    r"""**The load-bearing rule of the whole item.** Percentages and scores
    are the thing five different AI reviews all reached for, and the thing
    this order explicitly forbids: state facts, never scores."""
    from app.search.folding import VERSIONS, Fold

    result = _result(sources=(0, 1), recency=0.95, declares=True,
                     rerank_score=0.87)
    fold = Fold(head=result, older=(result,), reason=VERSIONS)
    for register in ("plain", "technical"):
        for line in why_result(result, parse_query("volcanoes magma"),
                               fold=fold, opens=3, register=register):
            assert line_check not in line.lower(), line


def test_the_technical_register_may_add_detail_the_plain_one_does_not():
    result = _result(rerank_score=0.8)
    plain = _lines(result, register="plain")
    technical = _lines(result, register="technical")
    assert len(technical) > len(plain)
    assert any("erank" in line for line in technical)


def test_a_broken_result_costs_the_explanation_not_the_page():
    """**Never raises.** This runs beside a result row; a bad value must lose
    the explanation, never the results."""
    class _Awkward:
        text = None
        sources = "not a tuple"

        def __getattr__(self, name):
            raise RuntimeError("this object is having a day")

    assert why_result(_Awkward(), parse_query("x")) == ()
    assert why_result(None, None) == ()


# --------------------------------------------------------------------------
# The switch, and the register
# --------------------------------------------------------------------------

def test_the_policy_decides_here_not_in_the_view():
    """The same rule `chips_for` follows: no surface carries a rule of its
    own, so the off-switch cannot be honoured in three places and forgotten
    in a fourth."""
    from app.search.policy import SEARCH, for_surface

    result = _result(sources=(0, 1))
    on = for_surface(SEARCH)
    assert explain_for(result, parse_query("volcanoes"), on)
    assert explain_for(result, parse_query("volcanoes"),
                       on.with_overrides(explain_results=False)) == ()


def test_the_register_follows_the_policy():
    from app.search.policy import SEARCH, for_surface

    result = _result(rerank_score=0.8)
    plain = explain_for(result, parse_query("volcanoes"),
                        for_surface(SEARCH).with_overrides(
                            notice_register="plain"))
    technical = explain_for(result, parse_query("volcanoes"),
                            for_surface(SEARCH).with_overrides(
                                notice_register="technical"))
    assert len(technical) > len(plain)


# --------------------------------------------------------------------------
# The signals reach the row at all
# --------------------------------------------------------------------------

def test_the_engine_carries_recency_and_declares_onto_the_result():
    r"""**The annotations were already being written and thrown away.**
    `recency.blend` and `definitions.boost` each record why they moved a hit,
    and `_to_result` dropped both on the floor - the same shape as `ext` and
    `mtime_ns` before the result rows learned to show them.
    """
    from app.search.engine import SearchEngine
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "why.db").connect()
    file_id = store.upsert_file(
        "C:/repo/engine.py", parent_dir="C:/repo", ext="py", size_bytes=1,
        mtime_ns=_NOW, status="INDEXED", source_kind="file")
    store.replace_chunks(file_id, [
        {"ordinal": 0, "text": "class Governor:\n    def check(self): pass"}])

    class _NoVectors:
        def search(self, *_a, **_k):
            return []

    class _NoModel:
        def embed(self, _t):
            raise RuntimeError("no model")

        def embed_all(self, _t):
            raise RuntimeError("no model")

    engine = SearchEngine(store, _NoVectors(), _NoModel())
    try:
        result = engine.search("Governor", use_cache=False).results[0]
        assert result.declares is True
        assert result.recency > 0.9
    finally:
        engine.close()


def test_open_counts_come_back_in_one_query_and_default_to_zero():
    """**"Never opened" is the ordinary state** and does not deserve a row -
    so a missing key means zero rather than an absent answer."""
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "opens.db").connect()
    assert store.open_counts([1, 2, 3]) == {}
    assert store.open_counts([]) == {}


def test_the_open_count_query_uses_the_partial_index():
    """Fifty hits are logged per search and about one is opened, so
    `idx_hits_opened` visits almost nothing. Counting per row instead would
    be fifty statements against a table that grows with every search anybody
    has ever run - the keyword path this project already had to fix once."""
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "plan.db").connect()
    plan = store.conn.execute(
        "EXPLAIN QUERY PLAN SELECT chunk_id, COUNT(*) FROM search_hits "
        "WHERE opened = 1 AND chunk_id IN (1,2,3) GROUP BY chunk_id"
    ).fetchall()
    assert any("idx_hits_opened" in str(row[-1]) for row in plan)
