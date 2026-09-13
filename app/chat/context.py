r"""§1c: context economy for small models.

Layer: L8b — `WORKORDER-202626270611-chat-tab.md` §1c.

**A budget derived from the model's actual window, not a guessed constant.**
`OllamaClient.context_length()` asks the model directly; everything here is
envelope arithmetic over that number, the same shape as the Index Tuning
screen's own compute-profile envelopes - detected facts in, a safe number
out, and the number moves the day the model does.

**Reuses `app.search.window`, rather than a second way to cut a passage
down.** `rerank_window` already centres on the densest cluster of query
terms at a given character width - built for the cross-encoder, but the
idea ("keep the part worth reading, drop the rest") is exactly what a small
model's context window needs too. This module decides the *width*; that
one still does the cutting.

**Map, not reduce.** `build_context` is the "extract" half of "extract-
then-combine": one sized window per document, so all of them together fit
the budget. Folding those windows into one synthesis prompt is the caller's
job - keeping the two apart is what lets a two-document caller and a
twenty-document caller share the same sizing logic.
"""

from __future__ import annotations

from typing import Optional, Sequence

from app.search.window import RERANK_WINDOW_CHARS, windows_for

__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_CONTEXT_LENGTH",
    "OUTPUT_RESERVE_TOKENS",
    "PROMPT_OVERHEAD_TOKENS",
    "context_budget_chars",
    "per_document_width",
    "build_context",
]

#: **The ratio already measured in this codebase, reused rather than
#: re-derived.** `app.search.window.RERANK_WINDOW_CHARS`'s own docstring:
#: "600 is about 150 tokens" - so a chat prompt and a reranker window agree
#: about what a token costs in characters, rather than each guessing its
#: own number.
CHARS_PER_TOKEN = RERANK_WINDOW_CHARS // 150

#: Used only when the model's context length cannot be determined at all -
#: Ollama unreachable, or a server old enough not to report it. Deliberately
#: small: every model actually seen on this project's own machine reports
#: 8,192 or more (`test_ollama_context_length.py`'s own fixture, taken from
#: a real `/api/tags` reply), so assuming less costs nothing when the real
#: number is known and only under-serves context when it genuinely is not.
DEFAULT_CONTEXT_LENGTH = 4096

#: Reserved for the model's own answer, so a budget filled to the absolute
#: limit does not leave it nothing to reply with.
OUTPUT_RESERVE_TOKENS = 512

#: Reserved for the fixed parts of a prompt that are not retrieved text -
#: instructions, the question, formatting. A round constant rather than a
#: per-call measurement: overestimating it slightly is cheap, and
#: underestimating it overflows the model's own window.
PROMPT_OVERHEAD_TOKENS = 512


def context_budget_chars(context_length: Optional[int]) -> int:
    """How many characters of retrieved text fit, after reserving room for
    the model's own answer and the prompt's fixed parts.

    Never negative - a context window too small to hold both reserves still
    returns `0` rather than a number a caller would have to guard against
    subtracting into.
    """
    length = context_length if context_length and context_length > 0 else DEFAULT_CONTEXT_LENGTH
    available_tokens = max(0, length - OUTPUT_RESERVE_TOKENS - PROMPT_OVERHEAD_TOKENS)
    return available_tokens * CHARS_PER_TOKEN


def per_document_width(budget_chars: int, document_count: int,
                       *, minimum: int = RERANK_WINDOW_CHARS) -> int:
    """The budget divided evenly across documents, floored at `minimum`.

    **The floor matters more than the division.** Below `RERANK_WINDOW_
    CHARS` a passage starts losing the surrounding context that makes a
    match meaningful - "the licence expires" without which licence - the
    same floor `app.search.window`'s own docstring names for the identical
    reason. A caller handed twenty documents against a small budget gets
    windows at the floor, not twenty slivers too thin to answer from; how
    many of those windows actually fit the budget is the caller's own
    decision once it has them; this module is just as available if the
    caller wants to drop a document rather than shrink every one.
    """
    if document_count <= 0:
        return budget_chars
    return max(minimum, budget_chars // document_count)


def build_context(
    passages: Sequence[str],
    terms: Sequence[str],
    *,
    context_length: Optional[int] = None,
) -> list[str]:
    """One sized, term-centred window per passage - the "extract" half of
    "extract-then-combine". Sizing is envelope arithmetic over
    `context_length` (see `context_budget_chars`); the actual cutting is
    `app.search.window.windows_for`, unchanged.
    """
    budget = context_budget_chars(context_length)
    width = per_document_width(budget, len(passages))
    return windows_for(passages, terms, width=width)
