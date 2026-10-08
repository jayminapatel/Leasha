r"""What the folder watch says under its switch. Words only; no Qt.

Layer: L5 (presenter)

Work order 0z, item F1. `app.cli watch --events jsonl` reports what it does as
`(kind, data)` events (`app/index/folder_watch.py`); this turns each into the
one sentence shown under "Index files as soon as they are saved" on the
Indexing page, or None for an event that should leave the line as it is.

Kept apart from the control so every sentence can be tested without a window.
"""

from __future__ import annotations

import time
from typing import Any, Optional

__all__ = ["OFF", "STARTING", "NO_FOLDERS", "watch_status"]

#: The switch is off: the line is empty rather than saying "off" under a box
#: that is plainly unticked.
OFF = ""
STARTING = "Starting to watch your folders…"
NO_FOLDERS = "There are no folders to watch yet. Add one under Folders to index."


def _clock(data: dict) -> str:
    """`HH:MM` for the event's time, or now when it carries none."""
    try:
        return time.strftime("%H:%M", time.localtime(float(data.get("t"))))
    except (TypeError, ValueError):
        return time.strftime("%H:%M")


def _error_message(error: Any) -> str:
    """The message out of an error dict or `AppError`, or ""."""
    if isinstance(error, dict):
        return str(error.get("message") or "")
    return str(getattr(error, "message", "") or "")


def watch_status(kind: str, data: dict, *, folders: int = 0) -> Optional[str]:
    """The sentence for one event, or None to leave the line unchanged.

    `folders` is how many folders are being watched, for the sentences that
    count them.
    """
    if kind == "ready":
        count = int(data.get("folders") or folders or 0)
        return ("Watching 1 folder for changes." if count == 1
                else f"Watching {count:,} folders for changes.")
    if kind == "updated":
        parts = []
        indexed, removed = int(data.get("indexed") or 0), int(data.get("removed") or 0)
        if indexed:
            parts.append(f"{indexed:,} indexed")
        if removed:
            parts.append(f"{removed:,} removed")
        if int(data.get("skipped") or 0):
            parts.append(f"{int(data['skipped']):,} could not be read")
        if not parts:
            return None
        names = [str(name) for name in (data.get("names") or [])][:3]
        line = f"Updated at {_clock(data)}: {', '.join(parts)}"
        return line + (f" ({', '.join(names)})." if names else ".")
    if kind == "busy":
        reason = str(data.get("reason") or "").rstrip(".")
        count = int(data.get("count") or 0)
        waiting = "1 change is" if count == 1 else f"{count:,} changes are"
        return f"{waiting} waiting: {reason}." if reason else f"{waiting} waiting."
    if kind == "problem":
        if data.get("error") is None:
            return f"Watching {data.get('root', 'the folder')} again."
        return _error_message(data.get("error")) or None
    if kind == "error":
        return _error_message(data.get("error")) or None
    if kind == "idle":
        return "Every folder is marked Archive, so there is nothing to watch."
    if kind == "ended":
        if data.get("expected"):
            return OFF
        return ("The folder watch stopped unexpectedly. It will be started "
                "again; files saved meanwhile are found by the next index run.")
    return None                                  # "watching", "pending", "stopped"
