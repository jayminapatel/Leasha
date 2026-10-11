"""Importing contacts: vCard, Google / Outlook / Apple CSV, any CSV by mapping, Excel.

Layer: L7

Order 1k 6i. Pure functions: text, bytes or a path in, an `ImportPreview` out.
Nothing is added to the book here - the Contacts page shows the preview with its
duplicates and the person decides - and a source file is only ever opened for
reading (rule 10): `import_file` opens `rb` and never writes beside it.

**Layouts.** `detect_layout(headers)` recognises Google (both its current and its
older column sets), Outlook, Leasha's own plain layout and an Apple-style layout
of labelled columns ("Home Email", "Mobile Phone", "Home ZIP"...). *UNVERIFIED*
against a real file: macOS Contacts itself exports only vCard, so the Apple CSV
layout is the common third-party one, written from its documentation. Anything
else is `unknown`: the preview asks for a mapping, `suggest_mapping` proposes
one, and `import_csv(text, mapping)` applies it.

**Mapping targets** (the value for a column header):
`given family additional prefix suffix nickname company department title notes`,
`name` (a full name: "Last, First" or "First Last"), `email[:label]`,
`phone[:label]`, `url[:label]`, `date[:label]` (default birthday),
`related[:label]`, `social[:label]`, `im[:label]`, `custom[:label]`,
`address.street|city|region|postcode|country[:label]` (columns with the same
label make one address), `group`, `favourite`, and `""` or `ignore` to skip.

**Source rows.** `source_row` is the record number in the file, 1 for the header
row of a CSV (so the first contact is row 2, as in a spreadsheet) and the
position of the card in a vCard file.
"""

from __future__ import annotations

import base64
import codecs
import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from app.contacts import _layouts as L
from app.contacts import phone as phones
from app.contacts import vcard
from app.contacts.card import Card, Field, Photo, SCALARS, field_key, units
from app.contacts.dupes import _UnionFind
from app.contacts.exporters import FAVOURITE_MARK

__all__ = ["ImportedContact", "ImportPreview", "InternalDuplicate", "import_vcard", "import_csv", "import_xlsx",
           "import_file", "detect_layout", "suggest_mapping", "find_internal_duplicates", "decode_bytes"]

csv.field_size_limit(50_000_000)             # a photo is a very long cell


@dataclass
class ImportedContact:
    card: Card
    source_row: int
    warnings: list[str] = field(default_factory=list)


@dataclass
class ImportPreview:
    contacts: list[ImportedContact] = field(default_factory=list)
    format: str = "unknown"          # vcard | google | outlook | apple | plain | mapped | unknown
    headers: list[str] = field(default_factory=list)
    mapping: dict[str, str] = field(default_factory=dict)
    needs_mapping: bool = False
    warnings: list[str] = field(default_factory=list)
    skipped_rows: list[int] = field(default_factory=list)      # records that held nothing
    source_name: str = ""

    @property
    def cards(self) -> list[Card]:
        return [c.card for c in self.contacts]


# --------------------------------------------------------------------------
# Text
# --------------------------------------------------------------------------

def decode_bytes(data: bytes) -> str:
    """UTF-8 (with or without a BOM) or UTF-16 (with a BOM); anything else as
    Windows-1252, which is what 2.1 vCards and old exports are."""
    if data.startswith(codecs.BOM_UTF8):
        return data[3:].decode("utf-8", "replace")
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16", "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", "replace")


def _text(data: Union[str, bytes]) -> str:
    text = decode_bytes(data) if isinstance(data, (bytes, bytearray)) else data
    return text.lstrip("﻿")


# --------------------------------------------------------------------------
# vCard
# --------------------------------------------------------------------------

def _structured_name(block: str) -> bool:
    """Does the card carry an N: line with something in it?"""
    for line in vcard._unfold(block):
        head, _, value = line.partition(":")
        if re.match(r"^(?:[\w-]+\.)?N(?:;|$)", head, flags=re.I):
            return bool(value.replace(";", "").strip())
    return False


