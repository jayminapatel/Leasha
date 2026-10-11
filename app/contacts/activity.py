"""What the index holds about one contact: mail, files exchanged, photos - and who a query names.

Layer: L7

Order 1k 6f and 8b. `contact_activity` (the MCP tool) and the Contacts page's "From your
index" panel read the same facts, so the reading is written once, here, as plain functions of
an open `SqliteStore`. Nothing here imports Qt and nothing here writes (rule 8: a headless
caller can use it; `tests/unit/test_mcp_contacts_readonly.py` holds the tool to reads).

Mail is matched on the contact's email addresses, in the message's sender and recipients, by
the Mail tab's own read (`browse_messages`). Photos are those of the Photo Tagger person the
contact is *accepted* as (`contact_pile_links.state = 'accepted'`): a suggestion is the
program's guess, not the person's word.

`strong_matches` is the Search page's bar for showing a contact card above the results: the
whole query equals a contact's name, begins it, or is one of their email addresses or phone
numbers exactly.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Optional

__all__ = ["ACCOUNT_SCHEME", "RECIPIENTS_CHARS", "when", "account_of_path", "mail_card",
           "is_address_in", "messages", "message_entry", "mail_with", "files_exchanged",
           "photos_of", "gather", "strong_matches", "MIN_PREFIX_CHARS"]

#: The longest recipients line a message is answered with.
RECIPIENTS_CHARS = 300

#: Where a message fetched from an account lives in the index (order 1k 5d).
ACCOUNT_SCHEME = "account://"

#: The shortest start of a name that may stand for the whole name in Search. Two letters
#: would put "Jo" and "Al" above half the book's searches; the whole name is always enough.
MIN_PREFIX_CHARS = 3


def when(seconds: Any) -> str:
    """A local date and time an AI program can read and compare, or `""`."""
    from datetime import datetime

    try:
        value = float(seconds or 0)
        return datetime.fromtimestamp(value).isoformat(timespec="minutes") if value > 0 else ""
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def account_of_path(path: Any, accounts: Optional[dict]) -> str:
    """The name of the account an `account://` path belongs to, or "" for any other path."""
    text = str(path or "")
    if not text.startswith(ACCOUNT_SCHEME):
        return ""
    from urllib.parse import unquote

    try:
        ident = unquote(text[len(ACCOUNT_SCHEME):].split("/", 1)[0])      # also a file attached to the message
        return str((accounts or {}).get(int(ident)) or "an account that is no longer set up")
    except (ValueError, TypeError):
        return ""


def mail_card(detail: Any, path: str, accounts: Optional[dict] = None) -> Optional[dict]:
    """A `mail_details` entry as the tools say it, or None for a non-message.

    *Order 1k 8a:* a message fetched from an account also says which one (`account`, its
    name), beside the sender and date. `accounts` is `{account id: name}`.
    """
    from app.search.marks import ATTACHMENT_MARKER

    if not detail:
        return None
    mail = {"subject": detail.get("subject") or "", "from": detail.get("sender") or "",
            "sent": when(detail.get("sent_at"))}
    if detail.get("attachment_of"):
        text = str(path or "")
        mail["attachment"] = text[text.find(ATTACHMENT_MARKER) + len(ATTACHMENT_MARKER):]
    named = account_of_path(path, accounts)
    if named:
        mail["account"] = named
    return mail


def is_address_in(text: Any, email: str) -> bool:
    """`email` as a whole address in `text` - `ann@x.com`, not `joann@x.com`."""
    pattern = r"(?<![\w.+%-])" + re.escape(email) + r"(?![\w-]|\.\w)"
    return re.search(pattern, str(text or ""), re.IGNORECASE) is not None


def messages(store: Any, emails: list[str], limit: int, *, attachments: bool = False,
             oldest: bool = False) -> list[dict]:
    """Messages from or to any of `emails`, newest first (oldest first for
    `oldest`), through the Mail tab's own read (`browse_messages`)."""
    rows: dict[int, dict] = {}
    fetch = 25 if oldest else min(limit * 4, 200)
    for email in emails[:10]:
        for side in ("sender", "recipient"):
            found = store.browse_messages(
                **{side: email}, has_attachment=True if attachments else None,
                sort="oldest" if oldest else "", limit=fetch)
            for row in found:
                if is_address_in(row.get(side if side == "sender" else "recipients"), email):
                    rows.setdefault(int(row["file_id"]), {**row, "_email": email, "_side": side})
    ordered = sorted(rows.values(), key=lambda r: (r.get("sent_at") is None, r.get("sent_at") or 0,
                                                   int(r["file_id"])), reverse=False)
    dated = [r for r in ordered if r.get("sent_at") is not None]
    undated = [r for r in ordered if r.get("sent_at") is None]
    dated = dated if oldest else dated[::-1]
    return (dated + undated)[:limit]


