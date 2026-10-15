"""5a: details from the vCard files in the index - what a matching contact lacks.

Layer: L7

Order 1s section 5a. Local only: the `.vcf` files the index holds are read again from disk, read-only
(`localfiles`), and parsed by the same reader the import uses (`importers.import_vcard`), so a card
reads here exactly as it would if the person imported it.

**Which card is this contact's.** In order, the first rule that finds a card wins for that card:

1. an email address on the card is one of the contact's (confidence 0.85);
2. a phone number on the card is one of the contact's, compared as numbers, not text (0.75);
3. the card's full name is exactly the contact's *and* both name the same company (0.7) -
   a name alone is not enough: two John Smiths are common, two at one company are not.

A card matched by phone or name that carries an email address belonging to a *different* contact is
someone else's card (a shared office line, a namesake) and is not used. Confidence stays at or below
0.85 for an email match and 0.75 for the rest, by this order's rule for local files: a vCard can be
years old, and its modified date is the only age it has.

**What is offered.** Only what the contact lacks: a blank name part, company, department or title,
and any email, phone, address, website, date, social profile or IM the card has and the contact does
not. Notes and free custom fields are not offered (they carry whatever the exporting program put
there); neither is the photo, which order 1k's photo sources own.

**Checked against the owner's index (2026-10-13).** Most of what the vCards offer comes from a few
phone backups mailed to the owner (one card for every contact they had), so each value is cleaned the
way a person reading it would: a name part whose words are already in the contact's name is not offered
("Jacqui" for "Mum Jacqui Weller"); an address already offered, or already on the card, written another
way (commas for line breaks) is offered once; a value with a character the export lost (U+FFFD) is not
offered, and a Windows dash or quote decoded as a control character is put back. A value in the wrong
field (an address in a phone field, a name in an email field) is not offered; of an address and a shorter
one inside it ("Burgess Hill, England" and the same with "United Kingdom") only the fuller is; and a card
found by phone number that names somebody else (an office line shared by two people) is not this
contact's. A backup holds
thousands of cards, so each file's cards are looked up by address, number and name (`_Book`), not
compared one by one for every contact (1.44 s a contact before, on the owner's index).

**Receipt.** The file's indexed path and its modified date; `Proposal.when` is that date. The age
window (D1) is applied to the same date, so a vCard last saved four years ago offers nothing with the
default six months.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Optional

from app.contacts import importers
from app.contacts.card import Card, field_key
from app.contacts.enrich import localfiles
from app.contacts.enrich.base import Proposal, emails_of
from app.contacts.enrich.company import company_key

__all__ = ["vcard_proposals", "EXTS", "match_rule"]

EXTS = ("vcf", "vcard")

#: Name parts and the work scalars, offered only when blank on the contact.
_SCALARS = ("prefix", "given", "additional", "family", "suffix", "nickname", "company", "department", "title")
#: Repeatable kinds offered when the contact does not already hold the same value.
_KINDS = ("email", "phone", "address", "url", "date", "social", "im")

_CONFIDENCE = {"email": 0.85, "phone": 0.75, "name": 0.7}


def _cards(data: bytes) -> list[Card]:
    return importers.import_vcard(data).cards


class _Book:
    """One file's cards, looked up by email address, phone number and full name."""

    def __init__(self, cards: list[Card], region: str) -> None:
        self.cards = cards
        self.by: dict[str, list[int]] = defaultdict(list)
        for i, c in enumerate(cards):
            keys = {f"e:{e}" for e in emails_of(c)}
            keys |= {f"p:{field_key(f, region)}" for f in c.values("phone") if f.value.strip()}
            name = _full_name(c)
            if name:
                keys.add(f"n:{name}")
            for k in keys:
                self.by[k].append(i)

    def candidates(self, contact: Card, region: str) -> list[Card]:
        keys = {f"e:{e}" for e in emails_of(contact)}
        keys |= {f"p:{field_key(f, region)}" for f in contact.values("phone") if f.value.strip()}
        name = _full_name(contact)
        if name:
            keys.add(f"n:{name}")
        hits = sorted({i for k in keys for i in self.by.get(k, ())})
        return [self.cards[i] for i in hits]


_C1 = re.compile("[\x80-\x9f]")


def _clean(value: str) -> Optional[str]:
    """The value as a person would read it, or None when the export lost a character (U+FFFD)."""
    if "\ufffd" in value:
        return None
    if _C1.search(value):                                # a Windows-1252 dash or quote read as Latin-1
        value = _C1.sub(lambda m: m.group().encode("latin-1").decode("cp1252", "replace"), value)
    return value


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.casefold()))