def _fix_card(card: Card, block: str) -> Card:
    # A card with no N: line got its name from FN, which `vcard.dumps` fills with
    # an email address or "No name" when there is no name. That is not a name.
    if not _structured_name(block):
        made = " ".join(x for x in (card.get("given"), card.get("family")) if x)
        borrowed = {"no name", card.get("nickname").casefold()} | {f.value.strip().casefold() for f in card.fields
                                                                       if f.kind in ("email", "phone")}
        if made and made.casefold() in borrowed:
            card.scalars.pop("given", None)
            card.scalars.pop("family", None)
    keep = []
    for f in card.fields:
        if f.kind == "custom" and f.label == FAVOURITE_MARK[2:].replace("-", " ").lower():
            card.favourite = True
        else:
            if f.kind == "im" and f.label == "impp":
                f.label = ""
            keep.append(f)
    card.fields = keep
    return card


def import_vcard(data: Union[str, bytes]) -> ImportPreview:
    """vCard 2.1, 3.0 and 4.0, any number per file."""
    text = _text(data)
    text = re.sub(r"(?im)^((?:item\d+\.)?)X-RELATED(?=[;:])", r"\1RELATED", text)      # how vcard.dumps spells 3.0
    out = ImportPreview(format="vcard")
    for n, block in enumerate(vcard.split_cards(text), 1):
        card = _fix_card(vcard.parse_one(block), block)
        out.contacts.append(ImportedContact(card, n, _check(card)))
    return out


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------

class Row:
    """One record: values by header, matched without regard to case, and the first
    non-empty value when a header is repeated."""

    def __init__(self, headers: list[str], values: list[str]) -> None:
        self.cells: dict[str, str] = {}
        self.order: list[str] = []
        for h, v in zip(headers, list(values) + [""] * (len(headers) - len(values))):
            v = v.replace("\r\n", "\n").replace("\r", "\n")
            key = h.strip().lower()
            if key not in self.cells:
                self.order.append(key)
                self.cells[key] = v.strip()
            elif not self.cells[key]:
                self.cells[key] = v.strip()

    def get(self, *names: str) -> str:
        for n in names:
            v = self.cells.get(n.lower(), "")
            if v:
                return v
        return ""

    def blank(self) -> bool:
        return not any(self.cells.values())


def _delimiter(first_line: str) -> str:
    counts = {d: 0 for d in (",", ";", "\t")}
    quoted = False
    for ch in first_line:
        if ch == '"':
            quoted = not quoted
        elif not quoted and ch in counts:
            counts[ch] += 1
    best = max(counts, key=lambda d: (counts[d], d == ","))
    return best if counts[best] else ","


def _read_csv(text: str) -> tuple[list[str], list[tuple[int, list[str]]]]:
    if text.lower().startswith("sep=") and "\n" in text:                 # Excel's own hint line
        text = text.split("\n", 1)[1]
    first = text.split("\n", 1)[0]
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=_delimiter(first))
    records = list(reader)
    if not records:
        return [], []
    headers = [h.strip().lstrip("﻿") for h in records[0]]
    return headers, [(n, r) for n, r in enumerate(records[1:], 2)]


# --------------------------------------------------------------------------
# Layout detection and the mapping suggestion
# --------------------------------------------------------------------------

_GOOGLE_NUMBERED = re.compile(r"^(e-mail|phone|address|website|event|relation|custom field|organization|im) (\d+) - (.+)$")
_LABELLED = re.compile(
    r"^(?:(?P<label>[a-z ]+?)\s+(?P<kind>e-?mail|email|phone|fax|page|url|street|city|state|zip|zip code|"
    r"postal code|postcode|country)"
    r"|(?P<kind2>e-?mail|email|phone|fax|address)\s*[(\[]\s*(?P<label2>[a-z ]+?)\s*[)\]]\s*(?P<part2>street|city|state|zip|"
    r"zip code|postal code|postcode|country)?)$")


def detect_layout(headers: Iterable[str]) -> Optional[str]:
    """`google`, `outlook`, `plain`, `apple`, or None when the columns are not one we know."""
    hs = [h.strip().lower() for h in headers]
    hset = set(hs)
    if any(L.PLAIN_HEADER_RE.match(h.strip()) for h in headers):
        return "plain"
    numbered = [m for m in (_GOOGLE_NUMBERED.match(h) for h in hs) if m]
    if numbered and any(m.group(1) in ("e-mail", "phone", "address") for m in numbered) \
            and ({"first name", "given name", "name"} & hset):
        return "google"
    if "e-mail address" in hset and ({"first name", "last name"} & hset) \
            and (hset & {"business phone", "home phone", "mobile phone", "job title", "company", "business street"}):
        return "outlook"
    if any(_apple_column(h) for h in hs) and ({"first name", "last name", "given name", "family name"} & hset):
        return "apple"
    return None


