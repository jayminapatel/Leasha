r"""A window that has closed must not leave a process behind.

Layer: L4

**What this is for.** Twice on the owner's machine - 2026-09-18 11:27 and
2026-09-19 12:23 - the window logged `closing: took 0.0s` and vanished, and the
process carried on with no window: once for nine hours, once until it was killed
from Task Manager. The single-instance lock is held for exactly that long, so a
relaunch runs into it, and an invisible process keeps eating the machine.

The cause found so far is fixed at its source (`MainWindow._drain_workers` now
waits on the index run; `Pipeline._extract_worker` now honours a stop). What was
**not** found is why the event loop stayed alive after `closeEvent` returned,
and a process at 0 CPU with 180 threads gave nobody a stack to look at before it
was killed. So this does two things, in this order:

1. **Say what it is stuck on.** Every thread's stack goes into the run log, once,
   `dump_after_s` after the close. The next occurrence names its own cause
   instead of being reasoned about.
2. **End it.** `exit_after_s` after the close, if the process is still here, it
   exits. By then the index run has had `INDEX_SHUTDOWN_GRACE_MS` to stop and
   the stores have been asked to close, so what is lost is what a Task Manager
   kill would have lost - which is the alternative it replaces.

Both timers are daemon threads: they never keep a healthy process alive, and a
process that exits normally never sees either fire.
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from typing import Any, Callable, Optional

from app.core.logging import logger

__all__ = ["arm", "format_stacks", "DUMP_AFTER_S", "EXIT_AFTER_S"]

_log = logger.bind(component="ui.exit_watchdog")

#: Seconds after the window closed at which every thread's stack is logged.
DUMP_AFTER_S = 30.0

#: Seconds after the window closed at which a surviving process is ended. Long
#: enough for an embedding batch and a final flush to finish on a slow machine;
#: short enough that nobody reaches for Task Manager first.
EXIT_AFTER_S = 300.0

#: Frames kept per thread. A native thread parked in a wait has a short stack;
#: a runaway recursion should not fill the log.
_MAX_FRAMES = 25


def format_stacks(frames: Optional[dict] = None,
                  names: Optional[dict] = None) -> str:
    """Every thread's Python stack, as one block of text.

    `frames` and `names` are injectable so this is testable without a second
    thread being in any particular state.
    """
    frames = frames if frames is not None else sys._current_frames()
    if names is None:
        names = {t.ident: t.name for t in threading.enumerate()}
    blocks: list[str] = []
    for ident, frame in sorted(frames.items(), key=lambda item: str(item[0])):
        stack = traceback.format_stack(frame)[-_MAX_FRAMES:]
        blocks.append(f"--- thread {names.get(ident, '?')} ({ident})\n"
                      + "".join(stack).rstrip())
    return "\n".join(blocks)


def _dump() -> None:
    try:
        _log.warning(
            "the window closed but this process is still running. "
            "Every thread's stack follows - this is what it is waiting on:\n{}",
            format_stacks())
    except Exception as exc:                      # noqa: BLE001 - diagnostics only
        _log.warning("could not dump the thread stacks: {}", exc)


def arm(*, dump_after_s: float = DUMP_AFTER_S,
        exit_after_s: float = EXIT_AFTER_S,
        exit: Callable[[int], Any] = os._exit,           # noqa: A002 - injectable
        dump: Callable[[], None] = _dump) -> list[threading.Timer]:
    """Start the two timers. Returns them, so a test can cancel or inspect them.

    Idempotent enough: `closeEvent` can run more than once (close to tray, then
    quit), and a second call simply starts a second pair - harmless, since the
    first to fire ends the process and the rest never run.
    """
    def end() -> None:
        # Flushed first: `os._exit` skips every handler, and the line saying
        # why is the one somebody will look for.
        _log.warning("still running {:.0f}s after the window closed, so it is "
                     "being ended", exit_after_s)
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:                          # noqa: BLE001
            pass
        exit(0)

    timers = [threading.Timer(dump_after_s, dump),
              threading.Timer(exit_after_s, end)]
    for timer in timers:
        timer.daemon = True
        timer.start()
    return timers
