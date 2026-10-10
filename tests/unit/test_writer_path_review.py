r"""The index run's writer path, after the review of 10 October 2026.

Layer: L3

One finding each, as the review numbered them:

* **P1 - a photo's models no longer hold up the documents behind it.** CLIP,
  the perceptual hash, faces and a video's keyframes ran on the consumer, the
  one thread that writes; they run on a picture worker now, fed by a bounded
  queue, drained and joined where reading ends.
* **P2 - a file the gate puts aside is not hashed first.** A new picture held
  for the images pass (or a video for the tail) was read end to end for a
  blake2b nothing needed.
* **P3 - the clean-up pass deletes picture vectors too**, not only text ones,
  vectors first and then SQLite.
* **P4 - a file's old text vectors are deleted once its rows commit**, never
  while the write group is open, and not at all if the group rolls back.
* **P5 - `bulk_fts="auto"` bulk-loads the word index** on an empty index and a
  large walk, and only then; and the triggers always come back.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from app.extract import ocr as ocr_module
from app.index import pipeline as module
from app.index.embedder import Embedder
from app.index.pipeline import IndexStats, Pipeline, PipelineConfig, _Extracted
from app.index.resources import ResourceLimits
from app.index.walker import Candidate, WalkConfig
from app.storage.sqlite_store import FileStatus, SqliteStore

#: 2026-06-01 - old enough that nothing counts as "edited just now".
OLD_NS = 1_780_272_000 * 1_000_000_000


class _Vectors:
    """Text vectors kept in a dict, `chunk_id -> file_id`, with every delete
    recorded in order."""

    def __init__(self) -> None:
        self.rows: dict[int, int] = {}
        self.deletes: list[list[int]] = []

    def ensure_table(self) -> None:
        pass

    def delete_by_file_ids(self, file_ids) -> None:
        wanted = [int(one) for one in file_ids]
        self.deletes.append(wanted)
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


def _pipeline(store, roots, vectors=None, **config) -> Pipeline:
    embedder = Embedder(dim=4, encoder=lambda texts: [[1.0, 0.0, 0.0, 0.0] for _ in texts])
    extra = {key: config.pop(key) for key in ("image_vectors", "phash_computer",
                                              "image_embedder") if key in config}
    return Pipeline(store, vectors if vectors is not None else _Vectors(), embedder,
                    PipelineConfig(
                        walk=WalkConfig(roots=list(roots)), workers=1, min_free_gb=0,
                        required_free_gb=0,
                        limits=ResourceLimits(pause_on_battery=False, cpu_percent=0,
                                              min_free_gb=0, low_priority=False),
                        **config),
                    **extra)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(OLD_NS, OLD_NS))
    return path


def _png(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 900)
    os.utime(path, ns=(OLD_NS, OLD_NS))
    return path


def _chunks(store, file_id: int) -> int:
    return store.conn.execute(
        "SELECT count(*) FROM chunks WHERE file_id = ?", (file_id,)).fetchone()[0]


@pytest.fixture()
def ocr_reads(monkeypatch):
    """OCR that finds a line in every picture, with no model."""
    def engine(_source):
        return ([([0, 0, 1, 1], "INVOICE 4471 Barnsley Dairy", 0.94)], 0.01)

    monkeypatch.setattr(ocr_module, "available", lambda: True)
    monkeypatch.setattr(ocr_module, "_load_engine", lambda: engine)


# ---------------------------------------------------------------------------
# P1. The picture worker
# ---------------------------------------------------------------------------

class _SlowPhash:
    """A picture model that takes `seconds` a photo and says on which thread."""

    def __init__(self, seconds: float = 0.0, *, fail: bool = False) -> None:
        self.seconds = seconds
        self.fail = fail
        self.threads: list[str] = []
        self.finished: list[float] = []

    def compute(self, _picture) -> str:
        self.threads.append(threading.current_thread().name)
        time.sleep(self.seconds)
        self.finished.append(time.monotonic())
        if self.fail:
            raise RuntimeError("simulated pHash failure")
        return "00ff00ff00ff00ff"


def _mixed(root: Path, photos: int, letters: int) -> None:
    for index in range(photos):
        _png(root / f"a{index:02d}.png")              # read first: "found" order is by name
    for index in range(letters):
        _write(root / f"z{index:02d}.txt", f"pump station letter {index}")


def _timed_letters(pipeline: Pipeline) -> list[float]:
    """When each text document's write returned, on the consumer."""
    written: list[float] = []
    real = pipeline._write_one

    def spy(item):
        out = real(item)
        if item.candidate.path.suffix == ".txt":
            written.append(time.monotonic())
        return out

    pipeline._write_one = spy
    return written