_ALIASES = {
    "given": ["firstname", "givenname", "first", "forename", "christianname"],
    "family": ["lastname", "surname", "familyname", "last"],
    "additional": ["middlename", "additionalname", "middle"],
    "prefix": ["prefix", "nameprefix", "salutation"],
    "suffix": ["suffix", "namesuffix"],
    "nickname": ["nickname", "alias"],
    "name": ["name", "fullname", "displayname", "contactname", "contact"],
    "company": ["company", "companyname", "organization", "organisation", "org", "employer", "business",
                "organizationname"],
    "department": ["department", "dept", "division"],
    "title": ["jobtitle", "position", "role", "occupation", "organizationtitle"],
    "notes": ["notes", "note", "comments", "comment", "description", "memo", "remarks"],
    "email": ["email", "emailaddress", "mail", "emailaddress1", "email1", "eaddress", "emailid"],
    "email:work": ["workemail", "emailwork", "businessemail", "email2"],
    "email:home": ["homeemail", "emailhome", "personalemail"],
    "phone": ["phone", "telephone", "tel", "phonenumber", "phone1", "telephonenumber"],
    "phone:mobile": ["mobile", "cell", "cellphone", "mobilephone", "mobilenumber", "mobileno", "cellular", "gsm"],
    "phone:home": ["homephone", "phonehome", "homenumber"],
    "phone:work": ["workphone", "businessphone", "phonework", "officephone", "work"],
    "phone:work fax": ["fax", "workfax", "businessfax"],
    "address.street": ["address", "street", "streetaddress", "address1", "addressline1"],
    "address.city": ["city", "town", "suburb", "locality"],
    "address.region": ["state", "province", "region", "county", "stateprovince"],
    "address.postcode": ["zip", "zipcode", "postcode", "postalcode", "postal"],
    "address.country": ["country", "countryregion"],
    "url": ["website", "url", "web", "webpage", "homepage", "site"],
    "date:birthday": ["birthday", "dob", "birthdate", "dateofbirth", "born"],
    "date:anniversary": ["anniversary"],
    "group": ["group", "groups", "categories", "category", "labels", "tags", "groupmembership"],
    "favourite": ["favourite", "favorite", "starred", "star"],
    "social": ["twitter", "instagram", "linkedin", "facebook", "social"],
}
_ALIAS_LOOKUP = {a: target for target, names in _ALIASES.items() for a in names}


def suggest_mapping(headers: Iterable[str]) -> dict[str, str]:
    """A proposed target for every header (see the module doc); `""` where nothing fits.
    The person corrects it in the mapping step."""
    headers = list(headers)
    norm = {h: re.sub(r"[^a-z0-9]", "", h.lower()) for h in headers}
    has_job_title = any(n in ("jobtitle", "position", "role", "occupation") for n in norm.values())
    out: dict[str, str] = {}
    taken: set[str] = set()
    for h in headers:
        n = norm[h]
        target = _ALIAS_LOOKUP.get(n, "")
        if n == "title":
            target = "prefix" if has_job_title else "title"
        if n in ("contact", "name") and any(x in norm.values() for x in ("firstname", "givenname")):
            target = ""
        # a second plain "email"/"phone" column is still an email/phone, but never overwrites a scalar twice
        if target and target in taken and target in SCALARS:
            target = ""
        if target:
            taken.add(target)
        out[h] = target
    return out


# --------------------------------------------------------------------------
# Building cards
# --------------------------------------------------------------------------

