r"""A photo the text pass held is read by the next "with the run" run (order 1b).

Layer: L3

Work order `indexing-cockpit-and-conductor` 1b (2026-10-14). The owner's index
held **36,505 photos as `ERR_OCR_HELD`, none face-scanned**: `INDEX_OCR_PASS`
was `after-run`, so every scheduled run was the text pass, which files a
picture as held and runs no picture step on it. The fix at go-live is to set
the schedule to `with-run` - which makes every scheduled run the `both` pass
(`run_setup.pass_for`). This proves that is enough: a `both` run takes each
held photo up again, makes its faces, its picture-search vector and its
duplicate fingerprint, and the row leaves `ERR_OCR_HELD`.
"""

from __future__ import annotations

import math
import os
import time
from pathlib import Path

import pytest

from app.extract import florence_tagger
from app.extract import ocr as ocr_module
from app.index import run_setup
from app.index.embedder import Embedder, l2_normalise
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore

LONG_AGO = 3600
HELD = "ERR_OCR_HELD"


class _NullVectors:
    def delete_by_file_ids(self, file_ids):
        pass

    def add(self, **kwargs):
        return len(kwargs.get("chunk_ids") or ())

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _Images:
    """The picture-search table, in memory."""

    def __init__(self) -> None:
        self.rows: dict[int, list[float]] = {}

    def ensure_table(self) -> None:
        pass

    def delete_by_file_ids(self, file_ids) -> None:
        for file_id in file_ids:
            self.rows.pop(file_id, None)

    def add_images(self, file_ids, vectors, *, exts=None, mtimes_ns=None) -> int:
        for file_id, vector in zip(file_ids, vectors):
            self.rows[int(file_id)] = list(vector)
        return len(list(file_ids))

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class _Clip:
    choice = None

    def embed(self, pictures):
        return [l2_normalise([math.cos(i + 1.0) for i in range(8)]) for _ in pictures]


class _Phash:
    def compute(self, _picture) -> str:
        return "f0f0f0f0f0f0f0f0"


def _embedder() -> Embedder:
    return Embedder(dim=8, encoder=lambda texts: [
        l2_normalise([math.sin(len(t) + i) for i in range(8)]) for t in texts])


@pytest.fixture(autouse=True)
def _fakes(monkeypatch):
    from app.extract import face_detect

    def engine(_source):
        return ([([0, 0, 1, 1], "a sign by the sea", 0.94)], 0.01)

    monkeypatch.setattr(ocr_module, "available", lambda: True)
    monkeypatch.setattr(ocr_module, "_load_engine", lambda: engine)
    monkeypatch.setattr(face_detect, "available", lambda: True)
    monkeypatch.setattr(face_detect, "detect_faces", lambda path, data=None: [
        face_detect.FaceDetection(bbox=(1.0, 1.0, 40.0, 40.0),
                                  embedding=bytes(16), confidence=0.9)])
    monkeypatch.setattr(florence_tagger, "available", lambda: False)
    yield
    florence_tagger.defer(False)


def _photo(root: Path) -> Path:
    from PIL import Image

    root.mkdir(parents=True, exist_ok=True)
    path = root / "beach.png"
    Image.new("RGB", (320, 240), (40, 90, 160)).save(path)
    stamp = time.time() - LONG_AGO
    os.utime(path, (stamp, stamp))
    return path


def _run(store, root, mode, images):
    pipeline = Pipeline(
        store, _NullVectors(), _embedder(),
        PipelineConfig(walk=WalkConfig(roots=[root]), workers=1, ocr_mode=mode,
                       people_recognition_enabled=True),
        image_embedder=_Clip(), image_vectors=images, phash_computer=_Phash())
    return pipeline.run()


def test_with_run_is_the_both_pass():
    """The setting the order changes at go-live, and the pass it gives."""
    class Settings:
        index_ocr_pass = "with-run"
        index_ocr_mode = "both"

    assert run_setup.pass_for(Settings()) == "both"


def test_a_held_photo_gets_its_faces_vector_and_fingerprint_on_a_both_run(tmp_path):
    root = tmp_path / "PhotosMaster"
    photo = _photo(root)
    images = _Images()
    with SqliteStore(tmp_path / "index.db") as store:
        _run(store, root, "text", images)
        held = store.get_file(str(photo))
        assert held is not None and held.skip_code == HELD
        assert not store.face_scanned(held.id), "the text pass runs no picture step"
        assert held.id not in images.rows

        _run(store, root, "both", images)
        after = store.get_file(str(photo))
        faces = store.faces_for_file(after.id)
        scanned = store.face_scanned(after.id)

    assert after.skip_code != HELD, "the row leaves the held queue"
    assert scanned and len(faces) == 1, "its faces were looked for and kept"
    assert after.id in images.rows, "it has a picture-search vector"
    assert after.phash == "f0f0f0f0f0f0f0f0", "and its duplicate fingerprint"
