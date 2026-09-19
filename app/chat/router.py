"""What kind of question is this? Rules first, a small model second.

Layer: L8b - no Qt, no store, no network unless a model is handed in. Work order
`202626270611-chat-tab` section 1a.

The router decides **how a question will be answered**, and the six answers are
different machines, not different prompts:

    LOOKUP     "what did we agree with the landlord about the deposit"
               -> read the passages, quote what they say
    AGGREGATE  "how many PDFs did Dave send in 2019"
               -> a database query; the number IS the result
    SYNTHESIS  "summarise everything about the Leeds audit"
               -> per-document extracts, combined
    FIND       "show me the photos of the kids at the beach in 2015"
               -> not prose at all: the results, as results
    ABSENCE    "do I have anything from the solicitor?"
               -> did retrieval find something, or say exactly what was searched
    FOLLOWUP   "and what about 2019?"
               -> resolved against the conversation, then answered as one of the above

**Rules first, the way the translator does it** (`app/search/translate.py`): a
sentence that says "how many" is an AGGREGATE question and needs no model to
notice. The rules are the router; a model is consulted only for what they cannot
place - and even then its one-word answer is checked against the six classes and
discarded if it is anything else. So the whole router runs, and is tested, with no
Ollama installed.

**The decision is visible.** `Route.explain()` is a sentence for the debug pane -
what the router chose and which rule chose it - because "why did it treat my
question as a search?" deserves an answer that is not a shrug.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from app.chat.text import content_tokens

__all__ = [
    "Route",
    "route_question",
    "resolve_followup",
    "CLASSES",
    "LOOKUP", "AGGREGATE", "SYNTHESIS", "FIND", "ABSENCE", "FOLLOWUP",
]

LOOKUP = "LOOKUP"
AGGREGATE = "AGGREGATE"
SYNTHESIS = "SYNTHESIS"
FIND = "FIND"
ABSENCE = "ABSENCE"
FOLLOWUP = "FOLLOWUP"

CLASSES = (LOOKUP, AGGREGATE, SYNTHESIS, FIND, ABSENCE, FOLLOWUP)


@dataclass(frozen=True)
class Route:
    """The router's decision."""

    kind: str
    #: `rules`, `model` or `default` - who decided.
    by: str
    #: The rule that fired, in plain words.
    reason: str
    #: The question as it will be searched: for a follow-up, the sentence with
    #: the conversation folded in; otherwise the question as typed.
    question: str
    #: For FOLLOWUP: the class the resolved question falls into.
    resolved_kind: str = ""

    @property
    def effective(self) -> str:
        """The class that decides which machine runs."""
        return self.resolved_kind or self.kind

    def explain(self) -> str:
        via = {"rules": "by rule", "model": "by the router model",
               "default": "by default"}.get(self.by, self.by)
        text = f"Routed as {self.kind} {via}: {self.reason}."
        if self.kind == FOLLOWUP and self.resolved_kind:
            text += f" Read as: '{self.question}' ({self.resolved_kind})."
        return text

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "by": self.by, "reason": self.reason,
                "question": self.question, "resolved_kind": self.resolved_kind}


# --------------------------------------------------------------------------- rules

#: Nouns that name a *collection in the index* rather than a fact inside a
#: document: "how many PDFs" counts files, "how many people attended" reads one.
COLLECTION_NOUNS = frozenset("""
file files document documents doc docs pdf pdfs email emails mail mails message
messages photo photos picture pictures image images spreadsheet spreadsheets
workbook workbooks presentation presentations slide slides deck decks
attachment attachments invoice invoices letter letters report reports note notes
scan scans drawing drawings receipt receipts contract contracts folder folders
""".split())

_HOW_MANY = re.compile(r"\bhow\s+many\b", re.I)
_NOUN_ALTERNATIVES = "|".join(sorted(COLLECTION_NOUNS, key=len, reverse=True))
#: The counted thing must be *what follows* "how many" - "how many observations
#: were raised in the final report" mentions a report and is about a number
#: inside one, not a count of reports.
_HOW_MANY_COLLECTION = re.compile(
    r"\bhow\s+many\s+(?:[A-Za-z][A-Za-z'-]*\s+){0,2}?(?:" + _NOUN_ALTERNATIVES + r")\b", re.I)