_SCALAR_COLUMNS = {
    "google": {"given": ("first name", "given name"), "additional": ("middle name", "additional name"),
               "family": ("last name", "family name"), "prefix": ("name prefix",), "suffix": ("name suffix",),
               "nickname": ("nickname",), "company": ("organization name", "organization 1 - name"),
               "title": ("organization title", "organization 1 - title"),
               "department": ("organization department", "organization 1 - department"), "notes": ("notes",)},
    "outlook": {"prefix": ("title",), "given": ("first name",), "additional": ("middle name",),
                "family": ("last name",), "suffix": ("suffix",), "nickname": ("nickname",),
                "company": ("company",), "title": ("job title",), "department": ("department",),
                "notes": ("notes",)},
    "apple": {"prefix": ("prefix", "name prefix"), "given": ("first name", "given name"),
              "additional": ("middle name", "additional name"), "family": ("last name", "family name"),
              "suffix": ("suffix", "name suffix"), "nickname": ("nickname",), "company": ("company", "organization"),
              "department": ("department",), "title": ("job title", "title"), "notes": ("note", "notes")},
    "plain": {name: (header.lower(),) for header, name in L.PLAIN_SCALARS},
}


def _scalars(row: Row, layout: str) -> dict[str, str]:
    return {name: row.get(*cols) for name, cols in _SCALAR_COLUMNS[layout].items() if row.get(*cols)}


def _split_full_name(name: str) -> dict[str, str]:
    name = name.strip()
    if "," in name:
        family, _, given = name.partition(",")
        return {k: v.strip() for k, v in (("given", given), ("family", family)) if v.strip()}
    bits = name.rsplit(" ", 1)
    if len(bits) == 2:
        return {"given": bits[0].strip(), "family": bits[1].strip()}
    return {"given": name} if name else {}


def _date(value: str) -> str:
    v = value.strip()
    m = re.fullmatch(r"(\d{4})-?(\d{2})-?(\d{2})(?:[T ].*)?", v)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return v


def _add(card: Card, kind: str, value: str, label: str = "", data: Optional[dict] = None, primary: bool = False) -> None:
    value = (value or "").strip()
    if value or data:
        card.fields.append(Field(kind, value, label, dict(data or {}), primary))


def _address(card: Card, label: str, parts: dict[str, str], text: str = "") -> None:
    data = {k: v.strip() for k, v in parts.items() if v and v.strip()}
    if data or text.strip():
        joined = ", ".join(data[k] for k in ("street", "city", "region", "postcode", "country") if k in data)
        card.fields.append(Field("address", text.strip() or joined, label, data=data))


def _google_card(row: Row) -> Card:
    card = Card(scalars=_scalars(row, "google"))
    if not (card.get("given") or card.get("family")) and row.get("name"):
        card.scalars.update(_split_full_name(row.get("name")))
    if row.get("birthday"):
        _add(card, "date", _date(row.get("birthday")), "birthday")
    groups: dict[tuple[str, int], dict[str, str]] = {}
    for key in row.order:
        m = _GOOGLE_NUMBERED.match(key)
        if m and row.cells[key]:
            groups.setdefault((m.group(1), int(m.group(2))), {})[m.group(3)] = row.cells[key]
    for (kind, _n), sub in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1])):
        labels = L.split_multi(sub.get("label") or sub.get("type") or "")
        if kind == "address":
            parts = {k: L.split_multi(sub.get(g, "")) for k, g in
                     (("street", "street"), ("city", "city"), ("region", "region"), ("postcode", "postal code"),
                      ("country", "country"), ("pobox", "po box"), ("ext", "extended address"),
                      ("text", "formatted"))}
            for i in range(max([len(v) for v in parts.values()] + [1])):
                pick = {k: (v[i] if i < len(v) else "") for k, v in parts.items()}
                street = "\n".join(x for x in (pick["pobox"], pick["street"], pick["ext"]) if x)
                _address(card, L.label_in(labels[i]) if i < len(labels) else "",
                         {"street": street, "city": pick["city"], "region": pick["region"],
                          "postcode": pick["postcode"], "country": pick["country"]}, pick["text"])
            continue
        values = L.split_multi(sub.get("value") or sub.get("name") or "")
        for i, v in enumerate(values):
            lab = L.label_in(labels[i]) if i < len(labels) else (L.label_in(labels[-1]) if labels else "")
            if not v:
                continue
            if kind == "e-mail":
                _add(card, "email", v, lab)
            elif kind == "phone":
                _add(card, "phone", v, lab)
            elif kind == "website":
                _add(card, "url", v, lab)
            elif kind == "event":
                _add(card, "date", _date(v), lab or "other")
            elif kind == "relation":
                _add(card, "related", v, lab)
            elif kind == "custom field":
                _add(card, "custom", v, lab)
            elif kind == "im":
                _add(card, "im", v, lab)
    for item in L.split_multi(row.get("labels", "group membership")):
        _group(card, item)
    return card


