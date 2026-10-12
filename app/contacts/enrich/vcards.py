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

**Receipt.** The file's indexed path and its modified date; `Proposal.when` is that date. The age
window (D1) is applied to the same date, so a vCard last saved four years ago offers nothing with the
default six months.
"""

from __future__ import annotations

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


def _full_name(card: Card) -> str:
    return " ".join(x for x in (card.get("given"), card.get("additional"), card.get("family")) if x).casefold()


def match_rule(contact: Card, other: Card, region: str) -> Optional[str]:
    """How `other` (a card from a file) is this contact's: "email", "phone", "name", or None."""
    mine = set(emails_of(contact))
    if mine & set(emails_of(other)):
        return "email"
    phones = {field_key(f, region) for f in contact.values("phone") if f.value.strip()}
    if phones & {field_key(f, region) for f in other.values("phone") if f.value.strip()}:
        return "phone"
    name = _full_name(contact)
    if name and " " in name and name == _full_name(other):
        a, b = company_key(contact.get("company")), company_key(other.get("company"))
        if a and a == b:
            return "name"
    return None


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
    for f in localfiles.indexed_files(hub.store, EXTS, since=since):
        cards = localfiles.cached("vcard", f, _cards) or []
        for other in cards:
            rule = match_rule(card, other, region)
            if rule is None or (rule != "email" and _someone_else(hub, cid, other)):
                continue
            conf = _CONFIDENCE[rule]
            receipt = f.receipt
            for name in _SCALARS:
                new = other.get(name).strip()
                if new and not card.get(name).strip() and f"s:{name}" not in offered:
                    offered.add(f"s:{name}")
                    out.append(Proposal(cid, name, "", "", new, "vcard", receipt, conf, when=f.mtime))
            for field in other.fields:
                if field.kind not in _KINDS or not (field.value.strip() or any(field.data.values())):
                    continue
                key = field_key(field, region)
                if key in have or key in offered:
                    continue
                offered.add(key)
                out.append(Proposal(cid, field.kind, field.label, "", field.value, "vcard", receipt, conf,
                                    data=dict(field.data), when=f.mtime))
    return out
