r"""Priority for one thread, not for the whole process.

Layer: L0

**Why this exists.** The window and the indexer share one process. The indexer
used to be made polite with `psutil.Process().nice(BELOW_NORMAL)`, which lowers
the *process* - so the window's own thread dropped with it, and inside the
process the interface and the indexer's four extraction threads ended up at the
same priority. Below-normal against other applications is what was wanted;
"the interface waits its turn behind its own indexer" is what came with it, and
it stayed that way after the run ended.

`SetThreadPriority` lowers only the calling thread. The process stays at normal,
every indexer thread lowers itself, and the interface thread is then the highest
priority thread in its own process - which is the one ordering that matters when
a run is competing with a keystroke for a core.

**LOWEST, not BELOW_NORMAL, on purpose.** A thread at `LOWEST` (-2) in a
`NORMAL` process has base priority 6, the same as a `NORMAL` thread in a
`BELOW_NORMAL` process. So against everybody *else* the indexer is exactly as
polite as before; the change is only that the window no longer shares its
ranking.

**Child processes do not inherit a thread priority**, only the process class
they are created with. The document converters (LibreOffice, ffmpeg) used to
inherit below-normal from the lowered process, so `child_creationflags` hands
the same class to them explicitly while a lowered run is active.

Never raises. A priority that could not be set costs the courtesy, never the run.

**2026-09-27, work order 0x §1b: the Windows calls moved.** The `kernel32`
code (`SetThreadPriority` and friends) now lives in
`app/core/osbridge/priority.py`, the one package allowed to make Windows-only
calls. It was moved, not changed: same calls, same flags, same log lines. What
stays here is the *policy* - when a run lowers its threads and its children -
and every name this module has always offered, so no caller changes.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

# The thread calls and the Windows constants are re-exported under their old
# names, so `from app.core.priority import lower_this_thread` (the index
# pipeline) and `priority.THREAD_PRIORITY_LOWEST` (the tests) keep working.
from app.core.osbridge._platform import is_windows
from app.core.osbridge.priority import (
    BELOW_NORMAL_PRIORITY_CLASS,
    THREAD_PRIORITY_LOWEST,
    current_thread_priority,
    lower_this_thread,
    restore_this_thread,
)

__all__ = [
    "lower_this_thread", "restore_this_thread", "background_thread",
    "set_children_low", "child_creationflags", "current_thread_priority",
]

#: True while an index run in a shared process is active, so a child process the
#: run starts is created below normal. A plain bool: written twice per run by the
#: thread that owns the run, read by extraction threads - a stale read for one
#: file's converter is harmless.
_children_low = False


def set_children_low(on: bool) -> None:
    global _children_low
    # Only Windows reads a priority class from `creationflags`; elsewhere the
    # flag would mean nothing, so it is never switched on there.
    _children_low = bool(on) and is_windows()


def child_creationflags() -> int:
    """Process-creation flags for a child started on behalf of a lowered run.

    `0` unless `set_children_low(True)` is in force, so an ordinary conversion
    (a preview, the command line, a test) is created exactly as it always was.
    OR it into whatever flags the call already passes.
    """
    return BELOW_NORMAL_PRIORITY_CLASS if _children_low else 0


@contextmanager
def background_thread(enabled: bool = True) -> Iterator[bool]:
    """Run the calling thread, and the children it starts, at background priority.

    Yields whether the thread was actually lowered. Restores on the way out,
    whatever the way out was.
    """
    if not enabled:
        yield False
        return
    previous = lower_this_thread()
    set_children_low(True)
    try:
        yield previous is not None
    finally:
        set_children_low(False)
        restore_this_thread(previous)
