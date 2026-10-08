"""When the web is worth asking: a rule, not a vibe.

Layer: L8b - no Qt, no network, no model. Pure text and numbers, so it is unit-tested as a
table (`tests/unit/test_chat_webrule.py`) and the engine can import it at the top: it cannot
reach anything. `app/chat/web.py`, which can, is still imported lazily by the engine only.

The owner's design (2026-09-20): *the chat should use local source though*, and *optionally
can augment from web*. So with the Web switch on the order of a turn is:

1. the files are searched and assessed first, always;
2. this module says whether the web is worth asking **for this question, given what the files
   came back with**;
3. only if it says yes is the "Ask before each web search" prompt shown and anything sent.

A question the files already answer never prompts and never connects. The verdict is one of:

============  =====================================================================
`reason`      the web is asked because...
============  =====================================================================
`asked`       the person's own words ask for it ("search the web", "google", "online")
`none`        no passage supports any part of the question (nothing retrieved, or the
              answer stage found nothing usable)
`thin`        the retrieval assessment said thin: no passage holds half its words
`partial`     the files cover only part of it (best passage under `PARTIAL_BELOW`) and
              the question is one the files cannot be the last word on: it is about
              the present ("latest", "news", "today", "price") or asks what or who an
              outside thing is ("what is", "who is")
`personal`    NO - about the person's own affairs (my / our / we / I): a web answer to
              it would be a guess about their life, and what they said stays home
`files`       NO - the files answered it; the web is not touched
============  =====================================================================

Word lists are deliberately short and plain; a question they miss is only ever a question
the web is not offered for, which is the safe way to be wrong.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

__all__ = ["WebNeed", "decide", "PARTIAL_BELOW", "asks_for_web", "is_external", "is_personal"]

#: Best-passage coverage of the question's words below which the files count as covering only
#: part of it. (`plan.assess` already calls under 0.5 thin.)
PARTIAL_BELOW = 0.8

#: A question about the person's own affairs. Kept identical in meaning to the engine's own
#: `_PERSONAL` (a test compares them) - this copy exists so the rule needs no import of it.
_PERSONAL = re.compile(r"\b(?:my|our|mine|ours|we|we've|we'd|i|i've|i'd|i'm|me|us)\b", re.I)

#: The person's own words ask for the web.
_ASKED = re.compile(
    r"\b(?:search|look(?:ing)?\s+(?:it\s+|that\s+|this\s+)?up|check|find|google|browse)\b"
    r"[^.?!]{0,30}\b(?:online|on\s+the\s+(?:web|internet)|the\s+(?:web|internet)|web)\b"
    r"|\bgoogle\b|\bweb\s+search\b|\bsearch\s+online\b|\bon\s+the\s+(?:web|internet)\b"
    r"|\bfrom\s+the\s+(?:web|internet)\b|\bonline\s+search\b|\bonline\W*$", re.I)

#: A question about the present, which a file cannot be the last word on.
_TIMELY = re.compile(
    r"\b(?:latest|newest|current|currently|news|today|tonight|tomorrow|yesterday|this\s+(?:week|"
    r"month|year)|right\s+now|nowadays|recent|recently|up[- ]to[- ]date|weather|forecast|"
    r"price|prices|exchange\s+rate|score|scores|release[sd]?)\b", re.I)

#: A question asking what or who an outside thing is.
_DEFINITIONAL = re.compile(
    r"^\W*(?:(?:so|and|ok|okay|hey|please)[\s,]+)*(?:what(?:'s|\s+is|\s+are|\s+was|\s+were)|"
    r"who(?:'s|\s+is|\s+are|\s+was|\s+were)|define|meaning\s+of|explain\s+what)\b", re.I)


@dataclass(frozen=True)
class WebNeed:
    """The verdict: `wanted`, and the `reason` code from the table above."""

    wanted: bool
    reason: str

    def as_dict(self) -> dict:
        """Plain values for the debug pane."""
        return {"wanted": self.wanted, "reason": self.reason}


def is_personal(text: str) -> bool:
    """Whether the question is about the person's own affairs - a first-person word."""
    return bool(_PERSONAL.search(text or ""))


def asks_for_web(text: str) -> bool:
    """The person's own words ask for the web."""
    return bool(_ASKED.search(text or ""))


def is_external(text: str) -> bool:
    """A question about the present, or about what / who an outside thing is."""
    text = text or ""
    return bool(_TIMELY.search(text) or _DEFINITIONAL.search(text))


def decide(text: str, *, sources: int, thin: bool, coverage: Optional[float] = None) -> WebNeed:
    """Should the web be asked for `text`, given what the files came back with?

    `sources` is how many usable passages the files gave (0 when the answer stage found none),
    `thin` is the retrieval assessment's verdict, `coverage` its best-passage share of the
    question's words (`None` when unknown, which is never read as "the files answered").
    """
    if is_personal(text):
        return WebNeed(False, "personal")
    if asks_for_web(text):
        return WebNeed(True, "asked")
    if sources <= 0:
        return WebNeed(True, "none")
    if thin:
        return WebNeed(True, "thin")
    if coverage is not None and coverage < PARTIAL_BELOW and is_external(text):
        return WebNeed(True, "partial")
    return WebNeed(False, "files")
