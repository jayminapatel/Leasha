r"""What is selected in the foreground application, for the mini-search box.
Adoptions §4a.

Layer: L5.

**Pre-fill only, never a search.** The box opens with the selected words
already typed in - selected, so the next keystroke replaces them - and it is
the person who decides what happens next. Nothing here ever calls the search
engine.

**Clipboard is the transport, and the clipboard is not this application's to
spend.** There is no supported way to ask an arbitrary foreground window "what
is selected" without either UI Automation (a much larger surface, and one that
some applications answer wrongly or not at all) or asking the window to copy
its selection the way a person would. This takes the second path - a
synthetic Ctrl+C - and the whole feature stands or falls on putting back
whatever was on the clipboard before it, unchanged. `snapshot_clipboard` and
`restore_clipboard` exist as their own functions, tested on their own,
because "restores byte-perfect" is the one promise here a person could
actually notice being broken.

**ctypes, not pywin32.** The same reasoning `hotkey.py` and
`single_instance.py` record: pywin32 is an optional dependency, needed only
for PST ingestion, and this is not allowed to need it either.

**Off Windows this reads nothing and returns `None`.** Consistent with
`hotkey.available()` - a function that cannot do the thing says so rather
than appearing to succeed.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Optional

from app.core.logging import logger

__all__ = [
    "available", "read_foreground_selection", "snapshot_clipboard",
    "restore_clipboard",
]

_log = logger.bind(component="ui.selection")

#: How long to give the foreground application to answer the synthetic copy.
#: Longer than a keystroke, shorter than a person would notice as a pause -
#: this runs between the hotkey firing and the box appearing.
COPY_TIMEOUT_S = 0.25
_POLL_S = 0.02


def available() -> bool:
    """Can a foreground selection be read on this machine at all?"""
    return sys.platform.startswith("win")


def snapshot_clipboard() -> Optional[Any]:
    r"""Every format currently on the clipboard, as an independent copy.

    **A copy, not the live handle Qt hands back.** `clipboard.mimeData()`
    describes the *system* clipboard at the moment it is called; reading it
    again after the clipboard has changed underneath is not defined to give
    the old answer. Copying each format's bytes into a fresh `QMimeData` is
    what makes "restore" mean something rather than nothing.

    `None` if there is no clipboard to read from (no `QGuiApplication` yet),
    which `restore_clipboard` treats as "nothing to put back".
    """
    from PyQt6.QtCore import QMimeData
    from PyQt6.QtGui import QGuiApplication

    clipboard = QGuiApplication.clipboard()
    if clipboard is None:
        return None
    live = clipboard.mimeData()
    snapshot = QMimeData()
    for fmt in live.formats():
        snapshot.setData(fmt, live.data(fmt))
    return snapshot


def restore_clipboard(snapshot: Optional[Any]) -> None:
    """Put a snapshot back, byte for byte. **Never raises.**"""
    if snapshot is None:
        return
    try:
        from PyQt6.QtGui import QGuiApplication

        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setMimeData(snapshot)
    except Exception as exc:                     # noqa: BLE001 - see docstring
        _log.warning("could not restore the clipboard: {}", exc)


def read_foreground_selection() -> Optional[str]:
    r"""The foreground application's selected text, or `None`. **Never raises.**

    **`None` for "there was no selection", not for "nothing happened".** A
    synthetic Ctrl+C into a window with nothing selected either does nothing
    or copies nothing new - either way the clipboard's text does not change,
    and that is exactly the signal used here: no change within the timeout
    means no selection, and the clipboard is left exactly as it was found.

    Off Windows, or on any failure, this returns `None` without touching the
    clipboard at all.
    """
    if not available():
        return None
    try:
        from PyQt6.QtGui import QGuiApplication

        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return None

        snapshot = snapshot_clipboard()
        before = clipboard.text()

        if not _send_copy():
            return None

        found = ""
        deadline = time.monotonic() + COPY_TIMEOUT_S
        while time.monotonic() < deadline:
            time.sleep(_POLL_S)
            current = clipboard.text()
            if current and current != before:
                found = current
                break

        restore_clipboard(snapshot)
        return found.strip() or None
    except Exception as exc:                     # noqa: BLE001 - see docstring
        _log.debug("could not read a foreground selection: {}", exc)
        return None


# -- Windows: the synthetic copy ---------------------------------------------

def _send_copy() -> bool:
    r"""Ctrl+C to whatever has focus, via `SendInput`. **Never raises.**

    `SendInput` rather than the older `keybd_event`: it is the API Microsoft
    documents as current, and it is no more code than the alternative -
    four `INPUT` structures, one call.
    """
    try:
        import ctypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)

        inputs = (_INPUT * 4)(
            _key_input(_VK_CONTROL),
            _key_input(_VK_C),
            _key_input(_VK_C, _KEYEVENTF_KEYUP),
            _key_input(_VK_CONTROL, _KEYEVENTF_KEYUP),
        )
        sent = user32.SendInput(4, ctypes.byref(inputs),
                                ctypes.sizeof(_INPUT))
        return int(sent) == 4
    except Exception as exc:                     # noqa: BLE001 - see docstring
        _log.debug("could not send the copy keystroke: {}", exc)
        return False


_VK_CONTROL = 0x11
_VK_C = 0x43
_KEYEVENTF_KEYUP = 0x0002
_INPUT_KEYBOARD = 1

try:                                             # pragma: no cover - Windows
    import ctypes as _ctypes

    class _KEYBDINPUT(_ctypes.Structure):
        _fields_ = [
            ("wVk", _ctypes.c_ushort), ("wScan", _ctypes.c_ushort),
            ("dwFlags", _ctypes.c_ulong), ("time", _ctypes.c_ulong),
            ("dwExtraInfo", _ctypes.POINTER(_ctypes.c_ulong)),
        ]

    class _INPUT_UNION(_ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT)]

    class _INPUT(_ctypes.Structure):
        _fields_ = [("type", _ctypes.c_ulong), ("union", _INPUT_UNION)]

    #: One extra-info cell shared by every synthetic key, since none of them
    #: read it back - `SendInput` only needs a valid pointer, not a live one.
    _EXTRA_INFO = _ctypes.c_ulong(0)

    def _key_input(vk: int, flags: int = 0) -> "_INPUT":
        union = _INPUT_UNION()
        union.ki = _KEYBDINPUT(vk, 0, flags, 0,
                               _ctypes.pointer(_EXTRA_INFO))
        return _INPUT(_INPUT_KEYBOARD, union)
except Exception:                                # noqa: BLE001 - not Windows
    _INPUT = None                                # type: ignore[assignment]

    def _key_input(vk: int, flags: int = 0) -> Any:  # pragma: no cover
        raise RuntimeError("not available off Windows")
