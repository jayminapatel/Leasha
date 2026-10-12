"""Should the Search page answer what is in the box? Rules only, no model.

Layer: L7 (presenter: Qt-free)

Order `smarter-answers` 3a, decision D3 (owner, 2026-10-12). The finder stays the
headline; a question gets a short answer card above its results. A word or a
name is a search, not a question, and gets no card - a card that appeared for
every "barnsley" would be noise, and would hold the one chat model for nothing.

Decided by Chat's own router with no model (`route_question(text)`), so the Search
page and Chat never disagree about what a question is.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = ["answer_wanted", "ANSWER_KINDS", "MIN_CHARS", "card_kinds_shown"]

#: The kinds a card answers: something to read out of the files, a count, or
#: "do I have...". FIND ("show me the invoices") is what the results already are.
ANSWER_KINDS = ("LOOKUP", "SYNTHESIS", "ABSENCE", "AGGREGATE")

#: Shorter than this is a word or two, never a question worth a model.
MIN_CHARS = 8

_FILTER = re.compile(r"(?:^|\s)[a-z]+:\S", re.IGNORECASE)


def answer_wanted(text: str) -> bool:
    """True when `text` reads as a question the files could answer."""
    cleaned = " ".join(str(text or "").split())
    if len(cleaned) < MIN_CHARS or cleaned.startswith("/") or _FILTER.search(cleaned):
        return False
    try:
        from app.chat.router import route_question

        route = route_question(cleaned)
    except Exception:                                   # noqa: BLE001 - no card, never an error
        return False
    if route.effective not in ANSWER_KINDS:
        return False
    # A long string of search words falls to LOOKUP by default; only a rule or a
    # question mark says the person actually asked something.
    return route.by == "rules" or cleaned.endswith("?")


def card_kinds_shown(kind: Any) -> bool:
    """Which finished turns the card shows. An answer and a count say something;
    "nothing found" and a list of files are what the results below already show."""
    return str(kind or "") in ("answer", "aggregate")
