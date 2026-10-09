r"""A Python stack for a native crash, in whichever Leasha process it happens.

Layer: L0.

**Half of this application is C++**, and not only in the window. The indexing
process holds PyMuPDF, ONNX Runtime and DirectML, OpenCV and LibreOffice's
bridge; a reader helper holds the parsing libraries. A fault inside any of
them kills the process where it stands: no Python exception, no `except`, no
traceback, and the run log simply stops mid-line. From the outside the window
says the indexing process "stopped unexpectedly" and names the file it was
reading - which is often not the file at fault, because four threads read at
once.

`faulthandler` installs operating-system handlers that print the Python stack
of the thread a fault is on. It costs nothing until something dies, and it is
the difference between an APPCRASH record naming a `.dll` and the name of the
function that was in it. The window has had it since 2026-08-27 (`app.main`);
on 2026-10-09 two native crashes in one day - PyMuPDF overnight, ONNX Runtime
at midday, both in the indexing process - left no stack at all, because the
indexing process and the reader helpers never installed one. The second was
diagnosed by parsing the Windows crash dump by hand. This module is that
handler, callable from any process, one file per kind of process so a
report can be attributed:

    logs/crash/crash.log            the window (the name the documents promise)
    logs/crash/index-crash.log      the indexing process (`app.cli index`)
    logs/crash/cli-crash.log        any other command (`app.cli ...`)
    logs/crash/reader-crash.log     the reader helpers (`app.index.read_process`)
    logs/crash/ocr-crash.log        the text-in-pictures helper (`app.index.ocr_process`)

**`all_threads=False`, always.** With `True` the handler was the crash
(2026-10-02): Windows' own dialogs raise and handle exceptions on threads of
their own, and on Windows `faulthandler` is called for every exception whose
code has the top bit set, handled or not; each one walked every Python
thread's stack without the GIL, and the 119th walk read a frame as it changed.
With `False` it writes the stack of the thread the exception is on, which is
the one that names a real crash. So a `Windows fatal exception: code
0x8001010e` heading with no stack is not a crash; `access violation` is.

**The file first.** `faulthandler.enable()` with no argument raises
`RuntimeError: sys.stderr is None` under `pythonw.exe`, which is how every
real run starts; it used to be called first and silently disabled the file
handler too (2026-08-27). The window then also asks for the console when
there is one (`also_stderr`), which *replaces* the destination - a developer
with a console sees the stack there - and `tests/unit/test_crash_reporting.py`
holds that behaviour. The indexing process and the reader helpers do not: the
index child's standard error is a pipe the window keeps, and the file is the
one place somebody will look.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import IO

__all__ = ["CRASH_FILES", "catch_native_crashes", "crash_file_for"]

#: The crash file for each kind of process, under `<log dir>/crash/`.
CRASH_FILES: dict[str, str] = {
    "window": "crash.log",
    "index": "index-crash.log",
    "reader": "reader-crash.log",
    "ocr": "ocr-crash.log",
}

#: Kept alive for the process lifetime. `faulthandler` writes to the file
#: descriptor it was given, so letting this be garbage collected closes the
#: file and the crash it was installed to record goes nowhere.
_CRASH_FILE: IO[str] | None = None


def crash_file_for(log_dir: Path | str, process: str) -> Path:
    """Where `process` ("window", "index", "reader") writes its crash report."""
    name = CRASH_FILES.get(process) or f"{process}-crash.log"
    return Path(log_dir) / "crash" / name


def catch_native_crashes(log_dir: Path | str, process: str = "window", *,
                         also_stderr: bool = False) -> IO[str] | None:
    """Turn a native fault into a stack trace instead of silence. Never raises.

    Returns the open crash file (`None` when it could not be opened - a
    diagnostic that prevents start-up is worse than no diagnostic). With
    `also_stderr`, a console that exists becomes the destination instead,
    which is the window's rule; see the module note.
    """
    global _CRASH_FILE

    import faulthandler

    try:
        path = crash_file_for(log_dir, process)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Deliberately not a context manager: the handle stays open for the
        # life of the process, which is the whole point.
        _CRASH_FILE = path.open("a", buffering=1, encoding="utf-8")
        faulthandler.enable(file=_CRASH_FILE, all_threads=False)
    except Exception:
        _CRASH_FILE = None

    if also_stderr or _CRASH_FILE is None:
        # The console as well, when there is one. `enable()` replaces the
        # destination, so this runs second and only when it can succeed.
        try:
            if sys.stderr is not None:
                faulthandler.enable(all_threads=False)
        except Exception:
            pass
    return _CRASH_FILE


def _release_for_tests() -> None:
    """Close the file and disable the handler. Tests only."""
    global _CRASH_FILE

    import faulthandler

    handle, _CRASH_FILE = _CRASH_FILE, None
    if handle is not None:
        with contextlib.suppress(Exception):
            handle.close()
    faulthandler.disable()
