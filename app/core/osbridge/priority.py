r"""Asking the operating system to run a thread, or the whole process, more politely.

Layer: L0 (part of `app.core.osbridge`)

**What "priority" means here.** When more programs want the processor than there
are cores, the operating system decides who runs next. A lower priority means
"let the others go first". Leasha lowers its *indexer* so that the person's
other programs - and Leasha's own window - stay quick while a long run grinds
on in the background. `app/core/priority.py` explains why that is done per
thread rather than for the whole process; this module is only the part that
talks to the operating system.

**Moved here, unchanged, from `app/core/priority.py`** (work order 0x §1b). The
Windows calls, their flags, the "could not ..." log lines and even the log's
component name (`core.priority`) are exactly what they were, so a Windows log
reads the same before and after the move.

**Per thread, on Windows only - on purpose.** Windows has `SetThreadPriority`,
which lowers one thread and can put it back afterwards. The other systems are
answered honestly with `None` ("nothing was changed"), which is what the
callers have always received there:

- **Linux** can lower one thread (`setpriority` with the thread's id), but an
  ordinary user is not allowed to raise it again. A pool thread lowered for
  one run would stay slow for every later job it picked up - including work
  the window is waiting for. Doing nothing is better than that.
- **macOS** does not give threads a "nice" value at all; the Mac way is a
  "quality of service" class set with `pthread_set_qos_class_self_np`.
  Whether that can be lowered *and restored* on the same thread, the way the
  indexer's pool threads need, is **(UNCONFIRMED on macOS)**, so it is not
  built. Until it is checked on a real Mac, a Mac run behaves as it does today.

**The whole process** is lowered through `psutil`, which already works on every
system; see `lower_process_priority` below.

Never raises. A priority that could not be set costs the courtesy, never the run.
"""

from __future__ import annotations

from typing import Any, Optional

from app.core.logging import logger
from app.core.osbridge._platform import is_windows

__all__ = [
    "THREAD_PRIORITY_LOWEST", "BELOW_NORMAL_PRIORITY_CLASS",
    "lower_this_thread", "restore_this_thread", "current_thread_priority",
    "lower_process_priority",
]

# The log keeps the component name it had before the move, so anybody searching
# old and new log files for `core.priority` finds both.
log = logger.bind(component="core.priority")

#: Windows' "lowest" thread priority. Thread priorities there run from -2
#: (lowest) through 0 (normal) to +2 (highest), relative to the process.
THREAD_PRIORITY_LOWEST = -2
#: What `GetThreadPriority` returns when it fails (Windows' own constant).
_THREAD_PRIORITY_ERROR_RETURN = 0x7FFFFFFF
#: The "below normal" priority class a new *process* can be created with.
#: Handed to `subprocess.Popen(creationflags=...)`, which only Windows reads.
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000

#: The loaded `kernel32` library, kept after the first call so the (small) cost
#: of loading it and describing its functions is paid once per process.
_kernel32 = None


def _api():
    """`kernel32` with the three calls typed, or None off Windows."""
    global _kernel32
    if not is_windows():
        return None
    if _kernel32 is None:
        # Imported here, not at the top: `ctypes.WinDLL` only exists on Windows,
        # and importing this module must never fail on a Mac.
        import ctypes
        from ctypes import wintypes

        k = ctypes.WinDLL("kernel32", use_last_error=True)
        # Telling ctypes the exact argument and return types matters: without
        # it a 64-bit handle can be cut down to 32 bits and point at nothing.
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


def lower_process_priority(psutil: Any) -> None:
    """Lower the whole current process below normal. Raises if it cannot.

    Moved from `SystemProbe.lower_priority` in `app/index/resources.py`,
    which still decides *whether* psutil is there and logs a failure in its own
    words; only the per-system part lives here.

    `psutil` is passed in rather than imported so the probe's tests can hand
    over a fake one, exactly as they always have.

    - **Windows**: psutil offers Windows' own "below normal" class, and the
      process also asks for low disk (I/O) priority - which matters as much as
      the processor, because the indexer reads the disk constantly. Not every
      Windows build allows the I/O request, so its failure is ignored.
    - **macOS and Linux**: psutil has no priority *classes* there, only the
      Unix "nice" number (0 is normal, 19 is the most polite); 10 is a
      middling "below normal". On a Mac this is the same Unix call and is
      expected to behave the same way (UNCONFIRMED on macOS).

    The test is "does psutil have the Windows class", not "is this Windows",
    because that is what the original code asked - keeping the question keeps
    the answer identical.
    """
    process = psutil.Process()
    if hasattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS"):
        process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        try:
            process.ionice(psutil.IOPRIO_LOW)
        except Exception:               # noqa: BLE001 - not on every Windows build
            pass
    else:
        process.nice(10)                # POSIX
