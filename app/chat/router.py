"""What kind of question is this? Rules first, a small model second.

Layer: L8b - no Qt, no store, no network unless a model is handed in. Work order
`202626270611-chat-tab` section 1a.

The router decides **how a question will be answered**, and the answers are
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
    CHAT       "hi", "thanks", "what can you do?", "shorter", "translate that to French"
               -> **no retrieval at all**: the model answers from the conversation

**Retrieval first (owner, 2026-09-20: "the chat should use local source though").**
The person's own files are the primary basis of every substantive answer, so CHAT is
deliberately narrow: only *social or meta* turns (greetings, thanks, "what can you
do?"), *instructions about the previous answer* ("shorter", "translate that", "why?",
"continue"), and *writing, rewriting or maths tasks that name no file of theirs*
skip the archive. **A general-sounding question - "what is a PST file?" - still
searches first**; the answer says so plainly when the files have nothing on it and
only then offers a short, labelled general answer. This supersedes the earlier
"a plain question must not go searching".

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
from typing import Any, Callable, Optional, Sequence

from app.chat.text import content_tokens

__all__ = [
    "Route",
    "route_question",
    "resolve_followup",
    "CLASSES",
    "LOOKUP", "AGGREGATE", "SYNTHESIS", "FIND", "ABSENCE", "FOLLOWUP", "CHAT",
    "chat_reason", "VALUE_NOUNS",
]

LOOKUP = "LOOKUP"
AGGREGATE = "AGGREGATE"
SYNTHESIS = "SYNTHESIS"
FIND = "FIND"
ABSENCE = "ABSENCE"
FOLLOWUP = "FOLLOWUP"
CHAT = "CHAT"

CLASSES = (LOOKUP, AGGREGATE, SYNTHESIS, FIND, ABSENCE, FOLLOWUP, CHAT)


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
#: Things that are *written in* a document rather than *being* one, 2026-10-04.
#: "Find Jaymin's passport number" asked for a nine-digit value that was sitting
#: in a retrieved passage, and was answered with a list of 23 messages that
#: happen to contain the words - because "find" read as "find me the files".
#: A sentence that names one of these wants the value quoted, whatever verb it
#: opens with; "find the photos of the kids" names none and stays a FIND.
VALUE_NOUNS = frozenset("""
number numbers no num id ids identifier code codes reference ref refs pin
address addresses postcode zipcode zip
date dates expiry expires expiration deadline birthday dob
amount amounts price prices cost costs fee fees rate rates total totals balance sum
phone mobile telephone fax iban bic swift sortcode account serial registration vin
username password passcode login licence license nino ni utr vat passport
""".split())
_VALUE_PHRASE = re.compile(
    r"\b(?:sort\s+code|account\s+number|phone\s+number|mobile\s+number|reference\s+number"
    r"|policy\s+number|order\s+number|invoice\s+number|serial\s+number|licence\s+number"
    r"|license\s+number|registration\s+number|passport\s+number|expiry\s+date|due\s+date"
    r"|date\s+of\s+birth|email\s+address|postal\s+address|home\s+address|ip\s+address)\b", re.I)


def _names_a_value(question: str) -> Optional[str]:
    """The value word the question names, or None: "number" in "find the
    passport number", "address" in "show me the landlord's address"."""
    phrase = _VALUE_PHRASE.search(question)
    if phrase:
        return phrase.group(0).lower()
    for word in re.findall(r"[A-Za-z]+", question):
        low = word.lower()
        if low in VALUE_NOUNS and low not in ("passport", "vat"):
            # "passport" and "vat" alone name a document ("find my passport");
            # with "number" after them they name a value, and the phrase above
            # already caught that.
            return low
    return None


#: "show me how..." is a question wearing a command.
_FIND_GUARD = re.compile(
    r"^\s*(?:please\s+|can\s+you\s+|could\s+you\s+)?(?:show|tell)\s+me\s+(?:how|what|when|who|why|whether)\b"
    r"|^\s*find\s+out\b|^\s*look\s+for\s+out\b", re.I)