def _address_key(text: str) -> str:
    """An address in any layout: its words, in order, without the commas and line breaks between them."""
    return " ".join(re.findall(r"\w+", text.casefold()))


_NAME_SCALARS = ("prefix", "given", "additional", "family", "suffix", "nickname")


def _full_name(card: Card) -> str:
    return " ".join(x for x in (card.get("given"), card.get("additional"), card.get("family")) if x).casefold()


def match_rule(contact: Card, other: Card, region: str) -> Optional[str]:
    """How `other` (a card from a file) is this contact's: "email", "phone", "name", or None."""
    mine = set(emails_of(contact))
    if mine & set(emails_of(other)):
        return "email"
    phones = {field_key(f, region) for f in contact.values("phone") if f.value.strip()}
    if phones & {field_key(f, region) for f in other.values("phone") if f.value.strip()}:
        return None if _names_disagree(contact, other) else "phone"
    name = _full_name(contact)
    if name and " " in name and name == _full_name(other):
        a, b = company_key(contact.get("company")), company_key(other.get("company"))
        if a and a == b:
            return "name"
    return None


def _names_disagree(contact: Card, other: Card) -> bool:
    """Both cards name a person and share no word of it: two people on one number."""
    a = _words(" ".join([contact.get("fn"), *(contact.get(n) for n in _NAME_SCALARS)]))
    b = _words(" ".join([other.get("fn"), *(other.get(n) for n in _NAME_SCALARS)]))
    return bool(a and b and not a & b)


_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _fits(kind: str, value: str) -> bool:
    """Is the value the kind of thing its field says? A backup sometimes holds an address as a phone."""
    if kind == "email":
        return bool(_EMAIL.match(value.strip()))
    if kind == "phone":
        return "@" not in value and sum(ch.isdigit() for ch in value) >= 6
    return True


def _someone_else(hub: Any, cid: int, other: Card) -> bool:
    """Does the card carry an address that is on a different contact? Then it is not this one's."""
    finder = getattr(hub, "find_by_email", None)
    if finder is None:
        return False
    return any(any(x != cid for x in finder(e)) for e in emails_of(other))


def vcard_proposals(hub: Any, cid: int, card: Card, *, region: str = "GB",
                    since: Optional[int] = None) -> list[Proposal]:
    """What the indexed vCards offer this contact. Newest file first; one proposal per value."""
    out: list[Proposal] = []
    offered: set[str] = set()
    have = {field_key(f, region) for f in card.fields}
    have |= {f"addr:{_address_key(f.value)}" for f in card.values("address") if f.value.strip()}
    named = _words(" ".join([card.get("fn"), *(card.get(n) for n in _NAME_SCALARS)]))
    held = [_words(f.value) for f in card.values("address") if f.value.strip()]
    for f in localfiles.indexed_files(hub.store, EXTS, since=since):
        book = localfiles.cached(f"vcard:{region}", f, lambda data: _Book(_cards(data), region))
        for other in book.candidates(card, region) if book else ():
            rule = match_rule(card, other, region)
            if rule is None or (rule != "email" and _someone_else(hub, cid, other)):
                continue
            conf = _CONFIDENCE[rule]
            receipt = f.receipt
            for name in _SCALARS:
                new = _clean(other.get(name).strip())
                if not new or card.get(name).strip() or f"s:{name}" in offered:
                    continue
                if name in _NAME_SCALARS and _words(new) <= named:
                    continue                             # already in the contact's name, in another part
                offered.add(f"s:{name}")
                out.append(Proposal(cid, name, "", "", new, "vcard", receipt, conf, when=f.mtime, opens=f.shown))
            for field in other.fields:
                if field.kind not in _KINDS or not (field.value.strip() or any(field.data.values())):
                    continue
                value = _clean(field.value)
                if value is None or any("\ufffd" in str(v) for v in field.data.values()):
                    continue
                if not _fits(field.kind, value):
                    continue
                keys = {field_key(field, region)}
                if field.kind == "address":
                    words = _words(value)
                    offered_addresses = [_words(str(p.new)) for p in out if p.field_kind == "address"]
                    if any(words <= w for w in held + offered_addresses):
                        continue                         # the same address, or less of it, is already there
                    out = [p for p in out if not (p.field_kind == "address" and _words(str(p.new)) < words)]
                    keys.add(f"addr:{_address_key(value)}")
                if keys & (have | offered):
                    continue
                offered |= keys
                out.append(Proposal(cid, field.kind, field.label, "", value, "vcard", receipt, conf,
                                    data=dict(field.data), when=f.mtime, opens=f.shown))
    return out
