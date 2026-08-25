"""What the search cache is allowed to reuse an answer for.

Layer: L4

A cache that cannot tell it is stale is a lie. `_cache_key` already says so in
its own docstring, about the index generation - and then keyed on a bare
rerank on/off flag, so **every result reranked by one model was served
afterwards as though another model had produced it**.

That matters more than it sounds. Swapping to a reranker measured 9.2x faster
and finding search unchanged is precisely what a stale cache looks like from
the outside, and the model would have taken the blame.

The general rule these tests encode: anything that changes the answer belongs
in the key, and anything that does not must stay out of it, or the cache
fragments for no reason.
"""

from __future__ import annotations

import pytest

from app.search.engine import SearchEngine
from app.search.query import parse_query


class _Store:
    """Only what `_cache_key` reads."""

    is_open = True
    generation = 7


class _Reranker:
    available = True

    def __init__(self, model_name):
        self.model_name = model_name


def _engine(model_name=None):
    engine = SearchEngine.__new__(SearchEngine)     # no stores, no models
    engine.store = _Store()
    engine.reranker = _Reranker(model_name) if model_name else None
    return engine


def key(engine, raw="quarterly report", *, rerank=True, limit=20):
    return engine._cache_key(raw, parse_query(raw), rerank, limit)


# --- what must change the key ------------------------------------------------

def test_a_different_reranker_is_a_different_key():
    """The bug. Two models, same query, must not share an entry."""
    fast = _engine("Xenova/ms-marco-MiniLM-L-6-v2")
    slow = _engine("BAAI/bge-reranker-base")

    assert key(fast) != key(slow)


def test_the_model_name_appears_in_the_key():
    assert "Xenova/ms-marco-MiniLM-L-6-v2" in key(
        _engine("Xenova/ms-marco-MiniLM-L-6-v2"))


def test_reranked_and_not_reranked_stay_separate():
    engine = _engine("Xenova/ms-marco-MiniLM-L-6-v2")

    assert key(engine, rerank=True) != key(engine, rerank=False)


def test_the_index_generation_still_invalidates():
    """The guarantee that was already there, kept."""
    engine = _engine("m")
    before = key(engine)
    engine.store.generation = 8

    assert key(engine) != before


@pytest.mark.parametrize("a, b", [
    ("quarterly report", "annual report"),
    ("report type:pdf", "report type:docx"),
    ("report after:2024", "report after:2023"),
    ("report from:dave", "report from:sue"),
])
def test_a_different_query_is_a_different_key(a, b):
    engine = _engine("m")

    assert key(engine, a) != key(engine, b)


def test_a_different_limit_is_a_different_key():
    engine = _engine("m")

    assert key(engine, limit=20) != key(engine, limit=50)


# --- what must NOT change the key -------------------------------------------

def test_the_model_is_left_out_when_reranking_is_off():
    """With reranking off no model touched the result.

    Keying on one would split the cache between two states that produce
    identical answers - a miss on every search after a model change, for
    nothing.
    """
    fast = _engine("Xenova/ms-marco-MiniLM-L-6-v2")
    slow = _engine("BAAI/bge-reranker-base")

    assert key(fast, rerank=False) == key(slow, rerank=False)


def test_no_reranker_at_all_still_produces_a_key():
    """`reranker` is None on a machine with no model. It must not raise."""
    assert key(_engine(None), rerank=True)


def test_case_and_padding_do_not_split_the_cache():
    engine = _engine("m")

    assert key(engine, "Quarterly Report") == key(engine, "  quarterly report ")


def test_a_closing_store_short_circuits_rather_than_raising():
    """A search in flight while the window closes is not an error."""
    engine = _engine("m")
    engine.store.is_open = False

    assert key(engine) == "closed"


def test_the_key_version_was_bumped():
    """`v2` keys were built without the model and must never be read as `v3`.

    Without the bump, every entry cached before this change would be served
    against the new key format - which is the stale read this fixes.
    """
    assert key(_engine("m")).startswith("v3|")
