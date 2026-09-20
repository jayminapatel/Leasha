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
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from typing import Iterator, Optional

from app.core.logging import logger

__all__ = [
    "lower_this_thread", "restore_this_thread", "background_thread",
    "set_children_low", "child_creationflags", "current_thread_priority",
]

log = logger.bind(component="core.priority")

THREAD_PRIORITY_LOWEST = -2
_THREAD_PRIORITY_ERROR_RETURN = 0x7FFFFFFF
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000

#: True while an index run in a shared process is active, so a child process the
#: run starts is created below normal. A plain bool: written twice per run by the
#: thread that owns the run, read by extraction threads - a stale read for one
#: file's converter is harmless.
_children_low = False

_kernel32 = None


def _api():
    """`kernel32` with the three calls typed, or None off Windows."""
    global _kernel32
    if sys.platform != "win32":
        return None
    if _kernel32 is None:
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.GetCurrentThread.restype = wintypes.HANDLE
        k.GetCurrentThread.argtypes = []
        k.GetThreadPriority.restype = ctypes.c_int
        k.GetThreadPriority.argtypes = [wintypes.HANDLE]
        k.SetThreadPriority.restype = wintypes.BOOL
        k.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        _kernel32 = k
    return _kernel32


def lower_this_thread() -> Optional[int]:
    """Lower the calling thread to `LOWEST`. Returns what it was, or None.

    The return value is what `restore_this_thread` takes; `None` means nothing
    was changed - not Windows, or the call was refused - and there is nothing to
    restore.
    """
    try:
        k = _api()
        if k is None:
            return None
        handle = k.GetCurrentThread()
        previous = int(k.GetThreadPriority(handle))
        if previous == _THREAD_PRIORITY_ERROR_RETURN:
            return None
        if not k.SetThreadPriority(handle, THREAD_PRIORITY_LOWEST):
            return None
        return previous
    except Exception as exc:                     # noqa: BLE001 - a courtesy
        log.debug("could not lower this thread's priority: {}", exc)
        return None


def restore_this_thread(previous: Optional[int]) -> None:
    """Put the calling thread back. A `None` (nothing was changed) does nothing.

    Matters for a pool thread, which outlives the run that lowered it; a
    `threading.Thread` simply ends.
    """
    if previous is None:
        return
    try:
        k = _api()
        if k is not None:
            k.SetThreadPriority(k.GetCurrentThread(), int(previous))
    except Exception as exc:                     # noqa: BLE001
        log.debug("could not restore this thread's priority: {}", exc)


def current_thread_priority() -> Optional[int]:
    """The calling thread's current priority, or None. For tests and the log."""
    try:
        k = _api()
        if k is None:
            return None
        value = int(k.GetThreadPriority(k.GetCurrentThread()))
        return None if value == _THREAD_PRIORITY_ERROR_RETURN else value
    except Exception:                            # noqa: BLE001
        return None


def set_children_low(on: bool) -> None:
    global _children_low
    _children_low = bool(on) and sys.platform == "win32"


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
