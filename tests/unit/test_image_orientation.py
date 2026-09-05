"""3b: EXIF orientation honoured wherever an image is decoded for display.

Layer: L5

**Portrait photos must not render sideways.** `read_orientation` already
existed in `app/extract/exif.py`, but nothing called it anywhere in the tree
- confirmed 2026-09-05 by grepping `app/ui/preview_loader.py` and both image
extractors. This tests the wiring: `decode_image`'s two branches (the plain
`QImage(path)` path every non-HEIF image format takes, and `_decode_heif` for
HEIC/HEIF) both now read the tag and correct for it before the pane ever sees
the pixels.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)

from PyQt6.QtGui import QImage                                  # noqa: E402
from PyQt6.QtWidgets import QApplication                        # noqa: E402

from app.ui.preview_loader import _apply_orientation, decode_image  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _four_corner_image() -> QImage:
    """A 4x2 `QImage` with a distinct colour in each corner.

    Non-square on purpose: a 90/270 rotation swapping width and height is a
    silent no-op on a square image and would prove nothing.
    """
    image = QImage(4, 2, QImage.Format.Format_RGB32)
    image.fill(0xFF000000)                              # black background
    image.setPixelColor(0, 0, _rgb(255, 0, 0))           # top-left: red
    image.setPixelColor(3, 0, _rgb(0, 255, 0))           # top-right: green
    image.setPixelColor(0, 1, _rgb(0, 0, 255))           # bottom-left: blue
    image.setPixelColor(3, 1, _rgb(255, 255, 0))         # bottom-right: yellow
    return image


def _rgb(r: int, g: int, b: int):
    from PyQt6.QtGui import QColor

    return QColor(r, g, b)


# --- `_apply_orientation`: the pure transform, independent of any decode ----

def test_orientation_1_is_left_untouched(qapp):
    image = _four_corner_image()
    result = _apply_orientation(image, 1)
    assert result is image                # no transform built, no copy made


def test_an_unrecognised_code_is_left_untouched(qapp):
    image = _four_corner_image()
    result = _apply_orientation(image, 0)
    assert result is image


def test_orientation_6_rotates_90_and_swaps_dimensions(qapp):
    image = _four_corner_image()
    result = _apply_orientation(image, 6)
    assert (result.width(), result.height()) == (2, 4)
    # top-left (red) rotated 90 clockwise lands at top-right.
    assert result.pixelColor(1, 0).getRgb()[:3] == (255, 0, 0)


def test_orientation_8_rotates_the_other_way_and_swaps_dimensions(qapp):
    image = _four_corner_image()
    result = _apply_orientation(image, 8)
    assert (result.width(), result.height()) == (2, 4)
    # top-left (red) rotated 90 counter-clockwise lands at bottom-left.
    assert result.pixelColor(0, 3).getRgb()[:3] == (255, 0, 0)


def test_orientation_3_rotates_180_keeping_dimensions(qapp):
    image = _four_corner_image()
    result = _apply_orientation(image, 3)
    assert (result.width(), result.height()) == (4, 2)
    # top-left (red) rotated 180 lands at bottom-right.
    assert result.pixelColor(3, 1).getRgb()[:3] == (255, 0, 0)


def test_orientation_2_mirrors_horizontally(qapp):
    image = _four_corner_image()
    result = _apply_orientation(image, 2)
    assert (result.width(), result.height()) == (4, 2)
    # top-left (red) mirrored horizontally lands at top-right.
    assert result.pixelColor(3, 0).getRgb()[:3] == (255, 0, 0)


# --- `decode_image`: the real pipeline, a JPEG with a real EXIF tag --------

def _save_jpeg_with_orientation(path: Path, orientation: int, size=(4, 2)) -> None:
    from PIL import Image

    img = Image.new("RGB", size, color="red")
    if orientation != 1:
        exif = img.getexif()
        exif[0x0112] = orientation
        img.save(str(path), format="JPEG", exif=exif)
    else:
        img.save(str(path), format="JPEG")


def test_decode_image_applies_a_real_files_orientation_tag(qapp, tmp_path: Path):
    photo = tmp_path / "portrait.jpg"
    _save_jpeg_with_orientation(photo, 6, size=(4, 2))

    image = decode_image(str(photo))

    assert image is not None
    # Stored as a 4x2 landscape frame with Orientation 6: corrected, it is
    # a 2x4 portrait - the whole point of 3b.
    assert (image.width(), image.height()) == (2, 4)


def test_decode_image_leaves_a_photo_with_no_orientation_tag_alone(qapp, tmp_path: Path):
    photo = tmp_path / "no_exif.jpg"
    _save_jpeg_with_orientation(photo, 1, size=(4, 2))

    image = decode_image(str(photo))

    assert image is not None
    assert (image.width(), image.height()) == (4, 2)


# --- HEIC/HEIF: the same correction, through the PIL branch ----------------

class _FakeFrame:
    def __init__(self) -> None:
        self.width = 4
        self.height = 2

    def convert(self, _mode: str) -> "_FakeFrame":
        return self

    def tobytes(self, _raw: str, _mode: str) -> bytes:
        return bytes(self.width * self.height * 4)

    def __enter__(self) -> "_FakeFrame":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def test_heic_decode_also_applies_orientation(qapp, monkeypatch, tmp_path: Path):
    """Same seam `test_heif_preview.py` fakes `pillow_heif` with - this adds
    a faked `read_orientation` so the HEIC branch's own correction, not just
    the plain-`QImage` branch's, is exercised end to end."""
    import sys
    import types

    fake_module = types.ModuleType("pillow_heif")
    fake_module.register_heif_opener = lambda: None
    monkeypatch.setitem(sys.modules, "pillow_heif", fake_module)
    monkeypatch.setattr("PIL.Image.open", lambda _path: _FakeFrame())
    monkeypatch.setattr("app.extract.exif.read_orientation", lambda _path: 6)

    photo = tmp_path / "photo.heic"
    photo.write_bytes(b"stub")

    image = decode_image(str(photo))

    assert image is not None
    # The fake frame is 4x2; Orientation 6 corrects it to 2x4, exactly as
    # the plain-JPEG path above.
    assert (image.width(), image.height()) == (2, 4)
