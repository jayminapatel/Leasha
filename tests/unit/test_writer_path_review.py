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
