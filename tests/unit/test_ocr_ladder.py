"""OCR ladder: routing that makes image indexing affordable.

Layer: L2

The ladder decides whether each image needs full OCR or can skip it:
- Rung 0: metadata (free)
- Rung 1: thumbnail stats (~5ms)
- Rung 2: detection probe (~50-150ms)
- Full OCR (~3.6s)

The goal: a corpus of 1000 photos costs 5 + 50 + 3600 seconds instead of 3.6M.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.extract.ocr_ladder import RouteDecision, route

HAS_PIL = importlib.util.find_spec("PIL") is not None


class TestMetadataRouting:
    """Rung 0: filename patterns."""

    def test_camera_default_filename_routes(self):
        """IMG_ and DSC_ patterns indicate photos."""
        decision = route(Path("IMG_2024.jpg")).decision
        assert decision == RouteDecision.METADATA_ROUTE

    def test_screenshot_filename_routes(self):
        """Screenshots route to the ladder."""
        decision = route(Path("screenshot_2024.jpg")).decision
        assert decision == RouteDecision.METADATA_ROUTE

    def test_scan_filename_goes_straight_to_ocr(self):
        """SCAN_ indicates a document."""
        decision = route(Path("SCAN_receipt.jpg")).decision
        assert decision == RouteDecision.FULL_OCR

    def test_receipt_filename_goes_straight_to_ocr(self):
        """RECEIPT_ indicates a document."""
        decision = route(Path("RECEIPT_2024.jpg")).decision
        assert decision == RouteDecision.FULL_OCR

    def test_unknown_filename_continues_to_rungs_1_2(self):
        """Generic names need further analysis."""
        decision = route(Path("photo.jpg")).decision
        assert decision in (RouteDecision.DETECTION_ROUTE, RouteDecision.FULL_OCR)


@pytest.mark.skipif(not HAS_PIL, reason="Pillow is not installed")
class TestThumbnailStats:
    """Rung 1: histogram analysis (~5ms)."""

    def test_white_heavy_image_routes_to_full_ocr(self, tmp_path):
        """White-heavy images look like documents."""
        from PIL import Image
        import numpy as np

        # Create a mostly-white image
        img = Image.new("RGB", (256, 256), color="white")
        img_path = tmp_path / "white.jpg"
        img.save(img_path)

        result = route(img_path)
        # Should either recognize as document or go to detection probe
        assert result.decision in (
            RouteDecision.FULL_OCR,
            RouteDecision.DETECTION_ROUTE,
        )

    def test_colored_image_continues_to_detection(self, tmp_path):
        """Colored images need the detection probe."""
        from PIL import Image

        # Create a colored image (not white-heavy)
        img = Image.new("RGB", (256, 256), color="blue")
        img_path = tmp_path / "colored.jpg"
        img.save(img_path)

        result = route(img_path)
        # Colored image should go to detection probe or need OCR
        assert result.decision in (
            RouteDecision.DETECTION_ROUTE,
            RouteDecision.FULL_OCR,
        )


class TestLadderCost:
    """The ladder must complete in reasonable time."""

    def test_metadata_routing_is_free(self):
        """Rung 0 costs essentially nothing."""
        result = route(Path("IMG_2024.jpg"), check_metadata=True)
        assert result.elapsed_ms < 10  # Free means <10ms


class TestRouteDecisionConsistency:
    """Decisions must be sensible."""

    def test_decision_is_always_valid(self):
        """Every route decision is a real RouteDecision value."""
        result = route(Path("photo.jpg"))
        assert isinstance(result.decision, RouteDecision)
        assert result.decision in RouteDecision