_SYNTHESIS = re.compile(
    r"\b(?:summari[sz]e|summary\s+of|overview\s+of|compare|comparison|differences?\s+between"
    r"|across\s+(?:all|the|my|every)|timeline\s+of|pros\s+and\s+cons|main\s+points|key\s+points"
    r"|themes?\s+(?:in|across|of)|recurring|in\s+general|how\s+has\s+.+\s+changed|how\s+did\s+.+\s+(?:evolve|change)"
    r"|what\s+do\s+(?:all|my|the)\s+.+\s+say|everything\s+(?:about|on|we\s+have))\b", re.I)

#: "mail about holiday from maya", "emails from the bank", "files on the boiler":
#: a kind of document and what it is about or who it is from, with no question
#: word. It asks for the documents, not for something they say.
_ARCHIVE_ALTERNATIVES = "|".join(sorted(COLLECTION_NOUNS, key=len, reverse=True))
ARCHIVE_FIND = re.compile(
    r"\b(?:" + _ARCHIVE_ALTERNATIVES + r")\s+(?:about|from|regarding|concerning|re|on|sent\s+(?:by|to)|to)\b",
    re.I)

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

    if _TASK_VERB.match(q):
        return SYNTHESIS, "asks for something written, which may draw on their files"

    if _FIND_GUARD.match(q):
        return LOOKUP, "a command that is really a question ('show me how...')"
    value = _names_a_value(q)
    if value is not None and _FIND.search(q) and not _SYNTHESIS.search(q):
        return LOOKUP, (f"asks for a value written in a document ('{value}'), "
                        "which is read and quoted, not listed")
    if _FIND.search(q) and not _SYNTHESIS.search(q):
        return FIND, "an instruction to find things - the answer is the results themselves"

    if _SYNTHESIS.search(q):
        return SYNTHESIS, "asks for something drawn from several documents"

    if _QUESTION_START.match(q) or q.endswith("?"):
        return LOOKUP, "a question about what documents say"
    if ARCHIVE_FIND.search(q):
        return FIND, "names a kind of document and what it is about - the answer is the documents"
    return None


# --------------------------------------------------------------------------- conversation (CHAT)

#: Words that say "my own files": any of them keeps a writing or maths request out
#: of CHAT, because the archive may have what it needs.
_ARCHIVE_WORDS = re.compile(
    r"\b(?:my|our|mine|ours|files?|folders?|documents?|docs?|emails?|e-mails?|mail|messages?"
    r"|photos?|pictures?|pdfs?|spreadsheets?|attachments?|invoices?|archive|index(?:ed)?"
    r"|inbox|drive|computer|pc|leasha)\b", re.I)

_SMALLTALK = re.compile(
    r"^\s*(?:(?:hi|hello|hey|hiya|howdy|yo|sup|good\s+(?:morning|afternoon|evening|day)|greetings)"
    r"(?:\s+(?:there|leasha|again|everyone|friend))?"
    r"|(?:thanks?|thank\s+you|thx|ta|cheers|many\s+thanks)"
    r"(?:\s+(?:a\s+lot|so\s+much|very\s+much|that\s+(?:helps|was\s+helpful|worked)|mate|leasha))?"
    r"|(?:ok(?:ay)?|cool|great|nice|perfect|awesome|brilliant|lovely|got\s+it|i\s+see|makes\s+sense"
    r"|sounds\s+good|fair\s+enough|good|fine)(?:\s*,?\s*(?:thanks?|thank\s+you|cheers))?"
    r"|(?:bye|goodbye|good\s*night|see\s+you(?:\s+later)?|cya|later|that'?s\s+all|that\s+is\s+all|never\s*mind)"
    r"|how\s+are\s+you(?:\s+(?:today|doing))?|how'?s\s+it\s+going|how\s+do\s+you\s+do|what'?s\s+up"
    r"|who\s+are\s+you|what\s+are\s+you|what'?s\s+your\s+name|what\s+is\s+your\s+name"
    r"|are\s+you\s+(?:there|real|an?\s+(?:ai|bot|robot|human)|listening|working)"
    r"|what\s+(?:can|could)\s+(?:you|i)\s+(?:do|ask(?:\s+you)?)(?:\s+(?:for\s+me|here|with\s+you))?"
    r"|what\s+do\s+you\s+do|what\s+is\s+this|what\s+can\s+this\s+do"
    r"|how\s+do\s+(?:you|i)\s+(?:work|use\s+(?:you|this|leasha))"
    r"|(?:can|could)\s+you\s+help(?:\s+me)?|i\s+need\s+(?:some\s+)?help|help(?:\s+me)?|help\s+please|please\s+help"
    r"|what\s+should\s+i\s+ask(?:\s+you)?|any\s+(?:tips|suggestions)|show\s+me\s+what\s+you\s+can\s+do"
    r"|tell\s+me\s+about\s+(?:yourself|you)|introduce\s+yourself)"
    r"\s*[!.?,;:)]*\s*$", re.I)

