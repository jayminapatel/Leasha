"""A short card for every document: what it is, who is in it, its dates and sums.

Layer: L6 - pure. No store, no model, no Qt: the drain in `Pipeline` hands this
the document's text and a model's reply, and this decides what may be kept.

Work order `smarter-answers` section 2 (owner, 2026-10-12). Embeddings find a
passage that *sounds like* a question; they do not know that a PDF is an
invoice, that the letter is from the landlord or that it is dated March 2024.
A small model reads the start of each document once, at indexing time, and the
card it writes is stored as one more passage labelled `AI document card` - so
"the plumber's invoice from last spring" can match a document whose own words
never say "invoice" or "plumber".

**The model is not trusted, it is read.** Its reply is JSON; anything outside
the fixed shape is dropped, and every person, organisation, amount and year
must *appear in the document's own text* or it is dropped too. A small model
asked for names will invent a plausible one; one that is checked against the
text it was given cannot. The summary is the one field that cannot be checked
this way, so the card says it is AI-written in the words a person reads, and a
card is never the receipt for an answer (Chat cites the passage, not the card).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Optional

__all__ = [
    "Card", "KINDS", "CARD_LABEL", "PROMPT_VERSION", "MAX_CHARS", "MIN_CHARS",
    "DOC_EXTS", "MAIL_EXTS", "build_prompt", "read_card", "card_text",
]

#: The chunk label, and the first words of the stored passage
#: (`add_caption_chunk` writes `"{label}: {text}"`). "AI" in it because the
#: standing rule is that AI-written text says so where a person reads it.
CARD_LABEL = "AI document card"

#: Raised when the prompt or the reading changes, so cards written the old way
#: are made again (`doc_cards.prompt_version`). Not a setting: nobody chooses it.
PROMPT_VERSION = 1

#: How much of the document the model reads. Not a setting (non-negotiable 11):
#: the first ~3,000 characters hold the title, the parties, the date and the
#: subject of nearly every letter, invoice and report, and the 1.5B model's
#: time per card grows with every character it reads. A measurement of cards
#: missing a fact that sat later in the document would change it.
MAX_CHARS = 3000

#: Below this a document has nothing a card would add to its own words
#: ("Thanks, see you then"). Not a setting, for the same reason.
MIN_CHARS = 200

#: What a card is written for: documents and mail. Code, drawings, data files,
#: pictures and archives are left out - a card says what a *document* is.
DOC_EXTS = frozenset({
    "pdf", "doc", "docx", "odt", "rtf", "txt", "md", "pptx", "ppt", "odp",
    "epub", "htm", "html", "pages", "wpd",
})
MAIL_EXTS = frozenset({"pst", "eml", "msg", "mbox", "olm"})

#: The kinds a card may name. A fixed list so a filter can rely on it; a model
#: that answers anything else gets "other".
KINDS = (
    "invoice", "receipt", "quote", "order", "statement", "contract", "agreement",
    "letter", "email", "newsletter", "report", "minutes", "proposal", "specification",
    "manual", "form", "certificate", "cv", "presentation", "policy", "homework",
    "essay", "article", "book", "notes", "plan", "ticket", "booking", "payslip",
    "tax", "medical", "legal", "other",
)

_MAX_ITEMS = 8
_MAX_SUMMARY = 320
_CURRENCIES = {"\u00a3": "GBP", "$": "USD", "\u20ac": "EUR", "\u20b9": "INR", "\u00a5": "JPY"}
_CODES = {"GBP", "USD", "EUR", "INR", "JPY", "CHF", "AUD", "CAD", "NZD", "AED", "ZAR",
          "SEK", "NOK", "DKK", "PLN", "SGD", "HKD", "CNY"}


@dataclass(frozen=True)
class Card:
    """What may be kept of one model reply."""

    kind: str
    people: tuple[str, ...] = ()
    organisations: tuple[str, ...] = ()
    dates: tuple[str, ...] = ()
    amounts: tuple[str, ...] = ()
    summary: str = ""
    dropped: tuple[str, ...] = field(default=(), compare=False)

    def as_row(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "people": json.dumps(list(self.people)),
            "organisations": json.dumps(list(self.organisations)),
            "dates": json.dumps(list(self.dates)),
            "amounts": json.dumps(list(self.amounts)),
            "summary": self.summary,
        }


def build_prompt(name: str, text: str) -> str:
    """The words said to the model. One document, JSON back."""
    body = (text or "")[:MAX_CHARS]
    kinds = ", ".join(KINDS)
    return (
        "Read this document and describe it as JSON with exactly these keys:\n"
        f'"kind": one of [{kinds}];\n'
        '"people": names of people in it, as written (at most 8);\n'
        '"organisations": companies, schools, councils or other bodies in it, as written (at most 8);\n'
        '"dates": the dates it is about, as YYYY-MM-DD (at most 8);\n'
        '"amounts": sums of money in it, as CURRENCY NUMBER, for example "GBP 450.00" (at most 8);\n'
        '"summary": what it is and what it is about, in at most two short sentences.\n'
        "Use only what the document says. Leave a list empty rather than guess.\n\n"
        f"File name: {name}\n"
        f"Document:\n{body}\n"
    )


def read_card(reply: str, source: str, *, today: Optional[date] = None) -> Optional[Card]:
    """The card a reply supports, or None when nothing usable came back.

    `source` is the text the model was shown; every name, sum and year is
    checked against it. What was dropped is kept in `Card.dropped` for the log.
    """
    data = _first_json(reply)
    if not isinstance(data, dict):
        return None
    folded = _fold(source)
    digits = re.sub(r"[^0-9]", "", source or "")
    dropped: list[str] = []

    kind = str(data.get("kind") or "").strip().lower()
    if kind not in KINDS:
        if kind:
            dropped.append(f"kind:{kind}")
        kind = "other"

    people = _names(data.get("people"), folded, dropped, "person")
    orgs = _names(data.get("organisations") or data.get("organizations"), folded, dropped, "org")
    dates = _dates(data.get("dates"), source or "", today or date.today(), dropped)
    amounts = _amounts(data.get("amounts"), digits, dropped)
    summary = _summary(data.get("summary"))
    if not (summary or people or orgs or dates or amounts) and kind == "other":
        return None
    return Card(kind, people, orgs, dates, amounts, summary, tuple(dropped))


def card_text(card: Card) -> str:
    """The passage stored for search. Plain words, so keyword and meaning search
    both find it; the label in front (`CARD_LABEL`) says who wrote it."""
    parts = [card.kind.capitalize() + "."]
    if card.people:
        parts.append("People: " + ", ".join(card.people) + ".")
    if card.organisations:
        parts.append("Organisations: " + ", ".join(card.organisations) + ".")
    if card.dates:
        parts.append("Dates: " + ", ".join(card.dates) + ".")
    if card.amounts:
        parts.append("Amounts: " + ", ".join(card.amounts) + ".")
    if card.summary:
        parts.append("Summary: " + card.summary)
    return " ".join(parts)


# -- reading the reply ------------------------------------------------------------


def _first_json(reply: str) -> Any:
    text = (reply or "").strip()
    if not text:
        return None
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:index + 1])
                except ValueError:
                    return None
    return None


def _fold(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").casefold())


def _items(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value:
        if isinstance(item, (str, int, float)):
            cleaned = re.sub(r"\s+", " ", str(item)).strip(" ,;.")
            if cleaned:
                out.append(cleaned)
    return out


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out = []
    for value in values:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            out.append(value)
        if len(out) == _MAX_ITEMS:
            break
    return tuple(out)


def _names(value: Any, folded: str, dropped: list[str], what: str) -> tuple[str, ...]:
    kept = []
    for name in _items(value):
        if len(name) < 2 or len(name) > 80:
            dropped.append(f"{what}:{name}")
        elif _fold(name) in folded:
            kept.append(name)
        else:
            dropped.append(f"{what}:{name}")       # not in the text: invented
    return _unique(kept)


_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _dates(value: Any, source: str, today: date, dropped: list[str]) -> tuple[str, ...]:
    kept = []
    for item in _items(value):
        match = _ISO.match(item)
        if not match:
            dropped.append(f"date:{item}")
            continue
        try:
            when = date(int(match[1]), int(match[2]), int(match[3]))
        except ValueError:
            dropped.append(f"date:{item}")
            continue
        # The year has to be in the document: a model filling in "today" for an
        # undated letter is the commonest invention, and this catches it.
        if when.year < 1900 or when.year > today.year + 5 or match[1] not in source:
            dropped.append(f"date:{item}")
            continue
        kept.append(when.isoformat())
    return _unique(sorted(set(kept)))


_AMOUNT = re.compile(r"^([A-Z]{3})\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)$")


def _amounts(value: Any, digits: str, dropped: list[str]) -> tuple[str, ...]:
    kept = []
    for item in _items(value):
        text = item.strip()
        for symbol, code in _CURRENCIES.items():
            if text.startswith(symbol):
                text = code + " " + text[len(symbol):].strip()
        match = _AMOUNT.match(text.upper())
        if not match or match[1] not in _CODES:
            dropped.append(f"amount:{item}")
            continue
        number = match[2].replace(",", "")
        whole = number.split(".")[0]
        # The figure's digits must be in the document's own digits.
        if not whole or whole not in digits:
            dropped.append(f"amount:{item}")
            continue
        kept.append(f"{match[1]} {number}")
    return _unique(kept)


def _summary(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    text = " ".join(sentences[:2])
    if len(text) > _MAX_SUMMARY:
        text = text[:_MAX_SUMMARY].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    return text
