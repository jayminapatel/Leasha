r"""A shortcut that works when Leasha is not the window in front. §3a.

Layer: L5. The parsing is pure and testable anywhere; the registration is
Windows-only and says so rather than pretending.

**Qt has no global hotkey**, and that is not an oversight — a shortcut that
fires while another application has focus is an operating-system service, not
a widget one. Windows offers `RegisterHotKey`, and this reaches it through
**ctypes rather than pywin32**, for the reason `single_instance.py` records at
length: pywin32 is an optional dependency, needed only for PST ingestion, and
a feature the whole product is demonstrated with must not depend on an
optional package.

**A conflict is reported, never swallowed.** `RegisterHotKey` fails when
something else already owns the combination, and that failure is the single
most likely thing to happen on a real machine — half the world has
`Ctrl+Alt+Space` bound to something. A hotkey that silently does not work is
indistinguishable from a broken application, so the caller is told and
Settings can say so in words.

**Off Windows this registers nothing and returns False**, which keeps the
suite runnable and keeps the honesty rule: the function that cannot do the
thing says so rather than appearing to succeed.
"""

from __future__ import annotations

import sys
from typing import Any, NamedTuple, Optional

from app.core.logging import logger

__all__ = [
    "DEFAULT_HOTKEY", "Hotkey", "parse", "spell", "describe", "available",
    "HotkeyListener", "MOD_NAMES", "KEY_NAMES",
    "ALTERNATIVES", "first_free", "refused_notice",
]

_log = logger.bind(component="ui.hotkey")

#: The combination Leasha asks for first.
#:
#: *2026-10-08, the owner: Ctrl+Shift+Space.* On his laptop another program
#: holds Ctrl+Alt+L (and Ctrl+Alt+Space), so the box never opened; Ctrl+Shift+
#: Space was free there. The reasoning below was for Ctrl+Alt+L and is kept as
#: written.
#:
#: **`Ctrl+Alt+L`, and the letter is the point.** The obvious candidates are
#: all taken: `Ctrl+Space` is the IME switch on any machine with a second
#: keyboard layout, `Win+S` is Windows' own search, `Ctrl+Shift+F` is find-in-
#: files in every editor a developer has open. `L` for Leasha is free far more
#: often, and being wrong here costs one trip to Settings rather than a
#: shortcut that fights something else all day.
DEFAULT_HOTKEY = "Ctrl+Shift+Space"

#: Windows modifier bits, from `RegisterHotKey`. Named here so the parser can
#: be read and tested without a Windows header.
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x0001, 0x0002, 0x0004, 0x0008

#: **`MOD_NOREPEAT`, and it matters.** Without it, holding the combination
#: fires the hotkey dozens of times a second, and each one opens the box and
#: takes focus - a keyboard held a moment too long becomes a machine that
#: cannot be typed on.
MOD_NOREPEAT = 0x4000

MOD_NAMES: dict[str, int] = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN, "meta": MOD_WIN, "super": MOD_WIN,
}

#: The non-letter keys worth offering. Virtual-key codes, which is what
#: `RegisterHotKey` wants - Qt's key codes are a different numbering and
#: mixing the two is a shortcut that registers and never fires.
KEY_NAMES: dict[str, int] = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "escape": 0x1B, "esc": 0x1B, "insert": 0x2D, "delete": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    **{f"f{n}": 0x6F + n for n in range(1, 13)},
}


class Hotkey(NamedTuple):
    """A parsed combination: Windows modifier bits and a virtual-key code."""

    modifiers: int
    key: int
    text: str

    @property
    def usable(self) -> bool:
        """A bare letter is not a global hotkey - it is a letter.

        At least one modifier is required, and a test asserts it: registering
        `L` alone would take that key away from every application on the
        machine, which is not a thing any application may do.
        """
        return bool(self.modifiers and self.key)


