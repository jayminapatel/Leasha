"""7a, rebuilt by order 1s section 3 and 4c: read a person's email signatures for their details.

Layer: L7

Local only: nothing here touches the network. For one contact:

1. **Their own messages** - `messages.sender` is exactly one of the card's
   addresses (3i), newest first, inside the age window (D1), at most
   `MAX_MESSAGES`; a message sent "on behalf of" somebody is skipped.
2. **Messages that quote them** (4c, `quoted`): their signature in the quoted
   chain of somebody else's reply, credited only through its own From line.
3. Every message is read through `base.full_text` - the indexed body, then the tail
   the index cut (4a) and, when the person switched it on, the original (4b) - and
   only the sender's own part of it (`sigtail.own_part`), cleaned (3a).
4. A block holding one of the owner's addresses or numbers is dropped whole; the owner's signature
   lines are removed from any other (3h, `owner`).
5. The block is what repeats across their messages (3b), else what an anchor marks
   (3c) - `sigblock`; each block is read by `sigparse.parse_block` (3d-3g).
6. The values are agreed across messages, newest wins, older ones become history
   (3j, `consensus`); a value that differs from the card's is offered as an update
   only when the agreement is strong, and is never pre-ticked (3k).

The rules stay deliberately cautious (order 1s, "precision before reach"): a wrong
title offered is worse than none.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Any, Iterable, Optional, Sequence

from app.contacts import phone as phones
from app.contacts.card import Card
from app.contacts.enrich import base, quoted
from app.contacts.enrich.base import Proposal, address_of, emails_of, format_day, mail_from
from app.contacts.enrich.consensus import Agreed, Reading, agree
from app.contacts.enrich.owner import NO_OWNER, OwnerDetails, learn_owner
from app.contacts.enrich.sigblock import Block, anchored_block, find_blocks, tail_lines
from app.contacts.enrich.sigparse import (  # noqa: F401 - logo.py reads the list here
    _SOCIAL_HOSTS,
    Signature,
    flat,
    parse_block,
)
from app.contacts.enrich.sigtail import clean_tail, own_part

__all__ = ["Signature", "find_block", "parse_block", "signature_proposals", "read_signatures", "proposals_from",
           "MAX_MESSAGES", "UPDATE_FROM", "Read"]

#: How many of a person's newest messages in the window are read (3j). Enough for the consensus to see
#: a signature repeat and a change; more would only slow a run (0.15 s a contact is the order's budget).
MAX_MESSAGES = 20
#: 3k: the confidence at which a value that differs from the card's is offered as an update. The same as
#: "Tick automatically from" (D4) by default - a change needs agreement, not one message.
UPDATE_FROM = 0.85
#: Keys of a `Signature` that mark how it was read, not values read from it.
_MARKS = frozenset({"name_matched", "website_unsure"})
_TAIL_KEEP = 200                                         # non-blank lines kept of a message before the tail is cut


def find_block(body: str, name_tokens: Sequence[str] = (), emails: Sequence[str] = ()) -> list[str]:
    """One message's block by its anchors alone (3c), after cleaning (3a). Kept for one-message callers."""
    return list(anchored_block(tail_lines(clean_tail(own_part(body))), name_tokens=name_tokens, emails=emails))


def _name_tokens(card: Card, sender_name: str = "") -> list[str]:
    toks: list[str] = []
    for key in ("given", "family"):
        for t in re.split(r"\s+", card.get(key).strip().lower()):
            if len(t) > 1:
                toks.append(t)
    if not toks and sender_name:
        toks = [t for t in re.split(r"[\s,]+", sender_name.lower()) if len(t) > 1]
    return toks


def _display(sender: str) -> str:
    return parseaddr(sender or "")[0]


def _on_behalf(sender: str) -> bool:
    """3i. Only the display name can say it: the index keeps one `sender` per message (no Sender: header),
    and Outlook writes "Bob Ray on behalf of Ann Lee" there."""
    return " on behalf of " in f" {_display(sender).lower()} "


def _lines(text: str) -> list[str]:
    """The cleaned tail of one message. The owner's lines are still in it: 3h needs to see the block they
    are in to drop it whole (`_owner_free`), which it could not do once they were gone."""
    lines = [ln.strip() for ln in clean_tail(own_part(text)).splitlines() if ln.strip()][-_TAIL_KEEP:]
    return tail_lines("\n".join(lines))


