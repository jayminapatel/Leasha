r"""Index tuning §6b: a dedicated thread carries the embed and the vector write.

Layer: L3

**What moved, and what did not.** The consumer still extracts, writes chunks
to SQLite and gathers a batch exactly as before; once a batch reaches
`embed_batch` it is handed to a second thread instead of being embedded and
written in place, and the consumer starts gathering the next batch
immediately. `_embed_pending` itself - the actual embed, the vector-store
write, `mark_embedded`, `mark_indexed_many` - is unchanged; only the thread
that calls it, and when, is new. See the module docstring in
`app/index/pipeline.py` and `_feed_async`/`_feed_sync`/`_feed_worker`.

**The anti-P1 rule applies to a thread exactly as it does to a setting**: a
feeder thread that exists and never actually overlaps anything is the same
defect as a control nobody reads. `test_the_next_batch_is_written_while_the_
previous_one_embeds` is the measurement, not an assumption - it blocks the
model deliberately and checks that SQLite kept moving anyway.

**The M6 fix has a sharper edge once embedding is asynchronous.** Before §6b,
"if pending_vectors: flush" was safe because nothing could still be running
once that line returned. Once a batch may already be in flight on the feeder
thread *before* the marker item even arrives, only a queue join - not the
emptiness of the local list - can say whether it has finished.
`test_a_markers_vectors_are_not_still_in_flight_when_it_is_written` pins
exactly that.
"""

from __future__ import annotations

import threading
import time
import zipfile
from pathlib import Path

import pytest

from app.core.errors import AppErrorException, make_error
from app.index.embedder import Embedder
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore


class FakeVectors:
    """A vector store minimal enough to drive a real `Pipeline` with."""

    def __init__(self) -> None:
        self.rows: dict[int, int] = {}

    def ensure_table(self) -> None:
        pass

    def delete_by_file_ids(self, file_ids) -> None:
        wanted = [int(one) for one in file_ids]
        for chunk_id, file_id in list(self.rows.items()):
            if file_id in wanted:
                del self.rows[chunk_id]

    def add(self, *, chunk_ids, file_ids, vectors) -> int:
        for chunk_id, file_id in zip(chunk_ids, file_ids, strict=True):
            self.rows[int(chunk_id)] = int(file_id)
        return len(list(chunk_ids))

    def maybe_compact(self, **_kwargs) -> bool:
        return False

    def maybe_create_index(self, **_kwargs) -> bool:
        return False

    def count(self) -> int:
        return len(self.rows)


def _corpus(root: Path, files: int) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        (root / f"f{index}.txt").write_text(
            f"pump station {index} commissioning report", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# The measurement: does the consumer actually keep moving?
# ---------------------------------------------------------------------------


def test_the_next_batch_is_written_while_the_previous_one_embeds(tmp_path):
    r"""**The whole point of §6b, measured rather than assumed.**

    The first batch's embed is deliberately blocked. On the old synchronous
    path nothing else could happen until it returned - the next batch's
    chunks could not reach SQLite before the model did. With the feeder
    thread doing the embedding, the consumer keeps writing behind it.
    """
    root = _corpus(tmp_path / "docs", files=8)

    embed_started = threading.Event()
    embed_may_return = threading.Event()
    calls: list[int] = []

    def slow_encode(texts):
        calls.append(len(texts))
        if len(calls) == 1:
            embed_started.set()
            assert embed_may_return.wait(timeout=5), "the test never released it"
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    embedder = Embedder(dim=4, encoder=slow_encode)
    vectors = FakeVectors()
    store = SqliteStore(tmp_path / "index.db").connect()
    config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1, embed_batch=2)
    pipeline = Pipeline(store, vectors, embedder, config)

    run_thread = threading.Thread(target=pipeline.run, daemon=True)
    run_thread.start()
    try:
        assert embed_started.wait(timeout=5), "the first batch never reached the model"

        # The first batch's embed is still blocked. If the consumer had to
        # wait for it - the behaviour before §6b - nothing past the first two
        # files could possibly be in SQLite yet.
        deadline = time.monotonic() + 2.0
        written = 0
        while time.monotonic() < deadline:
            written = store.conn.execute(
                "SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
            if written > 2:
                break
            time.sleep(0.02)

        assert written > 2, (
            "no further chunks reached SQLite while the first batch's embed "
            "was still blocked - the consumer waited on it, exactly as "
            "before the feeder thread existed")
    finally:
        embed_may_return.set()
        run_thread.join(timeout=10)

    assert not run_thread.is_alive(), "the run did not finish"
    store.close()


# ---------------------------------------------------------------------------
# The M6 fix, once embedding can already be in flight
# ---------------------------------------------------------------------------


def test_a_markers_vectors_are_not_still_in_flight_when_it_is_written(tmp_path):
    r"""**The exact fix `_write_marker`'s flush needed.**

    A batch can already be running on the feeder thread when the marker item
    arrives, handed off *before* the marker was even seen - so checking
    whether the local `pending_vectors` list is currently empty proves
    nothing. Only waiting on the feeder queue does. A real two-member
    archive with `embed_batch=1` puts each message in its own batch, and a
    deliberately slow second embed gives a real window for the marker to
    race it in, if the fix were ever lost.
    """
    root = tmp_path / "docs"
    root.mkdir()
    archive = root / "backup.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.txt", "pump station alpha commissioning report")
        zf.writestr("b.txt", "pump station bravo commissioning report")

    order: list[str] = []
    lock = threading.Lock()

    def encode(texts):
        time.sleep(0.15)
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    embedder = Embedder(dim=4, encoder=encode)
    vectors = FakeVectors()
    real_add = vectors.add

    def logged_add(**kwargs):
        result = real_add(**kwargs)
        with lock:
            order.append("add")
        return result

    vectors.add = logged_add                          # type: ignore[method-assign]

    store = SqliteStore(tmp_path / "index.db").connect()
    # Dated note, 2026-10-08: with "Make text searchable first" on, a marker no
    # longer waits for vectors - a passage without one is `embedded = 0` and the
    # next run fills it (`Pipeline._settle`). This rule is the classic mode's,
    # so the test runs with it off; `test_text_first.py` covers the other.
    config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1, embed_batch=1,
                            two_phase=False)
    pipeline = Pipeline(store, vectors, embedder, config)

    original_marker = pipeline._write_marker

    def logged_marker(item):
        with lock:
            order.append("marker")
        return original_marker(item)

    pipeline._write_marker = logged_marker             # type: ignore[method-assign]

    stats = pipeline.run()

    assert stats.indexed == 2, "both archive members should have been written"
    assert order == ["add", "add", "marker"], (
        "the marker was written while a batch handed off earlier was still "
        f"in flight: {order}")
    store.close()