#: Reactions and feelings - talk, not a search ("lol", "I'm bored", "that's funny"). A first-person
#: sentence that *wants* something ("I'm looking for...", "I need...") is not here: it is searched.
_CHATTER = re.compile(
    r"^\s*(?:lol|lmao|haha+|hehe+|wow|oh(?:\s+(?:no|dear|wow|nice|ok(?:ay)?|right|i\s+see))?|hmm+|ah+|aha|yay|oops|ouch"
    r"|nice(?:\s+one)?|good\s+(?:job|one)|well\s+done|no\s+worries|sorry|my\s+bad|never\s*mind|fair\s+enough"
    r"|i(?:'|\u2019)?m\s+(?:so\s+|very\s+|a\s+bit\s+)?(?:bored|tired|sad|happy|confused|stuck|fine|good|great|ok(?:ay)?|back|here|lost|worried|stressed|annoyed|excited)"
    r"|i\s+am\s+(?:so\s+|very\s+|a\s+bit\s+)?(?:bored|tired|sad|happy|confused|stuck|fine|good|great|ok(?:ay)?|back|here|lost|worried|stressed|annoyed|excited)"
    r"|i\s+(?:feel|love|hate|like)\b.*"
    r"|(?:that|this|it)(?:'|\u2019)?s\s+(?:so\s+|really\s+|very\s+)?(?:funny|great|cool|interesting|wrong|right|odd|weird|strange|amazing|helpful|useful|good|bad|nice)"
    r"|you(?:'|\u2019)?re\s+(?:so\s+|really\s+)?(?:great|right|wrong|funny|clever|smart|helpful|good)"
    r"|(?:i\s+)?(?:appreciate|love)\s+(?:it|that|you|this)|you\s+(?:rock|are\s+(?:great|right|wrong)))"
    r"\s*[!.?,;:)]*\s*$", re.I)

#: A greeting or thanks that leads into the real message: "hi, what did we agree about the deposit?".
_GREETING_LEAD = re.compile(
    r"^\s*(?:hi|hello|hey|hiya|howdy|good\s+(?:morning|afternoon|evening)|thanks|thank\s+you|ok(?:ay)?|right|so)"
    r"(?:\s+(?:there|leasha|again))?\s*[,!.:;-]+\s*(?=\S)", re.I)

