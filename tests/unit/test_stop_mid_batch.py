"""Work order 0u, item 6e: a stop that arrives mid-batch stops within one model call.

`_consume` gathers up to `embed_batch` passages (256) before embedding, and the
model was then given all of them - so a stop asked for while a batch was gathered
waited for the whole thing (256 passages, most of a minute on this machine). The
batch is now embedded a model call at a time (`CPU_INFER_BATCH`, 32) with the stop
flag looked at between calls.

Stopping early is only safe if nothing *claims* what was not written, so the tests
hold the other half of the change as tightly as the first:

* vectors are written for whole files only, and only those files are INDEXED;
* no completion marker is written for an archive whose batch was cut;
* no resume position is persisted past a message whose vector was abandoned;
* and a resume loses nothing and duplicates nothing.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from app.extract import base
from app.extract.base import Document
from app.index.embedder import CPU_INFER_BATCH, Embedder, l2_normalise
from app.index.pipeline import (
    RESUME_POSITION_META_KEY, RESUME_STATE_PREFIX, Pipeline, PipelineConfig, content_hash,
)
from app.index.resources import ResourceLimits
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore
from tests.unit.test_index_freshness import write_aged

FILES = 100


class RecordingVectors:
    """A vector store that behaves like the real one where it matters here:
    `delete_by_file_ids` removes, `add` inserts, and a chunk id is a key - so a
    duplicated or lost row is visible."""

    def __init__(self) -> None:
        self.rows: dict[int, int] = {}          # chunk id -> file id
        self.added = 0

    def delete_by_file_ids(self, file_ids) -> None:
        gone = set(file_ids)
        self.rows = {cid: fid for cid, fid in self.rows.items() if fid not in gone}

    def add(self, *, chunk_ids, file_ids, vectors, **_kw) -> int:
        assert len(vectors) == len(chunk_ids), "vectors and chunks out of step"
        for cid, fid in zip(chunk_ids, file_ids):
            assert cid not in self.rows, f"chunk {cid} was embedded twice"
            self.rows[cid] = fid
        self.added += len(chunk_ids)
        return len(chunk_ids)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class Model:
    """A fake embedder that can ask its own pipeline to stop, once."""

    def __init__(self, stop_after_calls: "int | None" = None) -> None:
        self.calls: list[int] = []
        self.stop_after_calls = stop_after_calls
        self.pipeline: "Pipeline | None" = None

    def encode(self, texts):
        self.calls.append(len(texts))
        if self.stop_after_calls is not None and len(self.calls) >= self.stop_after_calls:
            self.pipeline.request_stop()
        return [l2_normalise([math.sin(abs(hash(t)) % 100 + i) for i in range(8)]) for t in texts]

    def embedder(self) -> Embedder:
        return Embedder(dim=8, encoder=self.encode)


class ResumableArchive:
    """An mbox stand-in: many messages, `supports_resume`, and a position in meta.

    `reads_externally` is False, as for a real mbox: the file is hashed, and the
    resume position is keyed by that hash.
    """

    name = "resumable-archive"
    extensions = (".stopmbox",)
    reads_externally = False
    supports_resume = True
    opened = 0

    def extract(self, path: Path, *, resume_from: int = 0):
        type(self).opened += 1
        for n in range(resume_from, FILES):
            yield Document(
                path=path,
                text=f"Message number {n} about the Barnsley dairy audit.",
                source_kind="pst_message",
                virtual_path=f"mbox://{path.name}/E{n}",
                meta={RESUME_POSITION_META_KEY: n, "subject": f"m{n}"},
            )


@pytest.fixture(autouse=True)
def _register_archive():
    before = dict(base.REGISTRY)
    base.REGISTRY.pop(".stopmbox", None)
    ResumableArchive.opened = 0
    base.register(ResumableArchive())
    yield
    base.REGISTRY.clear()
    base.REGISTRY.update(before)


def _corpus(tmp_path: Path, *, archive: bool) -> Path:
    root = tmp_path / "docs"
    root.mkdir()
    if archive:
        write_aged(root / "mail.stopmbox", b"x" * 4096)
    else:
        for n in range(FILES):
            write_aged(root / f"doc{n:03d}.txt", f"Document number {n} about the dairy audit.")
    return root


def _run(db: Path, root: Path, vectors: RecordingVectors, model: Model):
    with SqliteStore(db) as store:
        config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1,
                                embed_batch=1000,      # one batch: nothing flushes early
                                limits=ResourceLimits(pause_on_battery=False, cpu_percent=0))
        pipeline = Pipeline(store, vectors, model.embedder(), config)
        model.pipeline = pipeline
        stats = pipeline.run()
        return stats, store.stats(), pipeline


def _status(counts: dict, name: str) -> int:
    return int((counts.get("files") or {}).get(name, 0))


def test_a_stop_mid_batch_costs_one_model_call_not_the_batch(tmp_path) -> None:
    """Fails on the code as it was: all 100 passages were embedded before it looked."""
    vectors, model = RecordingVectors(), Model(stop_after_calls=1)
    _run(tmp_path / "i.db", _corpus(tmp_path, archive=False), vectors, model)

    assert model.calls == [CPU_INFER_BATCH], model.calls
    assert vectors.added == CPU_INFER_BATCH


def test_only_files_whose_vectors_were_written_are_marked_indexed(tmp_path) -> None:
    vectors, model = RecordingVectors(), Model(stop_after_calls=1)
    _stats, counts, _p = _run(tmp_path / "i.db", _corpus(tmp_path, archive=False), vectors, model)

    assert _status(counts, "INDEXED") == CPU_INFER_BATCH
    assert _status(counts, "INDEXED") == len(vectors.rows), "an INDEXED file with no vector"


def test_a_resume_after_a_stop_loses_nothing_and_embeds_nothing_twice(tmp_path) -> None:
    db, root = tmp_path / "i.db", _corpus(tmp_path, archive=False)
    vectors = RecordingVectors()
    _run(db, root, vectors, Model(stop_after_calls=1))

    _stats, counts, _p = _run(db, root, vectors, Model())        # resumed; `add` asserts no repeats

    assert _status(counts, "INDEXED") == FILES
    assert counts["chunks_total"] == FILES
    assert len(vectors.rows) == FILES, "a passage lost its vector"
    assert vectors.added == FILES, "a passage was embedded twice across the two runs"


def test_a_stop_that_never_comes_embeds_everything_in_smaller_calls(tmp_path) -> None:
    """Slicing must not change a normal run: same vectors, more but smaller calls."""
    vectors, model = RecordingVectors(), Model()
    _stats, counts, _p = _run(tmp_path / "i.db", _corpus(tmp_path, archive=False), vectors, model)

    assert sum(model.calls) == FILES and max(model.calls) <= CPU_INFER_BATCH
    assert _status(counts, "INDEXED") == FILES and len(vectors.rows) == FILES


# --- what a cut batch must not claim ------------------------------------------

def test_an_archive_cut_by_a_stop_gets_no_completion_marker(tmp_path) -> None:
    """The marker says "read in full, do not open again"; here it would be a lie."""
    db, root = tmp_path / "i.db", _corpus(tmp_path, archive=True)
    vectors = RecordingVectors()
    _run(db, root, vectors, Model(stop_after_calls=1))
    assert ResumableArchive.opened == 1

    _run(db, root, vectors, Model())                              # must read it again
    assert ResumableArchive.opened == 2, "the cut archive was treated as finished"
    assert len(vectors.rows) == FILES

    _run(db, root, vectors, Model())                              # and now it is settled
    assert ResumableArchive.opened == 2


def test_no_resume_position_is_saved_past_an_abandoned_message(tmp_path) -> None:
    """A saved position would make the next run skip messages that have no vector."""
    db, root = tmp_path / "i.db", _corpus(tmp_path, archive=True)
    _run(db, root, RecordingVectors(), Model(stop_after_calls=1))

    digest = content_hash(root / "mail.stopmbox")
    with SqliteStore(db) as store:
        assert store.get_state(f"{RESUME_STATE_PREFIX}{digest}") is None


def test_an_archive_resumed_after_a_stop_ends_with_every_message(tmp_path) -> None:
    db, root = tmp_path / "i.db", _corpus(tmp_path, archive=True)
    vectors = RecordingVectors()
    _run(db, root, vectors, Model(stop_after_calls=1))
    _stats, counts, _p = _run(db, root, vectors, Model())

    assert counts["chunks_total"] == FILES
    assert len(vectors.rows) == FILES and vectors.added == FILES


# --- a file cut in half is abandoned whole ------------------------------------

def test_the_whole_file_prefix_never_ends_inside_a_file() -> None:
    pending = [(i, i // 3, f"t{i}") for i in range(9)]           # three files of three
    prefix = Pipeline._whole_file_prefix
    assert prefix(pending, 9) == 9
    assert prefix(pending, 6) == 6                               # exactly on a boundary
    assert prefix(pending, 5) == 3                               # file 1 is cut: dropped
    assert prefix(pending, 4) == 3
    assert prefix(pending, 2) == 0                               # the first file is cut


def test_a_file_split_by_a_stop_is_neither_written_nor_marked(tmp_path) -> None:
    """`_embed_pending` on passages grouped three to a file, stopped after 32 of 99."""
    from contextlib import contextmanager

    class Store:
        embedded: list[int] = []
        indexed: list[int] = []

        @contextmanager
        def batch(self):
            yield

        def mark_embedded(self, ids):
            self.embedded.extend(ids)

        def mark_indexed_many(self, file_ids):
            self.indexed.extend(file_ids)

    vectors, model, store = RecordingVectors(), Model(stop_after_calls=1), Store()
    config = PipelineConfig(walk=WalkConfig(roots=[tmp_path]), workers=1, embed_batch=1000)
    pipeline = Pipeline(store, vectors, model.embedder(), config)
    model.pipeline = pipeline

    pending = [(i, i // 3, f"passage {i}") for i in range(99)]   # 33 files of 3 passages
    pipeline._embed_pending(pending)

    # 32 passages were embedded; file 10 (passages 30-32) is split by that, so
    # only files 0-9 - 30 passages - are written and marked.
    assert model.calls == [CPU_INFER_BATCH]
    assert sorted(vectors.rows) == list(range(30))
    assert store.indexed == list(range(10))
    assert sorted(store.embedded) == list(range(30))
    assert pipeline._embed_abandoned
    assert pending == []


def test_a_stop_still_keeps_up_to_one_model_calls_worth_of_finished_files(tmp_path) -> None:
    """Regression (found by `test_layer3_acceptance`): the run's final flush happens
    with the stop flag already set, and abandoning *everything* then threw away the
    files a stopped run had just finished. The first slice always runs."""
    from contextlib import contextmanager

    class Store:
        indexed: list[int] = []

        @contextmanager
        def batch(self):
            yield

        def mark_embedded(self, ids):
            pass

        def mark_indexed_many(self, file_ids):
            self.indexed.extend(file_ids)

    vectors, model, store = RecordingVectors(), Model(), Store()
    config = PipelineConfig(walk=WalkConfig(roots=[tmp_path]), workers=1)
    pipeline = Pipeline(store, vectors, model.embedder(), config)
    pipeline.request_stop()                                       # already stopping

    pending = [(i, i // 2, f"passage {i}") for i in range(10)]   # five files, under one call
    pipeline._embed_pending(pending)

    assert sorted(store.indexed) == [0, 1, 2, 3, 4], "finished files were thrown away by a stop"
    assert len(vectors.rows) == 10
    assert not pipeline._embed_abandoned


def test_once_a_batch_was_cut_the_next_is_dropped_without_a_model_call(tmp_path) -> None:
    from contextlib import contextmanager

    class Store:
        @contextmanager
        def batch(self):
            yield

        def mark_embedded(self, ids):
            pass

        def mark_indexed_many(self, file_ids):
            pass

    vectors, model = RecordingVectors(), Model(stop_after_calls=1)
    config = PipelineConfig(walk=WalkConfig(roots=[tmp_path]), workers=1)
    pipeline = Pipeline(Store(), vectors, model.embedder(), config)
    model.pipeline = pipeline

    pipeline._embed_pending([(i, i, f"a{i}") for i in range(100)])   # cut after one call
    assert model.calls == [CPU_INFER_BATCH] and pipeline._embed_abandoned
    pipeline._embed_pending([(1000 + i, 1000 + i, f"b{i}") for i in range(100)])
    assert model.calls == [CPU_INFER_BATCH], "a batch queued behind a cut one was embedded"


# --- a worker must never leave the consumer polling for ever -------------------

class _ExitingArchive:
    name = "exiting"
    extensions = (".exitfile",)
    reads_externally = False

    def extract(self, path: Path):
        raise SystemExit(3)                     # not an `Exception`: escaped the old guard
        yield                                   # pragma: no cover


@pytest.mark.timeout(60, method="thread")
def test_a_file_that_raises_a_base_exception_costs_the_file_not_the_run(tmp_path) -> None:
    """The run used to poll `results` for ever: the worker thread died, no `_STOP`
    was sent, and `_consume` waited for it."""
    before = dict(base.REGISTRY)
    base.register(_ExitingArchive())
    try:
        root = tmp_path / "docs"
        root.mkdir()
        write_aged(root / "bad.exitfile", b"x" * 100)
        write_aged(root / "good.txt", "A good document about the dairy audit.")
        stats, counts, _p = _run(tmp_path / "i.db", root, RecordingVectors(), Model())
    finally:
        base.REGISTRY.clear()
        base.REGISTRY.update(before)

    assert stats.skipped == 1 and stats.indexed == 1, stats.as_dict()
