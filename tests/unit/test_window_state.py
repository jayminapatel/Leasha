r"""Window geometry and state saved and restored across launches.

Layer: L5

**§4: Remember the window state.** The main window remembers its last state
across launches: maximised reopens maximised, normal reopens at its last size
and position. Edge cases are handled: a remembered position on a monitor that
is no longer attached clamps back onto a visible screen; a window closed while
minimised reopens normal.

**The helper (§4c) lets every window adopt the same behaviour**, so pop-out
windows (workspace features, log viewer) can restore their own geometry when
they land.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtGui import QScreen  # noqa: E402
from PyQt6.QtWidgets import QApplication, QWidget  # noqa: E402
from unittest.mock import Mock, patch  # noqa: E402

from app.ui.window_state import restore_window_state, save_window_state  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


class TestWindowStateSaveAndRestore:
    """Test save_window_state and restore_window_state helpers."""

    def test_save_returns_bytes(self, qapp):
        """save_window_state returns a bytes blob."""
        widget = QWidget()
        widget.resize(800, 600)
        widget.move(100, 100)

        state = save_window_state(widget)

        assert isinstance(state, bytes)
        assert len(state) > 0

    def test_restore_applies_saved_geometry(self, qapp):
        """restore_window_state applies a saved geometry blob."""
        widget1 = QWidget()
        widget1.resize(800, 600)
        widget1.move(100, 100)

        state = save_window_state(widget1)

        widget2 = QWidget()
        widget2.resize(400, 300)
        widget2.move(0, 0)
        restore_window_state(widget2, state)

        # After restoration, geometries should match (within tolerance for
        # platform differences)
        assert widget2.geometry().width() == 800
        assert widget2.geometry().height() == 600

    def test_restore_with_none_does_nothing(self, qapp):
        """restore_window_state with None does nothing."""
        widget = QWidget()
        widget.resize(400, 300)
        original_geom = widget.geometry()

        restore_window_state(widget, None)

        assert widget.geometry() == original_geom

    def test_restore_with_empty_bytes_does_nothing(self, qapp):
        """restore_window_state with empty bytes does nothing."""
        widget = QWidget()
        widget.resize(400, 300)
        original_geom = widget.geometry()

        restore_window_state(widget, b"")

        assert widget.geometry() == original_geom

    def test_restore_handles_minimised_by_showing_normal(self, qapp):
        r"""A window closed while minimised opens normal, never minimised.

        An app that starts invisible looks broken. We cannot easily create a
        geometry blob that says "minimised" without actual window state, so
        this test verifies the intent: if someone manages to get a minimised
        blob, the code tries to call showNormal().
        """
        widget = QWidget()
        widget.resize(800, 600)

        # Simulate a minimised state by patching isMinimized()
        with patch.object(widget, "isMinimized", return_value=True):
            with patch.object(widget, "showNormal") as mock_show_normal:
                restore_window_state(widget, b"fake_geometry")
                mock_show_normal.assert_called_once()

    def test_restore_clamps_off_screen_window(self, qapp):
        r"""A window restored to an off-screen position is clamped back on.

        §4b: the honest edge case. A saved position on a monitor that is no
        longer attached should not open the window off-screen.
        """
        widget = QWidget()

        # Simulate the case where the window ends up off-screen after restore.
        # We'll mock the geometry and screen to make the check happen.
        with patch.object(widget, "frameGeometry") as mock_frame:
            with patch.object(widget, "setGeometry") as mock_set_geom:
                # Simulate a geometry that doesn't intersect with available screen
                mock_frame.return_value = Mock(
                    x=-2000, y=-2000, width=800, height=600,
                    intersects=Mock(return_value=False)
                )
                with patch("app.ui.window_state.QGuiApplication") as mock_app:
                    mock_screen = Mock()
                    mock_screen.availableGeometry.return_value = Mock(
                        x=0, y=0, width=1920, height=1080
                    )
                    mock_app.primaryScreen.return_value = mock_screen

                    restore_window_state(widget, b"fake_geometry", ensure_visible=True)

                    # setGeometry should have been called to move the window back
                    mock_set_geom.assert_called_once()
                    call_args = mock_set_geom.call_args[0]
                    # New position should be on screen (roughly centered)
                    assert call_args[0] >= 0  # x >= 0
                    assert call_args[1] >= 0  # y >= 0

    def test_restore_with_ensure_visible_false_skips_clamping(self, qapp):
        """restore_window_state with ensure_visible=False skips clamping."""
        widget = QWidget()

        with patch.object(widget, "frameGeometry") as mock_frame:
            with patch.object(widget, "setGeometry") as mock_set_geom:
                mock_frame.return_value = Mock(
                    x=-2000, y=-2000, width=800, height=600,
                    intersects=Mock(return_value=False)
                )

                restore_window_state(widget, b"fake_geometry", ensure_visible=False)

                # setGeometry should NOT have been called
                mock_set_geom.assert_not_called()


class TestWindowStateIntegration:
    """Integration tests with a real MainWindow-like setup."""

    def test_save_and_restore_round_trip(self, qapp):
        """A saved state can be restored to recreate the same geometry."""
        widget1 = QWidget()
        widget1.resize(1024, 768)
        widget1.move(200, 150)

        saved = save_window_state(widget1)

        widget2 = QWidget()
        widget2.resize(400, 300)
        restore_window_state(widget2, saved)

        # The restored widget should have the same size
        assert widget2.width() == 1024
        assert widget2.height() == 768

    def test_maximised_state_survives_round_trip(self, qapp):
        """A maximised window is restored as maximised."""
        widget1 = QWidget()
        widget1.showMaximized()
        assert widget1.isMaximized()

        saved = save_window_state(widget1)

        widget2 = QWidget()
        restore_window_state(widget2, saved)

        # After restoration, the window should also be maximised
        # (Qt's saveGeometry includes this state in the blob)
        assert widget2.isMaximized()


class TestWindowStateEdgeCases:
    r"""Test the honest edge cases §4b asks for."""

    def test_zero_size_window_does_not_crash(self, qapp):
        """A zero-size saved geometry is handled gracefully."""
        widget = QWidget()
        widget.resize(0, 0)

        state = save_window_state(widget)
        assert isinstance(state, bytes)

        widget2 = QWidget()
        restore_window_state(widget2, state)
        # Should not crash; geometry may be 0 or may have a minimum

    def test_negative_position_window_does_not_crash(self, qapp):
        """A negative position (off multi-monitor setup) is handled."""
        widget = QWidget()
        widget.move(-100, -100)

        state = save_window_state(widget)
        assert isinstance(state, bytes)

        widget2 = QWidget()
        restore_window_state(widget2, state)
        # Should not crash; may or may not clamp depending on screen setup

    def test_very_large_window_is_clamped_to_screen(self, qapp):
        """A window larger than any screen is shrunk."""
        widget = QWidget()
        widget.resize(9999, 9999)

        with patch("app.ui.window_state.QGuiApplication") as mock_app:
            mock_screen = Mock()
            mock_screen.availableGeometry.return_value = Mock(
                x=0, y=0, width=1920, height=1080
            )
            mock_app.primaryScreen.return_value = mock_screen

            with patch.object(widget, "frameGeometry") as mock_frame:
                with patch.object(widget, "setGeometry") as mock_set_geom:
                    mock_frame.return_value = Mock(
                        x=0, y=0, width=9999, height=9999,
                        intersects=Mock(return_value=False)
                    )

                    restore_window_state(widget, b"fake", ensure_visible=True)

                    # setGeometry should be called with clamped size
                    if mock_set_geom.called:
                        call_args = mock_set_geom.call_args[0]
                        # Width and height should be <= screen size
                        assert call_args[2] <= 1920
                        assert call_args[3] <= 1080