def message_entry(row: dict, accounts: Optional[dict] = None) -> dict:
    """One message as `contact_activity` says it: the `mail_card` fields, the
    path to give `read_text`, and whether it came from them or went to them."""
    card = mail_card({"subject": row.get("subject"), "sender": row.get("sender"),
                      "sent_at": row.get("sent_at")}, str(row.get("path") or ""), accounts) or {}
    recipients = str(row.get("recipients") or "")
    return {"path": str(row.get("path") or ""), **card,
            "to": recipients if len(recipients) <= RECIPIENTS_CHARS
            else recipients[:RECIPIENTS_CHARS - 1] + "…",
            "direction": "from them" if row.get("_side") == "sender" else "to them"}


def mail_with(store: Any, emails: list[str], limit: int, accounts: Optional[dict] = None) -> dict:
    """The latest mail with these addresses, and the first and last contact."""
    if not emails:
        return {"messages": [], "first_contact": "", "last_contact": "",
                "note": "This contact has no email address, so no mail can be matched."}
    latest = messages(store, emails, limit)
    first = messages(store, emails, 1, oldest=True)
    stamps = [r["sent_at"] for r in latest + first if r.get("sent_at")]
    return {"messages": [message_entry(r, accounts) for r in latest],
            "first_contact": when(min(stamps)) if stamps else "",
            "last_contact": when(max(stamps)) if stamps else ""}


def files_exchanged(store: Any, emails: list[str], limit: int,
                    accounts: Optional[dict] = None) -> list[dict]:
    """Files attached to messages with these addresses: each with the message it came on."""
    from app.core.row_facts import attachment_of

    out: list[dict] = []
    if not emails:
        return out
    for row in messages(store, emails, limit, attachments=True):
        message_path = str(row.get("path") or "")
        for file_id in store.file_ids_at_or_under(message_path):
            record = store.get_file_by_id(file_id)
            if record is None or attachment_of(record.path)[0] != message_path:
                continue
            out.append({"path": record.path, "name": attachment_of(record.path)[1],
                        "type": record.ext, "size_bytes": record.size_bytes,
                        "message": message_entry(row, accounts)})
            if len(out) >= limit:
                return out
    return out


