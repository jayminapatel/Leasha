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


@pytest.mark.parametrize("scope", ["all", "mail", "documents", "code"])
def test_every_scope_gets_its_own_key(scope):
    """`code` was added and nothing here covered scope at all.

    Without this, "All" and "Code" share an entry for the same typed text and
    whichever ran first answers for both. The comment in `_cache_key` says
    exactly that about mail, and the test that would have caught it did not
    exist - the scopes were listed in a docstring rather than asserted.
    """
    engine = _engine("m")
    parsed = parse_query("quarterly report").scoped(scope)
    keys = {
        other: engine._cache_key("quarterly report",
                                 parse_query("quarterly report").scoped(other), True, 20)
        for other in ("all", "mail", "documents", "code")
    }

    mine = engine._cache_key("quarterly report", parsed, True, 20)
    assert sum(1 for value in keys.values() if value == mine) == 1, (
        f"scope={scope} shares a cache entry with another scope")


def test_the_code_scope_is_a_real_scope():
    """A value not in `SCOPES` is silently dropped back to "all" by `scoped()`.

    So a typo in the scope list would make this whole filter a no-op that
    returns everything, looking like it works.
    """
    from app.search.query import SCOPES

    assert "code" in SCOPES
    assert parse_query("x").scoped("code").scope == "code"


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
    """Two strings that parse the same are the same search.

    The key no longer contains the raw text at all - it is built from the parse,
    which already folds case and whitespace for ordinary words. See the test
    below for the case where folding was actively wrong.
    """
    engine = _engine("m")

    assert key(engine, "Quarterly Report") == key(engine, "  quarterly report ")


def test_capitalised_operators_are_a_different_search():
    r"""**`raw.lower()` in the key made these one entry, and they are two.**

    `AND`, `OR` and `NOT` are operators only in capitals - that is precisely how
    the parser tells them from the English words people search for constantly.
    `pump AND valve` demands both terms; `pump and valve` is three ordinary
    words joined the default way. Folding the raw text meant whichever ran first
    answered for the other, for as long as the index generation held.
    """
    engine = _engine("m")

    assert key(engine, "pump AND valve") != key(engine, "pump and valve")


def test_a_closing_store_short_circuits_rather_than_raising():
    """A search in flight while the window closes is not an error."""
    engine = _engine("m")
    engine.store.is_open = False

    assert key(engine) == "closed"


def test_the_key_version_was_bumped():
    """A key format change must never let old entries be read as new ones.

    `v2` was built without the reranker model; `v3` folded the raw query's case,
    so `pump AND valve` and `pump and valve` shared an entry. Each bump exists
    because entries written under the previous format would otherwise be served
    against the new one - which is the stale read the version guards against.
    """
    assert key(_engine("m")).startswith("v4|")
