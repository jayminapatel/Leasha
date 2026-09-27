"""A run that did not finish, in words.

Layer: L5. Part of the presenter package; imports no Qt.

Work order `dates-live-log-and-interrupted-runs` §3. `app/index/interrupted.py`
decides *whether* the last run ended without finishing; this decides what the
Indexing page and the command line say about it. Its own module rather than
more of `indexing.py`, which another change is editing at the same time, and
because every branch here is a sentence somebody will read at the worst
possible moment - the morning after a power cut - and deserves its own test.

**Calm, on purpose.** Nothing is wrong. The files that were not reached are
still waiting, everything indexed is kept, and pressing Start carries on. A
warning colour or the word "error" would send the person looking for damage
that is not there - or, worse, to "Reset index", which is the one button that
would actually lose something.
"""

from __future__ import annotations

import time as _time
from typing import Any, Mapping, Optional

from app.ui.presenter.formatting import format_count
from app.ui.presenter.indexing import StatRow

#: The row's label on the Indexing page's "This index" panel.
UNFINISHED_LABEL = "Last run did not finish"

#: What starting again costs, said once and the same way everywhere.
CARRIES_ON = ("Starting the index again carries on from where it stopped, and "
              "nothing already indexed is lost.")


def stopped_when(stopped_at: Any, *, now: Optional[float] = None) -> str:
    """`"about 14:02 today"`, `"... yesterday"`, `"... on 27 September"`.

    **"About", because it is the last checkpoint and not the moment of death.**
    A run writes its record every few seconds while it reads, so the time is
    close - but a machine that lost power cannot say exactly when, and a
    precise-looking time that is a minute out is the kind of detail that makes
    somebody distrust the rest of the sentence.

    The day is `tm_mday` rather than `%d`: "07 September" reads as a form
    rather than a sentence, and `%-d` (no leading zero) does not exist on
    Windows.
    """
    try:
        moment = float(stopped_at)
    except (TypeError, ValueError):
        return "at an unknown time"
    current = now if now is not None else _time.time()
    then = _time.localtime(moment)
    today = _time.localtime(current)
    yesterday = _time.localtime(current - 86_400)
    clock = _time.strftime("%H:%M", then)
    if then[:3] == today[:3]:
        return f"about {clock} today"
    if then[:3] == yesterday[:3]:
        return f"about {clock} yesterday"
    day = f"{then.tm_mday} {_time.strftime('%B', then)}"
    if then.tm_year != today.tm_year:
        day += f" {then.tm_year}"
    return f"about {clock} on {day}"


def unfinished_reach(found: Mapping[str, Any]) -> str:
    """How far it had got, in one sentence that never invents a number.

    The count of files not reached is only knowable from a counting pass of the
    same folders (`app.cli scan`); `app/index/interrupted.py` leaves it `None`
    otherwise, and this says what *is* known instead.
    """
    missing = found.get("not_reached")
    seen = int(found.get("seen", 0) or 0)
    if missing:
        noun = "file" if int(missing) == 1 else "files"
        return f"About {format_count(int(missing))} {noun} had not been reached yet."
    if not seen:
        return "It stopped before it had looked at any files."
    noun = "file" if seen == 1 else "files"
    if found.get("walk_complete"):
        return (f"It had found all {format_count(seen)} {noun} and was part-way "
                "through reading them.")
    return (f"It had looked at {format_count(seen)} {noun} and had not finished "
            "going through your folders.")


def unfinished_run_rows(
    found: Optional[Mapping[str, Any]], *, running: bool = False,
    now: Optional[float] = None,
) -> list[StatRow]:
    """The row the Indexing page shows for a run that did not finish, or `[]`.

    `running` is the page's own knowledge that a run is under way - this
    window's or another process's. The record is read before a run takes the
    lock, so without it the row would still be on screen for the whole of the
    run that is carrying on, saying the opposite of what is happening.
    """
    if not found or running:
        return []
    return [StatRow(
        UNFINISHED_LABEL,
        f"Stopped at {stopped_when(found.get('stopped_at'), now=now)}",
        note=f"{unfinished_reach(found)} {CARRIES_ON}",
    )]


def unfinished_run_line(
    found: Optional[Mapping[str, Any]], *, carrying_on: bool = False,
    now: Optional[float] = None,
) -> str:
    """The same finding for a terminal (non-negotiable #8), or `""`.

    `carrying_on` is `app.cli index`, which is at that moment doing the thing
    the page tells the person to do, so it says so instead. ASCII only: this
    is printed to a Windows console.
    """
    if not found:
        return ""
    head = (f"The last index run did not finish: it stopped at "
            f"{stopped_when(found.get('stopped_at'), now=now)}. "
            f"{unfinished_reach(found)}")
    if carrying_on:
        return (f"{head} This run carries on from where it stopped; nothing "
                "already indexed is lost.")
    return f"{head} {CARRIES_ON}"
