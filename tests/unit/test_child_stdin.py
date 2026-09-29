"""The indexing child's command pipe must not freeze the rest of the child.

Layer: L3

2026-09-29, Windows CI: every child run sat still until the window sent its
first command. On Windows a synchronous pipe serves one operation at a time,
so while the command thread waited in a read on standard input, the next thing
to touch standard input - a library asking `isatty`, a process started to
inherit it - waited behind that read. `app.cli.index._private_stdin` keeps the
pipe aside and points standard input at the null device.

This starts a real process that does what the child does - a thread waiting
on the commands - and then, with no command sent, asks about standard input
and starts a Python process of its own. On Windows, before the fix, that is
where it froze. Everywhere it must finish at once, and the command sent
afterwards must still arrive.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]

CHILD = textwrap.dedent("""
    import os, subprocess, sys, threading
    sys.path.insert(0, {project!r})
    from app.cli.index import _private_stdin

    commands = _private_stdin()
    got = []
    done = threading.Event()

    def read():
        for line in commands:
            got.append(line.strip())
            done.set()

    threading.Thread(target=read, daemon=True).start()
    # The thread is now waiting in a read. Touch standard input the ways
    # libraries and started processes do.
    os.fstat(0)
    sys.stdin.isatty()
    subprocess.run([sys.executable, "-c", "import os; os.fstat(0)"], check=True,
                   timeout=60)
    print("free", flush=True)
    done.wait(60)
    print("command:" + (got[0] if got else ""), flush=True)
""")


def test_waiting_for_a_command_does_not_hold_up_the_rest_of_the_child() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD.format(project=str(PROJECT))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=str(PROJECT))
    lines: list[str] = []
    reader = threading.Thread(
        target=lambda: lines.extend(proc.stdout), daemon=True)  # type: ignore[arg-type]
    reader.start()
    try:
        deadline = threading.Event()
        for _ in range(600):                     # 60 s, with no command sent
            if any(line.strip() == "free" for line in lines):
                break
            deadline.wait(0.1)
        assert any(line.strip() == "free" for line in lines), (
            "the child froze while its command thread waited; stderr:\n"
            + (proc.stderr.read() if proc.poll() is not None else "(still running)"))
        proc.stdin.write("stop\n")               # type: ignore[union-attr]
        proc.stdin.flush()                       # type: ignore[union-attr]
        proc.wait(timeout=60)
        reader.join(10)
        assert "command:stop" in [line.strip() for line in lines]
    finally:
        if proc.poll() is None:
            proc.kill()
