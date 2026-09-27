r"""Turning a sentence into filters without a model.

Layer: L4 — pure and deterministic. Takes a sentence and, optionally, a store
to ask what names and file types the index actually holds. No network, no
Ollama, no Qt.

**Interpret currently needs a 4GB model to notice the word "pdf".** That is
the case for this file. `translate.py` is good at the hard half — genuinely
ambiguous sentences — and is unavailable on most machines, slow on the rest,
and non-deterministic everywhere. Most of what people type is not the hard
half: *"the invoice Dave sent me last year"* contains a name the index knows,
a word meaning "mail", a kind of document and a date, and every one of those
can be found by looking rather than by guessing.

**Chips, not rewrites** (§3b). Nothing here alters what somebody typed. It
returns a list of filters it recognised, each with the words it came from, and
the surface offers them as one-click chips. That is what makes a wrong guess
cost a glance instead of a search: the typed text is still there, unchanged,
and the chip is visibly separate from it.

**High precision by construction.** A name becomes `from:` only if that sender
is in the index; a noun becomes `type:` only if the index holds that
extension. The rules never invent a filter for a value the corpus cannot
satisfy, so the failure mode is a chip that does not appear, never a chip that
silently empties the results.

**[TUNE]** — every table in this file is marked. The owner deferred the
quality pass to after the indexing work (§3d): these tables land with obvious
contents and their tests, and tuning recall against real sentences is a later,
separate effort.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional, Sequence

__all__ = [
    "Chip", "Reading", "read", "MAIL_VERBS", "KIND_WORDS", "SENT_BY_ME",
    "Applied", "AppliedFilter", "apply", "AUTO_MAIL_WORDS",
]

#: Words that mean "this is about mail" [TUNE].
#:
#: **Verbs, because a noun is ambiguous and a verb is not.** "message" could be
#: the subject of a document; "emailed" could not.
MAIL_VERBS = frozenset({
    "email", "emailed", "emails", "mail", "mailed", "sent", "send", "sends",
    "received", "receive", "wrote", "replied", "reply", "forwarded", "cc",
    "attached", "attachment", "attachments", "inbox", "message", "messages",
})

#: Verbs that mean *the person searching* was the sender [TUNE].
#:
#: "what did I send to Priya" is a `to:` question; "the report Dave sent me"
#: is a `from:` one. Same verb, opposite filter, and the difference is which
#: pronoun sits beside it - which is why this needs the pronoun, not just the
#: verb.
SENT_BY_ME = frozenset({"i", "me", "my", "we", "us", "our"})

#: Everyday words for a kind of file, and the extensions they mean [TUNE].
#:
#: **Resolved against the index before use**: a word maps to whichever of its
#: extensions the corpus actually contains, so "spreadsheet" on a corpus of
#: `.csv` and no `.xlsx` produces a filter that finds something.
KIND_WORDS: dict = {
    # **The mail words name the `type:mail` group, not one extension.** They
    # used to offer `type:eml` - the first of `("eml", "msg")` - which is not
    # what typing `type:mail` means (msg, eml *and* pst) and missed every
    # message read out of an Outlook archive. `mail` itself was absent. A group
    # name is resolved against the index like an extension: it is usable when
    # the corpus holds any of its members (`_usable`).
    "mail": ("mail",),
    "mails": ("mail",),
    "email": ("mail",),
    "emails": ("mail",),
    "message": ("mail",),
    "messages": ("mail",),
    "pdf": ("pdf",),
    "pdfs": ("pdf",),
    "document": ("docx", "doc", "odt"),
    "documents": ("docx", "doc", "odt"),
    "letter": ("docx", "doc", "odt"),
    "letters": ("docx", "doc", "odt"),
    "report": ("pdf", "docx", "doc"),
    "reports": ("pdf", "docx", "doc"),
    "spreadsheet": ("xlsx", "xls", "csv", "ods"),
    "spreadsheets": ("xlsx", "xls", "csv", "ods"),
    "workbook": ("xlsx", "xls"),
    "presentation": ("pptx", "ppt", "odp"),
    "deck": ("pptx", "ppt"),
    "slides": ("pptx", "ppt"),
    "photo": ("jpg", "jpeg", "png", "heic"),
    "photos": ("jpg", "jpeg", "png", "heic"),
    "picture": ("jpg", "jpeg", "png", "heic"),
    "pictures": ("jpg", "jpeg", "png", "heic"),
    "image": ("jpg", "jpeg", "png", "heic", "gif", "webp"),
    "images": ("jpg", "jpeg", "png", "heic", "gif", "webp"),
    # Work order 202626270515. Resolved against the index like every word here:
    # only the extensions the corpus actually holds become a filter.
    "video": ("mp4", "mov", "mkv", "avi", "m4v", "wmv", "webm"),
    "videos": ("mp4", "mov", "mkv", "avi", "m4v", "wmv", "webm"),
    "film": ("mp4", "mov", "mkv", "avi", "m4v", "wmv", "webm"),
    "films": ("mp4", "mov", "mkv", "avi", "m4v", "wmv", "webm"),
    "recording": ("mp3", "m4a", "wav", "flac", "ogg", "opus"),
    "recordings": ("mp3", "m4a", "wav", "flac", "ogg", "opus"),
    "audio": ("mp3", "m4a", "wav", "flac", "ogg", "opus"),
    "drawing": ("dwg", "dxf", "pdf"),
    "drawings": ("dwg", "dxf", "pdf"),
    "invoice": ("pdf", "docx", "xlsx"),
    "invoices": ("pdf", "docx", "xlsx"),
    "notes": ("txt", "md", "docx"),
}

#: Month names, for "in June" [TUNE].
_MONTHS = {name: number for number, name in enumerate(
    ("january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"), start=1)}

#: Ways people say a message had something attached [TUNE].
#:
#: **A noun phrase, not the bare word.** "The attached report" is somebody
#: naming a document, and "I attached the wrong file" is about mail - but so
#: is "attachment". The negative forms are excluded here rather than mapped to
#: `has:no-attachment`, because "without the attachment" is far more often a
#: complaint inside a message than a filter somebody wants.
_ATTACHED = re.compile(r"""(?ix)
    \b(?:
        with \s+ (?:an? \s+ | something \s+ | the \s+ )? attach(?:ed|ment)
      | attachments?\b
      | has \s+ an? \s+ attachment
      | that \s+ (?:had|has) \s+ (?:an? \s+ )? attach(?:ed|ment)
    )""")

_YEAR = re.compile(r"\b(19|20)\d{2}\b")
_WORD = re.compile(r"[A-Za-z0-9'’]+")

#: Capitalised words that are not names [TUNE]. Sentences begin with a capital
#: and English is full of proper nouns that are not people, so a name is only
#: taken seriously when the index confirms it - this list just avoids asking.
_NOT_NAMES = frozenset({
    "i", "the", "a", "an", "what", "who", "when", "where", "which", "did",
    "do", "does", "is", "was", "are", "were", "can", "could", "please",
    "find", "show", "search", "look", "get", "my", "me", "we", "our",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday",
})


@dataclass(frozen=True)
class Chip:
    """One filter the rules recognised, and the words it came from.

    `source` is kept so the chip can point at the words it was read from - a
    chip somebody cannot trace back to their own sentence is a chip they
    cannot judge.
    """

    field: str
    value: str
    source: str = ""

    def as_filter(self) -> str:
        """The operator, exactly as the parser would accept it."""
        value = self.value
        if " " in value:
            value = f'"{value}"'
        return f"{self.field}:{value}"

    def label(self) -> str:
        """What the chip says, in the words of the person reading it."""
        if self.field == "has":
            return "with an attachment"
        wording = {
            "from": "from {}", "to": "to {}", "type": "{} files",
            "after": "after {}", "before": "before {}",
        }.get(self.field, self.field + " {}")
        return wording.format(self.value)


@dataclass(frozen=True)
class Reading:
    """What the rules made of a sentence. **The sentence itself is untouched.**"""

    sentence: str
    chips: tuple = ()
    #: Words no rule claimed. **What a model would be given** (§3c): rules run
    #: first and Ollama receives only this, so the prompt is smaller and the
    #: deterministic part stays deterministic.
    residue: str = ""
    #: True when the sentence looks like it is about mail.
    mail: bool = False

    @property
    def found(self) -> bool:
        return bool(self.chips) or self.mail

    def query(self) -> str:
        """Sentence plus filters — for a caller that wants one string.

        **Not what the surface uses.** §3b puts these on screen as chips
        beside the typed text; this exists for the CLI and for tests, where
        there is nowhere to click.
        """
        parts = [chip.as_filter() for chip in self.chips]
        if self.mail and not any(c.field == "type" for c in self.chips):
            parts.append("type:mail")
        return " ".join([self.residue or self.sentence, *parts]).strip()


def _words(sentence: str) -> list:
    return _WORD.findall(str(sentence or ""))


def _known(store: Any, kind: str, limit: int = 400) -> tuple:
    r"""Values of one kind that the index actually holds. **Never raises.**

    `kind` is the store's own vocabulary - `sender`, `recipient`, `ext` - not
    the query operator it feeds. **These were `from`, `to` and `type` in the
    first version of this file**, which are the operator names, and
    `distinct_values` returns `[]` for a kind it does not know. Both rules
    were therefore silently inert: no person chip ever fired, and the "only
    offer a type the corpus holds" safeguard passed everything because the set
    it checked against was empty. Nothing raised, and the output looked
    plausible.

    **`recipient` comes back as raw JSON** - `'["me@acme.com"]'` - because
    that is how the column is stored. Unpacked here rather than left for each
    caller to trip over.
    """
    getter = getattr(store, "distinct_values", None)
    if getter is None:
        return ()
    try:
        rows = getter(kind, limit=limit) or ()
    except Exception:                              # noqa: BLE001 - a helper
        return ()

    found: list = []
    for value in rows:
        text = str(value or "").strip()
        if text.startswith("["):
            try:
                import json

                found.extend(str(one) for one in json.loads(text) if one)
                continue
            except Exception:                      # noqa: BLE001
                pass
        if text:
            found.append(text)
    return tuple(dict.fromkeys(found))


def _usable(option: str, have: set) -> bool:
    """Whether a `KIND_WORDS` option can find anything in this corpus.

    An extension must be held; a group name (`mail`) is usable when any of the
    extensions `type:` expands it to is. An empty `have` means the index could
    not be asked, and then everything is allowed - fewer checks, not fewer
    chips (see `test_without_a_store_the_rules_still_read_what_they_can`).
    """
    if not have:
        return True
    from app.search.query import _EXT_GROUPS

    return any(ext in have for ext in _EXT_GROUPS.get(option, (option,)))


def _match_person(word: str, known: Sequence[str]) -> Optional[str]:
    r"""The indexed address whose name contains this word, if exactly one does.

    **Exactly one, or nothing.** "Chris" matching both `chris.yates@acme.com`
    and `chris.doyle@acme.com` is not a filter, it is a coin toss - and a chip
    that quietly picks one of two people is worse than no chip, because the
    results look complete.
    """
    needle = word.lower()
    if len(needle) < 3:
        return None
    hits = [value for value in known if needle in value.lower()]
    if len(hits) != 1:
        return None
    return hits[0]


#: Verbs where the sender and the recipient swap depending on who is beside
#: them [TUNE].
_SENDING = frozenset({"send", "sent", "sends", "email", "emailed", "mail",
                      "mailed", "wrote", "forwarded"})


def _i_am_the_sender(lowered: Sequence[str]) -> bool:
    r"""Whether the person searching is the one who sent it.

    **"What did I send to Priya" and "the report Dave sent me" use the same
    verb and want opposite filters**, so the verb cannot decide this. Word
    order can: a first-person pronoun *before* the sending verb means I sent
    it, so the name in the sentence is a recipient. After it - "Dave sent me"
    - the pronoun is the recipient and the name is the sender.
    """
    for position, word in enumerate(lowered):
        if word not in _SENDING:
            continue
        before = lowered[max(0, position - 2):position]
        return any(pronoun in SENT_BY_ME for pronoun in before)
    return False


def _dates(sentence: str, *, today: date) -> list:
    """`after:`/`before:` chips from the date phrases people actually type.

    Only the unambiguous ones [TUNE]: a bare year, a month name, and "last
    year". *"Six months ago"* and *"last summer"* are deliberately absent -
    they mean different spans to different people, and a wrong date filter
    hides documents silently, which is the failure this whole order exists to
    remove.
    """
    text = str(sentence or "").lower()
    found: list = []

    if "last year" in text:
        year = today.year - 1
        found.append(Chip("after", f"{year}-01-01", "last year"))
        found.append(Chip("before", f"{year}-12-31", "last year"))
        return found
    if "this year" in text:
        found.append(Chip("after", f"{today.year}-01-01", "this year"))
        return found

    years = [int(match.group(0)) for match in _YEAR.finditer(text)]
    for word in _words(text):
        month = _MONTHS.get(word)
        if month is None:
            continue
        year = years[0] if years else today.year
        last_day = 31 if month in (1, 3, 5, 7, 8, 10, 12) else (
            30 if month != 2 else (29 if year % 4 == 0 and
                                   (year % 100 != 0 or year % 400 == 0)
                                   else 28))
        found.append(Chip("after", f"{year}-{month:02d}-01", word))
        found.append(Chip("before", f"{year}-{month:02d}-{last_day}", word))
        return found

    if years:
        year = years[0]
        # **"before 2023" means before it, not during it.** The bare year on
        # its own means the year itself; only the word changes that.
        if re.search(r"\bbefore\s+" + str(year), text):
            return [Chip("before", f"{year}-01-01", f"before {year}")]
        if re.search(r"\bafter\s+" + str(year), text):
            return [Chip("after", f"{year}-12-31", f"after {year}")]
        found.append(Chip("after", f"{year}-01-01", str(year)))
        found.append(Chip("before", f"{year}-12-31", str(year)))
    return found


def read(sentence: str, store: Any = None, *,
         today: Optional[date] = None) -> Reading:
    r"""What the rules can tell about a sentence. **Never raises.**

    Deterministic and offline: the same sentence and the same index always
    give the same answer, which is the half of Interpret that can be tested,
    cached and explained. `store` is optional - without it, only the rules
    that need no corpus fire (dates, mail verbs), which is still more than
    nothing on a machine with no index yet.
    """
    text = str(sentence or "").strip()
    if not text:
        return Reading(sentence=text)

    words = _words(text)
    lowered = [word.lower() for word in words]
    chips: list = []
    claimed: set = set()

    mail = any(word in MAIL_VERBS for word in lowered)

    # -- people ----------------------------------------------------------
    senders = _known(store, "sender")
    recipients = _known(store, "recipient") or senders
    field = "to" if _i_am_the_sender(lowered) else "from"
    pool = recipients if field == "to" else senders
    # **One person, not two.** Two names in a sentence is a conversation -
    # "what Dave said to Priya" - and two `from:` filters AND into nothing,
    # which is a chip that empties the page. The first the index recognises is
    # the filter; the other stays as text and still matches.
    for position, word in enumerate(words):
        if lowered[position] in _NOT_NAMES or not word[:1].isupper():
            continue
        match = _match_person(word, pool)
        if match is None:
            continue
        chips.append(Chip(field, match, word))
        claimed.add(lowered[position])
        break

    # -- kinds of file ---------------------------------------------------
    have = {value.lower() for value in _known(store, "ext")}
    for position, word in enumerate(lowered):
        options = KIND_WORDS.get(word)
        if not options:
            continue
        usable = [ext for ext in options if _usable(ext, have)]
        if not usable:
            continue
        chips.append(Chip("type", usable[0], words[position]))
        claimed.add(word)
        break

    # -- attachments -----------------------------------------------------
    #
    # **Measured at 0% before this existed.** `evaluate --builtin` scores its
    # attachment question - *"emails with something attached about the
    # licence"* - at zero, because the filter works perfectly and no plain
    # sentence has ever produced it. `has:attachment` was reachable only by
    # typing the operator, which is precisely the knowledge tab one exists to
    # not require.
    if _ATTACHED.search(text):
        chips.append(Chip("has", "attachment", "attached"))
        claimed.update({"attached", "attachment", "attachments"})

    # -- dates -----------------------------------------------------------
    for chip in _dates(text, today=today or date.today()):
        chips.append(chip)
        claimed.update(_words(chip.source))

    residue = " ".join(word for word, low in zip(words, lowered)
                       if low not in claimed and low not in MAIL_VERBS)
    return Reading(sentence=text, chips=tuple(chips),
                   residue=residue.strip(), mail=mail)


# ---------------------------------------------------------------------------
# Applied, not only offered - "mail from 2017"
# ---------------------------------------------------------------------------
#
# **Owner decision, 2026-09-27: recognised filters are applied, not merely
# offered.** "mail from 2017" typed into the Search tab ran as the plain words
# `"mail" OR "2017"` and found nothing useful, while the right filters sat on
# the notice bar waiting for a click nobody made. This reverses §3b's "chips,
# not rewrites" for the few readings confident enough to act on; the work
# order's own text is left as released, and the reversal is recorded beside
# it. `read()` above is unchanged and still feeds the offers and Interpret -
# applying is a separate, narrower function.
#
# **What the person typed is still never altered.** The box keeps their words.
# What changes is the query that runs: the words a filter consumed leave the
# search terms, the filter joins it, and the window shows each one as a chip
# whose removal puts those words back as ordinary search terms (`declined`).

#: Words that mean "mail" confidently enough to apply `type:mail` [TUNE].
#:
#: **Deliberately narrower than `KIND_WORDS`.** "message" alone is left out:
#: "the error message" is a document search at least as often as a mail one,
#: so it stays an offer. The plural is kept - "messages from Dave" is not
#: about an error.
AUTO_MAIL_WORDS = frozenset({"mail", "mails", "email", "emails", "e-mail",
                             "e-mails", "messages"})

#: Words that, directly before a year, make it a date [TUNE]. "in 2017" and
#: "from 2017" name a period; "2017" alone might be a number in a file name.
_YEAR_PREPOSITIONS = frozenset({"in", "from", "during"})

#: Words that, directly before a known name, make it a person filter [TUNE].
_PERSON_PREPOSITIONS = {"from": "from", "by": "from", "to": "to"}

#: A bare four-digit year and nothing else in the token.
_YEAR_TOKEN = re.compile(r"^(?:19|20)\d{2}$")
#: Punctuation a word may carry at its edges in a sentence.
_EDGES = ".,;:!?()[]{}'\"’"


@dataclass(frozen=True)
class AppliedFilter:
    """One filter the rules applied to a query, and the words it replaced.

    `key` is what a person declines by removing the chip: the kind of filter
    and the words it was read from, so declining "2017" does not also decline
    "2018" typed a moment later - new words, a new reading.
    """

    kind: str                      # "type" | "date" | "person"
    operators: tuple = ()          # exactly as the parser accepts them
    words: str = ""                # the words consumed, as typed
    label: str = ""                # what the chip says

    @property
    def key(self) -> tuple:
        return (self.kind, self.words.lower())


@dataclass(frozen=True)
class Applied:
    """A sentence with its confident filters applied. `query` is what to run."""

    sentence: str
    query: str
    filters: tuple = ()

    @property
    def changed(self) -> bool:
        return bool(self.filters)


def _holds_mail(store: Any) -> bool:
    """Whether the index holds any mail - `type:mail`'s extensions. **Never
    raises.** A store that cannot be asked allows it, as `_usable` does.

    `SqliteStore.holds_ext` is a one-row seek; anything without it (a test's
    fake store) is asked through `distinct_values`, as `read()` asks.
    """
    from app.search.query import _EXT_GROUPS

    probe = getattr(store, "holds_ext", None)
    if callable(probe):
        try:
            return bool(probe(_EXT_GROUPS["mail"]))
        except Exception:                          # noqa: BLE001 - a helper
            return True
    return _usable("mail", {value.lower() for value in _known(store, "ext")})


def _tokens(text: str) -> list:
    """`(start, end, word)` for each whitespace token a filter may consume.

    **Anything the person wrote as syntax is out of reach**: a token inside
    quotes, a `field:value` operator, a `-exclusion`, a `/command` and the
    capitalised boolean words keep their exact meaning. `word` is the token
    with sentence punctuation trimmed; it is `""` for a protected token.
    """
    quoted = [(m.start(), m.end()) for m in re.finditer(r'"[^"]*"?', text)]
    found = []
    for match in re.finditer(r"\S+", text):
        token = match.group(0)
        inside = any(a <= match.start() < b for a, b in quoted)
        protected = (inside or ":" in token or token[:1] in "-!/\""
                     or token in ("OR", "AND", "NOT"))
        found.append((match.start(), match.end(), "" if protected else token.strip(_EDGES)))
    return found


def apply(sentence: str, store: Any = None, *, today: Optional[date] = None,
          declined: Sequence = ()) -> Applied:
    r"""The sentence as a query, with the filters the rules are sure of applied.

    **Never raises**, and never returns less than the sentence: anything not
    confidently recognised stays a search term exactly as typed. No model, no
    network - the first non-negotiable holds, because this is a regex, a
    dictionary and (with a store) three indexed `DISTINCT`s.

    What is applied, and why each is safe to act on:

    * **mail** - `mail`, `email(s)`, `messages` (`AUTO_MAIL_WORDS`) become
      `type:mail`, the same msg/eml/pst group typing `type:mail` gives. Only
      when the corpus holds mail, or cannot be asked.
    * **a year** - `in 2017`, `from 2017`, `during 2017` always; `before 2017`
      and `after 2017` as that edge; a **bare** `2017` only in a sentence that
      is about mail. That is the conservative line: *"invoice 2017"* keeps
      2017 as a word, because a document's only date is often its copy date
      (`mtime_ns`) while its name says 2017 - a year filter there would hide
      the very file asked for, which is the failure this project exists to
      remove. It is still *offered* on the notice bar. A message's sent date
      is a fact (schema v27), so in a mail sentence the year is safe. Two
      years in one sentence ("2016 to 2017") is a range these rules do not
      read, so neither is applied.
    * **a person** - a capitalised word matching exactly one sender (or
      recipient) the index holds, found the way `read()` finds it - applied
      only when the sentence is about mail or the name follows `from`/`by`/
      `to`. "Budget Dave" in a document search is left alone: a person filter
      would narrow it to mail.

    Filters the person already typed win: a typed `type:`, `after:`/`before:`
    or `from:`/`to:` switches the matching rule off. `declined` holds the
    `AppliedFilter.key` of every chip the person removed; those words stay
    search terms. If every word left over is a stopword or an instruction
    ("show me the"), the query is filters alone - a browse, newest first,
    which the engine already runs for `type:pdf after:2024` typed by hand.
    """
    from app.search.query import _INSTRUCTION_WORDS, _STOPWORDS, parse_query

    text = str(sentence or "").strip()
    try:
        return _apply(text, store, today or date.today(), set(declined or ()),
                      parse_query, _STOPWORDS | _INSTRUCTION_WORDS)
    except Exception:                              # noqa: BLE001 - a helper
        return Applied(sentence=text, query=text)


def _apply(text: str, store: Any, today: date, declined: set, parse_query: Any,
           filler: frozenset) -> Applied:
    if not text:
        return Applied(sentence=text, query=text)
    typed = parse_query(text, today=today)
    tokens = _tokens(text)
    lowered = [word.lower() for _, _, word in tokens]
    consumed: set = set()
    filters: list = []

    def take(kind: str, operators: tuple, positions: Sequence[int], label: str) -> None:
        words = " ".join(tokens[i][2] for i in sorted(set(positions)))
        chosen = AppliedFilter(kind, operators, words, label)
        if chosen.key in declined:
            return
        filters.append(chosen)
        consumed.update(positions)

    # -- mail ------------------------------------------------------------
    # **Every store lookup below is asked only when its answer could change
    # the query.** This runs before each search, keystrokes included, and the
    # obvious lookups are not cheap: measured on 200,000 files holding 60,000
    # messages, `distinct_values` took 25.5 ms for `ext`, 27.2 ms for `sender`
    # and 19.2 ms for `recipient` - 58 ms for "mail from 2017" when all three
    # were asked up front. Mail is now a one-row seek (`_holds_mail`), and the
    # people are only fetched for a capitalised word that could be applied.
    mail_at = [i for i, word in enumerate(lowered) if word in AUTO_MAIL_WORDS]
    about_mail = bool(mail_at) or any(word in _SENDING for word in lowered)
    if mail_at and not typed.ext and not typed.not_ext and _holds_mail(store):
        take("type", ("type:mail",), mail_at, "mail")

    # -- a person --------------------------------------------------------
    people: dict = {}

    def known(kind: str) -> tuple:
        if kind not in people:
            people[kind] = _known(store, kind)
        return people[kind]

    if not (typed.senders or typed.recipients):
        for i, (_, _, word) in enumerate(tokens):
            if (not word or i in consumed or word.lower() in _NOT_NAMES
                    or not word[:1].isupper()):
                continue
            before = lowered[i - 1] if i else ""
            if not (about_mail or before in _PERSON_PREPOSITIONS):
                continue                           # "Budget Dave": not asked at all
            field = _PERSON_PREPOSITIONS.get(before) or (
                "to" if _i_am_the_sender(lowered) else "from")
            pool = (known("recipient") or known("sender")) if field == "to" else known("sender")
            match = _match_person(word, pool)
            if match is None:
                continue
            positions = [i - 1, i] if before in _PERSON_PREPOSITIONS else [i]
            # The sending verb goes with the person: "Dave sent me".
            positions += [j for j, low in enumerate(lowered)
                          if low in _SENDING and j not in consumed]
            take("person", (Chip(field, match).as_filter(),), positions,
                 f"{field} {match}")
            break                                  # one person, as in `read()`

    # -- a year ----------------------------------------------------------
    years = [i for i, word in enumerate(lowered)
             if _YEAR_TOKEN.match(word) and i not in consumed]
    # A bare year needs the query to *be* mail - a mail or person filter
    # applied, or `type:mail` typed - not merely to mention it: "emails
    # type:pdf 2017" is a document search, where a year is not a safe filter.
    from app.search.query import _EXT_GROUPS

    about_mail = (any(chosen.kind in ("type", "person") for chosen in filters)
                  or (bool(typed.ext) and set(typed.ext) <= set(_EXT_GROUPS["mail"])))
    if len(years) == 1 and typed.after is None and typed.before is None:
        i = years[0]
        year = int(lowered[i])
        before = lowered[i - 1] if i else ""
        if before == "before":
            take("date", (f"before:{year - 1}-12-31",), [i - 1, i], f"before {year}")
        elif before == "after":
            take("date", (f"after:{year + 1}-01-01",), [i - 1, i], f"after {year}")
        elif before in _YEAR_PREPOSITIONS or about_mail:
            positions = [i - 1, i] if before in _YEAR_PREPOSITIONS else [i]
            take("date", (f"after:{year}-01-01", f"before:{year}-12-31"),
                 positions, f"in {year}")

    if not filters:
        return Applied(sentence=text, query=text)

    kept = [(start, end) for n, (start, end, _) in enumerate(tokens) if n not in consumed]
    leftover = [lowered[n] for n in range(len(tokens)) if n not in consumed]
    # Once the query is about mail, "sent" and "received" say nothing more -
    # left as the only term, "emails sent in 2017" would find just the
    # messages containing the word "sent".
    if any(chosen.kind in ("type", "person") for chosen in filters):
        filler = filler | MAIL_VERBS
    if all(word and word in filler for word in leftover):
        kept = []                                  # nothing left worth searching for
    words = " ".join(text[start:end] for start, end in kept)
    operators = [op for chosen in filters for op in chosen.operators]
    return Applied(sentence=text, query=" ".join([words, *operators]).strip(),
                   filters=tuple(filters))
