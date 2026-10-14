"""Workspace §4d: HEIC/HEIF preview, extending the image pipeline.

Layer: L5

**The same optional dependency `extract/ocr.py` already declares.** Qt has no
HEIC/HEIF image plugin, so `decode_image` routes these two extensions through
`pillow-heif` and PIL instead of the usual `QImage(path)` one-liner - and
degrades exactly like any other unreadable image when the optional package
is not installed, which on this machine it genuinely is not: the first two
tests below exercise the real absence, not a simulated one.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

from PySide6.QtWidgets import QApplication                       # noqa: E402

from app.ui.preview_loader import KIND_IMAGE, decode_image, kind_for  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def test_heic_and_heif_are_routed_to_the_image_kind():
    assert kind_for(Path("photo.heic")) == KIND_IMAGE
    assert kind_for(Path("photo.HEIF")) == KIND_IMAGE


def test_decoding_degrades_quietly_without_the_optional_package(tmp_path: Path):
    """`pillow-heif` is genuinely not installed in this environment - this is
    the real absence, not a simulated one. `None`, not an exception, exactly
    like any other image `decode_image` cannot read."""
    fake = tmp_path / "photo.heic"
    fake.write_bytes(b"not a real HEIC file")

    assert decode_image(str(fake)) is None


def test_a_missing_file_decodes_to_none_not_an_exception():
    assert decode_image("Z:/not/mounted/photo.heic") is None


# --- the successful path, with pillow-heif faked in --------------------------

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
    """The seam is faked, the same way `ocr.py`'s own tests fake its engine:
    `pillow-heif` need not be installed for this path to be exercised."""
    import sys
    import types

    fake_module = types.ModuleType("pillow_heif")
    fake_module.register_heif_opener = lambda: None
    monkeypatch.setitem(sys.modules, "pillow_heif", fake_module)
    monkeypatch.setattr("PIL.Image.open", lambda _path: _FakeFrame())

    photo = tmp_path / "photo.heic"
    photo.write_bytes(b"stub")

    image = decode_image(str(photo))

    assert image is not None
    assert not image.isNull()
    assert (image.width(), image.height()) == (4, 2)


def test_a_broken_heic_after_the_package_is_present_still_returns_none(
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

    assert decode_image(str(photo)) is None


@pytest.mark.parametrize("suffix, fmt", [(".png", "PNG"), (".jpg", "JPEG")])
def test_a_picture_too_big_for_qt_is_read_smaller_not_left_blank(tmp_path, monkeypatch, suffix, fmt):
    """2026-10-15: pictures over Qt's 256 MB image limit showed as blank tiles.
    The limit is lowered to 1 MB here, so a 1000-pixel picture stands in for one."""
    from PIL import Image
    from PySide6.QtGui import QImage, QImageReader

    import app.ui.preview_loader as loader

    photo = tmp_path / f"huge{suffix}"
    Image.new("RGB", (1000, 600), "orange").save(photo, fmt)
    monkeypatch.setattr(loader, "TOO_BIG_EDGE", 400)
    before = QImageReader.allocationLimit()
    QImageReader.setAllocationLimit(1)
    try:
        assert QImage(str(photo)).isNull()            # the refusal the owner saw
        image = loader.decode_image(str(photo))
    finally:
        QImageReader.setAllocationLimit(before)
    assert image is not None and (image.width(), image.height()) == (400, 240)
