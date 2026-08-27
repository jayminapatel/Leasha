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
    "email": ("eml", "msg"),
    "emails": ("eml", "msg"),
    "message": ("eml", "msg"),
    "messages": ("eml", "msg"),
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
        usable = [ext for ext in options if not have or ext in have]
        if not usable:
            continue
        chips.append(Chip("type", usable[0], words[position]))
        claimed.add(word)
        break

    # -- dates -----------------------------------------------------------
    for chip in _dates(text, today=today or date.today()):
        chips.append(chip)
        claimed.update(_words(chip.source))

    residue = " ".join(word for word, low in zip(words, lowered)
                       if low not in claimed and low not in MAIL_VERBS)
    return Reading(sentence=text, chips=tuple(chips),
                   residue=residue.strip(), mail=mail)
