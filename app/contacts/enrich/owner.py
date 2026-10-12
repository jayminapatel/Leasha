"""Order 1s 3h: never offer the owner's own details to anybody.

Layer: L7

A reply quotes the message it answers, and a client's message often carries the
owner's own name, mobile and address - quoted, forwarded, or typed into the client's
signature as "your account lead". Offered as the client's details, they would be
accepted with "Accept all" and synced to the owner's phone under somebody else's
name. So before any block is parsed, every line holding one of these is removed:

* one of the owner's addresses (the accounts, `run.owner_addresses`);
* one of the owner's phone numbers;
* a line of the owner's own signature.

**Where the numbers and the signature come from.** The contact book has no "this
card is me" mark (checked 2026-10-12: nothing in `app/contacts/` records one), so
they are learned the way any signature is (3b): from the owner's own sent mail, the
messages *from* an owner address. That is read at any age - an old number of the
owner's is still the owner's - and once per run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from app.contacts import phone as phonelib
from app.contacts.enrich import base
from app.contacts.enrich.sigblock import find_blocks, is_sign_off, tail_lines
from app.contacts.enrich.sigtail import clean_tail, line_key, own_part

__all__ = ["OwnerDetails", "NO_OWNER", "learn_owner", "OWNER_MESSAGES"]

#: How many of the owner's newest sent messages the signature is learned from: enough for 3b to see it
#: repeat; more only costs time.
OWNER_MESSAGES = 20
_NUMBER = re.compile(r"\+?\d[\d\s().\-]{6,}\d")


@dataclass(frozen=True)
class OwnerDetails:
    emails: frozenset = frozenset()
    phones: frozenset = frozenset()                     # match keys (E.164 where the number parses)
    lines: frozenset = frozenset()                      # `line_key`s of the owner's signature lines
    region: str = phonelib.DEFAULT_REGION

    def holds(self, line: str) -> bool:
        low = line.lower()
        if any(e in low for e in self.emails) or line_key(line) in self.lines:
            return True
        return any(phonelib.match_key(m.group(0), self.region) in self.phones for m in _NUMBER.finditer(line))

    def scrub(self, lines: Sequence[str]) -> list[str]:
        if not (self.emails or self.phones or self.lines):
            return list(lines)
        return [ln for ln in lines if not self.holds(ln)]


NO_OWNER = OwnerDetails()


def learn_owner(store: Any, owner_emails: Iterable[str], *, region: str = phonelib.DEFAULT_REGION,
                read_original: bool = False) -> OwnerDetails:
    """The owner's addresses, numbers and signature lines, from their own sent mail (any age)."""
    emails = frozenset(e.strip().lower() for e in owner_emails if e and "@" in e)
    if not emails:
        return NO_OWNER
    rows = base.mail_from(store, sorted(emails), limit=OWNER_MESSAGES)
    tails = [tail_lines(clean_tail(own_part(base.full_text(store, r.file_id, read_original=read_original))))
             for r in rows]
    names = [t for r in rows for t in re.split(r"[\s,]+", _display(r.sender).lower()) if len(t) > 1]
    keys: set[str] = set()
    numbers: set[str] = set()
    for block in find_blocks(tails, name_tokens=sorted(set(names)), emails=sorted(emails)):
        for ln in block:
            if not is_sign_off(ln) and len(line_key(ln)) >= 4:
                keys.add(line_key(ln))
            for m in _NUMBER.finditer(ln):
                key = phonelib.match_key(m.group(0), region)
                if key:
                    numbers.add(key)
    return OwnerDetails(emails, frozenset(numbers), frozenset(keys), region)


def _display(sender: str) -> str:
    from email.utils import parseaddr  # noqa: PLC0415
    return parseaddr(sender or "")[0]
