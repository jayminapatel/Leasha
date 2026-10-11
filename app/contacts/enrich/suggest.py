"""7e: people who mail you often and are in no contact - suggested, never added alone.

Layer: L7

Order 1k section 7e. Local only. `suggest_contacts` lists senders from `messages`
with at least `min_count` messages (default 3) whose address is on no contact -
including a contact in the bin, whom the person already chose to delete - and
which does not look automatic (no-reply, mailer-daemon, newsletters, alerts ...).
`accept_suggestions` creates the contacts the person picked, as one journal batch.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Any, Iterable, Optional, Sequence

from app.contacts.card import Card, Field
from app.contacts.enrich.base import SOURCE_PREFIX, format_day

__all__ = ["SuggestedContact", "suggest_contacts", "accept_suggestions", "looks_automatic", "DEFAULT_MIN_COUNT"]

DEFAULT_MIN_COUNT = 3

_AUTOMATIC_LOCAL = re.compile(
    r"(?:^|[._+-])(?:no[._-]?reply|do[._-]?not[._-]?reply|donotreply|noreply|mailer[._-]?daemon|postmaster|"
    r"bounces?|newsletters?|notifications?|notify|alerts?|digest|unsubscribe|mailer|auto[._-]?reply|"
    r"autoresponder|daemon|robot|bot)(?:$|[._+\-\d])", re.I)
_AUTOMATIC_NAME = re.compile(r"\b(?:no[- ]?reply|do not reply|newsletter|notifications?|mailer-daemon)\b", re.I)
_AUTOMATIC_DOMAIN = re.compile(r"^(?:bounce|bounces|mailer|email|em\d*|mg|news|newsletter|notify|mail\d*)\.", re.I)


@dataclass(frozen=True)
class SuggestedContact:
    name: str
    email: str
    count: int
    last_seen: Optional[int]                             # Unix seconds


def looks_automatic(address: str, name: str = "") -> bool:
    """True for no-reply, mailer-daemon, newsletter and notification senders."""
    local, _, domain = address.lower().partition("@")
    return bool(_AUTOMATIC_LOCAL.search(local) or _AUTOMATIC_NAME.search(name or "")
                or _AUTOMATIC_DOMAIN.match(domain))


def _known_addresses(store: Any) -> set[str]:
    """Every email on any contact, the bin included."""
    return {r[0] for r in store.conn.execute("SELECT DISTINCT norm FROM contact_fields WHERE kind = 'email'")}


def suggest_contacts(store: Any, *, min_count: int = DEFAULT_MIN_COUNT, owner_emails: Iterable[str] = (),
                     limit: int = 100) -> list[SuggestedContact]:
    """Frequent senders that are in no contact, most messages first."""
    known = _known_addresses(store)
    owner = {e.strip().lower() for e in owner_emails if e}
    counts: Counter[str] = Counter()
    last: dict[str, int] = {}
    names: dict[str, Counter[str]] = defaultdict(Counter)
    for r in store.conn.execute("SELECT sender, COUNT(*) AS n, MAX(sent_at) AS last FROM messages "
                                "WHERE sender IS NOT NULL AND sender != '' GROUP BY sender"):
        name, addr = parseaddr(r["sender"])
        addr = addr.strip().lower()
        if "@" not in addr:
            continue
        counts[addr] += int(r["n"])
        if r["last"] is not None:
            last[addr] = max(last.get(addr, 0), int(r["last"]))
        name = name.strip().strip("'\"").strip()
        if name and name.lower() != addr:
            names[addr][name] += int(r["n"])
    out: list[SuggestedContact] = []
    for addr, n in counts.most_common():
        if n < min_count:
            break
        if addr in known or addr in owner:
            continue
        name = names[addr].most_common(1)[0][0] if names[addr] else ""
        if looks_automatic(addr, name):
            continue
        out.append(SuggestedContact(name, addr, n, last.get(addr)))
        if len(out) >= limit:
            break
    return out


def _split_name(name: str) -> dict[str, str]:
    text = re.sub(r"\s+", " ", name or "").strip()
    if not text:
        return {}
    if "," in text:                                      # "Smith, Dave"
        family, _, given = text.partition(",")
        return {k: v.strip() for k, v in (("given", given), ("family", family)) if v.strip()}
    words = text.split(" ")
    if len(words) == 1:
        return {"given": words[0]}
    out = {"given": words[0], "family": words[-1]}
    if len(words) > 2:
        out["additional"] = " ".join(words[1:-1])
    return out


def accept_suggestions(hub: Any, suggestions: Sequence[SuggestedContact], *, actor: str = "enrich",
                       kept_in: Optional[Iterable[int]] = None) -> list[int]:
    """Create a contact for each suggestion the person accepted. Returns the new ids.

    One journal batch. An address that has become a contact since the list was
    made is skipped, so accepting twice never makes two.
    """
    batch = uuid.uuid4().hex
    made: list[int] = []
    for s in suggestions:
        if hub.find_by_email(s.email):
            continue
        card = Card(scalars=_split_name(s.name),
                    fields=[Field("email", s.email, "other", primary=True)])
        ref = f"{s.count} messages from {s.email}" + (f", last {format_day(s.last_seen)}" if s.last_seen else "")
        made.append(hub.create(card, actor=actor, kept_in=kept_in, batch_id=batch,
                               source=SOURCE_PREFIX + "suggest", source_ref=ref))
    return made
