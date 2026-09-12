r"""Which kind of question this is, decided before any retrieval runs.

Layer: L8b — `WORKORDER-202626270611-chat-tab.md` §1a.

Six classes, because the honest way to answer each is completely different:

* **LOOKUP** — extract one fact from a document ("what did the lease say
  about the deposit").
* **AGGREGATE** — counting, listing, filtering. **The index answers this, a
  query, never the model** ("how many PDFs did Dave send in 2019") — see
  `app/chat/aggregate.py` once §1d lands.
* **SYNTHESIS** — compares or summarises across more than one document
  ("what changed between the two drafts").
* **FIND** — really a search. Wants result rows, not prose ("show me the
  photos from the Diwali trip").
* **ABSENCE** — shaped as an existence check ("do I have...", "did I ever
  ..."). The retrieval may still succeed; what marks the class is the
  *question's own shape*, because a failed search on this shape needs the
  honest-scope answer in §1e, never "that doesn't exist" - see
  `app/chat/absence.py` once that section lands.
* **FOLLOW_UP** — depends on what was just said. Only assigned when there
  *is* a prior turn to depend on; the same wording with no history is a
  fresh, if vague, question and falls through to LOOKUP.

**Rules first, model assist second — the translator's own pattern**
(`app/search/translate.py`). Most questions are shaped clearly enough that a
keyword match settles it for free, deterministically, with no model and
nothing to test but a table of inputs and outputs. The model is asked only
when the rules genuinely cannot tell, and its answer is validated against the
fixed six classes exactly the way a translated query is validated against
the grammar in `translate._rejects` - a class the model invents, or prose
where a label was asked for, is rejected rather than passed through, and the
fallback is LOOKUP: the broadest class, so an unclassified question still
gets an honest attempt at an answer rather than being routed into a
specialised path it does not fit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Protocol, Sequence

from app.core.logging import logger

__all__ = [
    "QuestionClass",
    "RouteDecision",
    "classify",
    "build_prompt",
]

log = logger.bind(component="chat.router")

#: Seconds before giving up on the model and falling back to LOOKUP. Short,
#: deliberately: routing gates every other step in the loop, and nobody
#: clicked a button promising to wait for this the way Interpret's explicit
#: press justifies `translate.TRANSLATE_TIMEOUT_S`'s 30s.
#:
#: **Measured against `mistral:latest` (7.2B), the design target named in
#: this order's own header** - a genuinely cold load (`keep_alive=0`, then a
#: real classification call) took 15.91s; the next call, warm, took 0.86s.
#: 5s does not survive the cold case, and is kept anyway: Ollama keeps
#: generating server-side after the client's timeout gives up, so a cold
#: miss costs one safe LOOKUP fallback - a reasonable answer to most single-
#: fact questions regardless - while the model finishes warming in the
#: background, and every question after it in the session routes accurately
#: at well under a second. A timeout long enough to survive 15.91s would
#: make routing itself the slow part of the *first* question in every
#: session, for a fallback that already costs nothing worse than the
#: broadest class rather than a wrong one.
ROUTE_TIMEOUT_S = 5.0

#: Tokens the model may generate. A class name is one word - "AGGREGATE" is
#: the longest - so this is generous headroom, not a real budget; the point
#: is stopping a chatty model from explaining its reasoning at length while
#: the loop waits for a label it will parse the first line of anyway.
MAX_ROUTE_TOKENS = 8

_STOP = ["\n"]


class QuestionClass(str, Enum):
    """The six routes. A plain string enum so a decision serialises for the
    debug pane and a log line without a converter."""

    LOOKUP = "LOOKUP"
    AGGREGATE = "AGGREGATE"
    SYNTHESIS = "SYNTHESIS"
    FIND = "FIND"
    ABSENCE = "ABSENCE"
    FOLLOW_UP = "FOLLOW_UP"


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """The class, and how it was reached - visible in the debug pane so a
    wrong route is a two-second diagnosis rather than a mystery.

    `matched_rule` is the name of the rule that fired, or `None` when the
    model decided. `used_model` is false whenever a rule alone settled it,
    which is the common case and costs nothing.
    """

    question_class: QuestionClass
    matched_rule: Optional[str] = None
    used_model: bool = False
    #: The model's raw first line, kept for the debug pane even when it was
    #: rejected - "the model said 'sentiment' and that's not a class we have"
    #: is a more useful debug line than silence.
    raw_model_output: str = ""


class _Client(Protocol):
    """Just enough of `OllamaClient` to be faked in a test - the same
    seam `translate._Client` uses, for the same reason."""

    def health(self, *, force: bool = ...) -> bool: ...
    def has_model(self) -> bool: ...
    def generate(self, prompt: str, *, json_mode: bool = ..., temperature: float = ...,
                 timeout: Optional[float] = ..., max_tokens: Optional[int] = ...,
                 stop: Optional[list[str]] = ...) -> Any: ...


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

#: Checked in this order because a question can carry more than one signal -
#: "how many photos show the two of us" has both a count word and a subject -
#: and the ones earlier in this tuple are the harder-to-fake, more decisive
#: shape. AGGREGATE and ABSENCE both open with a distinctive grammatical
#: form; FIND and SYNTHESIS are marked by their verb; FOLLOW_UP is checked
#: last because it depends on history existing at all, which the others
#: never need.
#: **"how much" is deliberately not here.** "how much did the landlord
#: charge for the deposit" is a single figure in one document - a LOOKUP -
#: while "how much have I spent on the kitchen" sums across many. "how
#: many" does not share that ambiguity: it is reliably a count. Found live,
#: against the real model this order targets: the blunt version of this
#: rule misrouted the deposit question straight to AGGREGATE with no
#: candidate for the model to overrule, because a fired rule is never sent
#: on for a second opinion. Left for the model to decide rather than guessed
#: by a rule that would be wrong roughly half the time.
_AGGREGATE = re.compile(
    r"^\s*(?:how many|count|total number|number of)\b"
    r"|\b(?:list all|list every|show all|show every)\b",
    re.I,
)

_ABSENCE = re.compile(
    r"^\s*(?:do i have|did i have|have i (?:got|ever)|did i ever|"
    r"is there|are there|was there|were there)\b",
    re.I,
)

_FIND = re.compile(
    r"^\s*(?:show me|find me|find|search for|pull up|bring up)\b"
    r"|\b(?:photos? of|pictures? of|videos? of)\b",
    re.I,
)

_SYNTHESIS = re.compile(
    r"\b(?:summari[sz]e|summary of|compare|comparison|"
    r"what(?:'s| has| have)? (?:been )?changed|"
    r"what('?s| is| are) the difference|overall|in general|across all|"
    r"across every)\b",
    re.I,
)

#: A question with almost no content of its own - it leans entirely on
#: whatever was said a moment ago. **Checked only when there is history**
#: (see `classify`): the identical sentence opening a fresh conversation has
#: nothing to follow up on and is better answered as an ordinary, if vague,
#: LOOKUP than routed into a class with no prior turn to draw from.
_FOLLOW_UP = re.compile(
    r"^\s*(?:what about|and (?:the|what about)|tell me more|what else|"
    r"go on|the other one|the first one|the second one|that one|it,? "
    r"(?:then|too))\b",
    re.I,
)

#: `(pattern, class, rule name)`, tried in order. The rule name is what
#: `RouteDecision.matched_rule` reports.
_RULES: tuple[tuple[re.Pattern[str], QuestionClass, str], ...] = (
    (_AGGREGATE, QuestionClass.AGGREGATE, "aggregate_phrase"),
    (_ABSENCE, QuestionClass.ABSENCE, "absence_phrase"),
    (_FIND, QuestionClass.FIND, "find_verb"),
    (_SYNTHESIS, QuestionClass.SYNTHESIS, "synthesis_phrase"),
)


def _rule_match(question: str, *, has_history: bool) -> Optional[tuple[QuestionClass, str]]:
    """The first rule that fires, or `None` when the rules cannot tell.

    `has_history` gates `_FOLLOW_UP` alone - see that pattern's own
    docstring for why the other four never need it.
    """
    for pattern, question_class, name in _RULES:
        if pattern.search(question):
            return question_class, name
    if has_history and _FOLLOW_UP.search(question):
        return QuestionClass.FOLLOW_UP, "follow_up_phrase"
    return None


# ---------------------------------------------------------------------------
# Model assist
# ---------------------------------------------------------------------------

_LABELS = tuple(member.value for member in QuestionClass)

#: Only the first word of the reply is read - see `_extract_label` - so this
#: describes the classes but does not demand a bare label in case a small
#: model insists on padding anyway.
_CLASSIFY_TEMPLATE = (
    "Classify the question below into exactly one of these words:\n\n"
    "LOOKUP - a single fact to find in a document\n"
    "AGGREGATE - counting, listing or filtering many documents\n"
    "SYNTHESIS - comparing or summarising across documents\n"
    "FIND - really a search, wants documents back rather than an answer\n"
    "ABSENCE - asks whether something exists at all\n\n"
    "Reply with one word only, from the list above.\n\n"
    "Question: {question}\n"
    "Class:"
)


def build_prompt(question: str) -> str:
    """The classification prompt. **FOLLOW_UP is deliberately not offered
    here** - it depends on conversation history the model is not shown in
    this call, so asking it to choose a class it has no evidence for would
    only add a wrong answer to the rules' own inability to tell. A follow-up
    the rules missed is classified as whatever its content actually asks
    for, which is a safe fallback: a vague-but-fresh question and a genuine
    follow-up both survive being answered as LOOKUP."""
    return _CLASSIFY_TEMPLATE.format(question=question.strip())


def _extract_label(text: str) -> str:
    """Whichever known class name appears in the reply, or "" if none does.

    **Not "the first word".** A model asked for one word reliably wraps it
    in a sentence anyway - "The class is: AGGREGATE" - and taking literally
    the first token reads "The" as the answer. Searching for one of the six
    real labels is what actually recovers what the model meant, and it
    doubles as the validation: a reply naming none of them returns "", which
    `_model_classify` treats as a rejection rather than a class.
    """
    upper = (text or "").upper()
    for label in _LABELS:
        if re.search(rf"\b{label}\b", upper):
            return label
    return ""


def _model_classify(question: str, client: _Client, *, timeout_s: float) -> tuple[Optional[QuestionClass], str]:
    """`(class, raw_output)`. `class` is `None` on any failure or an answer
    outside the four labels this prompt actually offers - never raises."""
    try:
        if not client.health() or not client.has_model():
            return None, ""
        response = client.generate(
            build_prompt(question), temperature=0.0, timeout=timeout_s,
            max_tokens=MAX_ROUTE_TOKENS, stop=_STOP,
        )
        text = getattr(response, "text", "") or ""
    except Exception as exc:                        # noqa: BLE001 - boundary; routing must never raise
        log.debug("route classification failed: {}: {}", type(exc).__name__, exc)
        return None, ""

    label = _extract_label(text)
    # **Validated against the fixed set, exactly as a translated query is
    # validated against the grammar.** FOLLOW_UP is not in `_LABELS` here
    # because the prompt never offers it - see `build_prompt` - so a model
    # that invents it anyway is rejected the same as any other invention.
    if label in _LABELS and label != QuestionClass.FOLLOW_UP.value:
        return QuestionClass(label), text
    if label:
        log.info("route classification rejected: model said {!r}", label)
    return None, text


# ---------------------------------------------------------------------------
# The public entry point
# ---------------------------------------------------------------------------

def classify(
    question: str,
    *,
    client: Optional[_Client] = None,
    history: Optional[Sequence[Any]] = None,
    timeout_s: float = ROUTE_TIMEOUT_S,
) -> RouteDecision:
    """Which of the six classes this question is. **Never raises.**

    `history` is whatever the caller's conversation turns are - only its
    truthiness matters here, to gate `FOLLOW_UP`; the loop (§1b) is what
    reads the turns themselves once a question is actually routed there.

    Falls back to `QuestionClass.LOOKUP` when nothing - rules or model -
    can tell, on the same reasoning `translate.py` falls back to the raw
    text: an honest attempt at the broadest class beats refusing to route
    at all.
    """
    raw = (question or "").strip()
    has_history = bool(history)

    matched = _rule_match(raw, has_history=has_history)
    if matched is not None:
        question_class, rule_name = matched
        return RouteDecision(question_class=question_class, matched_rule=rule_name)

    if not raw or client is None:
        return RouteDecision(question_class=QuestionClass.LOOKUP)

    model_class, raw_output = _model_classify(raw, client, timeout_s=timeout_s)
    if model_class is not None:
        return RouteDecision(
            question_class=model_class, used_model=True, raw_model_output=raw_output,
        )
    return RouteDecision(
        question_class=QuestionClass.LOOKUP, used_model=True, raw_model_output=raw_output,
    )
