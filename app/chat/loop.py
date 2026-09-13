r"""§1b: plan a search, retrieve, and know when to stop searching.

Layer: L8b — `WORKORDER-202626270611-chat-tab.md` §1b.

Three moves, repeated up to `MAX_ROUNDS` times:

1. **Plan a query.** Round one reuses the translator - the same "English to
   the query syntax that already exists" machinery `app/search/translate.py`
   validates - rather than inventing a second, parallel way to turn a
   question into a query. Rounds two and three ask the model directly for a
   follow-up query, validated the same way translation validates its own
   output: parsed with `parse_query()`, and rejected rather than run if it
   does not.
2. **Retrieve**, through the real `SearchEngine` - the only retrieval path
   this application has. Nothing here duplicates it or reaches around it.
3. **Assess sufficiency.** The model sees what each round found - counts and
   filenames, never the full text, which is §1c's job to manage properly -
   and says either `ENOUGH` or proposes one more search. Bounded, because "a
   small model in a tight tool loop beats a small model with a stuffed
   window" is this order's own framing for *why* to loop, not a licence to
   loop forever: three rounds is the ceiling named in the order, and a loop
   that cannot terminate on its own is a hang wearing a feature's name.

**Narration is a parameter, not a print statement.** `on_narrate`, when
given, is called with one plain-words line per step - "the loop's own
progress is the trust-building UX," the order's own words - so the eventual
UI can show it live and a test can capture it as a plain list of strings
without a display.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

from app.core.logging import logger
from app.search.commands import grammar_for_model
from app.search.query import parse_query
from app.search.translate import clean_output

__all__ = [
    "LoopResult",
    "MAX_ROUNDS",
    "SUFFICIENCY_TIMEOUT_S",
    "RESULT_SUMMARY_LIMIT",
    "build_sufficiency_prompt",
    "run_loop",
]

log = logger.bind(component="chat.loop")

#: The ceiling named in the order's own §1b: "the model may request FURTHER
#: searches (bounded, ≤3 rounds)". Round one always runs (it is how the
#: question gets answered at all); this bounds how many times the model may
#: ask for *another* one on top of it.
MAX_ROUNDS = 3

#: Seconds before giving up on a sufficiency check and stopping the loop
#: where it stands, with whatever rounds have already run.
#:
#: **Measured against varying prompts, which is the case that matters and
#: the one an easy first measurement missed.** Timing the same prompt three
#: times in a row against `mistral:latest` (7.2B) showed 4.4-4.7s and looked
#: fine - but that reused an *identical* prompt each time, which Ollama's own
#: prompt cache speeds up. A real loop asks a different question every call:
#: five distinct fixture questions against `mistral` took 31-36s **each**,
#: six in a row against a live index all missed an 8s budget outright. The
#: same five questions against `qwen2.5:1.5b` - the "tiny+fast" role §4d of
#: this order designs for, not the "strongest affordable" Chat role - took
#: 4.6-7.6s. **This budget assumes that role's model, not Chat's.** Wiring
#: sufficiency to whichever model answers the conversation, once roles exist,
#: would need this reconsidered against that model's own numbers - a config
#: mistake here reads as "the loop hangs," not as a model chosen unwisely.
SUFFICIENCY_TIMEOUT_S = 10.0

#: Tokens the sufficiency check may generate. `ENOUGH` is one word; a follow-
#: up query is a handful more - `from:dave licence renewal` is four. Generous
#: headroom for either, not a real budget.
MAX_SUFFICIENCY_TOKENS = 32

#: Result titles shown per round in the sufficiency prompt. Enough to judge
#: whether the round found the right *kind* of thing without handing the
#: model enough text to start answering from - that is §1c's job, done
#: properly, once retrieved text actually needs to reach a model.
RESULT_SUMMARY_LIMIT = 5

#: A follow-up carrying no operator and no phrase, longer than this many
#: words, is prose rather than a query - `translate.py`'s own "translation
#: compresses" principle, applied here. **Found live**: `parse_query()`
#: happily accepts "I think you should look in the Documents folder" as a
#: seven-word bag of keywords, because that is exactly what a plain-English
#: search is for - but a model *proposing a follow-up query* that produces a
#: sentence has not proposed anything, and running it as a keyword search
#: would spend a round searching for the model's hedging rather than
#: learning anything new.
_MAX_PLAIN_WORDS = 6

_STOP = ["\n\n"]


class _Engine(Protocol):
    """Just enough of `SearchEngine` to be faked in a test."""

    def search(self, raw: str, **kwargs: Any) -> Any: ...


class _Translator(Protocol):
    """Just enough of `QueryTranslator` to be faked in a test."""

    def translate(self, sentence: str, store: Any = None) -> Any: ...


class _Client(Protocol):
    """Just enough of `OllamaClient` to be faked in a test - the same seam
    `translate._Client` and `router._Client` use, for the same reason."""

    def health(self, *, force: bool = ...) -> bool: ...
    def has_model(self) -> bool: ...
    def generate(self, prompt: str, *, json_mode: bool = ..., temperature: float = ...,
                 timeout: Optional[float] = ..., max_tokens: Optional[int] = ...,
                 stop: Optional[list[str]] = ...) -> Any: ...


@dataclass(frozen=True, slots=True)
class LoopResult:
    """What the loop did, and why it stopped - both are load-bearing for the
    narration and for the debug pane; a caller that only wanted `responses`
    could have called `SearchEngine.search()` directly.

    `queries` and `responses` are the same length and in the same order -
    `responses[i]` is what `queries[i]` returned.
    """

    question: str
    queries: tuple[str, ...] = ()
    responses: tuple[Any, ...] = ()
    rounds_used: int = 0
    #: Why the loop ended: `"empty_question"`, `"sufficient"` (the model said
    #: so), `"max_rounds"` (the ceiling was reached), `"no_further_query"`
    #: (no model, or the model had nothing more to propose), or
    #: `"invalid_follow_up"` (the model proposed something that does not
    #: parse - rejected rather than run, on the translator's own principle
    #: that half a query is worse than none).
    stopped_reason: str = "empty_question"
    #: Every narration line emitted, in order - so a caller that has no live
    #: UI yet (nothing does, before §3a) can still see what the loop did.
    narration: tuple[str, ...] = field(default_factory=tuple)

    @property
    def all_results(self) -> list:
        """Every result from every round, duplicates and all - callers that
        care about dedup (§1c, or whatever renders the final answer) do it
        themselves; this is the raw material."""
        found: list = []
        for response in self.responses:
            found.extend(getattr(response, "results", None) or ())
        return found


def _narrate(on_narrate: Optional[Callable[[str], None]], said: list[str], line: str) -> None:
    said.append(line)
    if on_narrate is not None:
        try:
            on_narrate(line)
        except Exception as exc:                    # noqa: BLE001 - a narration callback must never break the loop
            log.debug("narration callback failed: {}", exc)


def _round_summary(query: str, response: Any, index: int) -> str:
    results = list(getattr(response, "results", None) or ())
    names = ", ".join(Path(r.path).name for r in results[:RESULT_SUMMARY_LIMIT])
    if len(results) > RESULT_SUMMARY_LIMIT:
        names += ", …"
    return f'  Round {index} ("{query}"): {len(results)} result(s){" - " + names if names else ""}'


def build_sufficiency_prompt(question: str, rounds: Sequence[tuple[str, Any]]) -> str:
    """The model's only job: `ENOUGH`, or one more query in the syntax that
    already exists. **Never the retrieved text** - only counts and
    filenames, which is what keeps this call cheap and keeps the model from
    starting to answer before verification (§2) exists to check it."""
    lines = [f"Question: {question}", "", "Found so far:"]
    lines.extend(_round_summary(query, response, index)
                for index, (query, response) in enumerate(rounds, start=1))
    lines.extend([
        "",
        "If this is enough to answer the question, reply with exactly: ENOUGH",
        "Otherwise, reply with one more search query - nothing else - in this syntax:",
        "",
        grammar_for_model(),
        "",
        "Reply with ENOUGH, or the next query, and nothing else.",
    ])
    return "\n".join(lines)


def _is_enough(text: str) -> bool:
    cleaned = (text or "").strip().strip(".:!\"'").upper()
    return cleaned == "ENOUGH" or cleaned.startswith("ENOUGH")


def _next_query(text: str, *, seen: set[str]) -> tuple[Optional[str], str]:
    """`(query, reason)`. `query` is `None` when the loop must stop; `reason`
    is always one of `LoopResult.stopped_reason`'s three model-driven values
    and is only meaningful when `query` is `None`.

    **Validated exactly as `translate._rejects` validates a translation**:
    parsed with `parse_query()`, empty answers refused, and a query already
    tried this loop refused too - a model repeating its own last query is
    not requesting more information, it is stalling, and running the same
    search again would only spend a round to learn nothing. Both count as
    `"invalid_follow_up"`: from the loop's side, an unusable answer and a
    repeated one are the same outcome - nothing new to run.
    """
    cleaned = clean_output(text)
    if _is_enough(cleaned):
        return None, "sufficient"
    if not cleaned:
        return None, "no_further_query"
    try:
        parsed = parse_query(cleaned)
    except Exception as exc:                        # noqa: BLE001 - any parser failure rejects the query
        log.debug("follow-up query rejected: does not parse: {}", exc)
        return None, "invalid_follow_up"
    if parsed.unknown_operators or not (parsed.has_text or parsed.has_filters):
        return None, "invalid_follow_up"
    if (not parsed.has_filters and not parsed.phrases
            and len(cleaned.split()) > _MAX_PLAIN_WORDS):
        return None, "invalid_follow_up"
    if cleaned in seen:
        return None, "invalid_follow_up"
    return cleaned, ""


def _ask_sufficiency(question: str, rounds: list[tuple[str, Any]], client: _Client,
                     *, timeout_s: float, seen: set[str]) -> tuple[Optional[str], str]:
    """`(query, reason)` - see `_next_query`. **Never raises**: a broken
    sufficiency check ends the loop where it stands, which still has
    whatever earlier rounds found to show, rather than taking the whole
    answer down with it."""
    try:
        if not client.health() or not client.has_model():
            return None, "no_further_query"
        response = client.generate(
            build_sufficiency_prompt(question, rounds), temperature=0.0,
            timeout=timeout_s, max_tokens=MAX_SUFFICIENCY_TOKENS, stop=_STOP,
        )
        text = getattr(response, "text", "") or ""
    except Exception as exc:                        # noqa: BLE001 - boundary; the loop must never raise
        log.debug("sufficiency check failed: {}: {}", type(exc).__name__, exc)
        return None, "no_further_query"
    return _next_query(text, seen=seen)


def run_loop(
    question: str,
    *,
    engine: _Engine,
    client: Optional[_Client] = None,
    translator: Optional[_Translator] = None,
    policy: Any = None,
    max_rounds: int = MAX_ROUNDS,
    sufficiency_timeout_s: float = SUFFICIENCY_TIMEOUT_S,
    on_narrate: Optional[Callable[[str], None]] = None,
) -> LoopResult:
    """Plan, retrieve, and decide when to stop. **Never raises** - every
    failure inside a round ends the loop with whatever it already has,
    exactly as `translate.QueryTranslator.translate` always returns
    something usable rather than an error a caller has to remember to
    handle.
    """
    raw = (question or "").strip()
    said: list[str] = []
    if not raw:
        return LoopResult(question=raw, stopped_reason="empty_question")

    _narrate(on_narrate, said, "Searching…")

    if translator is not None:
        query = translator.translate(raw).query
    else:
        query = raw

    queries: list[str] = []
    responses: list[Any] = []
    seen: set[str] = set()
    rounds: list[tuple[str, Any]] = []

    for round_number in range(1, max(1, max_rounds) + 1):
        response = engine.search(query, policy=policy)
        queries.append(query)
        responses.append(response)
        seen.add(query)
        rounds.append((query, response))
        count = len(getattr(response, "results", None) or ())
        _narrate(on_narrate, said,
                f"Found {count} document{'s' if count != 1 else ''}.")

        if round_number >= max_rounds:
            return LoopResult(question=raw, queries=tuple(queries),
                              responses=tuple(responses), rounds_used=round_number,
                              stopped_reason="max_rounds", narration=tuple(said))
        if client is None:
            return LoopResult(question=raw, queries=tuple(queries),
                              responses=tuple(responses), rounds_used=round_number,
                              stopped_reason="no_further_query", narration=tuple(said))

        _narrate(on_narrate, said, "Deciding whether that is enough…")
        next_query, reason = _ask_sufficiency(raw, rounds, client,
                                              timeout_s=sufficiency_timeout_s, seen=seen)
        if next_query is None:
            return LoopResult(question=raw, queries=tuple(queries),
                              responses=tuple(responses), rounds_used=round_number,
                              stopped_reason=reason, narration=tuple(said))

        query = next_query
        _narrate(on_narrate, said, f'Searching again: "{query}"…')

    # Unreachable - the loop above always returns - kept only so a static
    # checker sees every path produce a `LoopResult`.
    return LoopResult(question=raw, queries=tuple(queries), responses=tuple(responses),
                      rounds_used=len(queries), stopped_reason="max_rounds",
                      narration=tuple(said))
