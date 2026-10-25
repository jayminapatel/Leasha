r"""A face too pixelated to keep is never stored - 2026-10-25.

Layer: L2

The owner: "if the photo is highly pixelated ignore it". `app.extract.face_quality`
reads two numbers off a face's crop - detail (Laplacian variance at no more than
112 px) and 8-pixel blockiness - and `face_detect.detect_faces` drops a face over
either line, after `big_enough`. These hold both measures on synthetic sharp and
spoilt crops, both sides of each line, the finder dropping a pixelated face and
keeping a sharp one, and a small sharp face left to the size rule alone.

The crops are made here, from noise, so no photograph is committed. The
measurement behind the two lines is in `face_quality`'s docstring.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from app.core.face_size import SMALLEST_FACE_PX
from app.extract import face_quality as fq

cv2 = pytest.importorskip("cv2")


def _sharp(side: int = 160, seed: int = 5) -> np.ndarray:
    """A face-sized patch with fine detail at every scale: smoothed grain plus
    a few broad shapes, BGR uint8."""
    rng = np.random.default_rng(seed)
    grain = cv2.GaussianBlur(rng.random((side, side)).astype("float32") * 255, (0, 0), 0.8)
    yy, xx = np.mgrid[0:side, 0:side] / side
    broad = 60 * np.sin(6 * xx) * np.cos(4 * yy)
    grey = np.clip(0.6 * grain + broad + 40, 0, 255).astype("uint8")
    return np.dstack([grey, grey, grey])


def _shrunk_and_back(image: np.ndarray, to: int) -> np.ndarray:
    h, w = image.shape[:2]
    small = cv2.resize(image, (to, to), interpolation=cv2.INTER_AREA)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_CUBIC)


def _jpeg(image: np.ndarray, quality: int) -> np.ndarray:
    ok, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def _whole(image: np.ndarray) -> tuple[float, float, float, float]:
    return (0.0, 0.0, float(image.shape[1]), float(image.shape[0]))


# -- the two measures, both sides of each line -------------------------------------

def test_a_sharp_crop_has_detail_and_no_blocks():
    sharp = _sharp()
    assert fq.detail(sharp) > fq.LEAST_DETAIL
    assert fq.blockiness(sharp) < fq.MOST_BLOCKY
    assert not fq.too_pixelated(sharp, _whole(sharp))


@pytest.mark.parametrize("to", [10, 14])
def test_a_face_blown_up_from_a_thumbnail_has_too_little_detail(to):
    spoilt = _shrunk_and_back(_sharp(), to)
    assert fq.detail(spoilt) < fq.LEAST_DETAIL
    assert fq.too_pixelated(spoilt, _whole(spoilt))


def test_a_blurred_face_has_too_little_detail():
    spoilt = cv2.GaussianBlur(_sharp(), (0, 0), 8)
    assert fq.detail(spoilt) < fq.LEAST_DETAIL
    assert fq.too_pixelated(spoilt, _whole(spoilt))


@pytest.mark.parametrize("quality", [5, 8])
def test_a_hard_saved_jpeg_is_too_blocky_though_its_detail_is_high(quality):
    spoilt = _jpeg(_sharp(), quality)
    assert fq.detail(spoilt) > fq.LEAST_DETAIL, "blocks are edges: detail alone misses this"
    assert fq.blockiness(spoilt) > fq.MOST_BLOCKY
    assert fq.too_pixelated(spoilt, _whole(spoilt))


def test_an_ordinary_jpeg_is_kept():
    kept = _jpeg(_sharp(), 75)
    assert fq.blockiness(kept) < fq.MOST_BLOCKY
    assert not fq.too_pixelated(kept, _whole(kept))


def test_the_blocks_are_found_wherever_the_grid_falls():
    """A rotation or crop moves the 8-pixel grid; the strongest of the eight
    positions is taken, so an offset crop reads the same."""
    spoilt = _jpeg(_sharp(200), 5)
    assert fq.blockiness(spoilt[3:, 5:]) > fq.MOST_BLOCKY


def test_a_big_crop_is_read_at_112_and_never_enlarged():
    big = _sharp(448)
    shrunk = fq._shrink(fq._grey(big), fq.MEASURE_SIDE)
    assert min(shrunk.shape) == fq.MEASURE_SIDE
    small = fq._grey(_sharp(60))
    assert fq._shrink(small, fq.MEASURE_SIDE) is small


def test_a_crop_that_cannot_be_read_is_not_judged():
    assert not fq.too_pixelated(np.zeros((10, 10, 3)), (5.0, 5.0, 6.0, 6.0))
    assert not fq.too_pixelated(None, (0.0, 0.0, 50.0, 50.0))


# -- the face finder ---------------------------------------------------------------

def _engine(boxes):
    def found(x1, y1, x2, y2):
        return SimpleNamespace(bbox=np.array([x1, y1, x2, y2], dtype="float32"),
                               normed_embedding=np.ones(4, dtype="float32"), det_score=0.9)
    return SimpleNamespace(get=lambda _image: [found(*b) for b in boxes])


def test_the_face_finder_drops_a_pixelated_face_and_keeps_a_sharp_one(monkeypatch, tmp_path):
    from app.extract import face_detect

    photo = np.zeros((200, 400, 3), dtype="uint8")
    photo[20:180, 20:180] = _sharp()                              # sharp, left
    photo[20:180, 220:380] = _shrunk_and_back(_sharp(seed=9), 12)  # blown up, right
    monkeypatch.setattr(face_detect, "_load", lambda: _engine([(20, 20, 180, 180),
                                                              (220, 20, 380, 180)]))
    monkeypatch.setattr(face_detect, "_read_bgr", lambda *_a: photo)

    faces = face_detect.detect_faces(tmp_path / "pair.jpg")

    assert [f.bbox[0] for f in faces] == [20.0]


def test_a_small_sharp_face_is_left_to_the_size_rule(monkeypatch, tmp_path):
    """Detail is read on a small face's own pixels, never enlarged - so a sharp
    face just over `SMALLEST_FACE_PX` is kept, and one just under is dropped by
    size, as before."""
    from app.extract import face_detect

    side = SMALLEST_FACE_PX + 4
    small = _sharp(side, seed=2)
    assert not fq.too_pixelated(small, _whole(small))
    photo = np.zeros((120, 120, 3), dtype="uint8")
    photo[10:10 + side, 10:10 + side] = small
    photo[70:70 + side, 70:70 + side] = small
    under = SMALLEST_FACE_PX - 1
    monkeypatch.setattr(face_detect, "_load", lambda: _engine([
        (10, 10, 10 + side, 10 + side), (70, 70, 70 + under, 70 + under)]))
    monkeypatch.setattr(face_detect, "_read_bgr", lambda *_a: photo)

    faces = face_detect.detect_faces(tmp_path / "small.jpg")

    assert [f.bbox[2] for f in faces] == [float(side)]
