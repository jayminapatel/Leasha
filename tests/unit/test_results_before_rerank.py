r"""The Search tab draws results at once, then redraws them when the reranker lands.

Layer: L5

2026-10-08, the owner. Measured on the owner's index, 15 queries x 3, with
nothing else running: the search itself 153 ms median; the reranker another
818 ms with MiniLM and 2,675 ms with bge-reranker-base. So a full search with
the reranker on answers twice under one generation - `progress` with the rows
before reranking, `finished` with the reranked ones - and the view draws both,
in place. The first pass is not logged as a search of its own.
"""

from __future__ import annotations

from app.search.engine import SearchResponse
from app.ui.presenter.search import draws_answer
from app.ui.workers import SearchWorker


class Reranker:
    def __init__(self, available: bool) -> None:
        self.available = available


class Engine:
    def __init__(self, *, reranker_on: bool) -> None:
        self.reranker = Reranker(reranker_on)
        self.calls: list[dict] = []

    def search(self, raw, **options):
        self.calls.append(dict(options))
        return SearchResponse()

    def interim(self, raw, **options):
        self.calls.append({"interim": True, **options})
        return SearchResponse()


def _run(engine: Engine, *, tier: str = "full", **options) -> list[tuple[str, int]]:
    worker = SearchWorker(engine, "boiler quote", tier=tier, generation=7, **options)
    landed: list[tuple[str, int]] = []
    worker.signals.progress.connect(lambda payload: landed.append(("early", payload[0])))
    worker.signals.finished.connect(lambda payload: landed.append(("finished", payload[0])))
    worker.run()
    return landed


def test_a_reranked_search_answers_first_without_the_reranker():
    engine = Engine(reranker_on=True)
    landed = _run(engine, rerank=True, scope="all")

    assert landed == [("early", 7), ("finished", 7)], landed
    first, second = engine.calls
    assert first["rerank"] is False and first["record"] is False, first
    assert second["rerank"] is True and "record" not in second, second


def test_no_early_answer_when_nothing_would_be_reranked():
    for engine, options in (
        (Engine(reranker_on=True), {"rerank": False}),     # the switch is off
        (Engine(reranker_on=False), {"rerank": True}),     # no model loaded
    ):
        assert _run(engine, scope="all", **options) == [("finished", 7)]
        assert len(engine.calls) == 1


def test_the_interim_tier_is_never_doubled():
    engine = Engine(reranker_on=True)
    assert _run(engine, tier="interim", scope="all") == [("finished", 7)]
    assert len(engine.calls) == 1


def test_which_answers_the_view_draws():
    # A newer search is drawn; an older one never is.
    assert draws_answer(8, 7, 7, early=False)
    assert not draws_answer(6, 7, 7, early=False)
    # Early then finished, under one generation: both drawn.
    assert draws_answer(8, 7, 7, early=True)
    assert draws_answer(8, 8, 7, early=False)
    # Finished first, early late: the early one must not replace it.
    assert not draws_answer(8, 8, 8, early=True)


def test_a_search_left_unrecorded_is_not_logged(tmp_path):
    from app.search.engine import SearchEngine
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore
    from tests.unit.conftest import _NoModel

    with SqliteStore(tmp_path / "i.db") as store, \
            VectorStore(tmp_path / "vectors", dim=384) as vectors:
        file_id = store.upsert_file(r"D:\boiler.txt", size_bytes=1, mtime_ns=1,
                                    status="INDEXED", ext="txt")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": "the boiler quote"}])
        engine = SearchEngine(store, vectors, _NoModel())
        logged: list[str] = []
        engine._log_search = lambda raw, *a, **k: logged.append(raw) or 1
        try:
            # Past the result cache: a cached answer is never logged at all.
            early = engine.search("boiler", rerank=False, record=False, use_cache=False)
            final = engine.search("boiler", rerank=False, use_cache=False)
        finally:
            engine.close()

    assert early.results, "the unrecorded search found nothing"
    assert early.search_id is None and final.search_id == 1
    assert logged == ["boiler"], "the early pass was logged as a search of its own"
