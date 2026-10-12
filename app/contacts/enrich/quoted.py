"""Order 1s 4c: signatures quoted inside other people's messages.

Layer: L7

Most of the people in the owner's book never mailed the owner directly (453 of the
979 with an address, the order's opening): they are in threads the owner was copied
on, and their signature is in the quoted chain of somebody else's reply. This reads
those chains - from `base.full_text`, so from the cut tail (4a) or the original
message (4b) once section 4 fills them in - and gives back each quoted message with
its own From address and Sent date.

**Credit is narrow.** A quoted block belongs only to the address in its *own* quoted
From line (never to the message's sender, never to anyone else on the thread), only
when its quoted Sent date is inside the age window (D1; an undated one only with Any
age), and the consensus (3j) takes 0.15 off a value seen only this way. A From line
with no address (Exchange's "From: Ann Lee") credits nobody.

**Which messages are read, cheaply.** A reply that quotes a person almost always
still has them on To or Cc (reply-all). So the candidates are the messages that list
one of the person's addresses among their *recipients*, newest first, inside the
window - found through `messages_fts`, the trigram index the Search tab already keeps
over `recipients`, so the look-up is indexed rather than a scan of every message.
An index built without that table (an old SQLite) falls back to a scan of
`recipients_lc` limited by the window. At most `QUOTED_MESSAGES` are read a contact.
A forward that dropped the person from the recipients is not found this way.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Iterable, Optional, Sequence

from app.contacts.enrich import base

__all__ = ["QuotedMessage", "quoted_messages", "parse_sent", "mail_quoting", "QUOTED_MESSAGES"]

#: How many messages that may quote a person are read for them, newest first. As `MAX_MESSAGES` for
#: direct mail: enough for the consensus to agree, few enough to stay well under 0.15 s a contact.
QUOTED_MESSAGES = 20

_HEADER = re.compile(
    r"^[ \t>]*From:[ \t]*(?P<from>[^\n]+)\n(?P<rest>(?:[ \t>]*(?:Sent|Date|To|Cc|Bcc|Subject|Importance|"
    r"Reply-To)[ \t]*:[^\n]*(?:\n|$)){1,7})", re.M | re.I)
_SENT = re.compile(r"^[ \t>]*(?:Sent|Date)[ \t]*:[ \t]*(?P<when>[^\n]+)", re.M | re.I)
_BRACKET_ADDR = re.compile(r"[\[<](?:mailto:)?(?P<addr>[^\s<>\[\]@]+@[^\s<>\[\]]+)[\]>]", re.I)
_BARE_ADDR = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_FORMATS = ("%d %B %Y %H:%M", "%d %B %Y %I:%M %p", "%B %d, %Y %I:%M %p", "%B %d, %Y %H:%M", "%d %b %Y %H:%M",
            "%b %d, %Y %I:%M %p", "%d %B %Y %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d %B %Y",
            "%B %d, %Y")


class QuotedMessage:
    """One quoted message: its sender's address, its Sent date (Unix seconds, UTC; None when unreadable)
    and its text (quote marks removed, its own quoted chain still in it - `sigtail.own_part` cuts that)."""

    def __init__(self, address: str, sent_at: Optional[int], text: str) -> None:
        self.address, self.sent_at, self.text = address, sent_at, text


def parse_sent(text: str) -> Optional[int]:
    """A quoted Sent/Date line as Unix seconds. A date with no zone is taken as UTC: Outlook writes the
    reader's local time, so it can be hours out - immaterial to a window measured in months."""
    t = re.sub(r"^\s*[A-Za-z]+,\s*", "", (text or "").strip())      # the weekday
    t = re.sub(r"\s+at\s+", " ", t).strip()
    for fmt in _FORMATS:
        try:
            return int(datetime.strptime(t, fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    try:
        dt = parsedate_to_datetime(text.strip())
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp())


def _address(from_line: str) -> str:
    m = _BRACKET_ADDR.search(from_line)
    if m:
        return m.group("addr").strip().lower()
    addr = parseaddr(from_line)[1].strip().lower()
    if "@" in addr:
        return addr
    bare = _BARE_ADDR.search(from_line)
    return bare.group(0).lower() if bare else ""


def quoted_messages(text: str) -> list[QuotedMessage]:
    """Every quoted message in `text` that has an Outlook-style header block, in order."""
    heads = list(_HEADER.finditer(text or ""))
    out: list[QuotedMessage] = []
    for n, m in enumerate(heads):
        end = heads[n + 1].start() if n + 1 < len(heads) else len(text)
        body = text[m.end():end]
        body = "\n".join(re.sub(r"^[ \t]*>+ ?", "", ln) for ln in body.splitlines())
        sent = _SENT.search(m.group("rest"))
        out.append(QuotedMessage(_address(m.group("from")), parse_sent(sent.group("when")) if sent else None, body))
    return out


def _has_messages_fts(store: Any) -> bool:
    known = getattr(store, "_enrich_messages_fts", None)
    if known is None:
        known = store.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'messages_fts'").fetchone() is not None
        try:
            store._enrich_messages_fts = known
        except AttributeError:
            pass
    return known


def mail_quoting(store: Any, emails: Sequence[str], *, since: Optional[int] = None,
                 limit: int = QUOTED_MESSAGES) -> list[base.MailRow]:
    """Messages *to* one of `emails` and not from them, newest first, inside the window (see the docstring)."""
    wanted = {e.lower() for e in emails if e}
    window = "" if since is None else "AND m.sent_at >= ? "
    out: dict[int, base.MailRow] = {}
    for e in wanted:
        tail = (*(() if since is None else (int(since),)), int(limit) * 2)
        rows: Iterable[Any] = ()
        indexed = _has_messages_fts(store)
        if indexed:
            try:
                rows = store.conn.execute(
                    "SELECT m.file_id, m.subject, m.sender, m.recipients, m.sent_at FROM messages_fts "
                    "JOIN messages m ON m.file_id = messages_fts.rowid WHERE messages_fts MATCH ? "
                    + window + "ORDER BY m.sent_at DESC LIMIT ?",
                    ('recipients : "' + e.replace('"', '""') + '"', *tail)).fetchall()
            except sqlite3.OperationalError:              # an index the trigram table cannot read
                indexed = False
        if not indexed:
            rows = store.conn.execute(
                "SELECT m.file_id, m.subject, m.sender, m.recipients, m.sent_at FROM messages m "
                "WHERE instr(m.recipients_lc, ?) > 0 " + window + "ORDER BY m.sent_at DESC LIMIT ?",
                (e, *tail)).fetchall()
        for r in rows:
            row = base._row(r)
            if base.address_of(row.sender) not in wanted:
                out[row.file_id] = row
    rows_out = sorted(out.values(), key=lambda m: (m.sent_at is None, -(m.sent_at or 0), m.file_id))
    return rows_out[:limit]
