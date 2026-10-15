"""5d: names from calendar invitations, and the people you meet as suggested contacts.

Layer: L7

Order 1s section 5d. Local only: the `.ics` files the index holds are read again from disk, read-only
(`localfiles`). From each event's `ORGANIZER` and `ATTENDEE` lines this reads two things and nothing
else - **never a phone number or a title**, because an invitation carries neither:

* **A name for a contact who has none.** A contact that is only an address gets the `CN=` name its
  invitations give that address, split into given and family names. The address is matched exactly,
  so the confidence may reach 0.85 when two or more invitations agree (0.75 from one); a contact whose
  invitations give two different names gets the commoner one, 0.1 less sure.
* **Suggested contacts.** An organiser of any invitation, and an attendee of at least `min_count`
  of them, whose address is on no contact, feeds "Suggested contacts" (order 1k 7e) beside the people
  who mail you. Rooms and resources (`CUTYPE=ROOM` / `RESOURCE`) and automatic senders are left out.

The age window (D1) is applied to the file's modified date, as for vCards (5a).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Iterable, Optional

from app.contacts import vcard
from app.contacts.card import Card
from app.contacts.enrich import localfiles
from app.contacts.enrich.base import Proposal, emails_of
from app.contacts.enrich.suggest import (DEFAULT_MIN_COUNT, SuggestedContact, _known_addresses, _split_name,
                                         looks_automatic)
from app.contacts.importers import decode_bytes

__all__ = ["EXTS", "Person", "people_in", "calendar_proposals", "calendar_suggestions"]

EXTS = ("ics",)

_NAME_PARTS = ("prefix", "given", "additional", "family", "suffix", "nickname")


class Person(tuple):
    """(role, name, address) - role is "organizer" or "attendee"."""

    @property
    def role(self) -> str:
        return self[0]

    @property
    def name(self) -> str:
        return self[1]

    @property
    def address(self) -> str:
        return self[2]


_PARAM = re.compile(r';([A-Za-z-]+)=("[^"]*"|[^;:]*)')


def people_in(data: bytes) -> list[Person]:
    """Every organiser and attendee in a calendar file, once each per file (organiser wins)."""
    text = decode_bytes(data).lstrip("﻿")
    seen: dict[str, Person] = {}
    for line in vcard._unfold(text):                    # RFC 5545 folds lines exactly as RFC 6350 does
        head, sep, value = _split(line)
        if not sep:
            continue
        prop = head.split(";", 1)[0].strip().upper()
        if prop not in ("ORGANIZER", "ATTENDEE"):
            continue
        params = {k.upper(): v.strip('"').strip() for k, v in _PARAM.findall(head)}
        if params.get("CUTYPE", "").upper() in ("ROOM", "RESOURCE"):
            continue
        addr = value.strip()
        if addr.lower().startswith("mailto:"):
            addr = addr[7:]
        addr = (params.get("EMAIL") or addr).strip().lower()
        if "@" not in addr:
            continue
        name = re.sub(r"\s+", " ", params.get("CN", "")).strip().strip("'\"").strip()
        if "@" in name:                                  # "CN=dave@x.com" is not a name
            name = ""
        role = "organizer" if prop == "ORGANIZER" else "attendee"
        if addr not in seen or (role == "organizer" and seen[addr].role != "organizer"):
            seen[addr] = Person((role, name or (seen[addr].name if addr in seen else ""), addr))
        elif name and not seen[addr].name:
            seen[addr] = Person((seen[addr].role, name, addr))
    return list(seen.values())


def _split(line: str) -> tuple[str, str, str]:
    """`ATTENDEE;CN="Smith: Dave":mailto:x` -> head and value, with a colon inside quotes kept in the head."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            return line[:i], ":", line[i + 1:]
    return line, "", ""


def _has_name(card: Card) -> bool:
    return any(card.get(p).strip() for p in _NAME_PARTS)


def calendar_proposals(hub: Any, cid: int, card: Card, *, since: Optional[int] = None) -> list[Proposal]:
    """A name for a contact that has none, from the invitations its address is on."""
    if _has_name(card):
        return []
    mine = set(emails_of(card))
    if not mine:
        return []
    names: Counter[str] = Counter()
    spelling: dict[str, str] = {}
    newest: dict[str, Any] = {}
    for f in localfiles.indexed_files(hub.store, EXTS, since=since):
        for p in localfiles.cached("ics", f, people_in) or []:
            if p.address in mine and p.name:
                key = p.name.casefold()
                names[key] += 1
                spelling.setdefault(key, p.name)
                newest.setdefault(key, f)                # files come newest first
    if not names:
        return []
    (key, n), *rest = names.most_common()
    conf = 0.75 if n == 1 else 0.85
    if rest:
        conf -= 0.1
    f = newest[key]
    more = sum(names.values()) - 1
    receipt = (f.receipt
               + (f" (and {more} more invitation{'s' if more != 1 else ''})" if more else ""))
    parts = _split_name(spelling[key])
    return [Proposal(cid, part, "", "", value, "calendar", receipt, round(conf, 2), when=f.mtime, opens=f.shown)
            for part, value in parts.items()]


def calendar_suggestions(store: Any, *, owner_emails: Iterable[str] = (), min_count: int = DEFAULT_MIN_COUNT,
                         since: Optional[int] = None) -> list[SuggestedContact]:
    """Organisers, and attendees of at least `min_count` invitations, who are in no contact. Most invitations first."""
    known = _known_addresses(store)
    owner = {e.strip().lower() for e in owner_emails if e}
    count: Counter[str] = Counter()
    organiser: set[str] = set()
    last: dict[str, int] = {}
    names: dict[str, Counter[str]] = defaultdict(Counter)
    for f in localfiles.indexed_files(store, EXTS, since=since):
        for p in localfiles.cached("ics", f, people_in) or []:
            count[p.address] += 1
            last[p.address] = max(last.get(p.address, 0), f.mtime)
            if p.role == "organizer":
                organiser.add(p.address)
            if p.name:
                names[p.address][p.name] += 1
    out = []
    for addr, n in count.most_common():
        if addr in known or addr in owner or (addr not in organiser and n < min_count):
            continue
        name = names[addr].most_common(1)[0][0] if names[addr] else ""
        if looks_automatic(addr, name):
            continue
        out.append(SuggestedContact(name, addr, n, last.get(addr), source="calendar"))
    return out
