r"""A stop lets a nearly-finished embedding batch finish, within a bound.

Layer: L3

2026-10-10, the owner's close at 07:03:02 (`logs/app/app_2026-10-10.log`):
"stopped mid-batch: 0 of 480 passages embedded and written" 1.4 s after the
stop - the batch is taken shortest first, so the passages one model call had
embedded were scattered across every file and none could be kept. A batch the
model can finish within `STOP_FINISH_BATCH_S` of the stop now finishes and is
written whole; one that cannot is cut exactly as before
(`test_stop_mid_batch.py` pins the cut itself).

The rest of that close - the run then sat in the vector-index phase until the
window ended it 34 s later - is the other half: a stopped run leaves the
table's index build, its forced rewrite and the word-index merge to the next.
"""

from __future__ import annotations

import threading
import time

from app.index import pipeline as module
from app.index.embedder import CPU_INFER_BATCH
from app.index.pipeline import Pipeline
from tests.unit.test_stop_mid_batch import FILES, Model, RecordingVectors, _corpus, _run


class SlowModel(Model):
    def __init__(self, delay_s: float, **kwargs) -> None:
        super().__init__(**kwargs)
        self.delay_s = delay_s

    def encode(self, texts):
        time.sleep(self.delay_s)
        return super().encode(texts)


def test_a_batch_that_can_finish_in_time_is_finished_and_written(tmp_path):
    vectors, model = RecordingVectors(), Model(stop_after_calls=1)
    _stats, counts, pipeline = _run(tmp_path / "i.db", _corpus(tmp_path, archive=False),
                                    vectors, model)

    assert sum(model.calls) == FILES, model.calls
    assert vectors.added == FILES and len(vectors.rows) == FILES
    assert int(counts["files"].get("INDEXED", 0)) == FILES
    assert not pipeline._embed_abandoned, "nothing was cut, so nothing is held back"


def test_a_batch_that_cannot_finish_in_time_is_cut_as_before(tmp_path, monkeypatch):
    # Four calls of 32 at 0.2 s each; after the first, the rest needs ~0.6 s
    # and the allowance is 0.3 s.
    monkeypatch.setattr(module, "STOP_FINISH_BATCH_S", 0.3)
    vectors, model = RecordingVectors(), SlowModel(0.2, stop_after_calls=1)
    _stats, _counts, pipeline = _run(tmp_path / "i.db", _corpus(tmp_path, archive=False),
                                     vectors, model)

    assert model.calls == [CPU_INFER_BATCH], model.calls
    assert pipeline._embed_abandoned


def _bare(*, stopping: bool, tail: bool = False, interrupted: bool = False) -> Pipeline:
    built = Pipeline.__new__(Pipeline)
    built._stop = threading.Event()
    if stopping:
        built._stop.set()
    built._interrupted = interrupted
    built._embed_abandoned = False
    built._tail_embedding = tail
    built._stop_seen_at = None
    return built


def test_no_stop_never_cuts():
    assert not _bare(stopping=False)._stop_cuts_embedding(time.monotonic(), 1, 10)


def test_the_allowance_is_shared_by_every_batch_after_the_stop(monkeypatch):
    monkeypatch.setattr(module, "STOP_FINISH_BATCH_S", 10.0)
    built = _bare(stopping=True)
    now = time.monotonic()
    # Half done in one second: one more second needed, well inside ten.
    assert not built._stop_cuts_embedding(now - 1.0, 50, 100)
    # The stop was first seen eleven seconds ago: nothing more is waited for.
    built._stop_seen_at = now - 11.0
    assert built._stop_cuts_embedding(now - 1.0, 99, 100)


def test_once_a_batch_was_cut_every_later_one_is_cut():
    built = _bare(stopping=True)
    built._embed_abandoned = True
    assert built._stop_cuts_embedding(time.monotonic(), 50, 100)


def test_the_run_ends_own_stop_flag_does_not_cut_its_photo_passages():
    """Every run's end sets the stop flag to unwind its threads; the photo
    passes' embedding after it answers only to the person's Stop."""
    assert not _bare(stopping=True, tail=True)._stop_cuts_embedding(time.monotonic(), 1, 10)
    assert _bare(stopping=True, tail=True, interrupted=True)._stopping_for_embed()


def test_a_stopped_run_leaves_the_vector_rewrite_and_the_merge_to_the_next(tmp_path):
    from tests.unit.test_media_tail_end import Vectors

    from app.index.embedder import Embedder
    from app.index.pipeline import PipelineConfig
    from app.index.resources import ResourceLimits
    from app.index.walker import WalkConfig
    from app.storage.sqlite_store import SqliteStore

    merged = []
    vectors = Vectors()

    def encode(texts):
        pipeline.request_stop()
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    root = _corpus(tmp_path, archive=False)
    with SqliteStore(tmp_path / "i.db") as store:
        store.optimize_fts = lambda: merged.append(1) or True
        config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1, embed_batch=10,
                                limits=ResourceLimits(pause_on_battery=False, cpu_percent=0),
                                bulk_fts="on")
        pipeline = Pipeline(store, vectors, Embedder(dim=4, encoder=encode), config)
        pipeline.run()
        dirty = store.get_state("fts_dirty")
    assert pipeline._interrupted
    assert vectors.calls == {}, f"a stopped run rewrote the vector table: {vectors.calls}"
    assert merged == [], "a stopped run merged the word index"
    assert not dirty, "the triggers a bulk load suspended were not put back"
