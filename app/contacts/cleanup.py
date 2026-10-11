"""Clean-up tools and finders for the contact book.

Layer: L7

Order 1k 6o, 6p, 7b. Each tool reads `(id, Card)` pairs and returns plain
`Change` objects - a preview the person accepts all at once or one at a time.
Nothing here writes: the hub applies the accepted changes as one journal batch,
which is also what undoes them in one step.

A `Change` says *which value* (`field`: `"phone"` for a phone number, `"s:given"`
for a scalar, `"s:company"`...), the text before and the text after. A phone
change is found by its `before` text inside the card, so a card holding the same
number twice is changed in both places.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Hashable, Iterable, Optional

from app.contacts import phone as phones
from app.contacts.card import Card, Field, field_key

__all__ = ["Change", "Finding", "FieldCopies", "FREE_MAIL_DOMAINS", "normalise_phones", "fix_name_case",
           "company_guess_from_email", "company_from_domain", "no_name", "no_email_or_phone", "only_a_name",
           "exact_field_copies", "remove_field_copies", "NAME_PARTICLES"]

Id = Hashable


@dataclass(frozen=True)
class Change:
    id: Any
    field: str
    before: str
    after: str
    reason: str = ""


@dataclass(frozen=True)
class Finding:
    id: Any
    reason: str


@dataclass(frozen=True)
class FieldCopies(Finding):
    kind: str = ""
    count: int = 0
    values: list = field(default_factory=list, compare=False, hash=False)


# --------------------------------------------------------------------------
# Phone numbers (6p.1)
# --------------------------------------------------------------------------

def normalise_phones(items: list[tuple[Id, Card]], style: str = "international",
                     region: str = phones.DEFAULT_REGION) -> list[Change]:
    """Every phone number that is not yet written in `style`.

    `international` is `+44 20 7946 0018`; `national` is `020 7946 0018` and is
    used only for numbers of `region` itself: a number from another country keeps
    its international form, because a national form could not be dialled from
    anywhere else.
    """
    if style not in ("international", "national"):
        raise ValueError("The phone format must be 'international' or 'national'.")
    out: list[Change] = []
    for id_, card in items:
        for f in card.values("phone"):
            raw = f.value.strip()
            if not raw:
                continue
            after = _format_phone(raw, style, region)
            if after and after != f.value:
                out.append(Change(id_, "phone", f.value, after, f"written as {style}"))
    return out


def _format_phone(raw: str, style: str, region: str) -> Optional[str]:
    if style == "national" and not _is_region(raw, region):
        style = "international"
    return phones.format_number(raw, style, region)


def _is_region(raw: str, region: str) -> bool:
    try:
        import phonenumbers  # noqa: PLC0415
        parsed = phonenumbers.parse(raw, region or None)
        # Country code, not region_code_for_number: that returns None for a number in an unassigned
        # range, and a number that does not validate can still be dialled nationally.
        return parsed.country_code == phonenumbers.country_code_for_region(region)
    except Exception:  # noqa: BLE001 - an unparseable number is simply not "in the region"
        return False


# --------------------------------------------------------------------------
# Name case (6p.2)
# --------------------------------------------------------------------------

#: Words a family name keeps in lower case ("Jan van der Berg"). Fixed list.
NAME_PARTICLES = frozenset({"van", "de", "von", "der", "di", "la", "le", "du", "da", "bin", "al", "den", "del",
                            "della", "dos", "das", "ter", "ten", "het", "zu", "af", "ibn", "el", "y", "of"})
_NAME_FIELDS = ("given", "additional", "family")


def _cap(word: str) -> str:
    return re.sub(r"[^\W\d_]+", lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(), word)


def _recase(value: str, force: bool) -> str:
    words = value.split(" ")
    out = []
    for w in words:
        if force:
            out.append(_cap(w) if w else w)
        elif not w or not (w.islower() or (w.isupper() and len(w) > 1)):
            out.append(w)                                  # already mixed (McDonald, O'Neil, Ann): the person's own
        elif w.lower() in NAME_PARTICLES and len(words) > 1:
            out.append(w.lower())
        else:
            out.append(_cap(w))
    return " ".join(out)


def fix_name_case(items: list[tuple[Id, Card]], force: bool = False) -> list[Change]:
    """Given, additional and family names written all in lower case or all in
    capitals. Particles (`van de von der di la le du da bin al`...) and names
    with their own capitals (McDonald, O'Neil, DeVito) are left alone unless
    `force`, which capitalises every word of every name (so `McDonald` becomes
    `Mcdonald`: the person asked for it).
    """
    out: list[Change] = []
    for id_, card in items:
        for name in _NAME_FIELDS:
            before = card.get(name)
            if not before.strip():
                continue
            after = _recase(before, force)
            if after != before:
                out.append(Change(id_, f"s:{name}", before, after, "capitalisation"))
    return out


# --------------------------------------------------------------------------
# Company from the email domain (6p.3, and enrichment 7b)
# --------------------------------------------------------------------------

#: Domains where the domain says nothing about an employer. A fixed list (rule 11:
#: nobody will tune it; a missing provider is a code change with a test). Enrichment
#: 7b uses `company_guess_from_email`, so the two always agree.
FREE_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "outlook.co.uk", "hotmail.com", "hotmail.co.uk", "hotmail.fr",
    "hotmail.de", "hotmail.it", "hotmail.es", "live.com", "live.co.uk", "live.fr", "live.de", "live.it",
    "live.nl", "msn.com", "passport.com", "yahoo.com", "yahoo.co.uk", "yahoo.fr", "yahoo.de", "yahoo.it",
    "yahoo.es", "yahoo.ca", "yahoo.com.au", "yahoo.co.in", "yahoo.co.jp", "yahoo.ie", "ymail.com",
    "rocketmail.com", "icloud.com", "me.com", "mac.com", "aol.com", "aol.co.uk", "aim.com", "proton.me",
    "protonmail.com", "protonmail.ch", "pm.me", "gmx.com", "gmx.net", "gmx.de", "gmx.co.uk", "gmx.at",
    "gmx.ch", "mail.com", "email.com", "usa.com", "zoho.com", "zohomail.com", "yandex.com", "yandex.ru",
    "mail.ru", "fastmail.com", "fastmail.fm", "tutanota.com", "tuta.io", "hey.com", "inbox.com",
    # United Kingdom and Ireland internet providers
    "btinternet.com", "btconnect.com", "bt.com", "sky.com", "virginmedia.com", "ntlworld.com",
    "blueyonder.co.uk", "talktalk.net", "tiscali.co.uk", "plus.com", "o2.co.uk", "orange.net", "freeserve.co.uk",
    "eircom.net", "iol.ie",
    # elsewhere
    "comcast.net", "verizon.net", "att.net", "sbcglobal.net", "bellsouth.net", "cox.net", "charter.net",
    "optonline.net", "shaw.ca", "rogers.com", "sympatico.ca", "bigpond.com", "optusnet.com.au", "web.de",
    "t-online.de", "orange.fr", "wanadoo.fr", "free.fr", "laposte.net", "libero.it", "naver.com", "qq.com",
    "163.com", "126.com", "sina.com",
})
#: Brands that are free mail on any country's domain (yahoo.co.nz, hotmail.nl...).
_FREE_MAIL_BRANDS = frozenset({"gmail", "googlemail", "hotmail", "yahoo", "outlook", "msn", "icloud", "aol",
                               "protonmail", "gmx", "zoho", "btinternet", "ymail", "rocketmail", "yandex"})
#: Second-level labels that belong to the country suffix (acme.co.uk -> the name is "acme").
_SLD = frozenset({"co", "com", "org", "net", "ac", "gov", "edu", "sch", "ltd", "plc", "nhs", "ne", "or", "gob",
                  "mil", "go", "gv"})


def _registrable(domain: str) -> tuple[str, str]:
    """(name label, registrable domain) for `mail.acme.co.uk` -> ("acme", "acme.co.uk")."""
    labels = [x for x in domain.lower().strip(".").split(".") if x]
    if len(labels) < 2:
        return "", ""
    take = 3 if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SLD else 2
    take = min(take, len(labels))
    reg = labels[-take:]
    return reg[0], ".".join(reg)


def company_guess_from_email(email: str) -> Optional[str]:
    """A company name from the domain of a work email, or None for free mail or
    anything that is not an address. `jane@mail.acme-widgets.co.uk` -> `Acme Widgets`."""
    text = (email or "").strip()
    if "@" not in text:
        return None
    domain = text.rsplit("@", 1)[1].strip().lower()
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", domain):
        return None
    name, registrable = _registrable(domain)
    if not name or registrable in FREE_MAIL_DOMAINS or name in _FREE_MAIL_BRANDS:
        return None
    return " ".join(w.capitalize() for w in name.split("-") if w) or None


def company_from_domain(items: list[tuple[Id, Card]]) -> list[Change]:
    """A blank company filled from a work email's domain. Only blanks are
    touched; a work-labelled or main address is tried first."""
    out: list[Change] = []
    for id_, card in items:
        if card.get("company").strip():
            continue
        emails = card.values("email")
        ranked = sorted(range(len(emails)), key=lambda i: (
            0 if "work" in emails[i].label.lower() else 1, 0 if emails[i].primary else 1, i))
        for i in ranked:
            guess = company_guess_from_email(emails[i].value)
            if guess:
                out.append(Change(id_, "s:company", "", guess, f"from {emails[i].value.strip().rsplit('@', 1)[1]}"))
                break
    return out


# --------------------------------------------------------------------------
# Finders (6p.4)
# --------------------------------------------------------------------------

_PERSON_NAME = ("prefix", "given", "additional", "family", "suffix")


def _has_value(card: Card, kind: str) -> bool:
    return any(f.value.strip() for f in card.values(kind))


def no_name(items: list[tuple[Id, Card]]) -> list[Finding]:
    """Contacts with no person name, nickname or company: nothing to call them by."""
    return [Finding(i, "has no name") for i, c in items
            if not any(c.get(k).strip() for k in (*_PERSON_NAME, "nickname", "company"))]


def no_email_or_phone(items: list[tuple[Id, Card]]) -> list[Finding]:
    return [Finding(i, "has no email address or phone number") for i, c in items
            if not _has_value(c, "email") and not _has_value(c, "phone")]


def only_a_name(items: list[tuple[Id, Card]]) -> list[Finding]:
    """A name and nothing else (a group does not count as something)."""
    out = []
    for i, c in items:
        has_name = any(c.get(k).strip() for k in (*_PERSON_NAME, "nickname"))
        other_scalar = any(v.strip() for k, v in c.scalars.items() if k not in (*_PERSON_NAME, "nickname"))
        other_field = any(f.value.strip() or any(f.data.values()) for f in c.fields)
        if has_name and not other_scalar and not other_field and c.photo is None:
            out.append(Finding(i, "has nothing but a name"))
    return out


def exact_field_copies(items: list[tuple[Id, Card]], region: str = phones.DEFAULT_REGION) -> list[FieldCopies]:
    """Values held twice inside one contact: the same email in any letter case,
    the same number in any format. One finding per contact and kind."""
    out: list[FieldCopies] = []
    for i, c in items:
        seen: dict[str, list[Field]] = {}
        for f in c.fields:
            if f.value.strip() or any(f.data.values()):
                seen.setdefault(field_key(f, region), []).append(f)
        per_kind: dict[str, list[Field]] = {}
        for copies in seen.values():
            if len(copies) > 1:
                per_kind.setdefault(copies[0].kind, []).extend(copies)
        for kind, fs in per_kind.items():
            out.append(FieldCopies(i, f"holds the same {kind} more than once", kind, len(fs), [f.value for f in fs]))
    return out


def remove_field_copies(card: Card, region: str = phones.DEFAULT_REGION) -> Card:
    """`card` without the second and later copies of a value; the first copy
    adopts a label or main flag it lacked. The input is not changed."""
    out = card.copy()
    out.fields = []
    kept: dict[str, Field] = {}
    for f in card.fields:
        if not f.value.strip() and not any(f.data.values()):
            out.fields.append(Field(f.kind, f.value, f.label, dict(f.data), f.primary))
            continue
        key = field_key(f, region)
        if key in kept:
            first = kept[key]
            if not first.label and f.label:
                first.label = f.label
            first.primary = first.primary or f.primary
            continue
        copy = Field(f.kind, f.value, f.label, dict(f.data), f.primary)
        kept[key] = copy
        out.fields.append(copy)
    return out
