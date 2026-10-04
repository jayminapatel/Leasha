r"""EXIF is read from every picture format, HEIC included.

Layer: L2

2026-10-05: every reader in `app/extract/exif.py` asked `img._getexif()`, which
only JPEG has. For a HEIC photo it raised, the reader caught it, and 3,741 of
the owner's 15,011 photos had no date taken, no place and no rotation.
"""

from __future__ import annotations

import datetime

import pytest

from app.extract import exif


def test_a_heic_photos_date_and_place_are_read(tmp_path):
    pytest.importorskip("pillow_heif")
    from PIL import Image

    from app.extract.heif import register_heif

    register_heif()
    data = Image.Exif()
    data.get_ifd(0x8769)[36867] = "2023:06:30 17:24:24"          # DateTimeOriginal
    data[0x8825] = {1: "N", 2: (26.0, 0.0, 56.48), 3: "E", 4: (50.0, 29.0, 42.92)}
    path = tmp_path / "photo.heic"
    Image.new("RGB", (32, 24), (90, 140, 200)).save(path, format="HEIF", exif=data.tobytes())

    assert exif.read_datetime(path) == datetime.datetime(2023, 6, 30, 17, 24, 24)
    latitude, longitude = exif.read_gps(path)
    assert round(latitude, 3) == 26.016 and round(longitude, 3) == 50.495
    assert exif.read_orientation(path) == 1


def test_a_jpeg_still_reads_as_it_always_did(tmp_path):
    from PIL import Image

    data = Image.Exif()
    data.get_ifd(0x8769)[36867] = "2006:06:15 09:00:00"
    path = tmp_path / "photo.jpg"
    Image.new("RGB", (32, 24)).save(path, exif=data.tobytes())
    assert exif.read_datetime(path) == datetime.datetime(2006, 6, 15, 9, 0, 0)
