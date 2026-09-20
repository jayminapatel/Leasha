r"""Closing the main window ends the application, even with a run and pop-outs open.

Layer: L5. Work order `202626191300` §6d.

**What is reproduced and what is not.** The live incident (the event loop still
running nine hours after `closeEvent`) has not been reproduced. One real mechanism
has: Qt quits when the last *visible top-level window* closes, so a pop-out or the
log window left open keeps the loop alive after the main window has gone.

**Runs in a child process, on purpose.** The thing under test is "the event loop
does not end", and a hung Qt loop cannot be interrupted by pytest-timeout - twice
it froze the whole pytest run. `close_scenario_child.py` builds the real
`MainWindow`, starts a real index run in the view's pool, shows a `LogWindow` and a
bare window, closes, and runs a real `app.exec()` with a backstop timer. It arms
`faulthandler.dump_traceback_later(150, exit=True)`, so a hang writes every thread's
stack to stderr and exits; the parent adds a hard wall-clock cap and kills the tree.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

CHILD = Path(__file__).with_name("close_scenario_child.py")
WALL_CLOCK_CAP = 200


def _run_child(scratch: Path) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(
        [sys.executable, str(CHILD), str(scratch)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = proc.communicate(timeout=WALL_CLOCK_CAP)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)                       # the whole tree
        out, err = proc.communicate()
        pytest.fail(f"the close scenario hung for {WALL_CLOCK_CAP}s and was killed.\n"
                    f"stdout: {out}\nstderr tail: {err[-3000:]}")
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


@pytest.mark.timeout(WALL_CLOCK_CAP + 30, method="thread")
def test_closing_the_window_ends_the_app_with_a_run_and_other_windows_open(tmp_path) -> None:
    done = _run_child(tmp_path)
    result = next((line for line in done.stdout.splitlines() if line.startswith("RESULT")), "")

    assert result, (
        f"the child never reported (exit {done.returncode}); if faulthandler fired, "
        f"its stacks are here:\n{done.stderr[-4000:]}")
    fields = dict(part.split("=", 1) for part in result.split()[1:] if "=" in part)
    assert fields, result

    assert fields["ended_by_timer"] == "False", (
        "the main window closed but the event loop kept running: another "
        f"top-level window was still visible. {result}")
    assert fields["popout_visible"] == "False" and fields["log_visible"] == "False", result
    assert fields["pool_active"] == "0", f"the run outlived the window. {result}"
    # Stopped within about a model call of the request, not after the whole batch
    # (order 0u 6e): the call in flight plus at most one more.
    assert int(fields["calls"]) < int(fields["of"]), result
    assert float(fields["waited"]) < 20, result
