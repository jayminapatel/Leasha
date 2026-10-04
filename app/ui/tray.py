"""The application icon, and an optional system tray presence.

Layer: L5

**The tray is opt-in.** An application that vanishes from the taskbar when you
did not ask it to is alarming - you close a window, it disappears, and there is
no obvious way to get it back. Both preferences start off.

**Quitting from the tray must be a real quit.** A tray icon that leaves a
process holding the index lock produces `ERR_DB_LOCKED` on the next launch with
no visible cause and nothing on screen to blame - the worst kind of bug, because
the symptom appears minutes later in a different session. `quit_requested` goes
through the window's normal close path, which releases the mutex and flushes
SQLite.

**Two icon files, deliberately.** Below 48px the navy ellipse in the full mark
becomes an indistinct dark mass that swamps the three shapes, so the tray uses a
blobs-only variant. Verified by rendering every size and looking at them, which
is the only way to know.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "icon_path", "tray_icon_path", "install_window_icon",
    "set_app_user_model_id", "set_window_relaunch", "TrayPresence",
    "ICON_FILE", "TRAY_ICON_FILE",
]

ICON_FILE = "leasha.ico"
TRAY_ICON_FILE = "leasha-tray.ico"

#: Arbitrary but stable - Windows only uses this to tell one app's windows
#: apart from another's, never displays it.
APP_USER_MODEL_ID = "Leasha.Leasha.DesktopApp.1"


def set_app_user_model_id(app_id: str = APP_USER_MODEL_ID) -> bool:
    """Give this process its own taskbar identity, separate from pythonw.exe.

    *2026-10-04 note: there is now a Start Menu shortcut (`app/core/osbridge/startmenu.py`),
    and it carries this same ID, so the window and the shortcut group together.*

    L9 (packaging) has not started - see HANDOFF.md - so today the app is
    always launched as a plain script, with no packaged `.exe` and no Start
    Menu shortcut to carry an icon resource of its own. Without this call,
    Windows has nothing to key the taskbar icon on but the interpreter's own
    path, `pythonw.exe`, shared by every Python GUI script on the machine -
    `application.setWindowIcon` sets the *window's* icon, but the taskbar
    button shows the generic interpreter icon (or none) regardless, because
    that is a property of the process identity, not the window. This is the
    documented fix: `SetCurrentProcessExplicitAppUserModelID`, called before
    any window exists. Once L9 packages a real `.exe`, that carries its own
    identity and this call becomes a no-op in practice - safe to leave in.

    True if the call succeeded. Windows-only; a no-op everywhere else. A
    failure here is cosmetic, same as `install_window_icon` below - never a
    reason to refuse to start.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(  # type: ignore[attr-defined]
            app_id)
        return True
    except Exception:                             # noqa: BLE001 - cosmetic only
        return False


def set_window_relaunch(window_id: int) -> bool:
    """Say how Windows should relaunch this window, and with which icon.

    **The running button already has the icon** - checked on 2026-09-20 by
    reading the real taskbar while the window was open. What was not checked, and
    is the likely remaining gap, is a *pinned* button: pinning writes a shortcut,
    and by default a shortcut takes its icon and command from the program it
    launches, which here is `pythonw.exe`. These four properties on the window are
    what Windows reads to build that shortcut from Leasha instead.
    `RelaunchCommand` needs the app id beside it and a display name, so all four
    are set together.

    Windows-only, and a failure is cosmetic like the icon itself. A pin made
    before this ran keeps whatever it was created with: unpin it and pin again.
    """
    if sys.platform != "win32":
        return False
    try:
        from win32com.propsys import propsys, pscon

        root = Path(__file__).resolve().parents[2]
        launcher = root / "leasha.cmd"
        icon = icon_path()
        if not launcher.is_file() or icon is None:
            return False
        store = propsys.SHGetPropertyStoreForWindow(int(window_id), propsys.IID_IPropertyStore)
        for key, value in (
            (pscon.PKEY_AppUserModel_ID, APP_USER_MODEL_ID),
            (pscon.PKEY_AppUserModel_RelaunchCommand, f'"{launcher}"'),
            (pscon.PKEY_AppUserModel_RelaunchIconResource, f"{icon},0"),
            (pscon.PKEY_AppUserModel_RelaunchDisplayNameResource, "Leasha"),
        ):
            store.SetValue(key, propsys.PROPVARIANTType(value))
        store.Commit()
        return True
    except Exception:                             # noqa: BLE001 - cosmetic only
        return False


#: `(frozen bundle dir, argv[0])` -> the assets folder found for it. Work order 0r
#: item 2b: the window asks for this ~40 times while it is built (once per icon
#: file), and each answer cost three `resolve()` calls and a stat - about a third
#: of a second in all, on the way to the first paint. Only a *found* folder is
#: remembered, so a folder that does not exist yet is looked for again.
_ASSETS_DIR_CACHE: dict[tuple[str, str], Path] = {}