def _owner_free(block: Block, owner: OwnerDetails) -> Block:
    """3h. A block with one of the owner's addresses or numbers in it is the owner's - every line of it,
    the title and the mobile beside the address too - so nothing of it is read. From any other block the
    owner's own signature lines go (a colleague's block sharing the owner's old company line)."""
    if owner.owns(block):
        return Block((), "")
    return Block(owner.scrub(block), block.how)


def _domain_companies(store: Any, cid: int, emails: Iterable[str]) -> set[str]:
    """3f: companies already on other live contacts with an address at one of this person's domains."""
    domains = {e.rsplit("@", 1)[-1] for e in emails if "@" in e}
    out: set[str] = set()
    for d in domains:
        try:
            for r in store.conn.execute(
                    "SELECT DISTINCT c.company FROM contacts c JOIN contact_fields f ON f.contact_id = c.id "
                    "WHERE f.kind = 'email' AND lower(f.value) LIKE ? AND c.id <> ? AND c.deleted_at IS NULL "
                    "AND c.company <> ''", ("%@" + d, int(cid))):
                out.add(r[0])
        except Exception:                                # noqa: BLE001 - a store without the contact tables
            return out
    return out


@dataclass
class Read:
    """What was read for one contact: the direct messages, each one's block, and every reading."""

    rows: list = field(default_factory=list)
    blocks: list = field(default_factory=list)            # one `Block` per direct row
    readings: list = field(default_factory=list)          # `consensus.Reading`, direct and quoted


def read_signatures(store: Any, cid: int, card: Card, *, region: str = phones.DEFAULT_REGION,
                    rows: Optional[list] = None, since: Optional[int] = None, owner: OwnerDetails = NO_OWNER,
                    read_original: bool = False, include_quoted: bool = True) -> Read:
    """Steps 1-5 of the module docstring for one contact. Reads only."""
    emails = emails_of(card)
    out = Read()
    if not emails:
        return out
    if rows is None:
        rows = mail_from(store, emails, limit=MAX_MESSAGES, since=since)
    wanted = set(emails)
    rows = [r for r in rows if address_of(r.sender) in wanted and not _on_behalf(r.sender)][:MAX_MESSAGES]
    texts: list[tuple[Any, Optional[int], list[str], bool]] = []
    for r in rows:
        texts.append((r, r.sent_at, _lines(base.full_text(store, r.file_id, read_original=read_original)), False))
    if include_quoted:
        texts += _quoted_texts(store, wanted, rows, since, owner, read_original)
    tokens = _name_tokens(card) or _name_tokens(card, next((_display(r.sender) for r in rows if _display(r.sender)), ""))
    blocks = [_owner_free(b, owner) for b in find_blocks([t[2] for t in texts], name_tokens=tokens, emails=emails)]
    companies = _domain_companies(store, cid, emails)
    for (row, when, _, is_quoted), block in zip(texts, blocks, strict=True):
        if not is_quoted:
            out.blocks.append(block)
        sender = address_of(row.sender) if not is_quoted else emails[0]
        sig = parse_block(block, name_tokens=tokens, sender_email=sender, region=region, own_emails=emails,
                          domain_companies=companies)
        for addr in [*sig.get("emails", [])]:
            if addr in owner.emails:
                sig["emails"].remove(addr)
        if owner.phones and sig.get("phone"):
            sig["phone"] = [(lb, n) for lb, n in sig["phone"] if phones.match_key(n, region) not in owner.phones]
        if any(k not in _MARKS and sig.get(k) for k in sig):
            out.readings.append(Reading(row, when, sig, list(block), is_quoted))
    out.rows = rows
    return out


#: A quoted Sent date carries no time zone (Outlook writes the reader's local time), so a quoted copy of a
#: message the person sent directly can be up to this far from its indexed `sent_at`. Within it, the quoted
#: copy is taken for the same message and not counted again - one message must not agree with itself (3j).
_SAME_MESSAGE_S = 14 * 3600


