"""5c: company and position from a LinkedIn export the person downloaded - nothing fetched from LinkedIn.

Layer: L7

Order 1s section 5c. The person points Settings > Accounts > Enrichment > "Use a LinkedIn export..." at
the `Connections.csv` LinkedIn emails them (`AccountSettings.enrich_linkedin_file`). The file is read
from disk, read-only; no request is made to LinkedIn or anywhere else.

**The file's shape.** LinkedIn's export starts with a few lines of notes ("Notes:", a sentence about
email addresses, a blank line) and only then the header row
`First Name,Last Name,URL,Email Address,Company,Position,Connected On`. The header is found by
looking for the row whose first two cells are "First Name" and "Last Name", so the notes - however many
LinkedIn writes this year - are passed over, and a file with no such row offers nothing.

**Which row is this contact's.** By email when the row has one (most connections hide theirs, so
this is the rarer case; confidence 0.85), else the exact full name *and* the same company as the
contact's card (0.7). A name with no company on the card is not matched: a name alone is not a person.

**What is offered.** The company when the card has none, and the position as the title when the card
has none. "Connected On" is the proposal's date (`Proposal.when`) - it is the only date the export
carries. It is *not* the age window's date (D1 is about mail): the position in an export is the one
the person shows today, whenever the two connected, so a connection made in 2015 is not stale.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.contacts.card import Card
from app.contacts.enrich import localfiles
from app.contacts.enrich.base import Proposal, emails_of
from app.contacts.enrich.company import company_key
from app.contacts.importers import decode_bytes
from app.core.logging import logger

__all__ = ["LinkedInRow", "read_export", "linkedin_proposals", "connected_on"]

_CONFIDENCE = {"email": 0.85, "name": 0.7}

_log = logger.bind(component="contacts.enrich.linkedin")
_SAID_MISSING: set[str] = set()


class LinkedInRow:
    __slots__ = ("first", "last", "email", "company", "position", "connected")

    def __init__(self, first: str, last: str, email: str, company: str, position: str,
                 connected: Optional[int]) -> None:
        self.first, self.last, self.email = first, last, email
        self.company, self.position, self.connected = company, position, connected

    @property
    def name(self) -> str:
        return f"{self.first} {self.last}".strip()


def connected_on(text: str) -> Optional[int]:
    """'12 Oct 2026' (LinkedIn's own spelling), or an ISO date, as Unix seconds at noon UTC; None if neither."""
    text = (text or "").strip()
    for fmt in ("%d %b %Y", "%d %B %Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            day = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return int(day.replace(hour=12, tzinfo=timezone.utc).timestamp())
    return None


def _parse(data: bytes) -> list[LinkedInRow]:
    rows = list(csv.reader(io.StringIO(decode_bytes(data).lstrip("﻿"))))
    start = next((i for i, r in enumerate(rows)
                  if len(r) >= 2 and r[0].strip().casefold() == "first name"
                  and r[1].strip().casefold() == "last name"), None)
    if start is None:
        return []
    head = [h.strip().casefold() for h in rows[start]]

    def cell(r: list[str], name: str) -> str:
        return r[head.index(name)].strip() if name in head and head.index(name) < len(r) else ""

    out = []
    for r in rows[start + 1:]:
        if not any(x.strip() for x in r):
            continue
        out.append(LinkedInRow(cell(r, "first name"), cell(r, "last name"), cell(r, "email address").lower(),
                               cell(r, "company"), cell(r, "position"), connected_on(cell(r, "connected on"))))
    return out


def read_export(path: str) -> list[LinkedInRow]:
    """The connections in the export at `path`, or [] when there is none, it is unreadable or not an export."""
    if not path:
        return []
    p = Path(path)
    try:
        st = p.stat()
    except OSError as exc:
        if path not in _SAID_MISSING:                    # once, not once for each of a thousand contacts
            _SAID_MISSING.add(path)
            _log.info("the LinkedIn export {} could not be read: {}", path, exc)
        return []
    f = localfiles.LocalFile(p, str(p), int(st.st_mtime), int(st.st_size))
    return localfiles.cached("linkedin", f, _parse) or []


def _full_name(card: Card) -> str:
    return " ".join(x for x in (card.get("given"), card.get("additional"), card.get("family")) if x).casefold()


def _match(card: Card, rows: list[LinkedInRow]) -> tuple[Optional[LinkedInRow], str]:
    mine = set(emails_of(card))
    for r in rows:
        if r.email and r.email in mine:
            return r, "email"
    name, firm = _full_name(card), company_key(card.get("company"))
    if not name or not firm:
        return None, ""
    for r in rows:
        if r.name.casefold() == name and company_key(r.company) == firm:
            return r, "name"
    return None, ""


def linkedin_proposals(cid: int, card: Card, path: str) -> list[Proposal]:
    """Company and position from the export at `path` for this contact, where the card lacks them."""
    row, rule = _match(card, read_export(path))
    if row is None:
        return []
    conf = _CONFIDENCE[rule]
    day = time_text(row.connected)
    receipt = f"{path} - LinkedIn connection" + (f", connected {day}" if day else "")
    out = []
    if row.company and not card.get("company").strip():
        out.append(Proposal(cid, "company", "", "", row.company, "linkedin", receipt, conf, when=row.connected))
    if row.position and not card.get("title").strip():
        out.append(Proposal(cid, "title", "", "", row.position, "linkedin", receipt, conf, when=row.connected))
    return out


def time_text(epoch_s: Optional[int]) -> str:
    if not epoch_s:
        return ""
    return datetime.fromtimestamp(int(epoch_s), timezone.utc).strftime("%d %B %Y").lstrip("0")