def test_picture_models_run_on_the_worker_not_the_consumer(tmp_path, ocr_reads):
    root = tmp_path / "docs"
    _mixed(root, photos=3, letters=2)
    phash = _SlowPhash()
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, [root], phash_computer=phash, read_order="found")
        stats = pipeline.run()
        hashed = [record.phash for record in store.iter_files()
                  if record.path.endswith(".png")]
    assert stats.indexed + stats.skipped == 5
    assert phash.threads == ["pictures"] * 3, phash.threads
    assert hashed == ["00ff00ff00ff00ff"] * 3, "every photo's hash still written"
    assert pipeline._picture_thread is None, "joined where reading ended"
    assert pipeline._pending_phashes == {} and pipeline._pending_images == []
    assert not [t for t in threading.enumerate() if t.name == "pictures"]


def test_the_letters_behind_a_slow_photo_are_written_before_its_model_finishes(
        tmp_path, ocr_reads):
    """The review's complaint, as an outcome: six photos at 0.2 s each ahead
    of four letters. Inline, the last letter waited for all six; now it is
    written while the worker is still on the photos."""
    root = tmp_path / "docs"
    _mixed(root, photos=6, letters=4)
    phash = _SlowPhash(0.2)
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, [root], phash_computer=phash, read_order="found")
        letters = _timed_letters(pipeline)
        pipeline.run()
    assert len(letters) == 4 and len(phash.finished) == 6
    assert max(letters) < max(phash.finished), (
        "the last letter waited for every photo's model")


def test_one_failing_picture_step_costs_only_itself(tmp_path, ocr_reads, monkeypatch):
    root = tmp_path / "docs"
    _png(root / "a.png")
    faces: list[int] = []
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, [root], phash_computer=_SlowPhash(fail=True),
                             read_order="found", people_recognition_enabled=True)
        monkeypatch.setattr(pipeline, "_maybe_detect_faces",
                            lambda candidate, file_id: faces.append(file_id))
        stats = pipeline.run()
    assert stats.indexed + stats.skipped == 1
    assert len(faces) == 1, "the face step still ran after the hash step failed"


def test_a_flush_asked_for_by_the_consumer_waits_for_the_photos_before_it(tmp_path):
    """M6: an archive's marker or a resume cursor comes after the pictures
    handed over before it - so the consumer's flush is done behind them."""
    photo = _png(tmp_path / "p.png")
    phash = _SlowPhash(0.3)
    with SqliteStore(tmp_path / "index.db") as store:
        file_id = store.upsert_file(str(photo), size_bytes=1, mtime_ns=OLD_NS,
                                    status=FileStatus.PENDING, ext="png")
        pipeline = _pipeline(store, [tmp_path], phash_computer=phash)
        pipeline._stats_ref = IndexStats()
        pipeline._start_picture_work()
        candidate = Candidate(path=photo, size_bytes=1, mtime_ns=OLD_NS)
        assert pipeline._hand_to_pictures(candidate, file_id, None, video=False)
        pipeline._flush_pending_images()
        assert store.get_file(str(photo)).phash == "00ff00ff00ff00ff"
        pipeline._finish_picture_work()
        pipeline._finish_picture_work()                  # twice is harmless
    assert pipeline._picture_thread is None


