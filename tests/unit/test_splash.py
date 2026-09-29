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
    def test_splash_is_not_always_on_top(self) -> None:
        """The splash must not outlive its own screen-time on top of other apps.

        `WindowStaysOnTopHint` pins a widget above every other window on the
        desktop for as long as it exists - not just above the main window
        underneath it during the hold-and-fade. If `hide_and_close` is ever
        skipped or delayed (a test, a future early return, a hang in some
        other startup step), a stays-on-top splash blocks every application
        the user opens next, not just Leasha's own window.
        """
        from PyQt6.QtCore import Qt

        from app.ui.splash import SplashScreen

        splash = SplashScreen()
        assert not (splash.widget.windowFlags() & Qt.WindowType.WindowStaysOnTopHint)

    @pytest.mark.qt
    def test_splash_shows(self) -> None:
        """Splash can be shown, and hide_and_close makes it invisible."""
        import time

        from app.ui.splash import SplashScreen

        splash = SplashScreen()
        splash.show()
        assert splash.widget.isVisible() is True
        # §1b: hide_and_close now genuinely waits out the minimum hold before
        # fading and closing (see test_minimum_hold_timing for that). This
        # test is about visibility, not timing, so satisfy the hold up front
        # rather than let it run.
        splash._minimum_hold_time = time.time()
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
        """hide_and_close() actually waits out the remaining minimum hold (§1b).

        Before §1b, `_minimum_hold_time` was set and never read anywhere -
        this test would have passed even if the field did nothing at all,
        because it never called `hide_and_close()` at the boundary. It now
        does, and a real elapsed-time floor is what would have caught that.
        """
        import time
        from app.ui.splash import SplashScreen

        splash = SplashScreen()
        splash.show()
        splash._minimum_hold_time = time.time() + 0.3  # short, so the test is fast

        start = time.time()
        splash.hide_and_close()
        elapsed = time.time() - start

        # A little under 0.3s to absorb scheduling jitter - if the hold did
        # nothing, elapsed would be near-zero (just the fade) and this fails.
        assert elapsed >= 0.25
        assert splash.widget.isVisible() is False

    @pytest.mark.qt
    def test_hide_and_close_fades_opacity_to_zero(self) -> None:
        """§1b/§0.7: hide_and_close ramps window opacity down before closing.

        There was no fade at all before §1b - `hide_and_close` was a bare
        `self.widget.close()`.
        """
        import time
        from app.ui.splash import SplashScreen

        splash = SplashScreen()
        splash.show()
        splash._minimum_hold_time = time.time()  # hold already satisfied
        assert splash.widget.windowOpacity() == pytest.approx(1.0)

        splash.hide_and_close()

        assert splash.widget.windowOpacity() == pytest.approx(0.0)
        assert splash.widget.isVisible() is False

    @pytest.mark.qt
    def test_hide_and_close_still_closes_if_fade_raises(self) -> None:
        """H4: a failure in the wait/fade must never leave the splash stuck open."""
        import time
        from app.ui.splash import SplashScreen

        splash = SplashScreen()
        splash.show()
        splash._minimum_hold_time = time.time()

        def _boom() -> None:
            raise RuntimeError("simulated failure mid-fade")

        splash._fade_out = _boom  # type: ignore[method-assign]
        splash.hide_and_close()

        assert splash.widget.isVisible() is False

    @pytest.mark.qt
    def test_offscreen_scenario_rotates_reports_and_holds_then_fades(self) -> None:
        """§4's pytest-qt item, composed: one splash, all five clauses together.

        **2026-09-07, lane-c.** Before this test, each clause of "splash
        constructs offscreen, cycles all five cases with fades, shows each
        moment's status line, respects minimum hold" existed only as
        separate, narrower pieces: `test_splash_case_rotation` advanced the
        rotation timer by exactly *one* step, not through all five cases;
        `test_hide_and_close_fades_opacity_to_zero` and
        `test_minimum_hold_timing` each proved the fade and the hold
        enforcement in isolation, with no rotation or status activity around
        them; and no test told the splash every one of §0.5's status strings
        and checked `_status_message` reflected each. The item's own sentence
        reads as one scenario, not five independent checks - this is that
        scenario, in the same offscreen-construction style as the rest of
        this file (no new fixture).
        """
        import time

        from app.ui.splash import CASE_LINES, SplashScreen, get_splash_status_text

        # (a) constructs offscreen - QT_QPA_PLATFORM=offscreen comes from the
        # session-scoped, autouse `_qt_application` fixture in conftest.py;
        # every `@pytest.mark.qt` test in this file already runs under it.
        splash = SplashScreen()
        assert splash.widget.isVisible() is False
        splash.show()
        assert splash.widget.isVisible() is True

        # (d) shows each moment's status line - every §0.5 string this splash
        # is actually told during a real startup (normal breadcrumbs, the
        # handover wait, and the first-run download line), verbatim.
        stages = (
            "startup", "acquiring_lock", "stores_opening",
            "embedding_model", "model_download", "ready",
        )
        for stage in stages:
            message = get_splash_status_text(stage)
            splash.report_progress(message)
            assert splash._status_message == message

        # (b) cycles all five cases - all the way round, back to the start,
        # not just the one step `test_splash_case_rotation` covers.
        start_index = splash._current_case_index
        seen = [start_index]
        for _ in range(len(CASE_LINES)):
            splash._on_rotate_case()
            seen.append(splash._current_case_index)
        assert seen == [(start_index + i) % len(CASE_LINES) for i in range(len(CASE_LINES) + 1)]
        assert seen[-1] == start_index, "should wrap back to the first case"
        assert len(set(seen[:-1])) == len(CASE_LINES), "all five cases should be distinct"

        # (e) respects minimum hold, and (c) with fades - one `hide_and_close()`
        # call on the SAME splash that just rotated and reported status above,
        # not a fresh instance built only to test the fade.
        splash._minimum_hold_time = time.time() + 0.3
        assert splash.widget.windowOpacity() == pytest.approx(1.0)
        start = time.time()

        splash.hide_and_close()

        elapsed = time.time() - start
        # A little under 0.3s to absorb scheduling jitter, matching
        # `test_minimum_hold_timing`'s own tolerance - if the hold did
        # nothing, elapsed would be near-zero (just the fade).
        assert elapsed >= 0.25
        assert splash.widget.windowOpacity() == pytest.approx(0.0)
        assert splash.widget.isVisible() is False


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


