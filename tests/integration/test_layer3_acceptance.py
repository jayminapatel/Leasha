"""Layer 3 acceptance tests.

The criteria from BUILD_SPEC_V2.md, verbatim:

  1. 10k-file corpus indexes end-to-end with zero unhandled exceptions.
  2. Kill the process at 50%; restart resumes within one batch of where it stopped.
  3. Re-running over an unchanged corpus does near-zero work.
  4. Touch one file -> only that file is re-indexed.
  5. Delete one file -> its rows disappear from SQLite, FTS and LanceDB.
  6. Inject a corrupt file mid-run -> run completes, file appears in the skipped panel.
  7. Fill the disk artificially -> pipeline pauses, does not corrupt, resumes.
  8. Throughput measured and recorded as the baseline for the UI's ETA.

Embedding uses an injected encoder throughout. The model is Layer 3's slowest
component by far and its correctness is Layer 3's least interesting property -
what these tests are about is whether the *pipeline* loses work, repeats work, or
falls over, and a real model would make every one of them minutes long.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from app.index.embedder import Embedder, l2_normalise
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import FileStatus, SqliteStore
from app.storage.vector_store import VectorStore

# Each test starts a thread pool and builds a LanceDB table, so the file runs in
# minutes rather than seconds. Marked slow so `-m "not slow"` gives a fast loop
# while the full suite still runs everything by default.
pytestmark = pytest.mark.slow

DIM = 384


def fake_encoder(texts):
    """Deterministic vectors keyed on the text, so identical chunks match."""
    out = []
    for text in texts:
        seed = float(abs(hash(text)) % 1000)
        out.append(l2_normalise([math.sin(seed + i) for i in range(DIM)]))
    return out


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    root.mkdir()
    for i in range(12):
        (root / f"doc{i:03d}.txt").write_text(
            f"Document {i}. The northern pump station was commissioned in March. "
            f"Flow rates were measured at three points across manifold {i}.",
            encoding="utf-8",
        )
    return root


@pytest.fixture()
def stores(tmp_path: Path):
    store = SqliteStore(tmp_path / "index.db").connect()
    vectors = VectorStore(tmp_path / "vectors", dim=DIM).connect()
    yield store, vectors
    store.close()
    vectors.close()


def build(stores, root: Path, **overrides) -> Pipeline:
    store, vectors = stores
    config = PipelineConfig(
        walk=WalkConfig(roots=[root], extensions=frozenset({".txt", ".md"})),
        workers=overrides.pop("workers", 2),
        checkpoint_every=overrides.pop("checkpoint_every", 5),
        **overrides,
    )
    return Pipeline(store, vectors, Embedder(dim=DIM, encoder=fake_encoder), config)


# --- 1 ----------------------------------------------------------------------

def test_a_corpus_indexes_end_to_end(stores, corpus: Path) -> None:
    store, vectors = stores
    stats = build(stores, corpus).run()

    assert stats.indexed == 12
    assert stats.skipped == 0
    assert stats.chunks >= 12
    assert store.stats()["files"][FileStatus.INDEXED] == 12
    assert vectors.count() == stats.chunks


def test_every_chunk_is_searchable_afterwards(stores, corpus: Path) -> None:
    """Indexed but not findable is the failure this whole layer exists to avoid."""
    store, _vectors = stores
    build(stores, corpus).run()

    hits = store.search_bm25('"pump station"')
    assert len(hits) >= 12


def test_chunks_and_vectors_agree(stores, corpus: Path) -> None:
    store, vectors = stores
    build(stores, corpus).run()

    sqlite_stats = store.stats()
    assert sqlite_stats["chunks_total"] == vectors.count()
    assert sqlite_stats["chunks_embedded"] == sqlite_stats["chunks_total"]


# --- 2 ----------------------------------------------------------------------

def test_an_interrupted_run_resumes_where_it_stopped(stores, corpus: Path) -> None:
    """Criterion 2. Resumability is the `files` table, not a saved position: a
    restart re-walks and skips what is already INDEXED for the cost of a stat."""
    store, _vectors = stores

    pipeline = build(stores, corpus, checkpoint_every=2)
    original_write = pipeline._write_one
    written = {"n": 0}

    def stop_halfway(item):
        result = original_write(item)
        written["n"] += 1
        if written["n"] >= 6:
            pipeline.request_stop()
        return result

    pipeline._write_one = stop_halfway            # type: ignore[method-assign]
    first = pipeline.run()

    assert 0 < first.indexed < 12, "the run really did stop part-way"
    partial = store.stats()["files"].get(FileStatus.INDEXED, 0)

    second = build(stores, corpus).run()

    assert second.unchanged == partial, "already-indexed files were skipped, not redone"
    assert store.stats()["files"][FileStatus.INDEXED] == 12, "and the rest completed"


def test_stopping_does_not_lose_completed_work(stores, corpus: Path) -> None:
    store, vectors = stores
    pipeline = build(stores, corpus, checkpoint_every=1)

    original = pipeline._write_one
    count = {"n": 0}

    def stop_after_three(item):
        result = original(item)
        count["n"] += 1
        if count["n"] >= 3:
            pipeline.request_stop()
        return result

    pipeline._write_one = stop_after_three        # type: ignore[method-assign]
    pipeline.run()

    indexed = store.stats()["files"].get(FileStatus.INDEXED, 0)
    assert indexed >= 3
    assert vectors.count() > 0, "vectors for completed files survived the stop"


# --- 3 ----------------------------------------------------------------------

def test_rerunning_an_unchanged_corpus_does_almost_nothing(stores, corpus: Path) -> None:
    """Criterion 3, and the reason the walker hashes lazily."""
    build(stores, corpus).run()
    second = build(stores, corpus).run()

    assert second.unchanged == 12
    assert second.indexed == 0
    assert second.chunks == 0


def test_an_unchanged_rerun_never_opens_a_file(stores, corpus: Path, monkeypatch) -> None:
    """The strong form: on a settled 100GB corpus the second pass must cost a
    directory walk, not a read of every byte."""
    build(stores, corpus).run()

    real_open = Path.open

    def watched_open(self, *args, **kwargs):
        if self.suffix == ".txt" and "corpus" in str(self):
            raise AssertionError(f"unchanged file was opened: {self}")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", watched_open)
    second = build(stores, corpus).run()
    assert second.unchanged == 12


# --- 4 ----------------------------------------------------------------------

def test_touching_one_file_reindexes_only_that_file(stores, corpus: Path) -> None:
    build(stores, corpus).run()

    edited = corpus / "doc005.txt"
    edited.write_text("Completely different content about valve replacement.", encoding="utf-8")

    second = build(stores, corpus).run()
    assert second.indexed == 1
    assert second.unchanged == 11


def test_the_edit_is_what_becomes_searchable(stores, corpus: Path) -> None:
    """Re-indexing that leaves the old chunks behind is worse than not
    re-indexing at all: the file then matches text it no longer contains."""
    store, _vectors = stores
    build(stores, corpus).run()

    edited = corpus / "doc005.txt"
    edited.write_text("Valve replacement scheduled for the autumn shutdown.", encoding="utf-8")
    build(stores, corpus).run()

    file_id = store.get_file(str(edited)).id
    texts = " ".join(chunk.text for chunk in store.chunks_for_file(file_id))
    assert "Valve replacement" in texts
    assert "manifold 5" not in texts, "stale chunks were replaced, not appended to"


def test_a_touched_but_identical_file_is_not_reindexed(stores, corpus: Path) -> None:
    """robocopy, restores and cloud sync all reset mtime without changing bytes."""
    import os

    build(stores, corpus).run()

    target = corpus / "doc003.txt"
    stat = target.stat()
    os.utime(target, ns=(stat.st_mtime_ns + 10_000_000_000,) * 2)

    second = build(stores, corpus).run()
    assert second.indexed == 0, "same bytes, so no work however the timestamp moved"


# --- 5 ----------------------------------------------------------------------

def test_deleting_a_file_removes_it_from_every_store(stores, corpus: Path) -> None:
    store, vectors = stores
    build(stores, corpus).run()

    doomed = corpus / "doc007.txt"
    file_id = store.get_file(str(doomed)).id
    chunk_ids = [chunk.id for chunk in store.chunks_for_file(file_id)]
    assert chunk_ids

    before = vectors.count()
    doomed.unlink()

    stats = build(stores, corpus).run()
    assert stats.deleted == 1
    assert store.get_file(str(doomed)) is None
    assert store.chunks_for_file(file_id) == []
    assert vectors.count() == before - len(chunk_ids)


def test_deleted_content_stops_matching_in_fts(stores, corpus: Path) -> None:
    store, _vectors = stores
    build(stores, corpus).run()

    (corpus / "doc007.txt").unlink()
    build(stores, corpus).run()

    assert not [hit for hit in store.search_bm25('"manifold 7"')]


def test_pruning_can_be_turned_off_for_a_partial_run(stores, corpus: Path) -> None:
    """A run over one root must not conclude the other roots were deleted."""
    store, _vectors = stores
    build(stores, corpus).run()
    (corpus / "doc007.txt").unlink()

    stats = build(stores, corpus, prune_missing=False).run()
    assert stats.deleted == 0
    assert store.get_file(str(corpus / "doc007.txt")) is not None


# --- 6 ----------------------------------------------------------------------

def test_a_corrupt_file_is_skipped_and_the_run_completes(stores, corpus: Path) -> None:
    """Criterion 6, and non-negotiable #4: one bad file never halts a batch."""
    store, _vectors = stores
    (corpus / "broken.pdf").write_bytes(b"This is not a PDF.")

    config = PipelineConfig(
        walk=WalkConfig(roots=[corpus], extensions=frozenset({".txt", ".pdf"})),
        workers=2, checkpoint_every=5,
    )
    stats = Pipeline(*stores, Embedder(dim=DIM, encoder=fake_encoder), config).run()

    assert stats.indexed == 12, "every good file still indexed"
    assert stats.skipped == 1
    assert stats.skipped_by_code == {"ERR_FILE_CORRUPT": 1}

    record = store.get_file(str(corpus / "broken.pdf"))
    assert record.status == FileStatus.SKIPPED
    assert record.skip_code == "ERR_FILE_CORRUPT"
    assert record.skip_detail