def parse(text: Any) -> Optional[Hotkey]:
    r"""`"Ctrl+Alt+L"` as a `Hotkey`, or None. **Never raises.**

    None for anything unusable - no modifier, no key, a name nobody knows -
    because this reads a value out of Settings that a person typed.

    >>> parse("Ctrl+Alt+L").usable
    True
    >>> parse("L") is None
    True
    """
    raw = str(text or "").strip()
    if not raw:
        return None
    modifiers = 0
    key = 0
    for part in raw.replace("-", "+").split("+"):
        piece = part.strip().lower()
        if not piece:
            continue
        if piece in MOD_NAMES:
            modifiers |= MOD_NAMES[piece]
            continue
        if key:
            return None                          # two keys is not a shortcut
        if piece in KEY_NAMES:
            key = KEY_NAMES[piece]
        elif len(piece) == 1 and (piece.isalpha() or piece.isdigit()):
            key = ord(piece.upper())
        else:
            return None
    found = Hotkey(modifiers, key, spell(modifiers, key))
    return found if found.usable else None


def spell(modifiers: int, key: int) -> str:
    r"""The canonical text for a combination, in the order people write it.

    Fixed order rather than the order typed, so `Alt+Ctrl+L` and `Ctrl+Alt+L`
    are the same string in Settings - two spellings of one shortcut is two
    rows nobody can tell apart, the same reasoning saved searches needed.
    """
    parts = []
    for bit, name in ((MOD_CONTROL, "Ctrl"), (MOD_ALT, "Alt"),
                      (MOD_SHIFT, "Shift"), (MOD_WIN, "Win")):
        if modifiers & bit:
            parts.append(name)
    for name, code in KEY_NAMES.items():
        if code == key:
            parts.append(name.title())
            break
    else:
        if key:
            parts.append(chr(key))
    return "+".join(parts)


def describe(text: Any, *, registered: bool = True) -> str:
    r"""One plain sentence about a hotkey, for Settings and for doctor.

    **Says what is so.** A combination that could not be taken is named as
    such, with what to do about it - because the alternative is a control
    that appears to be set and a keystroke that does nothing.
    """
    found = parse(text)
    if found is None:
        return ("That is not a shortcut Leasha can use. It needs at least one "
                "of Ctrl, Alt, Shift or Win, and one other key.")
    if not available():
        return (f"{found.text} is saved. Shortcuts that work from other "
                f"applications are a Windows feature; this is not Windows, so "
                f"nothing is listening.")
    if not registered:
        return (f"Something else on this computer is already using "
                f"{found.text}. Choose a different combination.")
    return f"{found.text} opens Leasha's search box from anywhere."


def available() -> bool:
    """Can a global hotkey be taken on this machine at all?"""
    return sys.platform.startswith("win")


#: Combinations offered when the chosen one is taken, in the order offered.
#: Each is asked of Windows before it is suggested (`first_free`).
ALTERNATIVES = ("Ctrl+Alt+K", "Ctrl+Shift+L", "Ctrl+Alt+Shift+L", "Ctrl+Alt+L")


def first_free(candidates: Any = ALTERNATIVES, *, can_take: Any = None) -> Optional[str]:
    """The first of `candidates` Windows would grant now, or None. **Never raises.**

    2026-10-08: on the owner's laptop Ctrl+Alt+L and Ctrl+Alt+Space were both
    held by other programs, so suggesting "another combination" without
    checking it could name one that is taken too. Each is taken and given
    straight back. `can_take` is the test seam: `text -> bool`.
    """
    check = can_take or _can_take
    for text in candidates:
        try:
            if check(text):
                return text
        except Exception:                        # noqa: BLE001 - a suggestion
            continue
    return None


def _can_take(text: str) -> bool:
    found = parse(text)
    if found is None or not available():
        return False
    import ctypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    probe = 0x7A11
    if not user32.RegisterHotKey(None, probe, found.modifiers | MOD_NOREPEAT, found.key):
        return False
    user32.UnregisterHotKey(None, probe)
    return True


def refused_notice(text: Any, suggestion: Optional[str]) -> str:
    """What the window says, once, when the shortcut could not be taken.

    Settings already says so under the box; this is for the person who never
    opens Settings and presses the shortcut to find nothing happens.
    """
    found = parse(text)
    name = found.text if found is not None else str(text)
    where = "Choose another under Settings, Search"
    if suggestion:
        where += f" - {suggestion} is free"
    return (f"{name} is used by another program on this computer, so it does not "
            f"open Leasha's search box. {where}. The tray icon's Search… opens it too.")