def photos_of(store: Any, contact_id: int, limit: int) -> tuple[str, list[dict]]:
    """`(how the contact is matched to a Photo Tagger person, their photos)`.

    The match is `accepted`, `suggested` or `none`; only an accepted one lists
    photos - a suggestion is the program's guess, not the person's word.
    """
    from app.core.row_facts import shown_date_ns

    try:
        link = store.conn.execute(
            "SELECT l.pile_id, l.state, p.name FROM contact_pile_links l "
            "JOIN piles p ON p.id = l.pile_id WHERE l.contact_id = ?", (contact_id,)).fetchone()
    except sqlite3.Error:
        return "none", []
    if link is None or link["state"] == "declined":
        return "none", []
    if link["state"] != "accepted":
        return "suggested", []
    ids = [int(r[0]) for r in store.conn.execute(
        "SELECT DISTINCT file_id FROM faces WHERE pile_id = ? ORDER BY file_id DESC LIMIT ?",
        (int(link["pile_id"]), min(limit * 4, 100)))]
    photos = []
    for file_id in ids:
        record = store.get_file_by_id(file_id)
        if record is None:
            continue
        when_ns = shown_date_ns(mtime_ns=record.mtime_ns, taken_at_ns=record.taken_at_ns, sent_at=None)
        photos.append((when_ns or 0, {"path": record.path,
                                      "taken": when(when_ns / 1e9) if when_ns else "",
                                      "person": link["name"] or ""}))
    photos.sort(key=lambda item: item[0], reverse=True)
    return "accepted", [item[1] for item in photos[:limit]]


def gather(store: Any, contact_id: int, emails: list[str], limit: int = 10,
           accounts: Optional[dict] = None) -> dict:
    """Everything the "From your index" panel shows for one contact, in one read (a worker's).

    `accounts` is `{account id: name}`, so a message from a live account names it. Each message
    also carries `sent_at` (seconds) so the panel can write the date as the person's locale does.
    """
    if emails:
        latest = messages(store, emails, limit)
        first = messages(store, emails, 1, oldest=True)
        stamps = [r["sent_at"] for r in latest + first if r.get("sent_at")]
        mail = {"messages": [{**message_entry(r, accounts), "sent_at": r.get("sent_at")} for r in latest],
                "first_contact": when(min(stamps)) if stamps else "",
                "last_contact": when(max(stamps)) if stamps else "",
                "first_at": min(stamps) if stamps else None, "last_at": max(stamps) if stamps else None}
    else:
        mail = {**mail_with(store, emails, limit, accounts), "first_at": None, "last_at": None}
    files = files_exchanged(store, emails, limit, accounts)
    link, photos = photos_of(store, contact_id, limit)
    return {"contact_id": contact_id, "emails": list(emails), **mail,
            "files_exchanged": files, "photo_match": link, "photos": photos}


# -- who a Search query names ---------------------------------------------------------------

def _fold(text: Any) -> str:
    return " ".join(str(text or "").casefold().split())


def strong_matches(hub: Any, query: str, limit: int = 2) -> list[Any]:
    """The contacts a Search query *names*, as `ContactRow`s, best first, at most `limit`.

    A high bar, because the card sits above every result and a wrong one is noise: the query
    is an email address or a phone number held by a contact exactly, or it equals a contact's
    display name, or (three letters or more) it is the start of one. A query that merely
    contains a name, or a word from a contact's company or notes, matches nothing here.
    Reads only; an empty book, an unreadable table or a blank query gives `[]`.
    """
    text = " ".join(str(query or "").split())
    if not text:
        return []
    try:
        if "@" in text and " " not in text:
            ids = hub.find_by_email(text)
            return _live(hub, ids, limit)
        if sum(c.isdigit() for c in text) >= 5 and not any(c.isalpha() for c in text):
            return _live(hub, hub.find_by_phone(text), limit)
        key = _fold(text)
        if len(key) < MIN_PREFIX_CHARS:
            return []
        exact: list[Any] = []
        prefix: list[Any] = []
        for hit in hub.search(text, limit=25):
            name = _fold(hit.display_name)
            if name == key:
                exact.append(hit)
            elif name.startswith(key):
                prefix.append(hit)
        prefix.sort(key=lambda h: (len(h.display_name), h.id))
        return _live(hub, [h.id for h in exact + prefix], limit)
    except sqlite3.Error:
        return []


def _live(hub: Any, ids: list[int], limit: int) -> list[Any]:
    out = []
    for cid in ids:
        row = hub.get_row(cid)
        if row is not None and row.deleted_at is None:
            out.append(row)
            if len(out) >= limit:
                break
    return out