def _group(card: Card, item: str) -> None:
    item = item.strip()
    if item.startswith("* "):
        if item[2:].strip().lower() == "starred":
            card.favourite = True
        return                                    # Google's own labels ("* myContacts")
    if item and item not in card.groups:
        card.groups.append(item)


def _outlook_card(row: Row) -> Card:
    card = Card(scalars=_scalars(row, "outlook"))
    if not (card.get("given") or card.get("family")) and row.get("display name", "name"):
        card.scalars.update(_split_full_name(row.get("display name", "name")))
    for col in ("e-mail address", "e-mail 2 address", "e-mail 3 address"):
        _add(card, "email", row.get(col))
    for slot, label in L.OUTLOOK_PHONE_IN.items():
        _add(card, "phone", row.get(slot), label)
    for block, label in L.OUTLOOK_ADDR.items():
        parts = {key: row.get(f"{block} {part}".lower()) for part, key in
                 (("Street", "street"), ("City", "city"), ("State", "region"), ("Postal Code", "postcode"),
                  ("Country/Region", "country"))}
        _address(card, label, parts)
    for col in ("web page", "personal web page"):
        _add(card, "url", row.get(col))
    _add(card, "im", row.get("imaddress"))
    for col, label in (("birthday", "birthday"), ("anniversary", "anniversary")):
        v = row.get(col)
        if v and not L.NO_DATE_RE.match(v):
            _add(card, "date", _date(v), label)
    for col, label in (("spouse", "spouse"), ("manager's name", "manager"), ("assistant's name", "assistant")):
        _add(card, "related", row.get(col), label)
    for item in re.split(r"[;]|\s:::\s", row.get("categories")):
        if item.strip() and item.strip() not in card.groups:
            card.groups.append(item.strip())
    return card


def _apple_column(header: str) -> Optional[tuple[str, str, str]]:
    """(kind, label, part) of a labelled column like "Home Email" or "Email (Work)"."""
    h = header.strip().lower()
    if h in ("iphone",):
        return "phone", "mobile", ""
    m = _LABELLED.match(h)
    if not m:
        return None
    kind = (m.group("kind") or m.group("kind2") or "").replace("e-mail", "email")
    label = (m.group("label") or m.group("label2") or "").strip()
    part = m.group("part2") or ""
    if kind in ("street", "city", "state", "zip", "zip code", "postal code", "postcode", "country"):
        part, kind = kind, "address"
    label = {"iphone": "mobile", "cell": "mobile", "business": "work", "office": "work"}.get(label, label)
    if kind == "fax":
        return "phone", f"{label} fax".strip(), ""
    if kind in ("page", "url"):
        return "url", label, ""
    if kind == "email" and label in ("e", ""):
        return "email", "", ""
    if kind == "phone" and not label:
        return "phone", "", ""
    return kind, label, part


_APPLE_PARTS = {"street": "street", "city": "city", "state": "region", "zip": "postcode", "zip code": "postcode",
                "postal code": "postcode", "postcode": "postcode", "country": "country"}


def _apple_card(row: Row) -> Card:
    card = Card(scalars=_scalars(row, "apple"))
    addresses: dict[str, dict[str, str]] = {}
    for key in row.order:
        value = row.cells[key]
        spec = _apple_column(key) if value else None
        if not spec:
            if key == "mobile" and value:
                _add(card, "phone", value, "mobile")
            continue
        kind, label, part = spec
        if kind == "address":
            addresses.setdefault(label, {})[_APPLE_PARTS.get(part, part)] = value
        elif kind in ("email", "phone", "url"):
            _add(card, kind, value, label)
    for label, parts in addresses.items():
        _address(card, label, parts)
    if row.get("birthday"):
        _add(card, "date", _date(row.get("birthday")), "birthday")
    for item in re.split(r"[;,]|\s:::\s", row.get("groups", "group", "categories")):
        _group(card, item)
    return card


