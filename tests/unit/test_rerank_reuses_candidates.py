r"""Work order 1h §5a: the reranked pass reranks the first pass's list.

Layer: L4

2026-10-10. With the reranker on, the window asks every search twice -
results at once, then the better order (`SearchWorker`, 2026-10-08). The
second call missed the result cache, because the rerank flag is rightly in
its key, so keyword search, the meaning vector, the picture lane and fusion
all ran again only to rerank a list the first call already had. These tests
count the retrievers through the real worker and the real engine: one of each
per search, and the same order as the two-retrieval path gave.
"""

from __future__ import annotations

import pytest

from app.search import keyword
from app.search.engine import SearchEngine
from app.ui.workers import SearchWorker


class _Embedder:
    """A meaning model that answers, so the vector half records no problem."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, texts):
        self.calls.extend(texts)
        return [[0.0] * 384 for _ in texts]

    def warm_up(self) -> None:
        pass


class _BrokenEmbedder(_Embedder):
    def embed(self, texts):
        self.calls.extend(texts)
        raise RuntimeError("no model in this test")


class _Vectors:
    """An empty vector table that counts how often it is asked."""

    def __init__(self) -> None:
        self.calls = 0

    def search(self, vector, *, k: int = 100, where=None):
        self.calls += 1
        return []


class _Reranker:
    """A fixed, deterministic reranker: reverses the fused order.

    Reversal is the order no fusion or blend step would produce by accident,
    so a reranked list that was reranked from the wrong input shows.
    """

    model_name = "fake-reranker"
    enabled = True
    available = True

    def __init__(self) -> None:
        self.calls = 0

    def warm_up(self) -> None:
        pass

    def rerank(self, query, hits, *, terms=()):
        self.calls += 1
        ranked = list(reversed(list(hits)))
        for position, hit in enumerate(ranked):
            hit["rerank_score"] = float(len(ranked) - position)
        return ranked


@pytest.fixture()
def store(tmp_path):
    from app.storage.sqlite_store import SqliteStore

    db = SqliteStore(tmp_path / "index.db").connect()
    for number, text in enumerate((
        "the boiler quote for the new flat",
        "boiler service history and boiler parts",
        "a boiler, a radiator and a thermostat",
        "annual boiler inspection notes",
    )):
        file_id = db.upsert_file(
            f"C:/home/boiler-{number}.txt", parent_dir="C:/home", ext="txt",
            size_bytes=1, mtime_ns=number + 1, status="INDEXED", source_kind="file",
        )
        db.replace_chunks(file_id, [{"ordinal": 0, "text": text}])
    yield db
    db.close()


@pytest.fixture()
def keyword_calls(monkeypatch):
    calls: list[int] = []
    real = keyword.search

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(keyword, "search", counted)
    return calls


def _engine(store, embedder=None, vectors=None, reranker=None):
    engine = SearchEngine(store, vectors or _Vectors(), embedder or _Embedder(),
                          reranker=reranker or _Reranker(), log_usage=False)
    engine._log_search = lambda *a, **k: None
    return engine


def _through_the_worker(engine, query: str = "boiler"):
    worker = SearchWorker(engine, query, tier="full", generation=1,
                          rerank=True, scope="all")
    landed: dict = {}
    worker.signals.progress.connect(lambda payload: landed.setdefault("early", payload[1]))
    worker.signals.finished.connect(lambda payload: landed.setdefault("final", payload[1]))
    worker.signals.failed.connect(lambda error: landed.setdefault("failed", error))
    worker.run()
    assert "failed" not in landed, landed.get("failed")
    return landed["early"], landed["final"]


def _order(response):
    return [(result.chunk_id, round(result.score, 9)) for result in response.results]


def test_a_reranked_search_retrieves_once(store, keyword_calls):
    embedder, vectors, reranker = _Embedder(), _Vectors(), _Reranker()
    engine = _engine(store, embedder, vectors, reranker)
    try:
        early, final = _through_the_worker(engine)
    finally:
        engine.close()

    assert early.results and not early.reranked
    assert final.reranked, "the second pass was not reranked"
    assert len(keyword_calls) == 1, "keyword search ran again for the reranked pass"
    assert vectors.calls == 1, "the meaning half ran again for the reranked pass"
    assert embedder.calls == ["boiler"], "the query was embedded twice"
    assert reranker.calls == 1


def test_the_reranked_order_is_the_order_two_retrievals_gave(store, keyword_calls):
    once = _engine(store)
    try:
        _early, reused = _through_the_worker(once)
    finally:
        once.close()

    # Today's path, by hand: a first pass, then a reranked pass that is
    # made to retrieve again (`use_cache=False` bypasses both caches).
    twice = _engine(store)
    try:
        twice.search("boiler", rerank=False, record=False)
        fresh = twice.search("boiler", rerank=True, use_cache=False)
    finally:
        twice.close()

    assert len(keyword_calls) == 3, "one retrieval for the worker's search, two by hand"
    assert _order(reused) == _order(fresh)
    assert [r.rerank_score for r in reused.results] == \
        [r.rerank_score for r in fresh.results]
    assert reused.keyword_count == fresh.keyword_count
    assert reused.vector_count == fresh.vector_count


def test_the_first_pass_is_left_as_it_was_gathered(store, keyword_calls):
    """The reranked pass works on a copy. Reranking the kept list in place
    would leave a reranked list for the next reranked pass to rerank again."""
    engine = _engine(store)
    try:
        engine.search("boiler", rerank=False, record=False)
        first = engine.search("boiler", rerank=True, use_cache=True)
        engine.cache.clear()                  # past the result cache, not the candidates
        second = engine.search("boiler", rerank=True, use_cache=True)
    finally:
        engine.close()

    assert len(keyword_calls) == 1
    assert _order(first) == _order(second)


def test_a_write_between_the_passes_retrieves_again(store, keyword_calls):
    """The generation is in the key: rows that may no longer exist are never
    reranked."""
    engine = _engine(store)
    try:
        engine.search("boiler", rerank=False, record=False)
        store.bump_generation()
        engine.search("boiler", rerank=True)
    finally:
        engine.close()

    assert len(keyword_calls) == 2


def test_a_degraded_first_pass_is_retrieved_again(store, keyword_calls):
    """A first pass whose meaning half failed is not reranked as it stood:
    the reranked pass tries the whole search again, as it always did."""
    engine = _engine(store, embedder=_BrokenEmbedder())
    try:
        engine.search("boiler", rerank=False, record=False)
        engine.search("boiler", rerank=True)
    finally:
        engine.close()

    assert len(keyword_calls) == 2


def test_nothing_is_kept_when_no_reranked_pass_can_follow(store, keyword_calls):
    """Chat, the command line and every surface with the reranker off pay
    nothing for this."""
    reranker = _Reranker()
    reranker.available = False
    engine = _engine(store, reranker=reranker)
    try:
        engine.search("boiler", rerank=False)
        assert len(engine._candidates) == 0
    finally:
        engine.close()
