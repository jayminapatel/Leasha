"""Order 1s 3h: never offer the owner's own details to anybody.

Layer: L7

A reply quotes the message it answers, and a client's message often carries the
owner's own name, mobile and address - quoted, forwarded, or typed into the client's
signature as "your account lead". Offered as the client's details, they would be
accepted with "Accept all" and synced to the owner's phone under somebody else's
name. So:

* a block holding one of the owner's addresses or numbers anywhere is the owner's,
  and is dropped whole - the mobile, direct line and title beside the owner's old
  address are the owner's too (`owns`);
* from any other block, a line of the owner's own signature is removed (`scrub`).

**Who the owner is.** The contact book has no "this card is me" mark (checked
2026-10-12: nothing in `app/contacts/` records one), and the accounts only know the
addresses the owner uses *now*. The first precision check (order 1s 9b) found the
owner's signature from a previous employer offered as a client's: his old work mobile,
his old direct line, his old work address in a quoted chain. So the owner's addresses
are also every sender of a message in one of the owner's Sent folders - an archive of
an old job still has its "Sent Items" - and the numbers and signature lines are
learned from all of those messages, at any age: an old number of the owner's is
still the owner's. A From display name equal to the owner's is only a hint (it
helps find the owner's sign-off in their own mail); a namesake elsewhere in the
index is not the owner.

**Cost.** The owner's sent mail can be thousands of messages. The signature is learned
in one pass - a line that recurs across the owner's messages, or sits in a block that
holds the owner's name - not by comparing every message with every other (3b's
`find_blocks` is quadratic), and what was learned is kept on the store for as long as
the owner's sent mail is unchanged, so a one-contact run does not learn it again.
"""

from __future__ import annotations

import contextlib
import re
from collections import Counter
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Any, Iterable, Sequence

from app.contacts import phone as phonelib
from app.contacts.enrich import base
from app.contacts.enrich.sigblock import (
    anchored_block,
    holds_name,
    is_sign_off,
    signature_line,
    tail_lines,
)
from app.contacts.enrich.sigtail import clean_tail, line_key, own_part

__all__ = ["OwnerDetails", "NO_OWNER", "learn_owner", "is_sent_folder", "SENT_FOLDERS", "owner_messages"]

_NUMBER = re.compile(r"\+?\d[\d\s().\-]{6,}\d")
_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")

#: The last part of a folder path that makes it a Sent folder, folded. Outlook, Gmail ("[Gmail]/Sent Mail"),
#: Apple Mail and IMAP servers in English, and the localised names Outlook gives the folder in the languages
#: the owner's mail comes in (the order's Benelux, France, Germany, Spain and Italy). Nothing in the code base
#: named them before (checked 2026-10-12), so the list is Outlook's own.
SENT_FOLDERS = frozenset({
    "sent", "sent items", "sent mail", "sent messages", "sent email", "sent e-mail", "sent-mail", "outbox sent",
    "verzonden items", "verzonden", "verzonden berichten",
    "gesendete elemente", "gesendete objekte", "gesendet",
    "\u00e9l\u00e9ments envoy\u00e9s", "elements envoyes", "messages envoy\u00e9s", "envoy\u00e9s",
    "elementos enviados", "enviados", "posta inviata", "elementi inviati", "itens enviados",
})


def is_sent_folder(path: str) -> bool:
    """Is `path` (an archive's folder path, either slash) one of the owner's Sent folders - by its last part."""
    parts = [p for p in re.split(r"[/\\]", path or "") if p.strip()]
    return bool(parts) and parts[-1].strip().casefold() in SENT_FOLDERS


def _emails_in(line: str) -> set[str]:
    return {m.group(0).lower().strip(".") for m in _EMAIL.finditer(line)}


@dataclass(frozen=True)
class OwnerDetails:
    emails: frozenset = frozenset()
    phones: frozenset = frozenset()                     # match keys (E.164 where the number parses)
    lines: frozenset = frozenset()                      # `line_key`s of the owner's signature lines
    region: str = phonelib.DEFAULT_REGION

    def _number_in(self, line: str) -> bool:
        return any(phonelib.match_key(m.group(0), self.region) in self.phones for m in _NUMBER.finditer(line))

    def owns_line(self, line: str) -> bool:
        """The line holds one of the owner's addresses (anywhere: "E  me@old.example") or numbers."""
        return bool(_emails_in(line) & self.emails) or (bool(self.phones) and self._number_in(line))

    def owns(self, block: Sequence[str]) -> bool:
        """3h: a block with an owner address or number in it is the owner's block, all of it."""
        return any(self.owns_line(ln) for ln in block)

    def holds(self, line: str) -> bool:
        return self.owns_line(line) or line_key(line) in self.lines

    def scrub(self, lines: Sequence[str]) -> list[str]:
        if not (self.emails or self.phones or self.lines):
            return list(lines)
        return [ln for ln in lines if not self.holds(ln)]


