r"""The tray menu's status line - what indexing is doing, without opening the window.

Layer: L5 (presenter)

2026-10-05, the owner: "the right click on the icon in the notification area
says indexed but the count does not seem right". It said "Indexing: N indexed"
- the files the *last run* newly read, often a handful when nothing had
changed, and the images pass's when that ran last - and it never moved while
a run was going. The owner chose a live line instead: the same data the rail's
pill paints from (`IndexingView.progressed`, `totals_shown`), so the two can
never disagree.
"""

from __future__ import annotations

import datetime as _dt
from typing import Optional

__all__ = ["tray_status", "IDLE_WORDS"]

#: Before the first count has arrived.
IDLE_WORDS = "Not indexing"


def tray_status(state: str, *, value: int = 0, total: int = 0, paused: bool = False,
                stopped_early: bool = False, documents: Optional[int] = None,
                finished_at: Optional[_dt.datetime] = None) -> str:
    """One line for the tray menu and its tooltip.

    `state` is the pill's: "running", "finished", "failed", anything else idle.
    `value`/`total` are the bar's own numbers; `documents` the index's file count.
    """
    if state == "running":
        progress = f"{value:,} of {total:,}" if total else "finding files…"
        return f"Paused – {progress}" if paused else f"Indexing – {progress}"
    if state == "failed":
        return "The last run stopped with a problem – click to see it"
    if state == "finished" and stopped_early:
        return "Stopped part-way – click to carry on"
    files = f"{documents:,} files" if documents is not None else ""
    if state == "finished":
        when = f"last run {finished_at:%H:%M}" if finished_at else ""
        tail = " · ".join(part for part in (files, when) if part)
        return f"Up to date – {tail}" if tail else "Up to date"
    return f"{files} in the index" if files else IDLE_WORDS