def _plain_card(row: Row) -> Card:
    card = Card(scalars=_scalars(row, "plain"))
    for item in L.split_multi(row.get("groups")):
        if item and item not in card.groups:
            card.groups.append(item)
    card.favourite = row.get("favourite").lower() in ("yes", "true", "1", "y")
    photo = row.get("photo")
    m = re.match(r"data:([\w/+.-]+);base64,(.*)$", photo, flags=re.S)
    if m:
        try:
            data = base64.b64decode(m.group(2), validate=False)
        except ValueError:
            data = b""
        if data:
            card.photo = Photo(m.group(1), data)
    slots: dict[tuple[str, int], dict[str, str]] = {}
    for key in row.order:
        m2 = re.match(r"^(email|phone|address|website|date|related|social|im|custom) (\d+) "
                      r"(label|value|primary|street|city|region|postcode|country)$", key)
        if m2:
            slots.setdefault((m2.group(1), int(m2.group(2))), {})[m2.group(3)] = row.cells[key]
    kind_of = {t.lower(): k for k, t in L.PLAIN_KINDS}
    for (title, _n), sub in sorted(slots.items(), key=lambda kv: (list(kind_of).index(kv[0][0]), kv[0][1])):
        kind = kind_of[title]
        parts = {k: sub.get(k, "") for k in ("street", "city", "region", "postcode", "country") if sub.get(k)}
        if not sub.get("value") and not parts:
            continue
        card.fields.append(Field(kind, sub.get("value", ""), sub.get("label", ""),
                                 data=parts if kind == "address" else {},
                                 primary=sub.get("primary", "").lower() in ("yes", "true", "1")))
    return card


_BUILDERS = {"google": _google_card, "outlook": _outlook_card, "apple": _apple_card, "plain": _plain_card}


# ---- mapping -----------------------------------------------------------------

def _mapped_card(row: Row, headers: list[str], mapping: dict[str, str]) -> Card:
    card = Card()
    addresses: dict[str, dict[str, str]] = {}
    seen_header: dict[str, int] = {}
    for h in headers:
        target = (mapping.get(h) or "").strip()
        if not target or target.lower() == "ignore":
            continue
        # a header that is repeated is read once per occurrence in its own column
        seen_header[h] = seen_header.get(h, 0) + 1
        value = row.cells.get(h.strip().lower(), "")
        if not value:
            continue
        base, _, label = target.partition(":")
        base, label = base.strip().lower(), label.strip()
        if base in SCALARS:
            if not card.get(base):
                card.scalars[base] = value
        elif base == "name":
            for k, v in _split_full_name(value).items():
                card.scalars.setdefault(k, v)
        elif base in ("email", "phone", "url"):
            for part in re.split(r"\s:::\s|;", value):
                _add(card, base, part, label)
        elif base == "date":
            _add(card, "date", _date(value), label or "birthday")
        elif base in ("related", "social", "im", "custom"):
            _add(card, base, value, label)
        elif base.startswith("address."):
            addresses.setdefault(label, {})[base.split(".", 1)[1]] = value
        elif base == "group":
            for item in re.split(r"\s:::\s|;", value):
                if item.strip() and item.strip() not in card.groups:
                    card.groups.append(item.strip())
        elif base == "favourite":
            card.favourite = value.lower() in ("yes", "true", "1", "y", "x", "starred")
    for label, parts in addresses.items():
        _address(card, label, parts)
    return card


# ---- warnings and the table loop --------------------------------------------

def _check(card: Card) -> list[str]:
    out = []
    for f in card.fields:
        v = f.value.strip()
        if f.kind == "email" and v and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", v):
            out.append(f"'{v}' does not look like an email address.")
        elif f.kind == "phone" and v and not phones.match_key(v):
            out.append(f"'{v}' does not look like a phone number.")
    if not (card.get("given") or card.get("family") or card.get("company") or card.get("nickname")
            or card.values("email") or card.values("phone")):
        out.append("This contact has no name, company, email address or phone number.")
    return out