def test_the_skipped_panel_can_group_by_cause(stores, corpus: Path) -> None:
    """Thousands of skips must be countable by reason, not read one by one."""
    store, _vectors = stores
    (corpus / "broken.pdf").write_bytes(b"not a pdf")
    (corpus / "binary.txt").write_bytes(b"\x00\x01\x02" * 400)

    config = PipelineConfig(
        walk=WalkConfig(roots=[corpus], extensions=frozenset({".txt", ".pdf"})),
        workers=2,
    )
    Pipeline(*stores, Embedder(dim=DIM, encoder=fake_encoder), config).run()

    summary = store.skipped_summary()
    assert summary["ERR_FILE_CORRUPT"] == 1
    assert summary["ERR_NO_TEXT_LAYER"] == 1


def test_an_unexpected_worker_exception_becomes_a_skip(stores, corpus: Path) -> None:
    """A bug in one extractor must cost that file, not the run."""
    store, _vectors = stores
    pipeline = build(stores, corpus)
    original = pipeline._extract_one
    calls = {"n": 0}

    def sometimes_explode(candidate, digest):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("a parser bug nobody anticipated")
        return original(candidate, digest)

    pipeline._extract_one = sometimes_explode     # type: ignore[method-assign]
    stats = pipeline.run()

    assert stats.indexed == 11
    assert stats.skipped == 1
    assert "ERR_UNEXPECTED" in stats.skipped_by_code


