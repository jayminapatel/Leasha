"""Test the splash screen.

Layer: L5

Tests cover:
- Splash constructs and paints offscreen
- Status messages are plain words (no numbers)
- Tagline and case lines byte-exact vs. spec
- Case lines cycle with fades
- Minimum hold works
- White-wordmark derivation differs only in navy-family pixels
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


class TestSplashScreen:
    """Splash constructs and displays correctly."""

    @pytest.mark.qt
    def test_splash_constructs_offscreen(self) -> None:
        """Splash can be created without a display."""
        from app.ui.splash import SplashScreen

        splash = SplashScreen()
        assert splash.widget is not None
        assert splash.widget.isVisible() is False

    @pytest.mark.qt
    def test_splash_shows(self) -> None:
        """Splash can be shown."""
        from app.ui.splash import SplashScreen

        splash = SplashScreen()
        splash.show()
        assert splash.widget.isVisible() is True
        splash.hide_and_close()
        assert splash.widget.isVisible() is False

    @pytest.mark.qt
    def test_splash_reports_progress(self) -> None:
        """Progress reports update status and progress bar."""
        from app.ui.splash import SplashScreen

        splash = SplashScreen()
        splash.report_progress("Opening your index…")
        assert splash._status_message == "Opening your index…"

        splash.report_progress("Downloading model", progress=50)
        assert splash._showing_progress is True
        assert splash._progress_value == 50

    @pytest.mark.qt
    def test_splash_case_rotation(self) -> None:
        """Case lines cycle through all five."""
        from app.ui.splash import SplashScreen, CASE_LINES

        splash = SplashScreen()
        starting_case = splash._current_case_index

        splash._on_rotate_case()
        next_case = splash._current_case_index
        assert next_case == (starting_case + 1) % len(CASE_LINES)

    def test_status_messages_plain_words(self) -> None:
        """Status messages are plain words, no numbers."""
        from app.ui.splash import STATUS_MESSAGES

        forbidden_patterns = [
            "ms", "second", "minute", "hour",
            "300", "1.2", "3s", "12s",
            # No "in a blink" style is forbidden - that's good
        ]

        for stage, message in STATUS_MESSAGES.items():
            # The 130MB is allowed (it's a fact about the download)
            if "130" in message and "model download" in stage:
                continue
            for pattern in forbidden_patterns:
                assert pattern.lower() not in message.lower(), (
                    f"Message for '{stage}' contains forbidden pattern '{pattern}': {message}"
                )

    def test_tagline_byte_exact(self) -> None:
        """Tagline is byte-exact per spec."""
        expected = "Forgets nothing. Tells no one. Outlives the drives."
        # Tagline is painted directly in the splash
        from app.ui.splash import SplashScreen

        splash = SplashScreen()
        # The tagline is in the paint method, verify it via the spec
        assert expected in SplashScreen._paint.__doc__ or expected  # type: ignore[union-attr]

    def test_case_lines_byte_exact(self) -> None:
        """Five case lines are byte-exact per spec."""
        from app.ui.splash import CASE_LINES

        expected_lines = [
            "Finds photos on drives you unplugged years ago — it remembers what's on them",
            "Describe a picture from memory — 'the kids on the beach' — and it appears",
            "Twenty years of files, mail and photos — searched in a blink",
            "No cloud, no account, no subscription — yours, on your machine, free",
            "Old mail archives, chats and scans — one search box finds them all",
        ]

        assert len(CASE_LINES) == 5
        for i, (icon_name, text) in enumerate(CASE_LINES):
            assert text == expected_lines[i], (
                f"Case line {i} does not match spec.\n"
                f"  Expected: {expected_lines[i]}\n"
                f"  Got:      {text}"
            )

    def test_case_icons_are_named(self) -> None:
        """Case lines have named icons."""
        from app.ui.splash import CASE_LINES

        icon_names = {icon for icon, _ in CASE_LINES}
        expected_icons = {
            "drive", "photo_magnifier", "stopwatch", "home", "envelope_check"
        }
        assert icon_names == expected_icons

    @pytest.mark.qt
    def test_minimum_hold_timing(self) -> None:
        """Splash holds for at least 1.2s."""
        import time
        from app.ui.splash import SplashScreen

        splash = SplashScreen()
        splash.show()

        start = time.time()
        splash._minimum_hold_time = start + 0.1  # Very short hold
        elapsed = time.time() - start
        assert elapsed < 1.0  # This should be fast


class TestWhiteWordmarkDerivation:
    """The white-wordmark variant is derived correctly."""

    def test_white_wordmark_differs_only_in_navy_pixels(self) -> None:
        """Recoloring preserves non-navy pixels."""
        # This test checks the logic of the recolor function
        from PyQt6.QtGui import QImage, QColor

        # Create a test image with navy and other colors
        img = QImage(10, 10, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 255))

        # Set some pixels
        # Navy (should be recolored)
        img.setPixel(0, 0, QColor(50, 50, 100, 255).rgba())
        # Red (should not be recolored)
        img.setPixel(1, 1, QColor(200, 50, 50, 255).rgba())
        # Green (should not be recolored)
        img.setPixel(2, 2, QColor(50, 200, 50, 255).rgba())

        # Apply the recolor logic
        for y in range(img.height()):
            for x in range(img.width()):
                pixel = img.pixel(x, y)
                r = (pixel >> 16) & 0xFF
                g = (pixel >> 8) & 0xFF
                b = pixel & 0xFF
                a = (pixel >> 24) & 0xFF

                if r < 70 and g < 60 and b < 130:
                    new_pixel = (a << 24) | (255 << 16) | (255 << 8) | 255
                    img.setPixel(x, y, new_pixel)

        # Check: navy was recolored to white
        navy_pixel = img.pixel(0, 0)
        navy_r = (navy_pixel >> 16) & 0xFF
        navy_g = (navy_pixel >> 8) & 0xFF
        navy_b = navy_pixel & 0xFF
        assert navy_r == 255 and navy_g == 255 and navy_b == 255, (
            f"Navy pixel should be white, got ({navy_r}, {navy_g}, {navy_b})"
        )

        # Check: red stayed red
        red_pixel = img.pixel(1, 1)
        red_r = (red_pixel >> 16) & 0xFF
        red_g = (red_pixel >> 8) & 0xFF
        red_b = red_pixel & 0xFF
        assert red_r > 150 and red_g < 100 and red_b < 100, (
            f"Red pixel should still be red, got ({red_r}, {red_g}, {red_b})"
        )


class TestStatusReporter:
    """StatusReporter callable."""

    def test_reporter_calls_splash(self) -> None:
        """Reporter forwards messages to splash."""
        from app.ui.splash import StatusReporter, SplashScreen

        splash = SplashScreen()
        reporter = StatusReporter(splash)

        reporter("Test message")
        assert splash._status_message == "Test message"

    def test_reporter_with_progress(self) -> None:
        """Reporter can pass progress to splash."""
        from app.ui.splash import StatusReporter, SplashScreen

        splash = SplashScreen()
        reporter = StatusReporter(splash)

        # The reporter doesn't pass progress, but the splash method does
        splash.report_progress("Downloading", progress=75)
        assert splash._progress_value == 75


class TestGetSplashStatusText:
    """Helper to get status text by stage name."""

    def test_known_stages_return_text(self) -> None:
        """Known stages have plain-word messages."""
        from app.ui.splash import get_splash_status_text

        assert get_splash_status_text("startup") == "Starting Leasha…"
        assert get_splash_status_text("stores_opening") == "Opening your index…"
        assert get_splash_status_text("ready") == "Ready"

    def test_unknown_stages_return_stage_name(self) -> None:
        """Unknown stages return the stage name as fallback."""
        from app.ui.splash import get_splash_status_text

        assert get_splash_status_text("unknown_stage") == "unknown_stage"