def assets_dir() -> Path:
    """Where the icons live, whether running from source or from a build.

    A frozen build puts `assets/` beside the executable; from source it is
    beside `app/`. Checking both means the icon does not silently vanish the
    first time somebody packages this.
    """
    key = (getattr(sys, "_MEIPASS", ""), sys.argv[0] if sys.argv else "")
    cached = _ASSETS_DIR_CACHE.get(key)
    if cached is not None:
        return cached
    candidates = [
        Path(getattr(sys, "_MEIPASS", "")) / "assets" if hasattr(sys, "_MEIPASS") else None,
        Path(sys.argv[0]).resolve().parent / "assets",
        Path(__file__).resolve().parents[2] / "assets",
    ]
    for candidate in candidates:
        if candidate and candidate.is_dir():
            _ASSETS_DIR_CACHE[key] = candidate
            return candidate
    return Path(__file__).resolve().parents[2] / "assets"


def icon_path(name: str = ICON_FILE) -> Optional[Path]:
    """The icon file, or None. **Never raises and never guesses.**

    A missing icon is a cosmetic loss; refusing to start over one would be
    absurd, and a silently substituted default would hide that the build is
    incomplete. Callers log the absence.
    """
    candidate = assets_dir() / name
    return candidate if candidate.is_file() else None


def tray_icon_path() -> Optional[Path]:
    return icon_path(TRAY_ICON_FILE)


def install_window_icon(application: Any) -> bool:
    """Set the taskbar and window icon. True if a file was found."""
    from PyQt6.QtGui import QIcon

    path = icon_path()
    if path is None:
        return False
    application.setWindowIcon(QIcon(str(path)))
    return True


class TrayPresence:
    """A tray icon, its menu, and the preferences that govern it.

    Deliberately not a QWidget: it owns a `QSystemTrayIcon` and forwards
    intentions to the window rather than acting on it, so the window keeps the
    single answer to "what does closing mean".
    """

    def __init__(self, window: Any) -> None:
        self._window = window
        self._tray: Any = None
        #: Both off. See the module docstring - vanishing unasked is alarming.
        self.minimise_to_tray = False
        self.close_to_tray = False

    # -- availability ---------------------------------------------------------

    @staticmethod
    def available() -> bool:
        """Is there a system tray at all?

        Returns False on a bare X session, some Linux desktops, and Windows with
        the notification area disabled by policy. **The preference is disabled
        with an explanation rather than failing silently**, because a switch that
        does nothing is worse than a switch that is not offered.
        """
        try:
            from PyQt6.QtWidgets import QSystemTrayIcon

            return bool(QSystemTrayIcon.isSystemTrayAvailable())
        except Exception:                        # noqa: BLE001 - a probe
            return False

    def install(self) -> bool:
        """Create the tray icon. False when there is no tray or no icon file."""
        if self._tray is not None:
            return True
        path = tray_icon_path()
        if path is None or not self.available():
            return False

        from PyQt6.QtGui import QIcon
        from PyQt6.QtWidgets import QMenu, QSystemTrayIcon

        self._tray = QSystemTrayIcon(QIcon(str(path)), self._window)
        menu = QMenu()
        menu.addAction("Show Leasha", self.restore)
        menu.addAction("Search…", self.restore_and_search)
        menu.addSeparator()
        self._status_action = menu.addAction("Indexing: idle")
        self._status_action.setEnabled(False)
        menu.addSeparator()
        # **A real quit.** Through the window's close path, so the mutex is
        # released and SQLite is flushed - see the module docstring.
        menu.addAction("Quit", self.quit)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_activated)
        self._tray.setToolTip("Leasha")
        self._tray.show()
        return True

    # -- behaviour ------------------------------------------------------------

    def _on_activated(self, reason: Any) -> None:
        from PyQt6.QtWidgets import QSystemTrayIcon

        # Both, because which one people expect differs by platform and habit,
        # and neither is destructive.
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.restore()

    def restore(self) -> None:
        # `bring_forward`, not `showNormal()`: the latter also un-maximises, so
        # a maximised window hidden to the tray used to come back small.
        from app.ui.window_state import bring_forward

        bring_forward(self._window)

    def restore_and_search(self) -> None:
        self.restore()
        focus = getattr(self._window, "_focus_search", None)
        if callable(focus):
            focus()

    def quit(self) -> None:
        """Close for real. `close_to_tray` is ignored here on purpose."""
        self.close_to_tray = False
        self.minimise_to_tray = False
        self.hide()
        self._window.close()

    def hide(self) -> None:
        if self._tray is not None:
            self._tray.hide()

    def set_status(self, text: str) -> None:
        """The tooltip and menu line, so the tray is informative while hidden."""
        if self._tray is None:
            return
        self._tray.setToolTip(f"Leasha — {text}")
        action = getattr(self, "_status_action", None)
        if action is not None:
            action.setText(f"Indexing: {text}")

    def notify_hidden(self) -> None:
        """Say where the window went, once.

        Somebody who has just turned this on has not yet learned that the icon
        by the clock is how they get back, and a window that disappears without
        explanation is indistinguishable from a crash.
        """
        if self._tray is None:
            return
        from PyQt6.QtWidgets import QSystemTrayIcon

        self._tray.showMessage(
            "Leasha is still running",
            "Click the icon by the clock to bring it back, or right-click for Quit.",
            QSystemTrayIcon.MessageIcon.Information,
            5_000,
        )

    @property
    def installed(self) -> bool:
        return self._tray is not None
