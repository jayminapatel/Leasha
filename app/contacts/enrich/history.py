"""7d: contact history - first and last contact, how often, who else is on the thread.

Layer: L7

Order 1k section 7d. Local only; reads `messages`. "Contact" is a message the
person sent to the owner or the owner sent to the person (or to them with others).

`refresh_last_contacted` keeps the derived column `contacts.last_contacted_at`
(milliseconds, like the other contact times) in step with the mail. It is a
derived value, not a content change: it writes no journal entry, no `rev`, and
does not mark the contact as edited, so it never triggers a sync push.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from app.contacts.card import Card
from app.contacts.enrich.base import address_of, emails_of, mail_with, recipients_of

__all__ = ["History", "contact_history", "refresh_last_contacted"]


@dataclass
class History:
    contact_id: int
    messages: int = 0
    from_them: int = 0
    to_them: int = 0
    first: Optional[int] = None                          # Unix seconds
    last: Optional[int] = None
    per_month: float = 0.0
    often: str = ""                                      # "about 4 a month", "a few times a year", ...
    with_others: list[tuple[str, int]] = field(default_factory=list)   # (address, messages shared)
    #: Order 1s D1: the start of the age window these counts cover (Unix seconds), or None for any age -
    #: so "first contact" can be said as "first in the last six months" and never mistaken for the first.
    since: Optional[int] = None


def _often(count: int, first: Optional[int], last: Optional[int]) -> str:
    if count <= 0:
        return ""
    if count == 1 or not first or not last or last <= first:
        return "once"
    months = max((last - first) / (30.44 * 86400), 1.0)
    rate = count / months
    if rate >= 8:
        return "most days" if rate >= 20 else "about every week"
    if rate >= 1.5:
        return f"about {round(rate)} a month"
    if rate >= 0.75:
        return "about once a month"
    if rate * 12 >= 3:
        return "a few times a year"
    return "about once a year" if rate * 12 >= 0.75 else "rarely"


def contact_history(store: Any, contact_id: int, *, card: Optional[Card] = None, hub: Any = None,
                    owner_emails: Iterable[str] = (), top: int = 5, since: Optional[int] = None) -> History:
    """Counts, dates and the people usually on the same messages.

    `owner_emails` are the owner's own addresses (the accounts), left out of
    "who else is on the thread" - otherwise the owner tops every list. Pass the
    card, or the hub to read it from.
    """
    if card is None and hub is not None:
        card = hub.get_card(contact_id)
    h = History(contact_id, since=since)
    if card is None:
        return h
    mine = set(emails_of(card))
    if not mine:
        return h
    owner = {e.strip().lower() for e in owner_emails if e}
    others: Counter[str] = Counter()
    dates: list[int] = []
    for m in mail_with(store, sorted(mine), since=since):
        sender = address_of(m.sender)
        rcpts = {address_of(x) for x in m.recipients} - {""}
        if sender in mine:
            h.from_them += 1
        elif rcpts & mine:
            h.to_them += 1
        else:
            continue
        h.messages += 1
        if m.sent_at:
            dates.append(int(m.sent_at))
        for a in ({sender} | rcpts) - mine - owner - {""}:
            others[a] += 1
    if dates:
        h.first, h.last = min(dates), max(dates)
    h.per_month = round(h.messages / max(((h.last - h.first) / (30.44 * 86400)) if h.first and h.last else 1, 1.0), 2) \
        if h.messages else 0.0
    h.often = _often(h.messages, h.first, h.last)
    h.with_others = others.most_common(top)
    return h


def refresh_last_contacted(hub: Any) -> int:
    """Set `contacts.last_contacted_at` from the mail, with one SQL `UPDATE`.

    One pass over `messages` builds a temporary address -> newest-date table;
    a single `UPDATE` then gives every live contact the newest date among its
    email addresses (`contact_fields.norm`). A contact with no mail keeps what
    it had. Returns the number of contacts written.
    """
    store = hub.store
    newest: dict[str, int] = {}
    for r in store.conn.execute("SELECT sender, recipients, sent_at FROM messages WHERE sent_at IS NOT NULL"):
        when = int(r["sent_at"])
        people = {address_of(r["sender"] or "")}
        people |= {address_of(x) for x in recipients_of(r["recipients"])}
        for a in people:
            if a and newest.get(a, -1) < when:
                newest[a] = when
    with store.write() as conn:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS enrich_last (addr TEXT PRIMARY KEY, at INTEGER NOT NULL) "
                     "WITHOUT ROWID")
        conn.execute("DELETE FROM enrich_last")
        conn.executemany("INSERT INTO enrich_last (addr, at) VALUES (?, ?)", list(newest.items()))
        cur = conn.execute(
            "UPDATE contacts SET last_contacted_at = (SELECT MAX(l.at) * 1000 FROM contact_fields f "
            "JOIN enrich_last l ON l.addr = f.norm WHERE f.contact_id = contacts.id AND f.kind = 'email') "
            "WHERE deleted_at IS NULL AND EXISTS (SELECT 1 FROM contact_fields f "
            "JOIN enrich_last l ON l.addr = f.norm WHERE f.contact_id = contacts.id AND f.kind = 'email') "
            "AND last_contacted_at IS NOT (SELECT MAX(l.at) * 1000 FROM contact_fields f "
            "JOIN enrich_last l ON l.addr = f.norm WHERE f.contact_id = contacts.id AND f.kind = 'email')")
        n = cur.rowcount
        conn.execute("DROP TABLE enrich_last")
    return int(n)
