"""Work order 0x item 5d: documents written back to back share one transaction.

Layer: L1 and L3

The indexer's writer used to open and commit a transaction for every document,
and bump the search cache's generation twice per document. Each statement costs
the writer a wait for Python's interpreter lock while the reading threads are
busy (see `Pipeline._begin_write_group` for the measurement), so documents that
arrive back to back are now written into one shared transaction, and the
generation moves once per transaction.

The speed is measured by `app.cli bench-pipeline`, not here. These tests hold
what must not change:

* the generation still moves whenever committed rows change;
* everything the embedding thread is given is committed first, so every passage
  ends up with its vector and every file INDEXED;
* a failure part-way through a group rolls the whole group back - nothing
  half-written is kept - and the next run picks it all up with nothing lost and
  nothing embedded twice.
"""

from __future__ import annotations

import pytest

from app.index.pipeline import Pipeline, PipelineConfig
from app.index.resources import ResourceLimits
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore
from tests.unit.test_stop_mid_batch import (
    FILES,
    Model,
    RecordingVectors,
    _corpus,
    _register_archive,  # noqa: F401 - autouse fixture: registers the test archive
)


# --- the store: one generation bump per transaction -------------------------

@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def _write_document(store, n: int) -> None:
    file_id = store.upsert_file(path=f"/doc{n}.txt", size_bytes=1, mtime_ns=1)
    store.replace_chunks(file_id, [{"text": f"passage {n}"}])


def test_a_batch_moves_the_generation_once_however_much_it_writes(store):
    before = store.generation
    with store.batch():
        for n in range(5):
            _write_document(store, n)
    assert store.generation == before + 1


def test_the_generation_moves_again_on_the_next_batch(store):
    """The "already bumped" note belongs to one transaction, not to the thread."""
    before = store.generation
    with store.batch():
        _write_document(store, 1)
    with store.batch():
        _write_document(store, 2)
    assert store.generation == before + 2


def test_a_batch_that_rolled_back_does_not_stop_the_next_one_bumping(store):
    before = store.generation
    with pytest.raises(RuntimeError), store.batch():
        _write_document(store, 1)
        raise RuntimeError("boom")
    assert store.generation == before, "a rolled-back bump must not be visible"
    with store.batch():
        _write_document(store, 2)
    assert store.generation == before + 1


def test_writes_outside_a_batch_still_move_it_every_time(store):
    before = store.generation
    _write_document(store, 1)                       # two writes, two transactions
    assert store.generation == before + 2


# --- the pipeline -------------------------------------------------------------

def _run(db, root, vectors, model, *, embed_batch=10, wrap_store=None):
    with SqliteStore(db) as store:
        if wrap_store is not None:
            wrap_store(store)
        config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1,
                                embed_batch=embed_batch,
                                limits=ResourceLimits(pause_on_battery=False, cpu_percent=0))
        pipeline = Pipeline(store, vectors, model.embedder(), config)
        model.pipeline = pipeline
        begins = []
        store.conn.set_trace_callback(
            lambda sql: begins.append(1) if sql.strip().upper().startswith("BEGIN") else None)
        try:
            stats = pipeline.run()
        finally:
            store.conn.set_trace_callback(None)
        unembedded = store.conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE embedded = 0").fetchone()[0]
        return stats, store.stats(), unembedded, len(begins)


def _indexed(counts: dict) -> int:
    return int((counts.get("files") or {}).get("INDEXED", 0))


def test_every_passage_handed_to_the_embedder_was_committed_first(tmp_path):
    """Small embedding batches, so hand-offs happen while documents are still
    arriving: the embedding thread marks passages embedded on its own
    connection, which only sees committed rows. Any passage it was given before
    its row was committed would be left `embedded = 0` here."""
    vectors = RecordingVectors()
    _stats, counts, unembedded, _begins = _run(
        tmp_path / "i.db", _corpus(tmp_path, archive=True), vectors, Model())

    assert unembedded == 0
    assert len(vectors.rows) == FILES
    assert counts["chunks_total"] == FILES


def test_messages_arriving_back_to_back_share_transactions(tmp_path):
    """One archive's messages arrive faster than they are written, so they are
    grouped: far fewer transactions on the writer's connection than messages."""
    vectors = RecordingVectors()
    _stats, _counts, _unembedded, begins = _run(
        tmp_path / "i.db", _corpus(tmp_path, archive=True), vectors, Model(),
        embed_batch=1000)

    assert begins < FILES, f"{begins} transactions for {FILES} messages"


