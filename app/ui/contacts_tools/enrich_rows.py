"""How the Enrich window lays out its suggestions (order 1s section 7). Pure; no widgets.

Layer: L5

* 7a - grouped by contact, the contact with the surest suggestion first and, inside each, the
  surest first; "how sure" said in words ("Seen in 4 messages", "Once, 5 months ago").
* 7b, D4 - a row starts ticked at or above **Tick automatically from** and is not shown below
  **Hide suggestions below**. A row that would change a value the card already has (`update`,
  set by the signature reader, 3k) never starts ticked, however sure: it replaces something the
  person may have typed.
* 7d - what the run said besides suggestions, as one line: the age window's start, the sources
  unreachable or not asked again, what Any age would add, and how many were hidden.

Pure so the rules can be tested without a window, and so the dialog module stays the shell.
"""

from __future__ import annotations

import time
from typing import Any, Iterable, Optional

__all__ = ["confidence_of", "starts_ticked", "sure_words", "age_words", "layout", "notes_line", "since_words"]

#: What a proposal that carries no confidence counts as: `Proposal`'s own default.
DEFAULT_CONFIDENCE = 0.5


def confidence_of(p: Any) -> float:
    try:
        return float(getattr(p, "confidence", DEFAULT_CONFIDENCE))
    except (TypeError, ValueError):
        return DEFAULT_CONFIDENCE


def starts_ticked(p: Any, tick_from: float) -> bool:
    """D4. Confidence decides what is pre-ticked, never what is applied; an update is never pre-ticked."""
    return not bool(getattr(p, "update", False)) and confidence_of(p) >= float(tick_from)


def age_words(seconds: Any, now: Optional[float] = None) -> str:
    """How long ago, in the words a person uses for an age: days, then months, then years."""
    from app.ui.presenter.formatting import format_when  # noqa: PLC0415 - the Search tab's own words

    try:
        at = int(seconds or 0)
    except (TypeError, ValueError):
        return ""
    if at <= 0:
        return ""
    days = int(((now if now is not None else time.time()) - at) // 86400)
    if days < 30:
        return format_when(at * 1_000_000_000, now=now)
    if days < 365:
        months = max(1, days // 30)
        return f"{months} month{'s' if months != 1 else ''} ago"
    years = days // 365
    return f"{years} year{'s' if years != 1 else ''} ago"


#: The Settings words for how sure (`logic.ENRICH_CHOICES`, "Tick automatically from"), lowest first.
_SURE = ((0.95, "Very sure"), (0.85, "Sure"), (0.75, "Fairly sure"), (0.0, "Not sure"))


def sure_words(p: Any, now: Optional[float] = None) -> str:
    """7a: how sure, in words. Mail counts first (how many messages agree, or when the one was sent);
    a source that does not read mail says it as the Settings page does, "Sure (85%)"."""
    seen = int(getattr(p, "seen", 0) or 0)
    if seen >= 2:
        return f"Seen in {seen} messages"
    when = getattr(p, "when", None)
    if when:
        age = age_words(when, now)
        return f"Once, {age}" if age else "Once"
    conf = confidence_of(p)
    word = next(w for floor, w in _SURE if conf >= floor)
    return f"{word} ({round(conf * 100)}%)"


def layout(proposals: list, skip: Iterable[int], hide_below: float) -> tuple[list[tuple[Any, list[int]]], int]:
    """[(contact id, [proposal indexes])] surest contact first, surest row first; and how many were hidden.

    `skip` are indexes already accepted or turned down. Ties keep the order the sources gave."""
    gone = set(skip)
    groups: dict[Any, list[int]] = {}
    hidden = 0
    for i, p in enumerate(proposals):
        if i in gone:
            continue
        if confidence_of(p) < float(hide_below):
            hidden += 1
            continue
        groups.setdefault(getattr(p, "contact_id", None), []).append(i)
    for idxs in groups.values():
        idxs.sort(key=lambda i: -confidence_of(proposals[i]))
    ordered = sorted(groups.items(), key=lambda kv: -confidence_of(proposals[kv[1][0]]))
    return ordered, hidden


def since_words(since: Optional[int]) -> str:
    if not since:
        return ""
    return time.strftime("%d %B %Y", time.gmtime(int(since))).lstrip("0")


def notes_line(since: Optional[int], notes: Iterable[str], hidden: int) -> str:
    """7d: the run's notes in one line - the window's start first, then what the sources said."""
    bits = []
    if since:
        bits.append(f"Reading mail since {since_words(since)}.")
    bits += [" ".join(str(n).split()) for n in notes if str(n).strip()]
    if hidden:
        bits.append(f"{hidden} less sure suggestion{' is' if hidden == 1 else 's are'} hidden "
                    "(Settings > Accounts > Enrichment > Hide suggestions below).")
    return " ".join(bits)
