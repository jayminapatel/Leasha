r"""Record what someone actually did in the window, so a bug report is evidence.

Layer: L5

Every UI bug in this project so far was found by a person clicking, and reported
as prose: *"the program crashed when i was clicking around, the thread is stuck"*.
That is a good report - it was enough to find four separate faults - but it costs
a round trip and a lot of guessing, and the crucial detail is usually the one
nobody thought to mention. In that case it was **which tab was open**: the graph
worker had been running for 200 seconds when the window closed, and nothing in
the report said so.

So this records the session instead. Every tab change, button, search, worker
start and finish, error and timing goes to one JSONL file with a millisecond
timestamp, and the file is written for someone else to read.

**Three rules, and they are the whole design.**

*Off unless asked.* A tool that watches by default is a tool people stop
trusting, and this application's entire promise is that nothing leaves the
machine. It is enabled by an explicit switch in Settings or `--debug` on the
command line, the window says so while it is on, and it writes only under
`logs\sessions\`.

*Never the cause of a failure.* A recorder that can raise is worse than no
recorder - it would turn a small bug into a crash, and it would do it inside the
handler for the bug you were trying to catch. Every method here swallows
everything.

*Content is never recorded, only shape.* A search is logged as its length,
whether it had operators, and how many results came back - not the query.
Filenames are logged as an extension and a size. The point is to reconstruct the
sequence of events, and a log full of somebody's actual searches is a liability
that would make the feature unusable for the one thing it is for: sending it to
somebody else.

Flushed on every write, because the session worth reading is the one that ended
badly, and a buffered log loses exactly the last few lines that mattered.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "DebugRecorder", "NullRecorder", "recorder_for", "SESSION_DIRNAME",
    "DEBUG_RECORDING_HELP",
]

#: The explanation shown on the Settings switch. Here rather than in the
#: widget because it is the *promise* the feature makes - that a session file
#: is safe to send - and that promise belongs beside the code that keeps it.
DEBUG_RECORDING_HELP = (
    "Writes a file under logs\\sessions\\ describing every tab change, button, "
    "search and error, so a bug can be diagnosed from evidence rather than from "
    "memory.\n\n"
    "It records the SHAPE of what happened, never the content: a search is "
    "recorded as its length and how many results came back, not as the text you "
    "typed, and a file as its extension rather than its name.\n\n"
    "Nothing leaves this machine unless you choose to send the file.\n\n"
    "Takes effect the next time you open the app."
)

SESSION_DIRNAME = "sessions"

#: Stop before a runaway loop fills the disk. A session that produces more
#: events than this is itself the finding, and the file says so before it stops.
MAX_EVENTS = 200_000


class NullRecorder:
    """The recorder when recording is off. Every call is a no-op.

    A null object rather than `if self._recorder is not None:` at forty call
    sites - the check that gets forgotten once is the crash in the code that
    exists to diagnose crashes.
    """

    enabled = False
    path: Optional[Path] = None

    def event(self, _kind: str, **_fields: Any) -> None:
        return None

    def close(self) -> None:
        return None

    def __enter__(self) -> "NullRecorder":
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None


class DebugRecorder:
    """Append one JSON object per line, describing what just happened."""

    enabled = True

    def __init__(self, path: Path, *, context: Optional[dict[str, Any]] = None) -> None:
        """Open the session file for appending; recording stays off if that fails."""
        self.path = Path(path)
        self._lock = threading.Lock()
        self._count = 0
        self._started = time.monotonic()
        self._handle: Any = None

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a", encoding="utf-8", newline="\n")
        except OSError:
            # Cannot open the log. Recording is a diagnostic aid, never a
            # precondition for the application running.
            self.enabled = False
            return

        self.event("session_start", **(context or {}), **_environment())

    # -- writing -------------------------------------------------------------

    def event(self, kind: str, **fields: Any) -> None:
        """Record one event. Never raises, whatever is passed to it."""
        if not self.enabled or self._handle is None:
            return
        try:
            with self._lock:
                if self._count >= MAX_EVENTS:
                    if self._count == MAX_EVENTS:
                        self._count += 1
                        self._write({
                            "kind": "recording_stopped",
                            "reason": f"more than {MAX_EVENTS:,} events - "
                                      "something is looping",
                        })
                    return
                self._count += 1
                self._write({"kind": kind, **{k: _safe(v) for k, v in fields.items()}})
        except Exception:                        # noqa: BLE001 - see module docstring
            pass

    def _write(self, payload: dict[str, Any]) -> None:
        """Append one JSON line, stamped, and flush it at once."""
        payload["at"] = datetime.now().isoformat(timespec="milliseconds")
        payload["t"] = round(time.monotonic() - self._started, 3)
        payload["thread"] = threading.current_thread().name
        self._handle.write(json.dumps(payload, default=str) + "\n")
        # Flushed every time on purpose: the session worth reading is the one
        # that ended in a way that skipped every cleanup path.
        self._handle.flush()

    def close(self) -> None:
        """Write `session_end` and close the file. Never raises; safe to call twice."""
        if self._handle is None:
            return
        try:
            self.event("session_end", events=self._count,
                       duration_s=round(time.monotonic() - self._started, 1))
            self._handle.close()
        except Exception:                        # noqa: BLE001
            pass
        finally:
            self._handle = None
            self.enabled = False

    def __enter__(self) -> "DebugRecorder":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def _safe(value: Any) -> Any:
    """Keep the shape, drop the content.

    Paths become an extension and a length; long strings become their length.
    Anything unrecognised is stringified and truncated. The rule is that a
    session file must be safe to send to somebody, or nobody will send one.
    """
    if isinstance(value, Path):
        return {"ext": value.suffix.lower(), "name_len": len(value.name)}
    if isinstance(value, str) and len(value) > 120:
        # Length only. An earlier version also kept `value[:60]` as a "head" to
        # make events easier to read, which is a content leak wearing a
        # debugging hat: sixty characters of a search query is the search query.
        # A test caught it, and it is exactly the mistake that would have made
        # session files unsafe to send - which would have made the whole feature
        # pointless, since sending them is the only thing they are for.
        return {"len": len(value)}
    if isinstance(value, (int, float, bool, type(None))):
        return value
    if isinstance(value, dict):
        return {k: _safe(v) for k, v in list(value.items())[:40]}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value[:40]]
    return str(value)[:200]


def _environment() -> dict[str, Any]:
    """What the reader needs before the first event makes sense."""
    info: dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "pid": os.getpid(),
    }
    try:
        # `version()`, not `build_info()["version"]`: the same string, without
        # the `git describe` `build_info` also runs. This is called while the
        # window is being built, and from `leasha.cmd` on the owner's laptop
        # that git call hung for its whole 5 s timeout - window visible at
        # 5.9 s instead of about 1 s (0r 2b, measured 2026-09-29).
        from app.core.version import version

        info["app"] = version()
    except Exception:                            # noqa: BLE001
        pass
    try:
        from PySide6.QtCore import QT_VERSION_STR

        info["qt"] = QT_VERSION_STR
    except Exception:                            # noqa: BLE001
        pass
    try:
        import psutil

        info["cores"] = psutil.cpu_count()
        info["ram_gb"] = round(psutil.virtual_memory().total / (1 << 30), 1)
    except Exception:                            # noqa: BLE001
        pass
    return info


def recorder_for(log_path: Any, *, enabled: bool, context: Optional[dict] = None) -> Any:
    """A real recorder when `enabled`, a `NullRecorder` otherwise.

    The one place that decides, so no caller has to. `log_path` is the
    application's existing log directory - session files live beside the logs
    rather than anywhere new, because there is already a documented answer to
    "where do I find the logs" and a second answer would be one too many.
    """
    if not enabled:
        return NullRecorder()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = Path(log_path)
    if folder.suffix:                            # a file was passed, not a folder
        folder = folder.parent
    return DebugRecorder(folder / SESSION_DIRNAME / f"session-{stamp}.jsonl",
                         context=context)
