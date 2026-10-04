r"""The Start-menu shortcut: Leasha in the Start menu, with its own icon.

Until Layer 9 packages an `.exe`, the window is `pythonw.exe -m app.main` run
from the project folder, and nothing put it in the Start menu - the only ways
in were `leasha.cmd` and a typed command.

**`pythonw.exe` directly, not `leasha.cmd`.** A `.cmd` opens a console for the
moment it takes to `start` the window; the shortcut runs exactly what that
script runs for the window, without the flash.

**The window's own AppUserModelID on the shortcut.** The window sets
`app.ui.tray.APP_USER_MODEL_ID` on itself at startup, so Windows groups its
taskbar button by that ID. A shortcut without the same ID is a different
application as far as the taskbar is concerned: the running window would sit
beside the Start entry as a second, unrelated button.

**Per-user, no administrator rights** - `%APPDATA%\...\Start Menu\Programs`,
the same boundary `add-to-path.ps1` and the `leasha://` scheme keep. `remove()`
is the other half, written at the same time.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

__all__ = ["SHORTCUT_NAME", "shortcut_spec", "shortcut_path", "create", "remove", "exists"]

SHORTCUT_NAME = "Leasha.lnk"


def shortcut_spec(root: Path) -> dict[str, str]:
    """What the shortcut holds for an installation at `root`. Pure, so the
    contents are testable on any platform."""
    from app.ui.tray import APP_USER_MODEL_ID, ICON_FILE

    root = Path(root)
    return {
        "target": str(root / "venv" / "Scripts" / "pythonw.exe"),
        "arguments": "-m app.main",
        "working_directory": str(root),
        "icon": str(root / "assets" / ICON_FILE),
        "description": "Search everything on this machine",
        "app_user_model_id": APP_USER_MODEL_ID,
    }


def shortcut_path() -> Optional[Path]:
    """`%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Leasha.lnk`, or
    None where there is no `%APPDATA%` (off Windows)."""
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None
    return (Path(appdata) / "Microsoft" / "Windows" / "Start Menu"
            / "Programs" / SHORTCUT_NAME)


def create(root: Path, destination: Optional[Path] = None) -> bool:
    """Write the shortcut. True when it was written. False off Windows, or
    when the interpreter it would point at is missing.

    Never raises: a missing shortcut is an inconvenience, never a failed
    install. Running it twice rewrites the same file.
    """
    spec = shortcut_spec(root)
    target = destination or shortcut_path()
    if target is None or not Path(spec["target"]).is_file():
        return False
    try:
        import pythoncom                                     # Windows only
        from win32com.propsys import propsys, pscon
        from win32com.shell import shell
    except ImportError:
        return False
    try:
        link: Any = pythoncom.CoCreateInstance(
            shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER,
            shell.IID_IShellLink)
        link.SetPath(spec["target"])
        link.SetArguments(spec["arguments"])
        link.SetWorkingDirectory(spec["working_directory"])
        if Path(spec["icon"]).is_file():
            link.SetIconLocation(spec["icon"], 0)
        link.SetDescription(spec["description"])
        store = link.QueryInterface(propsys.IID_IPropertyStore)
        store.SetValue(pscon.PKEY_AppUserModel_ID,
                       propsys.PROPVARIANTType(spec["app_user_model_id"]))
        store.Commit()
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        link.QueryInterface(pythoncom.IID_IPersistFile).Save(str(target), 0)
    except Exception:                             # noqa: BLE001 - never fail an install
        return False
    return Path(target).is_file()


def exists(destination: Optional[Path] = None) -> bool:
    """True when the shortcut file is there. Read-only, never raises."""
    target = destination or shortcut_path()
    return bool(target) and Path(target).is_file()


def remove(destination: Optional[Path] = None) -> bool:
    """Delete the shortcut. False if it was not there."""
    target = destination or shortcut_path()
    if not target or not Path(target).is_file():
        return False
    try:
        Path(target).unlink()
    except OSError:
        return False
    return True
