r"""Photo indexing does each piece of work once. 2026-10-05.

Layer: L2/L3

The owner asked for photo indexing to be "efficient and fast". Measured on their
photos on the processor:
- a description encoded the picture twice, once for the caption and once for the
  objects - 6.2-8.3 s of each 10-12 s task;
- faces and CLIP each decoded the photo again (0.15 s a HEIC);
- CLIP ran one photo at a time - 0.28 s a JPEG, 0.19 s in batches of eight;
- duplicate fingerprints (pHash) were never switched on: 0 of 15,011 photos.
"""

from __future__ import annotations

import contextlib
import os
from types import SimpleNamespace

import pytest

PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from app.extract import picture  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_cache():
    picture.clear()
    yield
    picture.clear()


def _photo(path, size=(64, 48), colour=(200, 30, 30), orientation=None):
    image = Image.new("RGB", size, colour)
    kwargs = {}
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        kwargs["exif"] = exif.tobytes()
    image.save(path, "JPEG", **kwargs)
    return path


# --- one decode, shared ----------------------------------------------------------------

def test_a_photo_is_decoded_once_and_turned_upright(tmp_path):
    path = _photo(tmp_path / "portrait.jpg", size=(64, 48), orientation=6)  # on its side
    first = picture.decoded(path)
    assert first.size == (48, 64), "turned as its EXIF says"
    assert picture.decoded(path) is first, "the second model gets the same picture"


def test_a_changed_photo_is_decoded_again(tmp_path):
    path = _photo(tmp_path / "a.jpg")
    first = picture.decoded(path)
    _photo(path, size=(80, 40))
    os.utime(path, ns=(1, 2_000_000_000_000_000_000))
    again = picture.decoded(path)
    assert again.size == (80, 40) and again is not first


def test_an_unreadable_photo_is_none_and_never_raises(tmp_path):
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not a picture")
    assert picture.decoded(bad) is None
    assert picture.decoded(tmp_path / "missing.jpg") is None


def test_the_copy_waiting_for_clip_is_small():
    small = picture.small_copy(Image.new("RGB", (4000, 3000)))
    assert min(small.size) == 256 and small.size[0] == round(4000 * 256 / 3000)
    assert picture.small_copy(Image.new("RGB", (100, 80))).size == (100, 80)


def test_faces_read_the_shared_decode(tmp_path, monkeypatch):
    np = pytest.importorskip("numpy")
    cv2 = pytest.importorskip("cv2")
    from app.extract import face_detect

    path = _photo(tmp_path / "f.jpg", colour=(250, 10, 10))
    calls = []
    real = picture.decoded
    monkeypatch.setattr(picture, "decoded", lambda p: calls.append(p) or real(p))
    image = face_detect._read_bgr(path, cv2, np)
    assert calls and image.shape == (48, 64, 3)
    blue, _green, red = (int(v) for v in image[24, 32])
    assert red > 150 and blue < 80, "blue-green-red, as the model expects"


# --- one encode per description -----------------------------------------------------------

def test_a_description_encodes_the_picture_once_for_both_tasks():
    import threading

    from app.ort.florence import OnnxFlorence

    model = OnnxFlorence.__new__(OnnxFlorence)
    model.lock = threading.Lock()
    encoded, tasks = [], []
    model.encode_image = lambda pixels: encoded.append(1) or "features"
    model.run_task = lambda pixels, task, max_new_tokens=128, image=None: (
        tasks.append((task, image)) or ("a dog on a beach" if "CAPTION" in task
                                        else "<loc_1>dog<loc_2>"))
    model.caption_and_tags(Image.new("RGB", (32, 32)))
    assert encoded == [1]
    assert [image for _task, image in tasks] == ["features", "features"]


# --- CLIP in batches -------------------------------------------------------------------------

class _Clock:
    @contextlib.contextmanager
    def stage(self, _name):
        yield


def _pipeline(embedded):
    from app.index.pipeline import Pipeline

    built = Pipeline.__new__(Pipeline)
    built.image_embedder = SimpleNamespace(
        embed=lambda pictures: embedded.append(len(pictures)) or [[0.1] * 4 for _ in pictures])
    built.image_vectors = object()
    built._clock = _Clock()
    built._pending_pictures, built._pending_images = [], []
    built._stats_ref = SimpleNamespace(warned_by_code={})
    built._log = SimpleNamespace(warning=lambda *a, **k: None, debug=lambda *a, **k: None)
    return built


def test_clip_runs_eight_photos_at_a_time_and_a_flush_takes_the_rest(tmp_path):
    from app.index import pipeline as module
    from app.index.walker import Candidate

    embedded: list = []
    built = _pipeline(embedded)
    for n in range(module.PICTURE_BATCH + 1):
        path = _photo(tmp_path / f"p{n}.jpg")
        built._maybe_embed_image(Candidate(path=path, size_bytes=1, mtime_ns=n), n)
    assert embedded == [module.PICTURE_BATCH]
    built._embed_pending_pictures()                  # what every flush now does first
    assert embedded == [module.PICTURE_BATCH, 1]
    assert [item[0] for item in built._pending_images] == list(range(module.PICTURE_BATCH + 1))


def test_one_picture_clip_cannot_read_costs_only_itself(tmp_path):
    from app.index.walker import Candidate

    built = _pipeline([])

    def embed(pictures):
        if len(pictures) > 1 or pictures[0].size == (13, 13):
            raise ValueError("one bad picture")
        return [[0.2] * 4]

    built.image_embedder = SimpleNamespace(embed=embed)
    good = _photo(tmp_path / "good.jpg")
    odd = tmp_path / "odd.jpg"
    Image.new("RGB", (13, 13)).save(odd)
    built._maybe_embed_image(Candidate(path=good, size_bytes=1, mtime_ns=1), 1)
    built._maybe_embed_image(Candidate(path=odd, size_bytes=1, mtime_ns=1), 2)
    built._embed_pending_pictures()
    assert [item[0] for item in built._pending_images] == [1]
    assert built._stats_ref.warned_by_code


# --- fingerprints switched on -----------------------------------------------------------------

def test_every_index_run_is_given_a_fingerprint_computer():
    """Read from the two places a run is built: the window and the command line."""
    from pathlib import Path

    from app.index.phash import PhashComputer, default_phash_computer

    assert isinstance(default_phash_computer(), PhashComputer)
    root = Path(__file__).resolve().parents[2] / "app"
    for relative in ("cli/index.py", "ui/controllers/index_controller.py"):
        assert "phash_computer=default_phash_computer()" in (root / relative).read_text(
            encoding="utf-8"), relative


def test_a_fingerprint_is_taken_from_the_shared_decode(tmp_path):
    from app.index.phash import PhashComputer

    path = _photo(tmp_path / "h.jpg")
    computer = PhashComputer()
    assert computer.compute(picture.decoded(path)) == computer.compute(path)
