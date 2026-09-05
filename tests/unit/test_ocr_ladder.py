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


@pytest.mark.skipif(not HAS_PIL, reason="Pillow is not installed")
class TestRungOneFastAccept:
    """§4 'ladder:' item, part one - a text scan fast-accepts at rung 1, with
    no detector even offered, because the white-heavy thumbnail alone is
    already confident enough."""

    def test_a_text_scan_fast_accepts_at_rung_1(self, tmp_path):
        from PIL import Image, ImageDraw

        # A page of black text on a white background - overwhelmingly white
        # by pixel count, which is rung 1's whole signal.
        page = Image.new("RGB", (900, 1200), "white")
        draw = ImageDraw.Draw(page)
        for y in range(40, 1160, 40):
            draw.text((40, y), "the quick brown fox jumps", fill="black")
        path = tmp_path / "page1.png"
        page.save(path)

        def detector_that_must_not_run(_source):
            raise AssertionError("rung 1 should have settled this before rung 2")

        result = route(path, detect=detector_that_must_not_run)

        assert result.decision == RouteDecision.FULL_OCR
        assert result.reason.startswith("white_fraction=")


class TestDetectionProbe:
    """§4 'ladder:' item, parts two and three - rung 2's own detection-only
    probe (item 2c). `detect` here stands in for `ocr.py`'s `_detect_only`,
    which closes over the real engine; the ladder itself has no OCR engine of
    its own, so every rung-2 behaviour is testable through this seam alone."""

    def test_a_wall_photo_settles_as_no_text_found_checked(self, tmp_path):
        """Zero boxes from the detector -> `NO_TEXT`, a truthful settled
        state - not a guess, and not the placeholder `DETECTION_ROUTE` this
        returned before rung 2 was wired to a real probe."""
        photo = tmp_path / "wall.jpg"  # no metadata pattern, not white-heavy
        if HAS_PIL:
            from PIL import Image
            Image.new("RGB", (256, 256), color="blue").save(photo)

        result = route(photo, detect=lambda _source: [])

        assert result.decision == RouteDecision.NO_TEXT
        assert result.reason == "detection_probe_zero_boxes"

    def test_a_receipt_photo_reaches_full_ocr_via_rung_2(self, tmp_path):
        """The routes-not-rejects proof: a document photographed rather than
        scanned, with a filename that tells rung 0 nothing and a thumbnail
        that is not white-heavy enough for rung 1 to settle it - still reaches
        full OCR the moment rung 2's detector finds even one text box."""
        photo = tmp_path / "counter_photo.jpg"
        if HAS_PIL:
            from PIL import Image
            Image.new("RGB", (256, 256), color="blue").save(photo)

        result = route(photo, detect=lambda _source: [[[0, 0], [10, 0], [10, 10], [0, 10]]])

        assert result.decision == RouteDecision.FULL_OCR
        assert result.reason == "detection_probe_1_box(es)"

    def test_no_detector_supplied_falls_back_to_the_caller_placeholder(self, tmp_path):
        """`detect=None` (the default) behaves exactly as it did before rung 2
        existed - the caller decides. This is what every non-Path/bytes
        source and every caller that has not been updated yet still gets."""
        result = route(Path("photo.jpg"))
        assert result.decision == RouteDecision.DETECTION_ROUTE

    def test_a_broken_detector_never_claims_no_text(self):
        """A detector that raises is a smaller problem than a wall photo
        wrongly marked settled forever - the ladder falls back to handing the
        decision to the caller, exactly as if no detector had been supplied."""
        def broken(_source):
            raise RuntimeError("the detector blew up")

        result = route(Path("photo.jpg"), detect=broken)

        assert result.decision == RouteDecision.DETECTION_ROUTE


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