def _quoted_texts(store: Any, wanted: set[str], direct: Sequence[Any], since: Optional[int], owner: OwnerDetails,
                  read_original: bool) -> list[tuple[Any, Optional[int], list[str], bool]]:
    """4c: the person's quoted messages inside other people's mail, inside the window, each once."""
    direct_times = [r.sent_at for r in direct if r.sent_at]
    quoted_minutes: set[int] = set()
    out = []
    for row in quoted.mail_quoting(store, sorted(wanted), since=since):
        text = base.full_text(store, row.file_id, read_original=read_original)
        for q in quoted.quoted_messages(text[len(own_part(text)):]):
            if q.address not in wanted:
                continue                                   # credited only to its own From line
            if since is not None and (q.sent_at is None or q.sent_at < since):
                continue                                   # D1: the quoted message's own date decides
            if q.sent_at:
                if any(abs(q.sent_at - t) <= _SAME_MESSAGE_S for t in direct_times) \
                        or q.sent_at // 60 in quoted_minutes:
                    continue                               # read directly already, or quoted in another reply
                quoted_minutes.add(q.sent_at // 60)
            out.append((row, q.sent_at, _lines(q.text), True))
    return out


def _receipt(a: Agreed) -> str:
    row = a.reading.row
    text = f"{(row.subject or '(no subject)')[:80]} - {format_day(row.sent_at)}".rstrip(" -")
    if a.reading.quoted:
        text += f" (quoted, sent {format_day(a.reading.when) or 'undated'})"
    return text


def proposals_from(cid: int, card: Card, readings: Sequence[Reading], *, region: str = phones.DEFAULT_REGION,
                   update_from: float = UPDATE_FROM, blocks: Optional[dict] = None) -> list[Proposal]:
    """Step 6: the agreed values as proposals - gaps, and (3k) strong changes to what the card holds.

    Each proposal carries the block its receipt was read from (`Proposal.block`, order 1s 9b); `blocks`,
    when given, is filled with `id(proposal) -> that block` as well, for callers that kept the old way."""
    out: list[Proposal] = []
    for a in agree(readings, region):
        p = _offer(cid, card, a, region, update_from)
        if p is not None:
            out.append(p)
            if blocks is not None:
                blocks[id(p)] = a.reading.block
    return out


def _norm_url(u: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", u.lower()).rstrip("/")


def _offer(cid: int, card: Card, a: Agreed, region: str, update_from: float) -> Optional[Proposal]:
    """One agreed value as a proposal, or None when the card already holds it (or, 3k, holds another value
    and the agreement is not strong enough to offer a change)."""
    kind = a.slot.split(":", 1)[0]
    extra = dict(when=a.reading.when, seen=a.seen, first_seen=a.first_seen, history=a.history,
                 block=list(a.reading.block))
    receipt = _receipt(a)
    if kind in ("title", "company", "department"):
        now = card.get(kind).strip()
        if now.casefold() == str(a.value).strip().casefold() or (now and a.confidence < update_from):
            return None
        return Proposal(cid, kind, "", now, a.value, "signature", receipt, a.confidence, update=bool(now), **extra)
    if kind == "phone":
        if any(phones.match_key(f.value, region) == phones.match_key(a.value, region) for f in card.values("phone")):
            return None
        return Proposal(cid, "phone", a.slot.split(":")[1], "", a.value, "signature", receipt, a.confidence, **extra)
    if kind == "email":
        if any(f.value.strip().lower() == a.value for f in card.values("email")):
            return None
        return Proposal(cid, "email", "work", "", a.value, "signature", receipt, a.confidence, **extra)
    if kind == "website":
        if a.reading.sig.get("website_unsure") and a.seen < 2:
            return None                                    # 9b: unlike their email and no known TLD - once is not enough
        if any(_norm_url(f.value) == _norm_url(a.value) for f in card.values("url")):
            return None
        return Proposal(cid, "url", "work", "", a.value, "signature", receipt, a.confidence, **extra)
    if kind == "address":
        have = {flat(f.data.get("postcode", "")) or flat(f.value) for f in card.values("address")}
        if (flat(a.data.get("postcode", "")) or flat(a.value)) in have:
            return None
        return Proposal(cid, "address", "work", "", a.value, "signature", receipt, a.confidence,
                        data=dict(a.data), **extra)
    return None


def signature_proposals(store: Any, cid: int, card: Card, *, region: str = phones.DEFAULT_REGION,
                        rows: Optional[list] = None, since: Optional[int] = None,
                        owner_emails: Iterable[str] = (), owner: Optional[OwnerDetails] = None,
                        read_original: bool = False, update_from: float = UPDATE_FROM) -> list[Proposal]:
    """Proposals from this person's signatures. Changes nothing.

    `rows` are their messages when the caller has already read them (the measuring command); left out,
    they are read here from `since` (the age window) on. `owner` is the owner's details learned once
    for a whole run (`owner.learn_owner`); left out, they are learned here from `owner_emails`."""
    if owner is None:
        owner = learn_owner(store, owner_emails, region=region, read_original=read_original)
    read = read_signatures(store, cid, card, region=region, rows=rows, since=since, owner=owner,
                           read_original=read_original)
    return proposals_from(cid, card, read.readings, region=region, update_from=update_from)