#: An instruction about *the previous answer* - meaningless without one.
_ABOUT_PREVIOUS = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|right|so|and|now|please|can\s+you|could\s+you|would\s+you|just|maybe)[, ]+)*"
    r"(?:"
    r"(?:make|keep)\s+(?:it|that|this)\s+(?:a\s+bit\s+|much\s+|way\s+)?"
    r"(?:shorter|longer|simpler|clearer|briefer|more\s+\w+|less\s+\w+|formal|informal|friendlier|punchier|concise)"
    r"|(?:shorter|longer|briefer|simpler|clearer|more\s+(?:detail|details|formal|casual|concise|friendly|polite)"
    r"|less\s+formal)(?:\s+(?:please|pls|version))?"
    r"|(?:shorten|lengthen|simplify|expand|elaborate|condense|summari[sz]e|tl;?dr|reword|rephrase|paraphrase"
    r"|rewrite|redo|repeat)(?:\s+(?:that|it|this|the\s+(?:last|previous)\s+(?:answer|one|reply)"
    r"|your\s+(?:answer|reply)))?(?:\s+(?:please|pls|again|more|a\s+bit|in\s+.{1,40}|as\s+.{1,40}|for\s+.{1,40}))?"
    r"|translate(?:\s+(?:that|it|this|your\s+(?:answer|reply)))?(?:\s+in(?:to)?\s+\w+|\s+to\s+\w+)?"
    r"|(?:in|into|as)\s+(?:french|spanish|german|italian|portuguese|dutch|hindi|nepali|chinese|japanese"
    r"|arabic|russian|english|bullets?|bullet\s+points?|a\s+(?:list|table|paragraph|sentence|nutshell)"
    r"|one\s+(?:line|sentence)|plain\s+(?:english|words))"
    r"|(?:say|put)\s+(?:that|it|this)\s+(?:again|differently|another\s+way|(?:in|as)\s+.{1,40})"
    r"|(?:continue|go\s+on|keep\s+going|carry\s+on|proceed|finish|more|and\s+then|then\s+what|what\s+else"
    r"|anything\s+else|next)(?:\s+(?:please|pls))?"
    r"|(?:explain|describe|say|tell\s+me|walk\s+me\s+through)\s+(?:that|it|this|more|again|further)"
    r"(?:\s+(?:again|more|simply|simpler|differently|(?:like|as\s+if)\s+.{1,40}|to\s+(?:me\s+)?(?:like\s+)?.{1,40}"
    r"|in\s+.{1,40}|please))?"
    r"|(?:explain|describe)\s+(?:it\s+|that\s+)?(?:like|as\s+if)\s+(?:i(?:'|’)?m|i\s+am)\s+.{1,40}"
    r"|eli5|explain\s+like\s+i(?:'|’)?m\s+.{1,20}"
    r"|why(?:\s+(?:is\s+that|is\s+it|was\s+that|so|not|though|do\s+you\s+say\s+that|did\s+you\s+say\s+that))?"
    r"|how\s+so|how\s+come|are\s+you\s+sure|really|what\s+do\s+you\s+mean|what\s+does\s+that\s+mean"
    r"|can\s+you\s+(?:say|explain)\s+(?:that|it)\s+(?:again|differently|more\s+simply)"
    r"|give\s+me\s+(?:an?\s+)?(?:example|examples|another(?:\s+one)?|more|a\s+summary)"
    r"|(?:another|one\s+more)(?:\s+(?:one|example))?"
    r"|(?:that'?s|that\s+is)\s+(?:wrong|not\s+right|incorrect|not\s+what\s+i\s+(?:asked|meant))"
    r"|no\s*,?\s*(?:i\s+meant|i\s+mean)\s+.{1,80}"
    r")(?:\s+(?:please|pls|thanks))?\s*[!.?,;:]*\s*$", re.I)

_TASK_VERB = re.compile(
    r"^\s*(?:please\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|i\s+(?:want|need|would\s+like)\s+(?:you\s+)?to\s+)?"
    r"(?:write|draft|compose|create|generate|brainstorm|come\s+up\s+with|make\s+(?:me\s+)?(?:a|an|some)"
    r"|give\s+me\s+(?:a|an|some)\s+(?:joke|poem|haiku|story|idea|ideas|example|recipe|name|names|slogan|quote)"
    r"|tell\s+me\s+(?:a|an)\s+(?:joke|story|riddle|fun\s+fact)|sing|rhyme|calculate|compute|solve|convert)\b", re.I)
_REWRITE_VERB = re.compile(
    r"^\s*(?:please\s+|can\s+you\s+|could\s+you\s+)?"
    r"(?:rewrite|rephrase|proofread|paraphrase|translate|edit|polish|reword|correct|improve|shorten|summari[sz]e|"
    r"fix\s+(?:the\s+)?(?:grammar|spelling|typos?|tone)|make\s+this|check\s+(?:the\s+)?(?:grammar|spelling)|"
    r"explain\s+this\s+(?:code|error|sentence|paragraph|text)|what\s+does\s+this\s+(?:code|error|mean))\b", re.I)
_ARITHMETIC = re.compile(
    r"^\s*(?:what(?:'s|\s+is)\s+|calculate\s+|compute\s+)?[-+]?\(?\s*\d[\d,.\s]*(?:[-+*/x×÷^%()]\s*\(?\d[\d,.\s]*\)?\s*)+"
    r"(?:=\s*)?\??\s*$"
    r"|^\s*what(?:'s|\s+is)\s+\d+(?:\.\d+)?\s*%\s+of\s+\d[\d,.]*\s*\??\s*$", re.I)
_CODE_FENCE = re.compile(r"```")


