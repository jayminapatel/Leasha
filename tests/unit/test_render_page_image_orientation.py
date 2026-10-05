"""`render_page._image` at parity with `preview_loader.decode_image`.

Layer: L5

**The pop-out's own decode path, not a second one.** `_image` used to be a
bare `QImage(str(path))` - no EXIF orientation correction (a portrait photo
rendered sideways in the pinned window) and no HEIC/HEIF route at all (Qt has
no plugin for either, so a bare `QImage` returns a null image and the pop-out
showed nothing). `decode_image` in `preview_loader.py` already carries both
fixes; `_image` now calls it rather than re-deriving them. This mirrors
`test_image_orientation.py` and `test_heif_preview.py`, exercising
`render_page.render()`/`_image()` instead of `preview_loader.decode_image()`
directly.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from PySide6.QtWidgets import QApplication                        # noqa: E402

from app.ui.render_page import _image, render                   # noqa: E402
from app.ui.view_of_file import View                            # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _save_jpeg_with_orientation(path: Path, orientation: int, size=(4, 2)) -> None:
    from PIL import Image

    img = Image.new("RGB", size, color="red")
    if orientation != 1:
        exif = img.getexif()
        exif[0x0112] = orientation
        img.save(str(path), format="JPEG", exif=exif)
    else:
        img.save(str(path), format="JPEG")


# --- `_image`: the same real-file EXIF correction `decode_image` gets ------

def test_image_applies_a_real_files_orientation_tag(qapp, tmp_path: Path):
    photo = tmp_path / "portrait.jpg"
    _save_jpeg_with_orientation(photo, 6, size=(4, 2))

    image = _image(str(photo))

    assert image is not None
    # Stored as a 4x2 landscape frame with Orientation 6: corrected, it is a
    # 2x4 portrait - a sideways photo in the pop-out is exactly what this
    # closes the gap on.
    assert (image.width(), image.height()) == (2, 4)


def test_image_leaves_a_photo_with_no_orientation_tag_alone(qapp, tmp_path: Path):
    photo = tmp_path / "no_exif.jpg"
    _save_jpeg_with_orientation(photo, 1, size=(4, 2))

    image = _image(str(photo))

    assert image is not None
    assert (image.width(), image.height()) == (4, 2)


def test_a_missing_file_decodes_to_none_not_an_exception(qapp):
    assert _image("Z:/not/mounted/photo.jpg") is None


# --- HEIC/HEIF: routed through the same PIL branch `decode_image` uses -----

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


def test_a_decodable_heic_produces_a_real_image(qapp, monkeypatch, tmp_path: Path):
    """The seam is faked, the same way `test_heif_preview.py` fakes it:
    `pillow-heif` need not be installed for this path to be exercised. Before
    this fix, a bare `QImage(path)` had no HEIF plugin at all and this
    returned None - the pop-out showed nothing for a HEIC photo."""
    import sys
    import types

    fake_module = types.ModuleType("pillow_heif")
    fake_module.register_heif_opener = lambda: None
    monkeypatch.setitem(sys.modules, "pillow_heif", fake_module)
    monkeypatch.setattr("PIL.Image.open", lambda _path: _FakeFrame())

    photo = tmp_path / "photo.heic"
    photo.write_bytes(b"stub")

    image = _image(str(photo))

    assert image is not None
    assert not image.isNull()
    assert (image.width(), image.height()) == (4, 2)


def test_heic_decode_also_applies_orientation(qapp, monkeypatch, tmp_path: Path):
    import sys
    import types

    fake_module = types.ModuleType("pillow_heif")
    fake_module.register_heif_opener = lambda: None
    monkeypatch.setitem(sys.modules, "pillow_heif", fake_module)
    monkeypatch.setattr("PIL.Image.open", lambda _path: _FakeFrame())
    monkeypatch.setattr("app.extract.exif.read_orientation", lambda _path: 6)

    photo = tmp_path / "photo.heic"
    photo.write_bytes(b"stub")

    image = _image(str(photo))

    assert image is not None
    # The fake frame is 4x2; Orientation 6 corrects it to 2x4, exactly as the
    # plain-JPEG case above.
    assert (image.width(), image.height()) == (2, 4)


def test_a_broken_heic_still_returns_none_not_an_exception(
    qapp, monkeypatch, tmp_path: Path
):
    import sys
    import types

    fake_module = types.ModuleType("pillow_heif")
    fake_module.register_heif_opener = lambda: None
    monkeypatch.setitem(sys.modules, "pillow_heif", fake_module)

    def explode(_path):
        raise OSError("cannot identify image file")

    monkeypatch.setattr("PIL.Image.open", explode)

    photo = tmp_path / "photo.heic"
    photo.write_bytes(b"not really a heic file")

    assert _image(str(photo)) is None


# --- `render()`: the whole pop-out pipeline, kind="image" ------------------

def test_render_applies_exif_orientation_end_to_end(qapp, tmp_path: Path):
    """Not just `_image` in isolation - `render()` is what `preview_window.py`
    actually calls, so the fix has to survive `_shape`'s own rotate/scale
    pass too."""
    photo = tmp_path / "portrait.jpg"
    _save_jpeg_with_orientation(photo, 6, size=(4, 2))

    image = render(str(photo), kind="image", view=View())

    assert image is not None
    assert (image.width(), image.height()) == (2, 4)


def test_render_shows_a_heic_photo_instead_of_nothing(qapp, monkeypatch, tmp_path: Path):
    """Before this fix, `render()` on a HEIC file returned None - `_image`
    called bare `QImage(path)`, which has no HEIF plugin - and the pop-out
    showed its "could not be drawn" card for every HEIC/HEIF photo."""
    import sys
    import types

    fake_module = types.ModuleType("pillow_heif")
    fake_module.register_heif_opener = lambda: None
    monkeypatch.setitem(sys.modules, "pillow_heif", fake_module)
    monkeypatch.setattr("PIL.Image.open", lambda _path: _FakeFrame())

    photo = tmp_path / "photo.heic"
    photo.write_bytes(b"stub")

    image = render(str(photo), kind="image", view=View())

    assert image is not None
    assert (image.width(), image.height()) == (4, 2)