_NUMBER_OF_COLLECTION = re.compile(
    r"\b(?:number|count)\s+of\s+(?:[A-Za-z][A-Za-z'-]*\s+){0,2}?(?:" + _NOUN_ALTERNATIVES + r")\b"
    r"|\bcount\s+(?:the|all|my)\s+(?:[A-Za-z][A-Za-z'-]*\s+){0,2}?(?:" + _NOUN_ALTERNATIVES + r")\b",
    re.I)
_HOW_MUCH_SPACE = re.compile(
    r"\bhow\s+(?:much|big|large)\b.*\b(?:space|storage|disk|size)\b"
    r"|\b(?:total|combined)\s+size\b|\bhow\s+big\s+(?:are|is)\s+(?:all|my|the)\b", re.I)
_LIST_ALL = re.compile(
    r"^\s*(?:please\s+)?(?:list|enumerate|give\s+me\s+a\s+list\s+of|give\s+me\s+(?:a\s+)?list)\b", re.I)
_NEWEST_OLDEST = re.compile(
    r"\bwhen\s+(?:was|is|did)\b.*\b(?:latest|newest|oldest|earliest|most\s+recent|last|first)\b"
    r"|\b(?:latest|newest|oldest|earliest|most\s+recent)\s+(?:file|document|email|photo|pdf|message|spreadsheet)\b", re.I)

_ABSENCE = re.compile(
    r"^\s*(?:(?:hey|please|so)[, ]+)?(?:"
    r"do\s+(?:i|we)\s+(?:have|got|own|keep)\b"
    r"|did\s+(?:i|we)\s+(?:ever|actually)\b"
    r"|have\s+(?:i|we)\s+(?:got|ever|any)\b"
    r"|is\s+there\s+(?:a|an|any|anything|something)\b"
    r"|are\s+there\s+(?:any|some)\b"
    r"|do\s+(?:you|leasha)\s+have\b"
    r"|does\s+(?:my|the)\s+(?:archive|index|computer|pc)\s+(?:have|contain)\b"
    r")", re.I)
_ABSENCE_ANYWHERE = re.compile(
    r"\b(?:is\s+there\s+anything|any\s+record\s+of|do\s+i\s+have\s+(?:a|an|any|anything))\b", re.I)

#: "Where is the valve schedule?" asks for a file; "where is the assembly point?"
#: asks what a document says. The difference is whether the thing asked for is a
#: kind of document, so `where is` is a FIND only when it names one.
_DOCUMENT_NOUNS = COLLECTION_NOUNS | frozenset("""
schedule agreement certificate itinerary matrix template register form statement policy
plan minutes recipe recipes spec specification manual guide sheet list log timetable
""".split())
_DOCUMENT_ALTERNATIVES = "|".join(sorted(_DOCUMENT_NOUNS, key=len, reverse=True))

_FIND = re.compile(
    r"^\s*(?:please\s+|can\s+you\s+|could\s+you\s+)?(?:"
    r"show(?:\s+me)?|find(?:\s+me)?|get\s+me|search(?:\s+for)?|look\s+for|pull\s+up|open|bring\s+up|display|fetch"
    r")\b"
    r"|^\s*where(?:'s|\s+(?:is|are))\s+(?:the\s+|my\s+|our\s+|a\s+|an\s+)?(?:[A-Za-z'-]+\s+){0,3}?"
    r"(?:" + _DOCUMENT_ALTERNATIVES + r")\b"
    r"|\b(?:photos?|pictures?|images?)\s+(?:of|from|with|at)\b", re.I)
#: "show me how..." is a question wearing a command.
_FIND_GUARD = re.compile(
    r"^\s*(?:please\s+|can\s+you\s+|could\s+you\s+)?(?:show|tell)\s+me\s+(?:how|what|when|who|why|whether)\b"
    r"|^\s*find\s+out\b|^\s*look\s+for\s+out\b", re.I)

_SYNTHESIS = re.compile(
    r"\b(?:summari[sz]e|summary\s+of|overview\s+of|compare|comparison|differences?\s+between"
    r"|across\s+(?:all|the|my|every)|timeline\s+of|pros\s+and\s+cons|main\s+points|key\s+points"
    r"|themes?\s+(?:in|across|of)|recurring|in\s+general|how\s+has\s+.+\s+changed|how\s+did\s+.+\s+(?:evolve|change)"
    r"|what\s+do\s+(?:all|my|the)\s+.+\s+say|everything\s+(?:about|on|we\s+have))\b", re.I)

_QUESTION_START = re.compile(
    r"^\s*(?:what|when|who|whom|whose|which|where|why|how|did|does|do|is|are|was|were|can|could"
    r"|has|have|will|would|should|tell\s+me)\b", re.I)

