r"""Where Leasha keeps its data by default, and how to name its Python in a message.

Layer: L0 (part of `app.core.osbridge`)

Two small answers that differ by operating system and were, until now, written
out as Windows text wherever they were needed.

**The default data folder.** The installer (`install.ps1`) suggests
`%LOCALAPPDATA%\Leasha` - the per-user "local application data" folder, which
only that Windows account can read. The Mac has its own agreed place for the
same thing, `~/Library/Application Support/<app>`, which Apple's guidelines
name for exactly this. This module only *answers the question*; it does not
change where the application actually reads its data from. `app/core/config.py`
still requires `DATA_PATH` to be set in `.env`, as it always has, and moving
that is a separate, deliberate change.

**The interpreter path in messages.** Many error messages tell the person what
to type, for example `venv\Scripts\python.exe -m pip install fastembed`. On a
Mac the same virtual environment keeps Python at `venv/bin/python`. The helpers
here give exactly the Windows text on Windows - character for character, so no
message changes there - and the Mac/Linux form elsewhere. The messages
themselves are switched over to use them in later sections, file by file, as
each file becomes free to edit.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.core.osbridge._platform import is_macos, is_windows

__all__ = [
    "APP_FOLDER_NAME",
    "default_data_folder",
    "venv_python_display",
    "venv_pip_display",
]

#: The folder name used inside each system's application-data location.
APP_FOLDER_NAME = "Leasha"


def default_data_folder() -> Path:
    r"""Where Leasha's index would live by default on this system.

    - **Windows**: `%LOCALAPPDATA%\Leasha`, exactly what `install.ps1`
      proposes (`Join-Path $env:LOCALAPPDATA "Leasha"`). If the variable is
      missing - it should never be, Windows sets it for every account - the
      folder it normally points at, `<home>\AppData\Local`, is used instead.
    - **macOS**: `~/Library/Application Support/Leasha`
      (UNCONFIRMED on macOS: the location is Apple's documented one; nothing
      has been installed there from this code yet).
    - **Linux and others**: `$XDG_DATA_HOME/Leasha`, or
      `~/.local/share/Leasha` when that is not set - the freedesktop.org
      convention. Only developers run Leasha there.

    Pure: nothing is created or checked on disk.
    """
    if is_windows():
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / APP_FOLDER_NAME
    if is_macos():
        return Path.home() / "Library" / "Application Support" / APP_FOLDER_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / APP_FOLDER_NAME


def venv_python_display() -> str:
    r"""The project's Python, written the way a person would type it here.

    Windows: exactly `venv\Scripts\python.exe` (the text every existing message
    uses). macOS and Linux: `venv/bin/python`.
    """
    if is_windows():
        return "venv\\Scripts\\python.exe"
    return "venv/bin/python"


def venv_pip_display() -> str:
    r"""The project's `pip`, written the way a person would type it here.

    Windows: exactly `venv\Scripts\pip`, as the existing "install the library"
    messages say it. macOS and Linux: `venv/bin/pip`.
    """
    if is_windows():
        return "venv\\Scripts\\pip"
    return "venv/bin/pip"
