"""RAW extractor: extract JPEG preview from camera RAW files.

Layer: L2

RAW files are camera sensor dumps - pixels only, no text. The embedded JPEG
preview has the same content, made at the same moment, and costs seconds
instead of minutes. OCR the preview, not the raw.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.extract.raw import RawExtractor

HAS_RAWPY = importlib.util.find_spec("rawpy") is not None


class TestRawExtractor:
    """The RAW extractor."""

    def test_extractor_name_is_raw(self):
        """Extractor is named 'raw'."""
        assert RawExtractor().name == "raw"

    def test_supports_canon_raw(self):
        """Canon CR2 files are supported."""
        assert RawExtractor().supports(Path("photo.cr2"))

    def test_supports_nikon_raw(self):
        """Nikon NEF files are supported."""
        assert RawExtractor().supports(Path("photo.nef"))

    def test_supports_adobe_raw(self):
        """Adobe DNG files are supported."""
        assert RawExtractor().supports(Path("photo.dng"))

    def test_supports_sony_raw(self):
        """Sony ARW files are supported."""
        assert RawExtractor().supports(Path("photo.arw"))

    def test_does_not_support_jpg(self):
        """JPEGs are not RAW files."""
        assert not RawExtractor().supports(Path("photo.jpg"))

    def test_requires_rawpy(self):
        """Extractor declares rawpy as optional dependency."""
        requires = RawExtractor().requires
        names = [r.module for r in requires]
        assert "rawpy" in names

    def test_requires_rawpy_is_not_hard(self):
        """Graceful degradation when rawpy is missing."""
        requires = RawExtractor().requires
        rawpy_req = next((r for r in requires if r.module == "rawpy"), None)
        assert rawpy_req is not None
        assert not rawpy_req.hard  # Soft dependency


class TestRawExtractorNeverRaises:
    """The extractor is defensive."""

    def test_extract_missing_file_returns_empty(self):
        """Extracting a missing file returns nothing, never raises."""
        result = list(RawExtractor().extract(Path("/nonexistent/file.cr2")))
        assert result == []

    def test_extract_without_rawpy_returns_empty(self, monkeypatch):
        """Without rawpy, extraction degrades gracefully."""
        if HAS_RAWPY:
            pytest.skip("rawpy is installed")

        # Extract should return nothing since rawpy is unavailable
        result = list(RawExtractor().extract(Path("photo.cr2")))
        assert result == []