# Follow-ups ---------------------------------------------------------------

_FOLLOW_OPENER = re.compile(
    r"^\s*(?:and|also|then|so|but|what\s+about|how\s+about|what\s+else|same\s+(?:for|question)"
    r"|and\s+for|tell\s+me\s+more|more\s+(?:on|about|detail)|go\s+on|why\s+(?:is|was|did)\s+(?:that|it|this))\b", re.I)
#: Words that stand in for something already discussed. `that`/`this` count only
#: when nothing but a verb follows them ("when was that sent") - "that contract"
#: is a noun phrase and stands on its own.
_PRONOUN = re.compile(
    r"\b(?:it|its|they|them|their|those|these|he|she|his|her|him"
    r"|the\s+(?:first|second|third|last|latter|former|same|other)\s+one"
    r"|(?:that|this)(?=\s*(?:[?.!,]|$|\s(?:is|was|were|say|says|said|mean|means|one|sent|from|include|includes)\b)))\b",
    re.I)


def _followup_reason(question: str, history: Sequence[Any]) -> Optional[str]:
    """Why this reads as a follow-up, or None. Needs earlier conversation."""
    if not any(getattr(turn, "role", "") == "user" for turn in history):
        return None
    words = question.split()
    lead = _FOLLOW_OPENER.match(question)
    if lead:
        return f"it opens with '{lead.group(0).strip()}', which continues an earlier question"
    tokens = content_tokens(question)
    # A pronoun standing in for the subject, and too little left to search on.
    pronoun = _PRONOUN.search(question)
    if pronoun and len(tokens) <= 3:
        return f"'{pronoun.group(0)}' refers back to the conversation and there is little else to search"
    if len(words) <= 3 and not _QUESTION_START.match(question) and not _FIND.match(question) \
            and re.match(r"^\s*(?:in|from|for|by|with|since|after|before|during|last|this)\b", question, re.I):
        return "a short fragment that only makes sense after the previous question"
    return None


def _has_collection_noun(question: str) -> bool:
    words = {w.lower() for w in re.findall(r"[A-Za-z]+", question)}
    return bool(words & COLLECTION_NOUNS)


def _rule_class(question: str) -> Optional[tuple[str, str]]:
    """`(class, reason)` from the rules, or None when they cannot tell."""
    q = question.strip()

    if _HOW_MANY.search(q):
        if _HOW_MANY_COLLECTION.search(q) or re.search(r"\b(?:in|on)\s+(?:my|the)\s+(?:archive|index|computer|pc|drive|folder)\b", q, re.I):
            return AGGREGATE, "'how many' of things in the index is counted, not read"
        return LOOKUP, "'how many' of something inside a document is read from the document"
    if _NUMBER_OF_COLLECTION.search(q):
        return AGGREGATE, "'number of' files is counted, not read"
    if _HOW_MUCH_SPACE.search(q):
        return AGGREGATE, "a size question is answered by adding up the index"
    if _LIST_ALL.match(q) and _has_collection_noun(q):
        return AGGREGATE, "'list' is a database listing"
    if _NEWEST_OLDEST.search(q) and _has_collection_noun(q):
        return AGGREGATE, "'latest'/'oldest' is a date query over the index"

    if _ABSENCE.match(q) or _ABSENCE_ANYWHERE.search(q):
        return ABSENCE, "'do I have...' asks whether something exists in the index"

    if _FIND_GUARD.match(q):
        return LOOKUP, "a command that is really a question ('show me how...')"
    if _FIND.search(q) and not _SYNTHESIS.search(q):
        return FIND, "an instruction to find things - the answer is the results themselves"

    if _SYNTHESIS.search(q):
        return SYNTHESIS, "asks for something drawn from several documents"

    if _QUESTION_START.match(q) or q.endswith("?"):
        return LOOKUP, "a question about what documents say"
    return None


_ROUTER_PROMPT = (
    "Classify the question into exactly one word.\n"
    "LOOKUP = asks what a document says. AGGREGATE = asks how many, or to list files. "
    "SYNTHESIS = asks to summarise or compare several documents. "
    "FIND = asks to show or find files. ABSENCE = asks whether something exists.\n"
    "Reply with the one word only.\n\n"
    "Question: {question}\nClass:"
)


