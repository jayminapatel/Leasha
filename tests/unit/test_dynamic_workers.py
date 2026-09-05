r"""Index tuning §6g: extraction workers may grow past the static count.

Layer: L3

**The static count stays the floor of a range, never the answer.**
`PipelineConfig.worker_ceiling` defaults to `0`, which disables growth
entirely - exactly today's behaviour for every existing caller, since none
of them set it. Only a caller that explicitly asks (§6g's own wording: "the
governor may raise... toward the envelope ceiling") gets anything different,
and even then only when the run is actually extraction-bound and the model is
not genuinely mid-batch on a processor right now.

**A dynamically-started worker has no `_STOP` marker of its own unless one is
enqueued for it.** `_produce` only ever sends as many as the *static* worker
count, so the growth path has to enqueue one more every time it starts a
thread - `test_a_real_extraction_bound_run_grows_and_still_indexes_correctly`
is the test that would have caught it if it did not: every file the walker
saw is still indexed, whichever thread happened to read it.

`test_workers_never_grow_without_a_ceiling` and
`test_growth_never_exceeds_the_ceiling` are the two halves of the anti-P1
rule this project keeps re-learning: not only that raising works, but that
*not* asking for it changes nothing, and that asking for less than infinity
is respected.
"""

from __future__ import annotations

import queue
import time
from pathlib import Path

import app.index.pipeline as pipeline_module
from app.index.embedder import Embedder
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.stages import WAITING
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore


class FakeVectors:
    def __init__(self) -> None:
        self.rows: dict[int, int] = {}

    def ensure_table(self) -> None:
        pass

    def delete_by_file_ids(self, file_ids) -> None:
        wanted = [int(one) for one in file_ids]
        for cid, fid in list(self.rows.items()):
            if fid in wanted:
                del self.rows[cid]

    def add(self, *, chunk_ids, file_ids, vectors) -> int:
        for cid, fid in zip(chunk_ids, file_ids, strict=True):
            self.rows[int(cid)] = int(fid)
        return len(list(chunk_ids))

    def maybe_compact(self, **_k) -> bool:
        return False

    def maybe_create_index(self, **_k) -> bool:
        return False

    def count(self) -> int:
        return len(self.rows)


def _corpus(root: Path, files: int) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        (root / f"f{index}.txt").write_text(
            f"pump station {index} commissioning report", encoding="utf-8")
    return root


def _pipeline(tmp_path: Path, *, worker_ceiling: int = 0, workers: int = 1
             ) -> tuple[Pipeline, SqliteStore]:
    (tmp_path / "docs").mkdir(parents=True, exist_ok=True)
    embedder = Embedder(dim=4, encoder=lambda texts: [
        [1.0, 0.0, 0.0, 0.0] for _ in texts])
    vectors = FakeVectors()
    store = SqliteStore(tmp_path / "index.db").connect()
    config = PipelineConfig(
        walk=WalkConfig(roots=[tmp_path / "docs"]), workers=workers,
        worker_ceiling=worker_ceiling, min_free_gb=0, required_free_gb=0)
    return Pipeline(store, vectors, embedder, config), store


def _empty_queues() -> tuple["queue.PriorityQueue", "queue.Queue"]:
    """A real `work`/`results` pair, empty - so a thread `_maybe_grow_workers`
    actually starts finds its `_STOP` marker waiting and exits cleanly,
    exactly as it would mid-run."""
    return queue.PriorityQueue(maxsize=64), queue.Queue(maxsize=64)


def _join_dynamic(pipeline: Pipeline) -> None:
    for worker in pipeline._dynamic_workers:                # noqa: SLF001
        worker.join(timeout=2)


# ---------------------------------------------------------------------------
# The decision itself, unit-tested against a controlled clock
# ---------------------------------------------------------------------------


def test_workers_never_grow_without_a_ceiling(tmp_path):
    """`worker_ceiling=0` is every existing caller. Nothing here may change
    what they get."""
    pipeline, store = _pipeline(tmp_path, worker_ceiling=0)
    pipeline._clock.add(WAITING, 100.0)          # about as extraction-bound as it gets
    work, results = _empty_queues()

    pipeline._maybe_grow_workers(work, results, workers=[1])

    assert pipeline._dynamic_workers == []
    store.close()


def test_a_balanced_run_does_not_grow(tmp_path):
    """`waiting` has to actually dominate - a run already spending most of
    its time on write or embed has nothing to gain from more readers."""
    pipeline, store = _pipeline(tmp_path, worker_ceiling=8)
    pipeline._clock.add(WAITING, 10.0)
    pipeline._clock.add("embed", 40.0)
    pipeline._clock.add("write", 50.0)
    work, results = _empty_queues()

    pipeline._maybe_grow_workers(work, results, workers=[1])

    assert pipeline._dynamic_workers == []
    store.close()


def test_growth_never_exceeds_the_ceiling(tmp_path):
    """The number in the setting is a ceiling, not a suggestion."""
    pipeline, store = _pipeline(tmp_path, worker_ceiling=2)
    pipeline._clock.add(WAITING, 100.0)
    work, results = _empty_queues()

    pipeline._maybe_grow_workers(work, results, workers=[1])
    pipeline._last_growth = 0.0                  # bypass the cooldown for this test
    pipeline._maybe_grow_workers(work, results, workers=[1])
    pipeline._last_growth = 0.0
    pipeline._maybe_grow_workers(work, results, workers=[1])   # would be a 3rd

    assert len(pipeline._dynamic_workers) == 1, (
        "workers=[1] already counts as one static worker, so the ceiling of "
        "2 allows exactly one more, not two")
    _join_dynamic(pipeline)
    store.close()