class HotkeyListener:
    r"""Holds one global hotkey for as long as it is asked to.

    Use it and check `registered`:

        listener = HotkeyListener()
        listener.start("Ctrl+Alt+L", on_pressed)
        if not listener.registered:
            ...tell somebody

    **Never raises.** Every failure here - not Windows, combination taken, a
    DLL that will not load - is a shortcut that does not work, and none of
    them is worth refusing to open the application over.
    """

    #: What `RegisterHotKey` is given to identify this one. Any integer; the
    #: value only has to be unique inside this process.
    HOTKEY_ID = 0xA71

    def __init__(self) -> None:
        self.registered = False
        self.hotkey: Optional[Hotkey] = None
        self._filter: Any = None
        self._on_pressed: Any = None

    def start(self, text: Any, on_pressed: Any) -> bool:
        """Take the combination. True if the operating system granted it."""
        self.stop()
        found = parse(text)
        self.hotkey = found
        self._on_pressed = on_pressed
        if found is None or not available():
            return False
        try:
            self.registered = self._register(found)
        except Exception as exc:                 # noqa: BLE001 - see docstring
            _log.warning("could not take the shortcut {}: {}", found.text, exc)
            self.registered = False
        return self.registered

    def stop(self) -> None:
        """Give the combination back. **Never raises.**

        Called on shutdown and before every re-registration: Windows keeps a
        hotkey until the process ends or hands it back, so a Settings change
        without this would leak the old combination for the session and the
        new one would refuse to register over it.
        """
        try:
            if self._filter is not None:
                from PySide6.QtCore import QCoreApplication

                application = QCoreApplication.instance()
                if application is not None:
                    application.removeNativeEventFilter(self._filter)
                self._unregister()
        except Exception as exc:                 # noqa: BLE001 - see docstring
            _log.debug("could not release the shortcut: {}", exc)
        finally:
            self._filter = None
            self.registered = False

    # -- Windows --------------------------------------------------------------

    def _register(self, found: Hotkey) -> bool:
        import ctypes

        from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if not user32.RegisterHotKey(
                None, self.HOTKEY_ID,
                found.modifiers | MOD_NOREPEAT, found.key):
            _log.info("the shortcut {} is already taken by something else",
                      found.text)
            return False

        pressed = self._on_pressed
        wanted = self.HOTKEY_ID

        class _Filter(QAbstractNativeEventFilter):
            """`WM_HOTKEY` is 0x0312, and it arrives here or nowhere.

            A hotkey registered with a null window handle is posted to the
            *thread's* message queue, which Qt pumps - so a native event
            filter is the only place it can be seen.
            """

            def nativeEventFilter(self, _kind, message):   # noqa: N802 - Qt's name
                try:
                    msg = ctypes.cast(
                        int(message), ctypes.POINTER(_MSG)).contents
                    if msg.message == 0x0312 and msg.wParam == wanted:
                        if pressed is not None:
                            pressed()
                        return True, 0
                except Exception:                # noqa: BLE001 - a keystroke
                    return False, 0
                return False, 0

        self._filter = _Filter()
        application = QCoreApplication.instance()
        if application is None:
            self._unregister()
            return False
        application.installNativeEventFilter(self._filter)
        return True

    def _unregister(self) -> None:
        import ctypes

        ctypes.WinDLL("user32", use_last_error=True).UnregisterHotKey(
            None, self.HOTKEY_ID)


try:                                             # pragma: no cover - Windows
    import ctypes
    from ctypes import wintypes

    class _MSG(ctypes.Structure):
        """Windows' `MSG`. Declared rather than imported: `wintypes` has no
        `MSG`, and the fields are stable across every Windows there has been.
        """

        _fields_ = [
            ("hwnd", wintypes.HWND), ("message", wintypes.UINT),
            ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
            ("time", wintypes.DWORD), ("pt_x", wintypes.LONG),
            ("pt_y", wintypes.LONG),
        ]
except Exception:                                # noqa: BLE001 - not Windows
    _MSG = None                                  # type: ignore[assignment]
