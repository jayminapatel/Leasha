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

from pathlib import Path

from app.core.osbridge._platform import is_windows

__all__ = ["console_python", "follow_descriptor_zero"]

#: `STD_INPUT_HANDLE` in the Win32 API.
_STD_INPUT_HANDLE = -10


def console_python(executable: str) -> str:
    """The Python that has a standard input and output, for `executable`.

    2026-10-04: an AI program starts `app.cli mcp` and talks to it on stdin and
    stdout. On Windows the window runs under `pythonw.exe`, which has neither,
    so its console twin `python.exe` beside it is the one to name. Elsewhere
    there is one Python, and it is returned as it is."""
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