NO_OWNER = OwnerDetails()


def _folders(store: Any) -> list[str]:
    try:
        return [r[0] for r in store.conn.execute(
            "SELECT DISTINCT folder_path FROM messages WHERE folder_path IS NOT NULL AND folder_path <> ''")]
    except Exception:                                    # an index from before schema 32 has no folder_path
        return []


def owner_messages(store: Any, owner_emails: Iterable[str]) -> tuple[frozenset, list[base.MailRow]]:
    """The owner's addresses and every message the owner sent: those in a Sent folder (any sender
    address - an Exchange archive may have none) and those from one of the owner's addresses."""
    emails = {e.strip().lower() for e in owner_emails if e and "@" in e}
    rows: dict[int, base.MailRow] = {}
    sent = [f for f in _folders(store) if is_sent_folder(f)]
    for start in range(0, len(sent), 400):
        chunk = sent[start:start + 400]
        for r in store.conn.execute(
                "SELECT file_id, subject, sender, recipients, sent_at FROM messages WHERE folder_path IN ("
                + ",".join("?" * len(chunk)) + ")", chunk):
            row = base._row(r)
            if " on behalf of " in f" {parseaddr(row.sender)[0].lower()} ":
                continue                                   # a delegate's message is not the owner's own
            rows[row.file_id] = row
            addr = base.address_of(row.sender)
            if addr:
                emails.add(addr)
    if emails:
        for row in base.mail_from(store, sorted(emails), limit=1_000_000):
            rows[row.file_id] = row
    return frozenset(emails), sorted(rows.values(), key=lambda m: (m.sent_at is None, -(m.sent_at or 0)))


def _cache_key(emails: frozenset, rows: Sequence[base.MailRow], region: str) -> tuple:
    return emails, region, len(rows), max((r.file_id for r in rows), default=0)


def learn_owner(store: Any, owner_emails: Iterable[str], *, region: str = phonelib.DEFAULT_REGION,
                read_original: bool = False) -> OwnerDetails:
    """The owner's addresses, numbers and signature lines, from all of the owner's sent mail (any age).

    `read_original` is not used for the owner's mail: opening thousands of originals once a run would
    cost minutes, and the cut tail kept at indexing time (4a) already hands back a cut signature."""
    del read_original
    emails, rows = owner_messages(store, owner_emails)
    if not emails and not rows:
        return NO_OWNER
    key = _cache_key(emails, rows, region)
    cached = getattr(store, "_enrich_owner", None)
    if cached is not None and cached[0] == key:
        return cached[1]
    names = sorted({t for r in rows for t in re.split(r"[\s,]+", parseaddr(r.sender)[0].lower())
                    if len(t) > 1 and "@" not in t})
    tails = [tail_lines(clean_tail(own_part(base.full_text(store, r.file_id)))) for r in rows]
    # 3b without the quadratic comparison: a line is part of the owner's signature when it recurs in two
    # or more of the owner's messages, or sits in a block that holds the owner's own name.
    recurring = Counter(k for t in tails for k in {line_key(ln) for ln in t if signature_line(ln)})
    keys: set[str] = set()
    numbers: set[str] = set()
    more_emails: set[str] = set()
    for tail in tails:
        block = anchored_block(tail, name_tokens=names, emails=sorted(emails))
        named = any(holds_name(ln, names) for ln in block)
        in_block = {line_key(ln) for ln in block}
        for ln in tail:
            k = line_key(ln)
            again = recurring[k] >= 2 and signature_line(ln)
            if not (again or (named and k in in_block)):
                continue
            if not is_sign_off(ln) and len(k) >= 4:
                keys.add(k)
            for m in _NUMBER.finditer(ln):
                found = phonelib.match_key(m.group(0), region)
                if found:
                    numbers.add(found)
            if again and k in in_block:
                more_emails |= _emails_in(ln)            # "E  me@old.example" in the owner's own block
    out = OwnerDetails(frozenset(emails | more_emails), frozenset(numbers), frozenset(keys), region)
    with contextlib.suppress(AttributeError):           # a store that takes no attributes learns each time
        store._enrich_owner = (key, out)
    return out
