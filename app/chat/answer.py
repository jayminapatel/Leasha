r"""Generate a cited answer over retrieved passages, and verify it before
showing anything.

Layer: L8b — the piece that connects §1b (the loop), §1c (context economy)
and §2 (verification) into the one path that actually answers a question,
rather than only retrieving for one.

**Nothing before this module in the order ever asked the model to write
free prose.** The router picks one of six words; the loop asks for one more
query or `ENOUGH`; aggregate phrasing is validated against a number it
cannot change. This is the first genuinely open-ended generation - which is
exactly why it could not be built before §2 existed to check it, and why it
is built now that §2 does.

**Retry once, with tighter instructions, if too much was dropped** - the
order's own words for §2a. Not a general robustness loop: one retry, with
one different prompt, and if that also thins out the honest answer is that
retrieval did not support a confident answer - which is the caller's cue to
fall back to the absence protocol (§1e) rather than show a threadbare one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Sequence

from app.chat.context import build_context
from app.chat.verify import SIMILARITY_THRESHOLD, VerifiedAnswer, verify_answer
from app.core.logging import logger

__all__ = [
    "Answer",
    "ANSWER_TIMEOUT_S",
    "MAX_ANSWER_TOKENS",
    "THIN_DROP_RATIO",
    "build_answer_prompt",
    "answer_question",
]

log = logger.bind(component="chat.answer")

#: Seconds before giving up on a generation call. Longer than routing or
#: sufficiency budgets - this is the model's actual answer, the thing the
#: person is waiting for, not a background decision - but still bounded,
#: because the loop's own narration ("Searching…", "Found N documents…")
#: has already been on screen for a while by the time this runs and an
#: unbounded wait after visible progress reads as a hang precisely because
#: something *was* happening a moment ago.
ANSWER_TIMEOUT_S = 30.0

MAX_ANSWER_TOKENS = 400

#: Above this fraction of sentences dropped, the answer is "thinned too
#: far" and the loop retries once - the order's own phrase. Not zero: a
#: single dropped sentence out of several is ordinary caution working as
#: intended, not a sign the whole attempt failed.
THIN_DROP_RATIO = 0.5

_STOP: list[str] = []


class _Client(Protocol):
    def health(self, *, force: bool = ...) -> bool: ...
    def has_model(self) -> bool: ...
    def generate(self, prompt: str, *, json_mode: bool = ..., temperature: float = ...,
                 timeout: Optional[float] = ..., max_tokens: Optional[int] = ...,
                 stop: Optional[list[str]] = ...) -> object: ...


class _Embedder(Protocol):
    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True, slots=True)
class Answer:
    """`sentences` is the only part safe to render - every one of them
    passed §2's two gates. `passages` is what was actually offered to the
    model, in citation order, so a caller can show "source 3" as something
    real. `thin` means even after the retry too little survived - the
    caller's cue to fall back to the absence protocol rather than show a
    threadbare answer."""

    question: str
    sentences: tuple[str, ...] = ()
    passages: tuple[str, ...] = ()
    retried: bool = False
    thin: bool = False


def build_answer_prompt(question: str, passages: Sequence[str], *, tighter: bool = False) -> str:
    """Numbered passages, an instruction to cite every sentence, and -
    second attempt only - a tighter version asking for simple, single-
    source sentences rather than the free composition that thinned out the
    first time.
    """
    numbered = "\n\n".join(f"[{index}] {text}" for index, text in enumerate(passages, start=1))
    instruction = (
        "Answer the question using ONLY the numbered passages below. "
        "Every sentence you write must end with a citation marker such as "
        "[1] or [2] naming the passage it comes from. Write nothing that is "
        "not supported by a passage. If the passages do not answer the "
        "question, say so plainly.\n\n"
    )
    if tighter:
        instruction += (
            "Your previous answer could not be verified against the "
            "passages closely enough. This time write only short, direct "
            "sentences that closely follow a single passage each, ending "
            "with that passage's citation marker.\n\n"
        )
    return f"{instruction}{numbered}\n\nQuestion: {question}\nAnswer:"


def _generate(prompt: str, client: _Client, *, timeout_s: float) -> str:
    """The raw text, or `""` on any failure. **Never raises.**"""
    try:
        if not client.health() or not client.has_model():
            return ""
        response = client.generate(prompt, temperature=0.0, timeout=timeout_s,
                                   max_tokens=MAX_ANSWER_TOKENS, stop=_STOP)
        return getattr(response, "text", "") or ""
    except Exception as exc:                        # noqa: BLE001 - boundary; must never raise
        log.debug("answer generation failed: {}: {}", type(exc).__name__, exc)
        return ""


def _is_thin(verified: VerifiedAnswer) -> bool:
    total = len(verified.sentences)
    if total == 0:
        return False                                # nothing to drop is not "thinned"
    return (verified.dropped_count / total) > THIN_DROP_RATIO


def answer_question(
    question: str,
    passages: Sequence[str],
    *,
    embedder: _Embedder,
    client: _Client,
    context_length: Optional[int] = None,
    terms: Sequence[str] = (),
    threshold: float = SIMILARITY_THRESHOLD,
    timeout_s: float = ANSWER_TIMEOUT_S,
) -> Answer:
    """Size the passages, generate a cited answer, verify it, and retry
    once with tighter instructions if too much was dropped. **Never
    raises** - a total failure returns an `Answer` with no sentences and
    `thin=True`, the same "nothing to show" a caller already has to handle
    for an empty retrieval.
    """
    raw = (question or "").strip()
    if not raw or not passages:
        return Answer(question=raw)

    sized = build_context(list(passages), terms, context_length=context_length)

    prompt = build_answer_prompt(raw, sized)
    generated = _generate(prompt, client, timeout_s=timeout_s)
    verified = verify_answer(generated, sized, embedder=embedder, threshold=threshold)
    retried = False

    if generated and _is_thin(verified):
        retried = True
        tighter_prompt = build_answer_prompt(raw, sized, tighter=True)
        generated = _generate(tighter_prompt, client, timeout_s=timeout_s)
        verified = verify_answer(generated, sized, embedder=embedder, threshold=threshold)

    return Answer(
        question=raw, sentences=verified.rendered, passages=tuple(sized),
        retried=retried, thin=_is_thin(verified) or not verified.rendered,
    )