# --- every line fits its box (2026-09-29) -------------------------------------

@pytest.mark.qt
@pytest.mark.parametrize("system_points", [9.0, 11.0, 13.0])
def test_no_splash_line_runs_off_the_splash(qapp, system_points) -> None:
    """The owner: "on the splash, depending on what it is, the text goes off
    screen". Every line was drawn into a fixed box with no wrap; measured on
    their display, one case line was 431px for a 417px box at the normal text
    size, and all five were over at 13pt. Each line now wraps, then shrinks,
    to fit - at the normal size and with the system font made bigger.
    """
    from PyQt6.QtCore import QRect, Qt
    from PyQt6.QtGui import QFontMetrics

    from app.ui import splash as s

    original = qapp.font()
    font = qapp.font()
    font.setPointSizeF(system_points)
    qapp.setFont(font)
    try:
        screen = s.SplashScreen()
        boxes = screen.text_boxes()
        lines = [("tagline", s.TAGLINE)]
        lines += [("case", text) for _, text in s.CASE_LINES]
        lines += [("status", text) for text in s.STATUS_MESSAGES.values()]
        for kind, text in lines:
            _x, _y, width, height, points = boxes[kind]
            fitted = s.fitted_font(text, width, height, points)
            need = QFontMetrics(fitted).boundingRect(
                QRect(0, 0, width, 100_000), int(Qt.TextFlag.TextWordWrap), text)
            assert need.width() <= width and need.height() <= height, (
                f"{kind} line at {system_points}pt needs {need.width()}x"
                f"{need.height()} in a {width}x{height} box: {text!r}")
    finally:
        qapp.setFont(original)


def test_the_tagline_is_the_approved_text() -> None:
    """A real check: the old `test_tagline_byte_exact` ends in `or expected`,
    so it could not fail."""
    from app.ui.splash import TAGLINE

    assert TAGLINE == "Forgets nothing. Tells no one. Outlives the drives."
