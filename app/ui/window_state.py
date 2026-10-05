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

__all__ = ["save_window_state", "restore_window_state", "bring_forward"]


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


def bring_forward(window: Any) -> None:
    """Show a window in front, **keeping it maximised if it was**.

    2026-09-27, reported by the owner: "the window does not remember its last
    state - it was maximised". Every path that brought Leasha back - the tray
    icon, a second launch, a `leasha://` link, "show me all of it" from the
    mini search - called `showNormal()`. That undoes minimising, but it also
    undoes *maximising*: Qt's "normal" means neither. So a maximised window
    hidden to the tray came back small.

    What each of those paths actually wants is narrower:
    - **minimised** -> un-minimise, back to whatever it was before (maximised
      or not). Clearing just the minimised flag does exactly that.
    - **hidden** (closed to the tray) -> show it. `show()` keeps the window's
      state, so a maximised window reappears maximised.
    - then raise it and give it focus. `raise_` alone is advisory on Windows;
      `activateWindow` is the half that brings it in front.
    """
    from PyQt6.QtCore import Qt

    state = window.windowState()
    if state & Qt.WindowState.WindowMinimized:
        window.setWindowState((state & ~Qt.WindowState.WindowMinimized)
                              | Qt.WindowState.WindowActive)
    if not window.isVisible():
        window.show()
    window.raise_()
    window.activateWindow()


def listen_for_front(on_front: Any) -> Any:
    r"""Call `on_front` the moment a second launch says "come forward".

    2026-10-05: a window hidden to the tray waited for the four-second poll of
    `run_lock.FRONT_STATE_KEY`. The second launch now posts
    `run_lock.FRONT_MESSAGE_NAME` to this window's handle - which a hidden
    window still has - and this filter hears it at once.

    **An application filter, not a `nativeEvent` override.** Overriding
    `MainWindow.nativeEvent` crashed the window while it was being built
    (an access violation inside `restoreGeometry`, 2026-10-05); the hotkey's
    filter (`ui/hotkey.py`) is the pattern this codebase has proven.

    Returns the filter, which the caller keeps alive; None off Windows or
    when it cannot be installed. Never raises.
    """
    import sys

    if sys.platform != "win32":
        return None
    try:
        import ctypes

        from PyQt6.QtCore import QAbstractNativeEventFilter, QCoreApplication

        from app.core.run_lock import front_message_id

        wanted = front_message_id()
        application = QCoreApplication.instance()
        if not wanted or application is None:
            return None
        offset = ctypes.sizeof(ctypes.c_void_p)   # MSG: HWND, then UINT message

        class _Filter(QAbstractNativeEventFilter):
            def nativeEventFilter(self, _kind, message):   # noqa: N802 - Qt's name
                try:
                    code = ctypes.c_uint.from_address(int(message) + offset).value
                    if code == wanted:
                        on_front()
                        return True, 0
                except Exception:                # noqa: BLE001 - never break the pump
                    pass
                return False, 0

        found = _Filter()
        application.installNativeEventFilter(found)
        return found
    except Exception:                            # noqa: BLE001
        return None
