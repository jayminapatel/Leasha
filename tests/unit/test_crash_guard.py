r"""The crash handler reaches every Leasha process, not only the window. 2026-10-09.

Two native crashes in one day - PyMuPDF overnight (`mupdfcpp64.dll`), ONNX
Runtime at midday (`onnxruntime_pybind11_state.pyd`), both access violations
in the *indexing* process - left no Python stack, because `faulthandler` was
installed by `app.main` for the window and nowhere else. The second had to be
diagnosed from the Windows crash dump by hand. `app.core.crash_guard` is the
handler the window had, callable from any process; these tests hold that it
writes where the documents say, that a real native fault leaves the name of
the function that was running, and that the index command and the reader
helper actually install it.

`test_crash_reporting.py` keeps holding the window's own rules (the file
under `pythonw.exe`, the console when there is one, no walk of other threads).
"""

from __future__ import annotations

import contextlib
import faulthandler
import inspect
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.core import crash_guard


@pytest.fixture(autouse=True)
def _release():
    yield
    crash_guard._release_for_tests()


@pytest.mark.parametrize("process, name", [
    ("window", "crash.log"), ("index", "index-crash.log"), ("reader", "reader-crash.log"),
])
def test_each_kind_of_process_writes_its_own_file(tmp_path, process, name) -> None:
    handle = crash_guard.catch_native_crashes(tmp_path, process)
    assert handle is not None
    assert (tmp_path / "crash" / name).exists()
    assert faulthandler.is_enabled()


def test_a_bad_log_directory_never_raises(tmp_path) -> None:
    """A diagnostic that prevents start-up is worse than no diagnostic."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    assert crash_guard.catch_native_crashes(blocker, "index") is None


#: A process that installs the index handler with no console, as `pythonw.exe`
#: starts it, then faults natively inside a named function. argv: the log dir.
_FAULT_SCRIPT = r"""
import sys
from pathlib import Path

sys.stderr = None                       # pythonw.exe: no console at all
if sys.platform == "win32":
    import msvcrt
    msvcrt.SetErrorMode(msvcrt.SEM_NOGPFAULTERRORBOX)   # no dialog, no report

from app.core.crash_guard import catch_native_crashes

catch_native_crashes(Path(sys.argv[1]), "index")

import faulthandler


def the_function_that_was_running():
    faulthandler._read_null()           # a real access violation


the_function_that_was_running()
"""


def test_a_native_fault_leaves_the_python_stack_in_the_file(tmp_path) -> None:
    """The whole point: the file names the function, where the Windows record
    named only the `.dll`. Run in a process of its own, because the fault
    ends it."""
    root = Path(crash_guard.__file__).resolve().parents[2]
    proc = subprocess.Popen(
        [sys.executable, "-c", _FAULT_SCRIPT, str(tmp_path)],
        cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        code = proc.wait(timeout=120)
    finally:
        _remove_windows_dump(proc.pid)
    assert code != 0, "the fault did not end the process"
    written = (tmp_path / "crash" / "index-crash.log").read_text(encoding="utf-8")
    assert "Stack (most recent call first)" in written, written
    assert "the_function_that_was_running" in written, written


def _remove_windows_dump(pid: int) -> None:
    """Windows may still keep a dump of the faulting child; it is this test's
    own artefact, so it goes. Written by Windows *after* the child has gone,
    so this waits a little for it. Never raises."""
    local = os.environ.get("LOCALAPPDATA")
    if not local or not sys.platform.startswith("win"):
        return
    folder = Path(local) / "CrashDumps"
    names = (f"python.exe.{pid}.dmp", f"pythonw.exe.{pid}.dmp")
    for _ in range(20):                          # up to ten seconds
        for name in names:
            if (folder / name).exists():
                with contextlib.suppress(OSError):
                    (folder / name).unlink()
                return
        time.sleep(0.5)


def test_the_index_command_and_the_reader_helper_install_it() -> None:
    """A guard, in the style of `test_read_process.py`: the two processes
    that had no handler on 2026-10-09 now reach for this one by name."""
    from app import cli as app_cli
    from app import main as app_main
    from app.index import read_process

    # At the CLI's real entry, not inside `cmd_index`: the handler keeps its
    # file open for the life of the process, and a test that calls the
    # command in-process must not pin a file in pytest's temp folder
    # (2026-10-09, found when two suites ran at once).
    assert 'catch_native_crashes(log_dir, "index" if' in inspect.getsource(app_cli.main)
    assert "catch_native_crashes" not in inspect.getsource(app_cli.index.cmd_index)
    assert 'catch_native_crashes(log_dir_for(), "reader")' in inspect.getsource(
        read_process.main)
    assert 'catch_native_crashes(log_dir, "window"' in inspect.getsource(
        app_main._catch_native_crashes)
