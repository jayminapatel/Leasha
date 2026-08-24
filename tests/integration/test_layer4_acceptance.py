"""Layer 4 acceptance tests.

The criteria from BUILD_SPEC_V2.md:

  1. A golden set of query->expected-document pairs; track recall@10.
  2. Warm search p95 <300ms on the real index.
  3. First search after launch <3s.
  4. Rerank on/off both work; toggling does not require a restart.
  5. Deleting the rerank model mid-session degrades gracefully with one log line.
  6. Malformed queries return results or a clean AppError - never a crash.
  7. Cache invalidates correctly after an incremental index run.
  8. Typing never blocks; typed operators and filter chips are interchangeable.

These run against a **real index** - files on disk, extracted, chunked, written
to SQLite and LanceDB, then searched. Only the embedding model is faked, because
a real one would make every test minutes long and would prove nothing about the
pipeline. The fake is deterministic and content-derived, so semantically similar
text really does land near itself.

Criterion 2 is measured here but at small scale: p95 on the *real* corpus needs
the real corpus and the real model, and is recorded in HANDOFF when run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.index.embedder import Embedder, l2_normalise
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.search.engine import SearchEngine
from app.search.rerank import Reranker
from app.storage.sqlite_store import SqliteStore
from app.storage.vector_store import VectorStore

pytestmark = pytest.mark.slow

DIM = 64
_WORD = re.compile(r"[a-z0-9]+")


def bag_of_words_encoder(texts):
    """A tiny deterministic embedder with real semantics.

    Each word contributes to fixed dimensions, so texts sharing vocabulary land
    near each other. That is enough for the vector side to behave like a vector
    side - which is what these tests need - without a 130MB download.
    """
    out = []
    for text in texts:
        vector = [0.0] * DIM
        for word in _WORD.findall(text.lower()):
            slot = hash(word) % DIM
            vector[slot] += 1.0
            vector[(slot * 7 + 3) % DIM] += 0.5
        if not any(vector):
            vector[0] = 1.0
        out.append(l2_normalise(vector))
    return out


#: query -> the file that should come back. The seed of the golden set that
#: Layer 10 will grow automatically from real clicks.
GOLDEN = {
    "pump station commissioning": "commissioning.txt",
    "valve replacement shutdown": "valves.txt",
    "flow rates manifold": "flowrates.txt",
    "safety induction training": "safety.txt",
    "budget forecast quarterly": "budget.txt",
}

CORPUS = {
    "commissioning.txt": "The northern pump station commissioning report was signed in March. "
                         "Commissioning of the pump station completed without incident.",
    "valves.txt": "Two isolation valves require replacement before the next shutdown. "
                  "Valve replacement is scheduled during the autumn shutdown window.",
    "flowrates.txt": "Flow rates were measured at three points across the manifold. "
                     "The manifold flow rate readings were within tolerance.",
    "safety.txt": "All contractors must complete safety induction training before site access. "
                  "Safety induction covers permits, isolation and emergency procedures.",
    "budget.txt": "The quarterly budget forecast shows capital spend ahead of plan. "
                  "Budget forecasting for the next quarter is under review.",
    "misc.txt": "Catering arrangements for the site visit have been confirmed.",
}


def _age(root: Path, *, seconds: int = 3600) -> None:
    """Backdate the corpus past the walker's recent-edit window, so incremental
    behaviour is exercised rather than the hot-file path."""
    import os
    import time

    when = time.time() - seconds
    for path in root.rglob("*"):
        if path.is_file():
            os.utime(path, (when, when))


@pytest.fixture()
def indexed(tmp_path: Path):
    """A real index over a real corpus."""
    root = tmp_path / "corpus"
    root.mkdir()
    for name, body in CORPUS.items():
        (root / name).write_text(body, encoding="utf-8")
    _age(root)

    store = SqliteStore(tmp_path / "index.db").connect()
    vectors = VectorStore(tmp_path / "vectors", dim=DIM).connect()
    embedder = Embedder(dim=DIM, encoder=bag_of_words_encoder)

    Pipeline(store, vectors, embedder, PipelineConfig(
        walk=WalkConfig(roots=[root], extensions=frozenset({".txt"})), workers=2,
    )).run()

    yield store, vectors, embedder, root
    store.close()
    vectors.close()


@pytest.fixture()
def engine(indexed):
    store, vectors, embedder, _root = indexed
    engine = SearchEngine(store, vectors, embedder)
    yield engine
    engine.close()


def names(response) -> list[str]:
    return [Path(result.path).name for result in response.results]


# --- 1: the golden set ------------------------------------------------------

@pytest.mark.parametrize("query,expected", list(GOLDEN.items()))
def test_golden_set_returns_the_right_document(engine, query: str, expected: str) -> None:
    response = engine.search(query, limit=10)
    assert response.results, f"{query!r} returned nothing"
    assert expected in names(response)[:3], (
        f"{query!r} -> {names(response)[:3]}, expected {expected} in the top 3"
    )


def test_recall_at_10_over_the_golden_set(engine) -> None:
    """The regression guard. If a change to chunking, fusion or ranking makes
    search worse, this is what notices."""
    found = sum(
        1 for query, expected in GOLDEN.items()
        if expected in names(engine.search(query, limit=10))
    )
    recall = found / len(GOLDEN)
    assert recall == 1.0, f"recall@10 dropped to {recall:.0%}"


def test_both_retrievers_contribute(engine) -> None:
    """If everything is found by one retriever only, fusion is decoration."""
    response = engine.search("pump station commissioning", limit=10)
    sources = {tuple(result.sources) for result in response.results}
    assert any(len(s) > 1 for s in sources), "nothing was found by both retrievers"


def test_a_result_can_explain_itself(engine) -> None:
    """Trust comes from being able to ask why something matched."""
    response = engine.search("valve replacement", limit=5)
    explanations = {result.explain() for result in response.results}
    assert explanations
    assert all(text.strip() for text in explanations)


# --- 2 and 3: the budget ----------------------------------------------------

def test_warm_search_is_fast(engine) -> None:
    """p95 <300ms is the number the whole layer is arranged around. This is a
    small index, so it proves the pipeline has no gross inefficiency; the real
    number needs the real corpus."""
    engine.search("pump station")            # warm the path
    timings = sorted(engine.search("pump station", use_cache=False).elapsed_ms
                     for _ in range(20))
    p95 = timings[int(len(timings) * 0.95) - 1]
    assert p95 < 300, f"p95 was {p95:.0f}ms"


def test_the_cache_makes_a_repeat_search_cheaper(engine) -> None:
    class Cache(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

        def set(self, key, value):
            self[key] = value

    engine.cache = Cache()
    first = engine.search("pump station commissioning")
    second = engine.search("pump station commissioning")

    assert not first.from_cache
    assert second.from_cache
    assert names(second) == names(first)


# --- 4 and 5: reranking -----------------------------------------------------

def test_rerank_reorders_without_dropping_results(indexed) -> None:
    store, vectors, embedder, _root = indexed

    def scorer(_query, passages):
        return [float(len(p)) for p in passages]     # deterministic, and not the fused order

    engine = SearchEngine(store, vectors, embedder,
                          reranker=Reranker(scorer=scorer, top_n=5))
    try:
        plain = engine.search("valve", rerank=False, use_cache=False)
        ranked = engine.search("valve", rerank=True, use_cache=False)
        assert set(names(plain)) == set(names(ranked)), "reranking reorders, never filters"
        assert ranked.reranked
    finally:
        engine.close()


def test_toggling_rerank_needs_no_restart(indexed) -> None:
    store, vectors, embedder, _root = indexed
    engine = SearchEngine(store, vectors, embedder,
                          reranker=Reranker(scorer=lambda _q, p: [1.0] * len(p)))
    try:
        assert engine.search("valve", rerank=True, use_cache=False).reranked
        assert not engine.search("valve", rerank=False, use_cache=False).reranked
    finally:
        engine.close()


def test_a_missing_rerank_model_degrades_to_the_fused_order(indexed) -> None:
    """Criterion 5. An optional precision step must never be able to fail a
    search - returning nothing because a nice-to-have could not load is worse
    than never having had it."""
    store, vectors, embedder, _root = indexed

    def explode(_query, _passages):
        raise FileNotFoundError("the model was deleted mid-session")

    reranker = Reranker(scorer=explode)
    engine = SearchEngine(store, vectors, embedder, reranker=reranker)
    try:
        response = engine.search("valve replacement", use_cache=False)
        assert response.results, "the search still succeeded"
        assert not response.reranked
        assert not reranker.available, "and it will not be retried all session"
    finally:
        engine.close()


def test_a_reranker_returning_the_wrong_count_is_ignored(indexed) -> None:
    store, vectors, embedder, _root = indexed
    engine = SearchEngine(store, vectors, embedder,
                          reranker=Reranker(scorer=lambda _q, _p: [1.0]))
    try:
        assert engine.search("valve", use_cache=False).results
    finally:
        engine.close()


# --- 6: malformed queries ---------------------------------------------------

HOSTILE = ['"unclosed', "AND AND", "NOT", "((((", "*", "-", "^foo", "a NEAR/ b",
           "café naïve", "x" * 10_000, "", "   ", "type:", "after:notadate", "😀😀"]


@pytest.mark.parametrize("raw", HOSTILE)
def test_malformed_queries_never_crash(engine, raw: str) -> None:
    response = engine.search(raw)
    assert response is not None
    assert isinstance(response.results, list)


@pytest.mark.parametrize("raw", HOSTILE)
def test_malformed_queries_never_crash_the_interim_tier(engine, raw: str) -> None:
    assert isinstance(engine.interim(raw).results, list)


def test_an_unparseable_operator_is_surfaced_not_swallowed(engine) -> None:
    response = engine.search("after:nextthursday pump")
    assert response.parsed.unknown_operators == ("after:nextthursday",)
    assert response.results, "and the rest of the query still ran"


# --- 7: cache invalidation --------------------------------------------------

def test_the_cache_is_invalidated_by_indexing(indexed) -> None:
    """Criterion 7, and the difference between a cache and a lie: without the
    index generation in the key, a re-indexed file keeps returning text that has
    just been deleted."""
    store, vectors, embedder, root = indexed

    class Cache(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

        def set(self, key, value):
            self[key] = value

    engine = SearchEngine(store, vectors, embedder, cache=Cache())
    try:
        before = engine.search("catering arrangements")
        assert "misc.txt" in names(before)

        (root / "misc.txt").write_text(
            "Scaffolding permits for the tank farm inspection.", encoding="utf-8"
        )
        _age(root)
        Pipeline(store, vectors, embedder, PipelineConfig(
            walk=WalkConfig(roots=[root], extensions=frozenset({".txt"})), workers=1,
        )).run()

        after = engine.search("catering arrangements")
        assert not after.from_cache, "the generation changed, so the cached entry is dead"
        assert "misc.txt" not in names(after) or "catering" not in _text_of(store, after)
    finally:
        engine.close()


def _text_of(_store, response) -> str:
    return " ".join(result.text.lower() for result in response.results)


# --- 8: typed operators and the interim tier --------------------------------

def test_typed_filters_narrow_the_results(engine, indexed) -> None:
    _store, _vectors, _embedder, root = indexed
    (root / "note.md").write_text("Pump station notes in markdown.", encoding="utf-8")

    unfiltered = engine.search("pump station", use_cache=False)
    filtered = engine.search("pump station type:txt", use_cache=False)
    assert filtered.results
    assert all(name.endswith(".txt") for name in names(filtered))
    assert len(filtered.results) <= len(unfiltered.results)


def test_a_filter_only_query_lists_what_matches(engine) -> None:
    """`type:txt` with no terms is a browse, not an error."""
    response = engine.search("type:txt", use_cache=False)
    assert response.results
    assert all(name.endswith(".txt") for name in names(response))


def test_a_filter_that_matches_nothing_returns_nothing_calmly(engine) -> None:
    response = engine.search("pump station type:xlsx", use_cache=False)
    assert response.results == []


def test_the_interim_tier_touches_no_model(indexed) -> None:
    """As-you-type must not embed a query per keystroke burst."""
    store, vectors, _embedder, _root = indexed

    def explode(_texts):
        raise AssertionError("the interim tier must not embed")

    engine = SearchEngine(store, vectors, Embedder(dim=DIM, encoder=explode))
    try:
        response = engine.interim("pump st")
        assert response.interim
        assert response.results, "and it still found something"
    finally:
        engine.close()


def test_the_interim_tier_is_not_logged(indexed) -> None:
    """It is a glance at a half-typed word, not intent. Recording it would
    poison the data Layer 10 depends on."""
    store, vectors, embedder, _root = indexed
    engine = SearchEngine(store, vectors, embedder)
    try:
        engine.interim("pum")
        engine.interim("pump")
        assert store.recent_searches() == []

        engine.search("pump station")
        assert len(store.recent_searches()) == 1
    finally:
        engine.close()


# --- usage logging (the evidence Layer 10 needs) ----------------------------

def test_a_search_and_its_results_are_recorded(indexed) -> None:
    store, vectors, embedder, _root = indexed
    engine = SearchEngine(store, vectors, embedder)
    try:
        response = engine.search("valve replacement shutdown")
        assert response.search_id is not None

        logged = store.recent_searches()[0]
        assert logged["query"] == "valve replacement shutdown"
        assert logged["hits"] == len(response.results)

        rows = store.conn.execute(
            "SELECT chunk_id, rank FROM search_hits WHERE search_id = ? ORDER BY rank",
            (response.search_id,),
        ).fetchall()
        assert [row["rank"] for row in rows] == list(range(1, len(response.results) + 1))
    finally:
        engine.close()


def test_opening_a_result_is_recorded(indexed) -> None:
    """The single most valuable signal in the system."""
    store, vectors, embedder, _root = indexed
    engine = SearchEngine(store, vectors, embedder)
    try:
        response = engine.search("pump station")
        engine.record_open(response.search_id, response.results[0].chunk_id)

        opened = store.conn.execute(
            "SELECT opened, opened_at FROM search_hits WHERE search_id = ? AND chunk_id = ?",
            (response.search_id, response.results[0].chunk_id),
        ).fetchone()
        assert opened["opened"] == 1
        assert opened["opened_at"] is not None
    finally:
        engine.close()


def test_logging_failure_never_fails_a_search(indexed) -> None:
    """A tuning feature seven layers away must not be able to break the feature
    people actually use."""
    store, vectors, embedder, _root = indexed

    def explode(*_args, **_kwargs):
        raise RuntimeError("the usage table is locked")

    store.log_search = explode                          # type: ignore[method-assign]
    engine = SearchEngine(store, vectors, embedder)
    try:
        response = engine.search("pump station")
        assert response.results
        assert response.search_id is None
    finally:
        engine.close()


def test_the_usage_log_can_be_cleared(indexed) -> None:
    """A record of what someone searched on their own machine is theirs to erase."""
    store, vectors, embedder, _root = indexed
    engine = SearchEngine(store, vectors, embedder)
    try:
        engine.search("pump station")
        engine.search("valve replacement")
        assert len(store.recent_searches()) == 2

        assert store.clear_usage_log() == 2
        assert store.recent_searches() == []
        assert store.conn.execute("SELECT COUNT(*) FROM search_hits").fetchone()[0] == 0
    finally:
        engine.close()


# --- empty index ------------------------------------------------------------

def test_searching_before_any_index_run_is_calm(tmp_path: Path) -> None:
    store = SqliteStore(tmp_path / "empty.db").connect()
    vectors = VectorStore(tmp_path / "v", dim=DIM).connect()
    engine = SearchEngine(store, vectors, Embedder(dim=DIM, encoder=bag_of_words_encoder))
    try:
        response = engine.search("anything at all")
        assert response.results == []
    finally:
        engine.close()
        store.close()
        vectors.close()
