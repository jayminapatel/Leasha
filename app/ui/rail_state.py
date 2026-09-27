r"""What the rail's indexing pill says. Qt-free.

Layer: L5 (presenter half)

UI Redesign (202626160950 §2d). The Indexing page stopped being a rail entry
and became a small pill at the rail's foot: a headline word, a 3px bar and one
line of figures. The words are decided here, so a test can check every state
without a display - the same split `pinned.py` and `timeline.py` already
make for their views.

Four states, one function. The figures are formatted with `format_count`
from `presenter` so the pill and the Indexing page never disagree about the
same number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from app.ui.presenter import format_count

__all__ = ["PillState", "TONES", "IDLE", "RUNNING", "FINISHED", "FAILED", "pill_text",
           "pill_fraction"]

IDLE = "idle"
RUNNING = "running"
FINISHED = "finished"
FAILED = "failed"


@dataclass(frozen=True)
class PillState:
    """`headline` and `detail` for the pill, plus whether the bar is busy.

    `tone` is which colour the pill's status dot takes (owner, 2026-09-27:
    the pill "looks big and out of place", so it became an icon with a
    coloured dot over one short word, like the rail buttons around it). One
    of `TONES`; the rail turns it into a theme token, so this stays Qt-free.
    """

    headline: str
    detail: str
    busy: bool = False
    tone: str = "quiet"


#: The status dot's colours, as theme tokens. "quiet" is for an index nobody
#: has counted yet, or one with nothing in it.
TONES: dict[str, str] = {
    "working": "accent",
    "held": "warning",
    "failed": "danger",
    "done": "kind_code",
    "quiet": "text_faint",
}


def pill_text(state: str, *, indexed: int = 0, documents: Optional[int] = None,
              paused: bool = False, stopped_early: bool = False,
              error: str = "") -> PillState:
    """The pill's two lines for a state.

    `documents` is the index's document count, shown when nothing is running;
    `indexed` is this run's count while it is. `None` for `documents` means
    nobody has counted yet, and the pill says nothing rather than "0 files".
    """
    if state == RUNNING:
        if paused:
            return PillState("Paused", f"{format_count(indexed)} so far", busy=True,
                             tone="held")
        return PillState("Indexing", f"{format_count(indexed)} so far", busy=True,
                         tone="working")
    if state == FAILED:
        return PillState("Needs attention", (error or "The last run failed").strip(),
                         tone="failed")
    if state == FINISHED and stopped_early:
        return PillState("Stopped", f"{format_count(indexed)} indexed", tone="held")
    # Finished whole, or idle with a count.
    if documents is None:
        return PillState("Index", "")
    # **An empty index is not "up to date".** On the very first run the pill
    # said "Up to date - 0 files" while the Search page, beside it, said
    # "Nothing has been indexed yet" - two answers to one question, and the
    # reassuring one was wrong (grabbed 2026-09-27, order 0x section 9). New
    # words, for a state that had none of its own; "Up to date" is unchanged
    # for every index that holds something.
    if documents == 0:
        return PillState("Nothing yet", "0 files")
    return PillState("Up to date", f"{format_count(documents)} files", tone="done")


def pill_fraction(value: Any, total: Any) -> Optional[float]:
    """`value / total` clamped to `[0, 1]`, or `None` for an indeterminate bar.

    Mirrors `presenter.progress_for`'s `(0, 0)` convention: a zero total is
    "we do not know yet", which the pill draws as a moving bar rather than an
    empty one.
    """
    try:
        v, t = float(value), float(total)
    except (TypeError, ValueError):
        return None
    if t <= 0:
        return None
    return max(0.0, min(1.0, v / t))