def _import_table(headers: list[str], records: list[tuple[int, list[str]]], mapping: Optional[dict[str, str]],
                  name: str = "") -> ImportPreview:
    prev = ImportPreview(headers=headers, source_name=name)
    layout = None
    if mapping is None:
        layout = detect_layout(headers)
        if layout is None:
            prev.format, prev.needs_mapping = "unknown", True
            prev.mapping = suggest_mapping(headers)
            return prev
        prev.format = layout
    else:
        prev.format, prev.mapping = "mapped", dict(mapping)
    for n, values in records:
        row = Row(headers, values)
        if row.blank():
            prev.skipped_rows.append(n)
            continue
        card = _BUILDERS[layout](row) if layout else _mapped_card(row, headers, mapping or {})
        if not units(card):
            prev.skipped_rows.append(n)
            continue
        prev.contacts.append(ImportedContact(card, n, _check(card)))
    return prev


def import_csv(data: Union[str, bytes], mapping: Optional[dict[str, str]] = None) -> ImportPreview:
    """A CSV in a known layout (detected from its header row), or any CSV through
    `mapping`. With no mapping and an unknown layout the preview has
    `needs_mapping` set, no contacts, and `mapping` holding `suggest_mapping`'s guess."""
    headers, records = _read_csv(_text(data))
    return _import_table(headers, records, mapping)


def import_xlsx(path, mapping: Optional[dict[str, str]] = None) -> ImportPreview:
    """First sheet of an Excel file, same layouts as the CSV. Opened read-only."""
    from openpyxl import load_workbook  # noqa: PLC0415
    path = Path(path)
    with open(path, "rb") as fh:                                      # never a write handle on the source
        wb = load_workbook(fh, read_only=True, data_only=True)
        try:
            ws = wb.worksheets[0]
            rows = [["" if v is None else _cell(v) for v in r] for r in ws.iter_rows(values_only=True)]
        finally:
            wb.close()
    if not rows:
        return ImportPreview(source_name=path.name)
    headers = [h.strip() for h in rows[0]]
    return _import_table(headers, [(n, r) for n, r in enumerate(rows[1:], 2)], mapping, path.name)


def _cell(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if hasattr(v, "isoformat"):
        return v.date().isoformat() if hasattr(v, "date") else v.isoformat()
    return str(v)


def import_file(path, mapping: Optional[dict[str, str]] = None) -> ImportPreview:
    """Import by file type: `.vcf`, `.csv` (and `.txt`), `.xlsx`. Read-only."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return import_xlsx(path, mapping)
    with open(path, "rb") as fh:
        raw = fh.read()
    is_vcard = suffix in (".vcf", ".vcard") or (suffix not in (".csv", ".txt") and b"BEGIN:VCARD" in raw[:4096].upper())
    prev = import_vcard(raw) if is_vcard else import_csv(raw, mapping)
    prev.source_name = path.name
    return prev


# --------------------------------------------------------------------------
# Copies inside the file
# --------------------------------------------------------------------------

@dataclass
class InternalDuplicate:
    indices: list[int]
    reasons: list[str] = field(default_factory=list)


def find_internal_duplicates(cards: Iterable[Union[Card, ImportedContact]],
                             region: str = phones.DEFAULT_REGION) -> list[InternalDuplicate]:
    """Positions of cards in the import that share an email address or a phone
    number (compared with `card.field_key`), so the preview can warn before the
    hub looks for duplicates against the book."""
    items = [c.card if isinstance(c, ImportedContact) else c for c in cards]
    seen: dict[str, list[int]] = {}
    for i, card in enumerate(items):
        for f in card.fields:
            if f.kind == "email" and f.value.strip() or f.kind == "phone" and phones.match_key(f.value, region):
                lst = seen.setdefault(field_key(f, region), [])
                if i not in lst:
                    lst.append(i)
    uf = _UnionFind()
    for i in range(len(items)):
        uf.add(i)
    why: dict[int, list[str]] = {}
    for key, idx in seen.items():
        for j in idx[1:]:
            uf.union(idx[0], j)
    for key, idx in seen.items():
        if len(idx) > 1:
            kind, _, value = key.partition(":")
            why.setdefault(uf.find(idx[0]), []).append(f"same {kind}: {value}")
    groups: dict[int, list[int]] = {}
    for i in range(len(items)):
        groups.setdefault(uf.find(i), []).append(i)
    return [InternalDuplicate(sorted(g), why.get(root, [])) for root, g in sorted(groups.items(), key=lambda kv: min(kv[1]))
            if len(g) > 1]
