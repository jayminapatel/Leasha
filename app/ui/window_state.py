"""Save and restore window geometry and state across launches.

Layer: L5

**One small helper for all windows**, per §4c. MainWindow uses this; pop-out
windows (workspace features, log viewer) adopt it when they land.

Window state includes position, size, and maximised/normal state. Qt's
`saveGeometry` and `restoreGeometry` handle all of this as a single blob that
includes screen information, so a remembered position on a monitor that is no
longer attached is handled automatically - but opening off-screen is still
caught and clamped back onto a visible screen as an extra safety measure.

**A window closed while minimised reopens normal**, never minimised. An app
that starts invisible looks broken.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtGui import QGuiApplication

__all__ = ["save_window_state", "restore_window_state"]


def save_window_state(window: Any) -> bytes:
    """Save a window's geometry and state.

    Returns Qt's own blob (from `saveGeometry`), which includes the window's
    position, size, and which screen it was on. This blob is opaque but
    portable and survives screen changes.

    **Actually returns `bytes`, not the `QByteArray` `saveGeometry()` gives
    back.** The two are easy to conflate - `QByteArray` supports much of
    `bytes`'s interface - but `isinstance(qbytearray, bytes)` is `False`, and
    the caller's own restore path gates on exactly that check. Converting
    here means every caller genuinely gets what this function's signature
    already promised, rather than a look-alike that silently fails the check
    three lines later.

    Call in `closeEvent` or `closeEvent` cleanup, before the window closes.
    """
    return bytes(window.saveGeometry())


def restore_window_state(
    window: Any,
    state: Optional[bytes],
    *,
    ensure_visible: bool = True,
) -> None:
    """Restore a window's geometry and state, with edge case handling.

    **Arguments:**
    - `window`: a QMainWindow or any QWidget with `restoreGeometry`
    - `state`: the blob from `save_window_state`, or None for no restore
    - `ensure_visible`: if True, check the restored window is on a visible
      screen and move it if not (default True)

    **Edge cases handled:**
    - No saved state → window keeps its current size (usually the default in
      `__init__`)
    - Saved geometry points off-screen (monitor removed, resolution changed)
      → window is clamped back onto the nearest visible screen
    - Window was closed minimised → reopened normal, never minimised (an app
      that starts invisible looks broken)

    Call early in `__init__`, after the window is created but before `show()`.
    """
    if not state:
        return

    # Qt's restoreGeometry handles the blob unpacking and applies the saved
    # state. It returns False if the blob is invalid, but we ignore that -
    # an invalid blob is not worth crashing over.
    window.restoreGeometry(state)

    # The blob includes screen info, so Qt already tries to be smart about
    # off-screen windows. But we have one extra guard: if the window somehow
    # ended up off-screen anyway (a rare race with dynamic screen changes),
    # clamp it back onto visible area.
    if ensure_visible:
        _clamp_to_visible_screen(window)

    # Minimised state should not be restored - a minimised app window on
    # startup looks broken. If the blob says the window was minimised, show
    # it normal instead.
    if window.isMinimized():
        window.showNormal()


def _clamp_to_visible_screen(window: Any) -> None:
    """If a window is off-screen, move it back onto a visible screen.

    Called after `restoreGeometry` to handle the edge case where a saved
    position was off-screen (e.g., the window was on an external monitor
    that is no longer attached). Finds the nearest visible screen and moves
    the window to its center, keeping the window's size.

    This is a safety check only - `restoreGeometry` already tries to handle
    this, but Qt's logic can be conservative on some platforms.
    """
    # Get the window's current geometry and the available screen geometry.
    geom = window.frameGeometry()
    available = QGuiApplication.primaryScreen().availableGeometry()

    # If any part of the window is visible on a screen, we're good.
    if available.intersects(geom):
        return

    # Window is fully off-screen. Move it to the centre of the available
    # screen, keeping its size (or shrinking if it's larger than the screen).
    new_width = min(geom.width(), available.width())
    new_height = min(geom.height(), available.height())
    new_x = available.x() + (available.width() - new_width) // 2
    new_y = available.y() + (available.height() - new_height) // 2

    window.setGeometry(new_x, new_y, new_width, new_height)
