r"""§1d: AGGREGATE answers are computed, not generated.

Layer: L8b — `WORKORDER-202626270611-chat-tab.md` §1d.

"the number in the answer IS the query result, injected — the model may
only phrase around values it cannot alter (template slots, not free
generation over numbers)."

The number always comes from `app.search.keyword.count_matching` - a real
SQL count, exact and unlimited, never the model's guess. The model's only
job, if one is given at all, is to wrap that number in a natural sentence -
and its answer is **validated against the number it was handed**, the same
discipline `translate.py` validates a translated query against the grammar
and `chat.loop` validates a follow-up query against the parser: an answer
that drops the number, changes it, or invents a second one nearby is
rejected in favour of a plain templated sentence that cannot be wrong,
because a wrong number stated confidently is worse than a plain one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from app.core.logging import logger
from app.search.keyword import count_matching
from app.search.query import parse_query

__all__ = ["AggregateAnswer", "PHRASING_TIMEOUT_S", "answer_aggregate", "build_phrasing_prompt"]

log = logger.bind(component="chat.aggregate")

#: Seconds before giving up on phrasing and using the plain templated
#: sentence. Short, and the fallback is never worse than the model's
#: sentence would have been - see the module docstring - so there is
#: nothing to be gained by waiting longer for a nicety.
PHRASING_TIMEOUT_S = 8.0

MAX_PHRASING_TOKENS = 64

_STOP = ["\n\n"]

#: A run of digits, optionally with thousands separators - `47`, `1,234`.
#: Matches how a model is asked to write the number back (see
#: `build_phrasing_prompt`) and how `format_count`-style output elsewhere in
#: this application renders one.
_NUMBER = re.compile(r"\b\d{1,3}(?:,\d{3})*\b")


class _Store(Protocol):
    """Just enough of `SqliteStore` to be faked in a test."""

    conn: Any


class _Translator(Protocol):
    def translate(self, sentence: str, store: Any = None) -> Any: ...


class _Client(Protocol):
    """Just enough of `OllamaClient` to be faked in a test - the same seam
    every other chat module uses."""

    def health(self, *, force: bool = ...) -> bool: ...
    def has_model(self) -> bool: ...
    def generate(self, prompt: str, *, json_mode: bool = ..., temperature: float = ...,
                 timeout: Optional[float] = ..., max_tokens: Optional[int] = ...,
                 stop: Optional[list[str]] = ...) -> Any: ...


@dataclass(frozen=True, slots=True)
class AggregateAnswer:
    """`count` is the number of record - always trustworthy, always the
    thing to show if `sentence`'s phrasing is ever in doubt. `sentence` is
    the phrased answer, which is either the model's wording (validated) or
    the plain template - either way it contains `count` verbatim."""

    question: str
    query: str
    count: int
    sentence: str
    used_model: bool = False


def build_phrasing_prompt(question: str, count: int) -> str:
    """The model's only job: wrap `count` in one sentence, changing nothing
    about the number itself.

    **An example, not just the rule** - `translate.build_prompt`'s own
    finding, confirmed again here: asked only for "the number, unchanged, in
    a sentence", a 1.5B model answered with the bare digit and nothing else -
    correct, but not what was asked for. One example fixed it immediately.
    Rules describe the format; examples demonstrate it.
    """
    return (
        f"Answer the question in one short, natural sentence. "
        f"The answer is the number {count} - use exactly that number, "
        f"written the same way, and no other number.\n\n"
        "Example:\n"
        "Question: how many contracts are there\n"
        "Answer: There are 12 contracts.\n\n"
        f"Question: {question}\n"
        f"Answer:"
    )


def _default_sentence(count: int) -> str:
    noun = "document" if count == 1 else "documents"
    return f"{count:,} matching {noun}."


def _validate_phrasing(text: str, count: int) -> Optional[str]:
    """The phrased sentence, or `None` when it must be rejected.

    **Every number in the reply must be `count`.** One number, matching
    exactly, is a sentence that phrases the answer; zero numbers means the
    model dropped it; two or more - even if one of them is `count` - means
    it stated an alternative, and which one is right is exactly the
    question this function exists to never leave open.
    """
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    found = [match.group(0).replace(",", "") for match in _NUMBER.finditer(cleaned)]
    if found != [str(count)]:
        return None
    return cleaned


def _phrase(question: str, count: int, client: _Client, *, timeout_s: float) -> Optional[str]:
    """The model's sentence, validated - or `None` on any failure, at which
    point the caller uses the plain template. **Never raises.**"""
    try:
        if not client.health() or not client.has_model():
            return None
        response = client.generate(
            build_phrasing_prompt(question, count), temperature=0.0,
            timeout=timeout_s, max_tokens=MAX_PHRASING_TOKENS, stop=_STOP,
        )
        text = getattr(response, "text", "") or ""
    except Exception as exc:                        # noqa: BLE001 - boundary; must never raise
        log.debug("aggregate phrasing failed: {}: {}", type(exc).__name__, exc)
        return None
    return _validate_phrasing(text, count)


def answer_aggregate(
    question: str,
    *,
    store: _Store,
    translator: Optional[_Translator] = None,
    client: Optional[_Client] = None,
    timeout_s: float = PHRASING_TIMEOUT_S,
) -> AggregateAnswer:
    """The count, computed - and a sentence around it, phrased if a model is
    given and validated, plain otherwise. **Never raises.**"""
    raw = (question or "").strip()
    query = translator.translate(raw).query if translator is not None else raw
    parsed = parse_query(query)
    count = count_matching(store, parsed)

    sentence = None
    used_model = False
    if client is not None:
        sentence = _phrase(raw, count, client, timeout_s=timeout_s)
        used_model = sentence is not None
    if sentence is None:
        sentence = _default_sentence(count)

    return AggregateAnswer(question=raw, query=query, count=count,
                           sentence=sentence, used_model=used_model)
