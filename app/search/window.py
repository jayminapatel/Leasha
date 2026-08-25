"""Cutting a passage down to the part worth scoring.

Layer: L4

A cross-encoder reads the query and one passage together, and its cost is at
best linear in the passage length - attention within the sequence is quadratic,
so it is usually worse than linear. The reranker was being handed **whole
chunks**: a mean of 1,070 characters and a maximum of 2,734, when the part that
decides relevance is the sentence or two around the query terms.

**The rest is not free, it is most of the bill.** Every character sent is
tokenised, embedded and attended over, to reach a verdict the model would have
reached from a fraction of it.

This is deliberately *not* `presenter.build_snippet`. That one produces
highlight offsets for painting and lives in the UI layer; this produces text for
scoring and lives beside the search. They share an idea - centre on the densest
cluster of terms - and nothing else, and merging them would tie the reranker's
input to a display decision.
"""

from __future__ import annotations

import re
from typing import Iterable, Sequence

__all__ = ["rerank_window", "RERANK_WINDOW_CHARS"]

#: Characters of each passage shown to the cross-encoder.
#:
#: 600 is about 150 tokens - roughly two sentences either side of a match, which
#: is what a person needs to judge relevance and therefore what the model needs.
#: The mean chunk is 1,070 characters, so this is not a dramatic cut; it is the
#: long ones, at 2,700, that were quietly costing three times their share.
#:
#: **Not smaller.** Below about 300 characters a match can lose the context that
#: makes it meaningful - "the licence expires" without what licence - and the
#: reranker starts scoring fragments rather than passages.
RERANK_WINDOW_CHARS = 600


def _terms_pattern(terms: Sequence[str]) -> "re.Pattern[str] | None":
    cleaned = [
        re.escape(term.strip().rstrip("*"))
        for term in terms
        if term and term.strip().rstrip("*")
    ]
    if not cleaned:
        return None
    cleaned.sort(key=len, reverse=True)
    return re.compile(r"\b(" + "|".join(cleaned) + r")", re.IGNORECASE)


def rerank_window(
    text: str,
    terms: Sequence[str],
    *,
    width: int = RERANK_WINDOW_CHARS,
) -> str:
    """The `width` characters of `text` most worth scoring.

    Centred on the densest run of query terms. Falls back to the opening of the
    passage when nothing matches - which is what a vector-only hit looks like:
    it matched on meaning, so there is no term to centre on, and the opening is
    as good a guess as any.

    Never returns more than `width` characters, and never splits a word at
    either end: a truncated token is noise to a tokeniser and can change how the
    surrounding ones are split.
    """
    flat = " ".join((text or "").split())
    if len(flat) <= width or width <= 0:
        return flat

    pattern = _terms_pattern(terms)
    matches = list(pattern.finditer(flat)) if pattern else []
    if not matches:
        return _snap(flat, 0, width)

    # The window containing the most matches. A passage where the terms appear
    # together is more relevant than one where they are scattered, and it is the
    # cluster the model should be reading.
    best_start, best_count = matches[0].start(), 0
    for match in matches:
        start = match.start()
        count = sum(1 for other in matches if start <= other.start() < start + width)
        if count > best_count:
            best_count, best_start = count, start

    start = max(0, best_start - width // 3)      # a little context before the hit
    return _snap(flat, min(start, max(0, len(flat) - width)), width)


def _snap(text: str, start: int, width: int) -> str:
    """`width` characters from `start`, not breaking a word at either end."""
    end = min(len(text), start + width)
    if start > 0:
        space = text.find(" ", start, start + 40)
        start = space + 1 if space != -1 else start
    if end < len(text):
        space = text.rfind(" ", end - 40, end)
        end = space if space != -1 else end
    return text[start:end].strip()


def windows_for(
    passages: Iterable[str],
    terms: Sequence[str],
    *,
    width: int = RERANK_WINDOW_CHARS,
) -> list[str]:
    """`rerank_window` over a batch, in order."""
    return [rerank_window(text, terms, width=width) for text in passages]