def _ask_model(llm: Any, question: str) -> Optional[str]:
    """The router model's one-word answer, checked against the classes."""
    try:
        reply = llm.generate(_ROUTER_PROMPT.format(question=question),
                             temperature=0.0, max_tokens=6, timeout=20.0,
                             stop=["\n"])
        text = str(getattr(reply, "text", reply) or "").strip().upper()
    except Exception:                                    # noqa: BLE001 - assist only
        return None
    word = re.sub(r"[^A-Z]", "", text.split()[0]) if text.split() else ""
    return word if word in (LOOKUP, AGGREGATE, SYNTHESIS, FIND, ABSENCE) else None


def route_question(
    question: str,
    history: Sequence[Any] = (),
    *,
    llm: Any = None,
) -> Route:
    """Classify `question` in the context of `history`. **Never raises.**

    `llm` is the router model, or `None`. It is asked only when no rule fires -
    the minority case - and its answer is validated before it is believed.
    """
    text = str(question or "").strip()
    reason = _followup_reason(text, history)
    if reason is not None:
        resolved = resolve_followup(text, history, llm=llm)
        inner = _rule_class(resolved)
        # A follow-up that resolves to nothing recognisable is still a lookup:
        # the person is asking about what was just discussed.
        return Route(FOLLOWUP, "rules", reason, resolved,
                     resolved_kind=inner[0] if inner else LOOKUP)

    found = _rule_class(text)
    if found is not None:
        return Route(found[0], "rules", found[1], text)

    if llm is not None:
        answer = _ask_model(llm, text)
        if answer is not None:
            return Route(answer, "model", "no rule fired, so the router model chose", text)

    # Nothing decided it. A short noun phrase ("kids at the beach 2015") is a
    # search; anything longer is a question about content.
    if len(text.split()) <= 6:
        return Route(FIND, "default", "no rule fired and it is a short phrase, so it is searched for", text)
    return Route(LOOKUP, "default", "no rule fired; treated as a question about content", text)


# --------------------------------------------------------------------------- follow-ups

_DATE_WORDS = re.compile(
    r"\b(?:(?:19|20)\d{2}|last\s+year|this\s+year|(?:january|february|march|april|may|june|july"
    r"|august|september|october|november|december))\b", re.I)


def _subject_of(question: str) -> str:
    """A question with its question-word scaffolding removed."""
    text = re.sub(r"[?!.]+\s*$", "", question.strip())
    text = re.sub(r"^\s*(?:and|also|then|so|but)\b[, ]*", "", text, flags=re.I)
    text = re.sub(r"^\s*(?:what|who|when|where|why|how|which)\s+(?:did|does|do|is|are|was|were|has|have|can|could|many|much)?\s*",
                  "", text, flags=re.I)
    return text.strip()


def resolve_followup(question: str, history: Sequence[Any], *, llm: Any = None) -> str:
    """The follow-up as a sentence that stands alone. **Never raises.**

    Deterministic first: "and what about 2019?" after "what did we agree about
    the deposit" becomes "what about 2019 regarding: agree about the deposit",
    with the earlier question's own dates dropped when the follow-up names new
    ones - so "2019" replaces "2018" rather than fighting it. A planner model,
    when there is one, is asked to write it properly; its sentence is used only
    if it is short and shares a word with what was said.
    """
    text = str(question or "").strip()
    previous = next((getattr(turn, "text", "") for turn in reversed(list(history))
                     if getattr(turn, "role", "") == "user"), "")
    if not previous:
        return text

    prior_subject = _subject_of(previous)
    if _DATE_WORDS.search(text):
        prior_subject = _DATE_WORDS.sub("", prior_subject).strip()
    tail = re.sub(r"^\s*(?:and|also|then|so|but|what\s+about|how\s+about|and\s+for)\b[, ]*", "",
                  text, flags=re.I).strip(" ?.!")
    standalone = f"{tail} ({prior_subject})".strip() if tail else previous

    if llm is not None:
        try:
            reply = llm.generate(
                "Rewrite the last question so it makes sense on its own, using the earlier "
                "question for context. One line, no explanation.\n\n"
                f"Earlier question: {previous}\nLast question: {text}\nRewritten:",
                temperature=0.0, max_tokens=48, timeout=20.0, stop=["\n"])
            rewritten = str(getattr(reply, "text", "") or "").strip().strip('"')
            shared = set(content_tokens(rewritten)) & (set(content_tokens(previous))
                                                       | set(content_tokens(text)))
            if 3 <= len(rewritten) <= 200 and shared:
                return rewritten
        except Exception:                                # noqa: BLE001 - assist only
            pass
    return standalone
