"""Run pytest for a GitHub run so the step ends when pytest has finished, not 90 minutes later.

Layer: L0

    python scripts/ci_pytest.py -- tests -q -m "not jvm and not e2e" --timeout=600

**Why this exists (2026-10-25).** Windows Run on `main` at 9e45ff3 printed its last failure
at 13:27 and was cancelled by the 90-minute ceiling at 13:55. After the summary the process
did not end: the main thread was in native shutdown (no Python frame), the end-of-run
watchdog (`tests/conftest.py`, `faulthandler.dump_traceback_later(300, exit=True)`)
dumped the threads at 13:32, and the step still waited - for a process left behind that
held the step's output open (the runner's last line was "Cleaning up orphan processes").

So pytest writes to a file, not to the step's own output, and this script copies the file
to the log as it grows. The step waits for pytest's process alone; anything pytest leaves
behind holds the file, never the step.

**When pytest has finished** is told by this same module, loaded into pytest as a plugin
(`-p ci_pytest`): its `pytest_sessionfinish` writes the exit status to `DONE_ENV`'s file.
Not pytest's "3 failed, 18000 passed in ..." line - the project's `addopts` already holds
`-q`, so a step's own `-q` makes it `-qq`, which leaves that line out. Once the status is
written pytest gets `GRACE_S` to end by itself, and is then ended with that status.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

#: Seconds pytest is given to end by itself after its session has finished
#: (`LEASHA_CI_PYTEST_GRACE` overrides it, for the test of this script).
GRACE_S = float(os.environ.get("LEASHA_CI_PYTEST_GRACE", "60"))
#: How often the file is copied to the log.
POLL_S = 2.0
#: The environment variable naming the file the plugin writes the exit status to.
DONE_ENV = "LEASHA_CI_PYTEST_DONE"


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001 - pytest's hook signature
    """The plugin half: say the session has finished, and how."""
    done = os.environ.get(DONE_ENV)
    if done:
        Path(done).write_text(str(int(exitstatus)), encoding="ascii")


def finished_status(done: Path) -> Optional[int]:
    """The exit status the plugin wrote, or None while the session is still going."""
    try:
        return int(done.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None


def main(argv: list[str]) -> int:
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    out, done = Path("pytest-output.txt").resolve(), Path("pytest-done.txt").resolve()
    done.unlink(missing_ok=True)
    here = str(Path(__file__).resolve().parent)
    env = dict(os.environ, **{DONE_ENV: str(done)})
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [here, env.get("PYTHONPATH", "")]))
    with out.open("wb") as sink:
        process = subprocess.Popen([sys.executable, "-m", "pytest", "-p", "ci_pytest", *argv],
                                   stdout=sink, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, env=env)
    shown = 0                                   # bytes of the file already copied
    finished_at: Optional[float] = None
    while True:
        ended = process.poll() is not None
        data = out.read_bytes()
        if len(data) > shown:
            sys.stdout.buffer.write(data[shown:])
            sys.stdout.flush()
            shown = len(data)
        if ended:
            return int(process.returncode)
        status = finished_status(done)
        if status is not None and finished_at is None:
            finished_at = time.monotonic()
        elif finished_at is not None and time.monotonic() - finished_at > GRACE_S:
            print(f"\npytest finished its session (exit status {status}) and had not ended "
                  f"{GRACE_S:.0f} s later; ending it.", flush=True)
            process.kill()
            process.wait(timeout=30)
            return int(status if status is not None else 1)
        time.sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