# ---------------------------------------------------------------------------
# An embed failure still ends the run, now that most batches are async
# ---------------------------------------------------------------------------


def test_an_embed_failure_handed_off_asynchronously_still_ends_the_run(tmp_path):
    r"""**The same guarantee as before §6b, now that most batches never block
    the consumer at all.** An error on a batch the consumer was not waiting
    on must not be swallowed just because nothing was watching it directly
    when it happened - it has to surface by the time `run()` returns, exactly
    as `test_embedding_gap.py` already requires for the synchronous case.
    """
    root = _corpus(tmp_path / "docs", files=4)

    def explode(_texts):
        raise AppErrorException(make_error(
            "ERR_MODEL_LOAD", "index.embedder", details="deliberate"))

    embedder = Embedder(dim=4, encoder=explode)
    vectors = FakeVectors()
    store = SqliteStore(tmp_path / "index.db").connect()
    config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1, embed_batch=1)
    pipeline = Pipeline(store, vectors, embedder, config)

    with pytest.raises(AppErrorException):
        pipeline.run()

    store.close()


def test_a_run_with_only_one_batch_never_starts_the_feeder_for_nothing(tmp_path):
    """A corpus smaller than `embed_batch` takes only the final, blocking
    flush - the async path never fires, and that must still work."""
    root = _corpus(tmp_path / "docs", files=3)

    embedder = Embedder(dim=4, encoder=lambda texts: [
        [1.0, 0.0, 0.0, 0.0] for _ in texts])
    vectors = FakeVectors()
    store = SqliteStore(tmp_path / "index.db").connect()
    config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1, embed_batch=256)
    pipeline = Pipeline(store, vectors, embedder, config)

    stats = pipeline.run()

    assert stats.indexed == 3
    assert vectors.count() == stats.chunks
    store.close()


def test_the_feeder_thread_ends_when_the_run_does(tmp_path):
    """No thread left running after `run()` returns - a daemon thread that
    outlives the run it belonged to is a leak the next run should not have
    to share a queue with."""
    root = _corpus(tmp_path / "docs", files=2)

    embedder = Embedder(dim=4, encoder=lambda texts: [
        [1.0, 0.0, 0.0, 0.0] for _ in texts])
    vectors = FakeVectors()
    store = SqliteStore(tmp_path / "index.db").connect()
    config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1, embed_batch=1)
    pipeline = Pipeline(store, vectors, embedder, config)

    before = {t.name for t in threading.enumerate()}
    pipeline.run()
    after_names = {t.name for t in threading.enumerate()} - before

    assert "feeder" not in after_names
    store.close()