def test_no_picture_model_means_no_worker_and_the_old_inline_path(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        pipeline = _pipeline(store, [tmp_path])
        pipeline._start_picture_work()
        assert getattr(pipeline, "_picture_thread", None) is None
        candidate = Candidate(path=tmp_path / "p.png", size_bytes=1, mtime_ns=1)
        assert pipeline._hand_to_pictures(candidate, 1, None, video=False) is False


# ---------------------------------------------------------------------------
# P2. The gate before the hash
# ---------------------------------------------------------------------------

def test_a_picture_the_text_pass_holds_is_not_hashed(tmp_path, monkeypatch):
    from app.index import walker

    root = tmp_path / "docs"
    photo = _png(root / "held.png")
    letter = _write(root / "a.txt", "pump station")
    hashed: list[str] = []
    real = walker.content_hash

    def spy(path, **kwargs):
        hashed.append(Path(path).name)
        return real(path, **kwargs)

    monkeypatch.setattr(walker, "content_hash", spy)
    with SqliteStore(tmp_path / "index.db") as store:
        stats = _pipeline(store, [root], ocr_mode="text").run()
        held = store.get_file(str(photo))
        read = store.get_file(str(letter))
    assert stats.skipped_by_code.get("ERR_OCR_HELD") == 1
    assert hashed == ["a.txt"], "only the file that is read is hashed"
    assert held.status == FileStatus.SKIPPED and held.content_hash is None
    assert read.content_hash


# ---------------------------------------------------------------------------
# P3. Both vector stores on a delete
# ---------------------------------------------------------------------------

class _ImageVectors:
    def __init__(self, store) -> None:
        self.store = store
        self.deletes: list[tuple[list[int], bool]] = []

    def delete_by_file_ids(self, file_ids) -> None:
        ids = [int(one) for one in file_ids]
        # Was the row still there? Vectors first, then SQLite.
        still = all(self.store.conn.execute(
            "SELECT 1 FROM files WHERE id = ?", (i,)).fetchone() for i in ids)
        self.deletes.append((ids, still))

    def __getattr__(self, name):
        # ensure_table, maybe_create_index, maybe_compact: nothing to do.
        return lambda *args, **kwargs: None


def test_a_vanished_file_loses_its_picture_vectors_too(tmp_path):
    root = tmp_path / "docs"
    gone = _write(root / "gone.txt", "pump station")
    _write(root / "kept.txt", "pump station two")
    with SqliteStore(tmp_path / "index.db") as store:
        assert _pipeline(store, [root]).run().indexed == 2
        file_id = store.get_file(str(gone)).id
        gone.unlink()
        images = _ImageVectors(store)
        text = _Vectors()
        stats = _pipeline(store, [root], text, image_vectors=images).run()
        assert store.get_file(str(gone)) is None
    assert stats.deleted == 1
    assert images.deletes == [([file_id], True)], "picture vectors deleted, before the row"
    assert [file_id] in text.deletes


def test_forget_files_deletes_both_sides(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        file_id = store.upsert_file(str(tmp_path / "p.png"), size_bytes=1, mtime_ns=1)
        images, text = _ImageVectors(store), _Vectors()
        pipeline = _pipeline(store, [tmp_path], text, image_vectors=images)
        assert pipeline.forget_files([file_id]) == 1
        assert store.get_file(str(tmp_path / "p.png")) is None
    assert images.deletes == [([file_id], True)] and text.deletes == [[file_id]]


# ---------------------------------------------------------------------------
# P4. Old text vectors go once the rows commit
# ---------------------------------------------------------------------------

def _indexed_letter(tmp_path, store, vectors) -> tuple[Path, int]:
    root = tmp_path / "docs"
    letter = _write(root / "a.txt", "pump station")
    assert _pipeline(store, [root], vectors).run().indexed == 1
    file_id = store.get_file(str(letter)).id
    assert vectors.count() >= 1 and _chunks(store, file_id) >= 1
    return letter, file_id


def _emptied(letter: Path) -> _Extracted:
    candidate = Candidate(path=letter, size_bytes=letter.stat().st_size, mtime_ns=OLD_NS)
    return _Extracted(candidate, "digest", key=str(letter), chunks=[])


def test_a_rolled_back_group_keeps_the_old_passages_and_their_vectors(tmp_path):
    vectors = _Vectors()
    with SqliteStore(tmp_path / "index.db") as store:
        letter, file_id = _indexed_letter(tmp_path, store, vectors)
        before = vectors.count()
        pipeline = _pipeline(store, [letter.parent], vectors)
        pipeline._begin_write_group()
        assert pipeline._write_one(_emptied(letter)) == []
        assert vectors.count() == before, "no LanceDB delete inside the open group"
        pipeline._abandon_write_group()
        assert _chunks(store, file_id) >= 1, "the old passages are back"
    assert vectors.count() == before, "and so are their vectors"
    assert pipeline._vector_deletes == []


def test_a_committed_group_deletes_the_old_vectors_after_the_commit(tmp_path):
    vectors = _Vectors()
    with SqliteStore(tmp_path / "index.db") as store:
        letter, file_id = _indexed_letter(tmp_path, store, vectors)
        pipeline = _pipeline(store, [letter.parent], vectors)
        seen_at_delete: list[int] = []
        real = vectors.delete_by_file_ids

        def delete(file_ids):
            # Run outside the write group: a fresh connection sees it committed.
            seen_at_delete.append(_chunks(store, file_id))
            real(file_ids)

        vectors.delete_by_file_ids = delete
        pipeline._begin_write_group()
        pipeline._write_one(_emptied(letter))
        pipeline._commit_write_group()
        assert getattr(pipeline, "_write_group", None) is None
    assert vectors.count() == 0
    assert seen_at_delete == [0], "deleted once, after the empty passages committed"


def test_with_no_group_open_the_delete_runs_at_once(tmp_path):
    vectors = _Vectors()
    with SqliteStore(tmp_path / "index.db") as store:
        letter, _file_id = _indexed_letter(tmp_path, store, vectors)
        pipeline = _pipeline(store, [letter.parent], vectors)
        pipeline._write_one(_emptied(letter))
    assert vectors.count() == 0