# --- 7 ----------------------------------------------------------------------

def test_low_disk_stops_the_run_without_corrupting_it(stores, corpus: Path, monkeypatch) -> None:
    """Criterion 7. Stopping must preserve everything already written."""
    import shutil as shutil_module

    store, vectors = stores
    pipeline = build(stores, corpus, checkpoint_every=2, min_free_gb=100)

    class Tiny:
        total = free = used = 1  # 1 byte free

    monkeypatch.setattr(shutil_module, "disk_usage", lambda _p: Tiny)
    monkeypatch.setattr("app.index.pipeline.shutil.disk_usage", lambda _p: Tiny)

    stats = pipeline.run()

    assert stats.stopped_early is not None
    assert stats.stopped_early.code == "ERR_DISK_SPACE"
    assert store.integrity_check(), "the database is intact"
    assert store.stats()["chunks_total"] == vectors.count(), "stores still agree"


def test_the_run_resumes_after_space_is_freed(stores, corpus: Path, monkeypatch) -> None:
    import shutil as shutil_module

    store, _vectors = stores

    class Tiny:
        total = free = used = 1

    monkeypatch.setattr(shutil_module, "disk_usage", lambda _p: Tiny)
    monkeypatch.setattr("app.index.pipeline.shutil.disk_usage", lambda _p: Tiny)
    build(stores, corpus, checkpoint_every=2, min_free_gb=100).run()

    monkeypatch.undo()
    stats = build(stores, corpus).run()

    assert stats.stopped_early is None
    assert store.stats()["files"][FileStatus.INDEXED] == 12


