"""The process's standard input handle, as Windows keeps it apart from descriptor 0.

Layer: L0

Windows remembers a process's standard input twice: as C descriptor 0 and as
a Win32 handle (`GetStdHandle`), which is what a process started later
inherits. `os.dup2` onto descriptor 0 updates the handle only in a console
program - `pythonw`, which runs the window and so its indexing child, is not
one. `app.cli.index._private_stdin` needs both to point at the null device;
this is the Windows half. Elsewhere there is nothing to do: descriptor 0 *is*
the standard input.
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.core.osbridge._platform import is_windows

__all__ = ["CLI_PROGRAM", "console_python", "follow_descriptor_zero", "own_python"]

#: In a packaged build, the console program beside `Leasha.exe` that runs
#: Leasha's own code the way `python.exe` would: `-m module`, `-c code`, or a
#: script path (`packaging/leasha_entry.py`).
CLI_PROGRAM = "leasha-cli.exe" if sys.platform == "win32" else "leasha-cli"


def own_python() -> str:
    """The program to start Leasha's own code in a child process.

    `sys.executable`, as always - **except in a packaged build** (2026-10-05,
    the first Windows installer). There `sys.executable` is `Leasha.exe`
    itself, which opens the window whatever it is given, so `-m app.cli
    index` would have started a second window instead of indexing. The
    console program beside it is the one that understands `-m`.
    """
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).with_name(CLI_PROGRAM))
    return sys.executable

#: `STD_INPUT_HANDLE` in the Win32 API.
_STD_INPUT_HANDLE = -10


def console_python(executable: str) -> str:
    """The Python that has a standard input and output, for `executable`.

    2026-10-04: an AI program starts `app.cli mcp` and talks to it on stdin and
    stdout. On Windows the window runs under `pythonw.exe`, which has neither,
    so its console twin `python.exe` beside it is the one to name. Elsewhere
    there is one Python, and it is returned as it is."""
    if getattr(sys, "frozen", False):
        return own_python()                 # a packaged build: see `own_python`
    path = Path(executable)
    if is_windows() and path.name.lower() == "pythonw.exe":
        return str(path.with_name("python.exe"))
    return str(path)


def follow_descriptor_zero() -> None:
    """Point the Win32 standard input handle at whatever descriptor 0 now is.
    No-op off Windows. Raises OSError if Windows refuses."""
    if not is_windows():
        return
    import ctypes
    import msvcrt

    if not ctypes.windll.kernel32.SetStdHandle(_STD_INPUT_HANDLE,
                                               msvcrt.get_osfhandle(0)):
        raise OSError("SetStdHandle(STD_INPUT_HANDLE) failed")
