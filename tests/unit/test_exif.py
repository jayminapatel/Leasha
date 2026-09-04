"""EXIF metadata extraction from images.

Layer: L2

**EXIF date is THE date for photos.** File mtime lies after drive copies.
EXIF DateTimeOriginal survives them and is load-bearing for time-based search.
"""

from __future__ import annotations

import datetime
import importlib.util
from pathlib import Path

import pytest

from app.extract.exif import read_datetime, read_orientation

HAS_PIL = importlib.util.find_spec("PIL") is not None


@pytest.mark.skipif(not HAS_PIL, reason="Pillow is not installed")
class TestExifDate:
    """EXIF date extraction."""

    def test_image_without_exif_returns_none(self, tmp_path):
        """A new image with no EXIF date returns None."""
        from PIL import Image

        img = Image.new("RGB", (100, 100), color="white")
        img_path = tmp_path / "no_exif.jpg"
        img.save(img_path)

        result = read_datetime(img_path)
        assert result is None

    def test_bad_path_returns_none(self):
        """A non-existent path returns None, never raises."""
        result = read_datetime(Path("/nonexistent/file.jpg"))
        assert result is None

    def test_corrupted_image_returns_none(self, tmp_path):
        """A file that is not an image returns None."""
        fake_img = tmp_path / "fake.jpg"
        fake_img.write_bytes(b"not an image")

        result = read_datetime(fake_img)
        assert result is None


@pytest.mark.skipif(not HAS_PIL, reason="Pillow is not installed")
class TestExifOrientation:
    """EXIF orientation tag extraction."""

    def test_image_without_orientation_returns_normal(self, tmp_path):
        """A new image with no orientation tag defaults to 1 (normal)."""
        from PIL import Image

        img = Image.new("RGB", (100, 100), color="white")
        img_path = tmp_path / "no_orient.jpg"
        img.save(img_path)

        result = read_orientation(img_path)
        assert result == 1  # Normal/0 degrees

    def test_bad_path_returns_normal(self):
        """A non-existent path returns 1 (normal), never raises."""
        result = read_orientation(Path("/nonexistent/file.jpg"))
        assert result == 1

    def test_corrupted_image_returns_normal(self, tmp_path):
        """A corrupted file returns 1 (normal), never raises."""
        fake_img = tmp_path / "fake.jpg"
        fake_img.write_bytes(b"not an image")

        result = read_orientation(fake_img)
        assert result == 1


class TestExifConsistency:
    """EXIF functions should never raise."""

    def test_read_datetime_never_raises(self, tmp_path):
        """read_datetime is safe to call on anything."""
        # None of these should raise
        read_datetime(Path("/dev/null"))
        read_datetime(tmp_path / "missing.jpg")
        read_datetime(tmp_path)  # directory

    def test_read_orientation_never_raises(self, tmp_path):
        """read_orientation is safe to call on anything."""
        # None of these should raise
        read_orientation(Path("/dev/null"))
        read_orientation(tmp_path / "missing.jpg")
        read_orientation(tmp_path)  # directory
