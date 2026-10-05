r"""Bringing another process's window to the front, and the message that asks for it.

Layer: L0 (part of `app.core.osbridge`)

**Moved here, unchanged, from `app/core/run_lock.py`** (2026-10-05). The two
functions were written there on the day the second launch learned to front the
open window at once, and they call `user32` - which the osbridge guard
(`tests/unit/test_osbridge_guard.py`) refuses anywhere outside this package.
`run_lock` keeps both names and hands the work on, so nothing that calls them
had to change. The Windows calls, their argument types and their order are
exactly what they were.

**macOS and Linux are answered honestly**: `0` for the message number and
`None` for the fronting - "nothing was touched" - which is what the callers
have always received there. The second launch then falls back to the polled
`run_lock.request_front`, which works on every system. A Mac way to front
another process's window at once (`NSRunningApplication.activate`) is not
built; it would be a new feature, not a move.

Never raises. Fronting is a nicety; the poll still fronts the window.
"""

from __future__ import annotations

from typing import Optional

from app.core.osbridge._platform import is_windows

__all__ = ["front_message_id", "front_window"]


def front_message_id(name: str) -> int:
    """This session's number for the message called `name`; 0 off Windows or on failure."""
    if not is_windows():
        return 0
    try:
        # Imported here, not at the top: `ctypes.WinDLL` only exists on Windows,
        # and importing this module must never fail on a Mac.
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.RegisterWindowMessageW.argtypes = [ctypes.c_wchar_p]
        user32.RegisterWindowMessageW.restype = ctypes.c_uint
        return int(user32.RegisterWindowMessageW(name))
    except Exception:                    # noqa: BLE001
        return 0


def front_window(pid: int, hwnd: int, message_name: str) -> Optional[bool]:
    """Bring another process's window to the front. See `run_lock.front_window`.

    None when the handle is gone or belongs to another process (or off
    Windows); True when the window was told directly with the message called
    `message_name`; False when it is alive but the message could not be posted.
    """
    if not is_windows():
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
        user32.PostMessageW.argtypes = [
            wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]

        handle = wintypes.HWND(hwnd)
        if not user32.IsWindow(handle):
            return None
        owner = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
        if owner.value != int(pid):
            return None
        user32.AllowSetForegroundWindow(int(pid))
        message = front_message_id(message_name)
        if message and user32.PostMessageW(handle, message, 0, 0):
            return True
        if user32.IsIconic(handle):
            user32.ShowWindow(handle, 9)     # SW_RESTORE - back to maximised if it was
        user32.SetForegroundWindow(handle)
        return False
    except Exception:                    # noqa: BLE001 - a nicety; the poll still fronts it
        return None