def test_growth_respects_its_own_cooldown(tmp_path):
    """Checked at every checkpoint - a corpus of tiny files checkpoints
    often, and that must not turn into a thread storm."""
    pipeline, store = _pipeline(tmp_path, worker_ceiling=8)
    pipeline._clock.add(WAITING, 100.0)
    work, results = _empty_queues()

    pipeline._maybe_grow_workers(work, results, workers=[1])
    pipeline._maybe_grow_workers(work, results, workers=[1])
    pipeline._maybe_grow_workers(work, results, workers=[1])

    assert len(pipeline._dynamic_workers) == 1, (
        "three attempts inside the cooldown window must add at most one worker")
    _join_dynamic(pipeline)
    store.close()


def test_growth_is_skipped_while_the_model_is_mid_batch_on_a_processor(tmp_path):
    """The one condition unique to this item: a new reader would compete
    with the model for the same cores, so it waits its turn."""
    pipeline, store = _pipeline(tmp_path, worker_ceiling=8)
    pipeline._clock.add(WAITING, 100.0)
    pipeline._embedding_now.set()
    work, results = _empty_queues()

    pipeline._maybe_grow_workers(work, results, workers=[1])

    assert pipeline._dynamic_workers == [], (
        "grew a worker while the model was mid-batch on the processor")
    store.close()


def test_growth_proceeds_on_a_graphics_card_even_mid_batch(tmp_path):
    """The device check exists because CPU contention is the reason to
    wait - a graphics card embed does not compete with extraction for cores,
    so there is nothing here to defer to."""
    from app.index import backends

    pipeline, store = _pipeline(tmp_path, worker_ceiling=8)
    pipeline._clock.add(WAITING, 100.0)
    pipeline._embedding_now.set()
    pipeline.embedder.choice = backends.Choice(
        device=backends.GPU, providers=(), why="test")
    work, results = _empty_queues()

    pipeline._maybe_grow_workers(work, results, workers=[1])

    assert len(pipeline._dynamic_workers) == 1
    _join_dynamic(pipeline)
    store.close()


def test_a_full_work_queue_holds_off_growth_rather_than_losing_a_stop_marker(
    tmp_path,
):
    r"""**The fix this file exists to pin.** If the `_STOP` marker could not
    be enqueued, the worker must not start either - a thread reading from
    `work` with no marker addressed to it is exactly the bug that lost a
    file in a real run before this was caught."""
    pipeline, store = _pipeline(tmp_path, worker_ceiling=8)
    pipeline._clock.add(WAITING, 100.0)
    pipeline._expected_stops = 1                 # what run() would have set for one worker
    work = queue.PriorityQueue(maxsize=1)
    work.put((1, 1, "occupying the one slot", None))
    results = queue.Queue(maxsize=8)

    pipeline._maybe_grow_workers(work, results, workers=[1])

    assert pipeline._dynamic_workers == []
    assert pipeline._expected_stops == 1, "no marker went in, so nothing should count"
    store.close()


def test_workers_grown_is_recorded_only_when_it_happened(tmp_path):
    """A run where the ceiling was never reached says nothing extra - the
    same "empty means nothing to report" convention `IndexStats` uses
    everywhere else."""
    _corpus(tmp_path / "docs", files=2)
    pipeline, store = _pipeline(tmp_path, worker_ceiling=0)

    stats = pipeline.run()

    assert "workers_grown_to" not in stats.resolved
    store.close()


# ---------------------------------------------------------------------------
# A real run: extraction-bound, and it actually grows
# ---------------------------------------------------------------------------


def test_a_real_extraction_bound_run_grows_and_still_indexes_correctly(
    tmp_path, monkeypatch,
):
    r"""**The anti-P1 test.** Not that the decision function agrees with
    itself, but that a real `Pipeline.run()`, genuinely bottlenecked on
    extraction, actually starts another reader - and that everything it
    reads is still indexed correctly, because a dynamically-started worker
    reads from and writes to exactly the same shared queues as the static
    ones, and carries its own `_STOP` marker so `_consume` cannot finish
    counting before it has.
    """
    _corpus(tmp_path / "docs", files=24)

    real_extract = pipeline_module.extract

    def slow_extract(path, **kwargs):
        # `**kwargs` matters: work order 202626270509's resume_from cursor
        # (`_extract_stream` in pipeline.py) always calls `extract(path,
        # resume_from=...)` now, even for a fresh run where it is 0 - a
        # narrower stand-in here raised `TypeError: unexpected keyword
        # argument 'resume_from'` for every one of the 24 files, which
        # `mark_skipped` recorded as `ERR_UNEXPECTED` and this test's own
        # `stats.indexed == 24` assertion then reported, correctly, as "a
        # file went missing" - not the dynamic-worker bug that message
        # describes, but a stand-in that had fallen behind the real
        # function's signature.
        time.sleep(0.05)
        return real_extract(path, **kwargs)

    monkeypatch.setattr(pipeline_module, "extract", slow_extract)

    pipeline, store = _pipeline(tmp_path, worker_ceiling=4, workers=1)
    pipeline.config.checkpoint_every = 1
    pipeline.config.checkpoint_seconds = 0.05
    pipeline._last_growth = -1000.0              # no cooldown wait on the first check

    stats = pipeline.run()

    assert stats.indexed == 24, (
        "a file went missing - almost certainly a dynamically-started "
        "worker whose _STOP marker never existed")
    assert stats.chunks == stats.vectors, "every chunk still got a vector"
    assert len(pipeline._dynamic_workers) >= 1, (
        "a run spending most of its time waiting on a single slow reader "
        "never grew a second one")
    assert stats.resolved.get("workers_grown_to", 0) > 1
    store.close()
