"""The Chat tab's list of conversations: how it is grouped and filtered.

Layer: L5 (no Qt - `app/ui/widgets/chat_session_list.py` only draws this)

UI phase B (2026-10-13), "chat should behave like a chat": the list reads as every
chat app's does - pinned conversations first, then Today, Yesterday, Previous 7 days,
Previous 30 days, then one heading per month. The filter box narrows it in memory:
the conversations are already loaded with their turns, so nothing is read again.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

__all__ = ["PINNED_HEADING", "SEARCH_CHATS", "SEARCH_CHATS_TIP", "day_heading",
           "matches", "group_sessions", "PIN_LABEL", "UNPIN_LABEL"]

PINNED_HEADING = "Pinned"
SEARCH_CHATS = "Search chats"
SEARCH_CHATS_TIP = ("Show only the conversations whose name or messages have these words. "
                    "Clear the box to see them all.")
PIN_LABEL = "Pin to top"
UNPIN_LABEL = "Unpin"


def day_heading(when: float, now: float) -> str:
    """The heading a conversation last used at `when` sits under, seen at `now`.

    Calendar days in local time, not 24-hour windows: something from 23:50 last
    night is "Yesterday" at 00:10, as a person would say it."""
    today = datetime.fromtimestamp(now).date()
    try:
        day: date = datetime.fromtimestamp(float(when or 0)).date()
    except (OverflowError, OSError, ValueError):
        day = date(1970, 1, 1)
    age = (today - day).days
    if age <= 0:
        return "Today"
    if age == 1:
        return "Yesterday"
    if age <= 7:
        return "Previous 7 days"
    if age <= 30:
        return "Previous 30 days"
    return f"{_MONTHS[day.month - 1]} {day.year}"


#: Spelled out here, not `strftime("%B")`: that follows the machine's locale, and
#: every other word on the page is English.
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")


def matches(session: Any, query: str) -> bool:
    """Every word of `query` is in the conversation's name or in what was said.

    Case does not matter. The messages count as well as the name, because an
    automatic name is a guess and the words someone remembers are their own."""
    words = (query or "").casefold().split()
    if not words:
        return True
    text = " ".join([str(getattr(session, "title", "") or "")] + [
        str(getattr(t, "text", "") or "") for t in (getattr(session, "turns", None) or ())])
    haystack = text.casefold()
    return all(word in haystack for word in words)


def group_sessions(sessions: Iterable[Any], now: float,
                   query: str = "") -> list[tuple[str, list[Any]]]:
    """`[(heading, [session, ...]), ...]` in the order the list draws them.

    Pinned ones first under `PINNED_HEADING`, whatever their date; the rest by
    `day_heading`, newest first. A heading with nothing under it after the filter
    is left out."""
    kept = sorted((s for s in sessions if matches(s, query)),
                  key=lambda s: float(getattr(s, "updated", 0) or 0), reverse=True)
    groups: list[tuple[str, list[Any]]] = []
    pinned = [s for s in kept if getattr(s, "pinned", False)]
    if pinned:
        groups.append((PINNED_HEADING, pinned))
    for session in kept:
        if getattr(session, "pinned", False):
            continue
        heading = day_heading(getattr(session, "updated", 0), now)
        if groups and groups[-1][0] == heading:
            groups[-1][1].append(session)
        else:
            groups.append((heading, [session]))
    return groups
