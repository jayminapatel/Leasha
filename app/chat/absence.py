r"""§1e: when nothing matches, say so honestly - never "that doesn't exist".

Layer: L8b — `WORKORDER-202626270611-chat-tab.md` §1e.

**This is the loop's own principle 4, not only the ABSENCE route's.** The
router's `ABSENCE` class names questions *shaped* like an existence check
("do I have…"); this module answers any question `run_loop` (§1b) came back
from with nothing at all, whichever class routed it there - a `LOOKUP` that
found nothing deserves exactly the same honesty an "is there…" question
does. Conflating "not in the index" with "does not exist" is the dishonesty
this whole order refuses to ship, on principle 4 of its own opening
paragraph.

Three things, always together: **what was searched** (every query the loop
actually ran, visibly - so a bad translation is a two-second read rather
than a mystery, the same transparency `translate.py` already promises for
search itself); **the honest scope** (nothing in *this index* matched -
never a claim about the world, and naming what the index currently covers
so the boundary is concrete rather than implied); and **somewhere to go
next** (different words; a drive that has not been indexed yet).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from app.chat.loop import LoopResult

__all__ = ["AbsenceAnswer", "is_empty", "answer_absence"]

#: Said in place of a source list when no folders are configured at all -
#: the ordinary state of a fresh install, not a fault to apologise for.
_NO_ROOTS = "no folders are set up to be searched yet"


@dataclass(frozen=True, slots=True)
class AbsenceAnswer:
    """`sentence` is the whole answer - narration is `LoopResult.narration`'s
    job, not this module's, and is not repeated here."""

    question: str
    queries: tuple[str, ...]
    sentence: str


def is_empty(result: LoopResult) -> bool:
    """Whether a loop found nothing at all, across every round it ran.

    **The gate a caller checks before reaching for this module.**
    `answer_absence`'s whole sentence is built on the premise that nothing
    matched; calling it for a loop that found something would print a false
    claim, so this is what a caller asks first.
    """
    return not result.all_results


def answer_absence(result: LoopResult, *, roots: Sequence[str] = ()) -> AbsenceAnswer:
    """The honest answer for a loop that found nothing. Never claims the
    thing does not exist - only that this index does not contain it, and
    says what the index currently covers so that scope is concrete."""
    searched = "; ".join(f'"{query}"' for query in result.queries) or f'"{result.question}"'
    sources = ", ".join(roots) if roots else _NO_ROOTS

    sentence = (
        f"Nothing in Leasha's index matches \"{result.question}\".\n\n"
        f"Searched: {searched}\n\n"
        f"Sources currently indexed: {sources}\n\n"
        "Try different words, or check whether it might be on a drive or "
        "folder that has not been indexed yet."
    )
    return AbsenceAnswer(question=result.question, queries=result.queries, sentence=sentence)