def test_a_failure_part_way_through_a_group_keeps_nothing_half_written(tmp_path):
    """The 40th message's passages fail to write, after its file row was
    written in the same (shared) transaction. The group is rolled back whole;
    the group's other messages are written again each on their own, the failing
    one is retried once on its own and succeeds, and **the run goes on**
    (2026-10-08: it used to end on the error, which was non-negotiable 3 broken -
    the owner's ten-minute run died on one `.doc`); and the next run finds
    nothing to do, with nothing lost and nothing embedded twice
    (`RecordingVectors.add` asserts that)."""
    db, root = tmp_path / "i.db", _corpus(tmp_path, archive=True)
    vectors = RecordingVectors()

    def fail_on_the_fortieth(store):
        real = store.replace_chunks
        calls = {"n": 0}

        def replace_chunks(file_id, chunks):
            calls["n"] += 1
            if calls["n"] == 40:
                raise RuntimeError("disk said no")
            return real(file_id, chunks)

        store.replace_chunks = replace_chunks

    stats, _counts, _unembedded, _begins = _run(
        db, root, vectors, Model(), wrap_store=fail_on_the_fortieth)
    # The store said no once; the group was put back and the document retried
    # on its own, so nothing was skipped and nothing was lost.
    assert stats.skipped == 0 and stats.indexed == FILES, (
        "a store that refuses once is retried, not the end of the run")

    with SqliteStore(db) as store:
        orphans = store.conn.execute(
            "SELECT COUNT(*) FROM files f WHERE f.source_kind <> 'archive' "
            # 2026-10-04: not a name the scan listed before reading (PENDING,
            # `store.add_waiting_files`) - a placeholder, not a committed read.
            # 2026-10-08: nor the skipped message's own row, which is FAILED
            # with the reason and has no passages by design.
            "AND f.status NOT IN ('PENDING', 'FAILED') "
            "AND NOT EXISTS (SELECT 1 FROM chunks c WHERE c.file_id = f.id)").fetchone()[0]
    assert orphans == 0, "a message row was committed without its passages"

    _stats, counts, unembedded, _begins = _run(db, root, vectors, Model())
    assert unembedded == 0
    assert len(vectors.rows) == FILES, "a message lost its vector"
    assert counts["chunks_total"] == FILES
    assert _indexed(counts) >= FILES


# --- the writer's page cache (0x 5d) -------------------------------------------

def _cache_kib(store) -> int:
    return -int(store.conn.execute("PRAGMA cache_size").fetchone()[0])


def test_the_write_cache_is_a_quarter_of_the_file_within_its_bounds(store, monkeypatch):
    from app.storage import sqlite_store as module

    default = _cache_kib(store)
    assert store.size_write_cache() == module.WRITE_CACHE_FLOOR_KIB   # a new file is tiny
    assert _cache_kib(store) == module.WRITE_CACHE_FLOOR_KIB

    class TenGigabytes:
        """Stands in for the file's path: only `stat` is asked of it here."""

        def stat(self):
            return type("Stat", (), {"st_size": 10 * 1024 ** 3})()

    monkeypatch.setattr(store, "db_path", TenGigabytes())
    assert store.size_write_cache() == module.WRITE_CACHE_CEILING_KIB    # capped
    assert _cache_kib(store) == module.WRITE_CACHE_CEILING_KIB
    monkeypatch.undo()

    store.restore_write_cache()
    assert _cache_kib(store) == default


def test_the_write_cache_is_this_threads_alone(store):
    """Search and the window keep the default: the setting is per connection."""
    import threading

    default = _cache_kib(store)
    store.size_write_cache()
    seen = {}
    thread = threading.Thread(target=lambda: seen.setdefault("kib", _cache_kib(store)))
    thread.start()
    thread.join()
    assert seen["kib"] == default
    store.restore_write_cache()


def test_restoring_without_sizing_first_changes_nothing(store):
    default = _cache_kib(store)
    store.restore_write_cache()
    assert _cache_kib(store) == default


def test_a_run_gives_the_cache_back(tmp_path):
    vectors = RecordingVectors()
    db, root = tmp_path / "i.db", _corpus(tmp_path, archive=True)
    with SqliteStore(db) as store:
        default = _cache_kib(store)
        config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1,
                                limits=ResourceLimits(pause_on_battery=False, cpu_percent=0))
        model = Model()
        pipeline = Pipeline(store, vectors, model.embedder(), config)
        model.pipeline = pipeline
        pipeline.run()
        assert _cache_kib(store) == default