def test_an_unreadable_disk_check_does_not_stop_the_run(stores, corpus: Path, monkeypatch) -> None:
    """Not being able to check free space is not a reason to stop indexing."""
    def refuse(_path):
        raise OSError("no such device")

    monkeypatch.setattr("app.index.pipeline.shutil.disk_usage", refuse)
    stats = build(stores, corpus, checkpoint_every=2).run()
    assert stats.indexed == 12
    assert stats.stopped_early is None


# --- 8 ----------------------------------------------------------------------

def test_throughput_is_measured_and_recorded(stores, corpus: Path) -> None:
    """Criterion 8: the UI's ETA has to come from somewhere real."""
    store, _vectors = stores
    stats = build(stores, corpus).run()

    assert stats.elapsed_s > 0
    assert stats.files_per_minute > 0
    assert stats.mb_per_minute >= 0

    recorded = stats.as_dict()
    assert "files_per_minute" in recorded and "mb_per_minute" in recorded
    assert store.get_state("last_run") is not None
    assert store.get_state("last_run_stats") is not None


def test_progress_is_reported_during_the_run(stores, corpus: Path) -> None:
    """A 100GB run that reports nothing until it finishes is indistinguishable
    from one that has hung."""
    seen: list[int] = []
    build(stores, corpus, checkpoint_every=3).run(on_progress=lambda s: seen.append(s.indexed))

    assert len(seen) >= 2
    assert seen == sorted(seen), "progress only ever moves forward"


# --- retrying locked files --------------------------------------------------

def test_a_previously_locked_file_is_retried(stores, corpus: Path, monkeypatch) -> None:
    """The program holding it may well have closed since, and nothing else would
    ever look at that file again."""
    store, _vectors = stores
    target = corpus / "doc002.txt"

    real_extract = None
    import app.index.pipeline as pipeline_module

    def locked(path):
        if path == target:
            from app.core.errors import raise_error
            raise_error("ERR_FILE_LOCKED", "test", path=str(path))
        return real_extract(path)

    real_extract = pipeline_module.extract
    monkeypatch.setattr(pipeline_module, "extract", locked)

    first = build(stores, corpus).run()
    assert first.skipped_by_code == {"ERR_FILE_LOCKED": 1}

    monkeypatch.undo()
    second = build(stores, corpus).run()
    assert second.indexed == 1
    assert store.get_file(str(target)).status == FileStatus.INDEXED


# --- backpressure -----------------------------------------------------------

def test_a_small_queue_does_not_deadlock(stores, corpus: Path) -> None:
    """Bounded queues are what stop a fast walker exhausting memory ahead of a
    slow embedder - but only if they cannot wedge."""
    stats = build(stores, corpus, queue_size=2, workers=3).run()
    assert stats.indexed == 12


def test_a_single_worker_works(stores, corpus: Path) -> None:
    assert build(stores, corpus, workers=1).run().indexed == 12


def test_an_empty_corpus_is_not_an_error(stores, tmp_path: Path) -> None:
    empty = tmp_path / "nothing"
    empty.mkdir()
    stats = build(stores, empty).run()
    assert stats.indexed == 0 and stats.skipped == 0