def chat_reason(question: str, history: Sequence[Any]) -> Optional[str]:
    """Why this turn needs no retrieval (a plain sentence), or `None`.

    Narrow on purpose - see the module docstring: only social turns, instructions
    about what was already said, and tasks that name none of the person's files."""
    text = str(question or "").strip()
    if not text:
        return None
    if _SMALLTALK.match(text):
        return "a greeting, thanks or a question about Leasha itself - nothing in the files is needed"
    if _CHATTER.match(text):
        return "a reaction or a feeling, not a question - nothing in the files is needed"
    if _ABOUT_PREVIOUS.match(text):
        return "an instruction about the previous answer - it needs the conversation, not the files"
    words = text.split()
    pasted = len(words) >= 18 or "\n" in text or bool(_CODE_FENCE.search(text))
    if _REWRITE_VERB.match(text) and (pasted or re.match(r"^\s*(?:please\s+)?translate\b", text, re.I)):
        return "rewriting or explaining text the person supplied - it is all in their message"
    if _CODE_FENCE.search(text) and not _ARCHIVE_WORDS.search(_CODE_FENCE.split(text)[0]):
        return "a question about code the person pasted"
    if _ARITHMETIC.match(text):
        return "arithmetic - computed, not looked up"
    if _TASK_VERB.match(text) and not _ARCHIVE_WORDS.search(text):
        return "a writing, code or maths task that names none of their files"
    return None


_ROUTER_PROMPT = (
    "Classify the question into exactly one word.\n"
    "LOOKUP = asks what a document says. AGGREGATE = asks how many, or to list files. "
    "SYNTHESIS = asks to summarise or compare several documents. "
    "FIND = asks to show or find files. ABSENCE = asks whether something exists.\n"
    "Reply with the one word only.\n\n"
    "Question: {question}\nClass:"
)


def _ask_model(llm: Any, question: str,
               should_stop: Optional[Callable[[], bool]] = None) -> Optional[str]:
    """The router model's one-word answer, checked against the classes."""
    from app.chat.llm import stop_kwargs

    try:
        reply = llm.generate(_ROUTER_PROMPT.format(question=question),
                             temperature=0.0, max_tokens=6, timeout=20.0,
                             stop=["\n"], **stop_kwargs(llm, should_stop))
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
    should_stop: Optional[Callable[[], bool]] = None,
) -> Route:
    """Classify `question` in the context of `history`. **Never raises.**

    `llm` is the router model, or `None`. It is asked only when no rule fires -
    the minority case - and its answer is validated before it is believed.
    `should_stop` (2026-10-04, code review) is the question's Stop, handed to it.
    """
    text = str(question or "").strip()
    social = chat_reason(text, history)
    if social is not None:
        return Route(CHAT, "rules", social, text)
    lead = _GREETING_LEAD.match(text)
    if lead is not None and lead.end() < len(text):
        # "hi, what did we agree about the deposit?" is the question after the hello.
        return route_question(text[lead.end():], history, llm=llm, should_stop=should_stop)
    reason = _followup_reason(text, history)
    if reason is not None:
        resolved = resolve_followup(text, history, llm=llm, should_stop=should_stop)
        inner = _rule_class(resolved)
        # A follow-up that resolves to nothing recognisable is still a lookup:
        # the person is asking about what was just discussed.
        return Route(FOLLOWUP, "rules", reason, resolved,
                     resolved_kind=inner[0] if inner else LOOKUP)

    found = _rule_class(text)
    if found is not None:
        return Route(found[0], "rules", found[1], text)

    if llm is not None:
        answer = _ask_model(llm, text, should_stop)
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


def resolve_followup(question: str, history: Sequence[Any], *, llm: Any = None,
                     should_stop: Optional[Callable[[], bool]] = None) -> str:
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
        from app.chat.llm import stop_kwargs

        try:
            reply = llm.generate(
                "Rewrite the last question so it makes sense on its own, using the earlier "
                "question for context. One line, no explanation.\n\n"
                f"Earlier question: {previous}\nLast question: {text}\nRewritten:",
                temperature=0.0, max_tokens=48, timeout=20.0, stop=["\n"],
                **stop_kwargs(llm, should_stop))
            rewritten = str(getattr(reply, "text", "") or "").strip().strip('"')
            shared = set(content_tokens(rewritten)) & (set(content_tokens(previous))
                                                       | set(content_tokens(text)))
            if 3 <= len(rewritten) <= 200 and shared:
                return rewritten
        except Exception:                                # noqa: BLE001 - assist only
            pass
    return standalone
