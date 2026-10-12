"""What every enrichment source shares: the proposal, applying it, the mail reader.

Layer: L7

Order 1k section 7. Enrichment **proposes and never applies**: each source
returns `Proposal` objects, the person accepts some, and `apply_proposals` writes
exactly those through `ContactHub.update` as one journal batch, so the whole
acceptance is undone in one step and every accepted value records where it came
from (`hub.sources()`, rule: "every enriched value records its source").

Also here, because three sources need them:

* `GuardedTransport` - wraps any `provider.Transport` and refuses a URL whose
  host is not on an `AllowedHosts` list *before* the inner transport sees it. The
  real `UrllibTransport` already checks; wrapping a test double the same way is
  what lets the tests prove that no other host could have been reached.
* `normalise_image` - bytes in, a small decoded PNG/JPEG out, or None. Nothing
  fetched from the internet is stored unless Pillow can decode it.
* mail readers over the `messages` and `chunks` tables.
"""

from __future__ import annotations

import io
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from email.utils import parseaddr
from typing import Any, Iterable, Optional, Sequence

from app.accounts.http import AllowedHosts
from app.accounts.provider import HttpResponse, Transport
from app.contacts.card import SCALARS, Card, Field, Photo, field_key

__all__ = [
    "Proposal", "ApplyResult", "apply_proposals", "GuardedTransport", "normalise_image",
    "address_of", "emails_of", "recipients_of", "MailRow", "mail_from", "mail_with", "body_text",
    "format_day", "SOURCE_PREFIX", "proposal_key",
]

#: `contact_sources.source` of an accepted value is this plus the proposal's `source`.
SOURCE_PREFIX = "enrichment:"


@dataclass
class Proposal:
    """One value an enrichment source offers. Nothing is changed by making one.

    `field_kind` is a scalar name from `card.SCALARS` (`company`, `title`), a
    repeatable kind from `card.KINDS` (`phone`, `url`, `address`, `custom`) or
    `photo`. `old` is what the card holds now ("" when nothing); `new` is the text
    offered, or a `Photo` for a photo. `source` is the short kind (`signature`,
    `company`, `face`, `gravatar`, `logo`, `wikidata`), `receipt` what the
    person is shown as proof (a message's subject and date, or a URL).
    `data` carries an address's parts.
    """

    contact_id: int
    field_kind: str
    field_label: str
    old: str
    new: Any
    source: str
    receipt: str
    confidence: float = 0.5
    data: dict[str, str] = field(default_factory=dict)
    #: Order 1s 2d: when the message (or file) the value came from was sent, Unix seconds; None when it
    #: is not from a dated source.
    when: Optional[int] = None
    #: Order 1s 3j: how many messages agreed on this value, and when the first of them was sent.
    seen: int = 1
    first_seen: Optional[int] = None
    #: 3j: older values that disagreed, in words ("was Senior Engineer until March 2026"). Shown, never offered.
    history: str = ""
    #: 3k: this replaces a value the card already holds (`old`). The review never ticks it by itself (D4),
    #: whatever its confidence: a change a person did not look at is the one that does harm.
    update: bool = False

    @property
    def is_scalar(self) -> bool:
        return self.field_kind in SCALARS

    @property
    def is_photo(self) -> bool:
        return self.field_kind == "photo"

    def as_field(self) -> Field:
        return Field(self.field_kind, str(self.new), self.field_label, dict(self.data))

    def key(self) -> str:
        """The `contact_sources.field_key` this proposal would own once accepted."""
        return proposal_key(self)


def proposal_key(p: Proposal, region: str = "GB") -> str:
    if p.is_photo:
        return "photo"
    if p.is_scalar:
        return f"s:{p.field_kind}"
    return "f:" + field_key(p.as_field(), region)


@dataclass
class ApplyResult:
    batch_id: str = ""
    applied: list[int] = field(default_factory=list)       # indexes into the proposals list
    skipped: list[int] = field(default_factory=list)       # accepted, but nothing to do (already there)
    changed_contacts: list[int] = field(default_factory=list)


def apply_proposals(hub: Any, proposals: Sequence[Proposal], accepted_indexes: Iterable[int],
                    actor: str = "enrich") -> ApplyResult:
    """Write the accepted proposals - and only those - as one journal batch.

    Each accepted proposal is written with `source='enrichment:<kind>'` and its
    receipt as `source_ref`. For one contact and one place (a scalar, or the
    photo) only the first accepted proposal is used. A value already on the card
    is skipped, never duplicated. Returns which indexes were applied.
    """
    result = ApplyResult(batch_id=uuid.uuid4().hex)
    wanted = sorted({int(i) for i in accepted_indexes if 0 <= int(i) < len(proposals)})
    by_contact: dict[int, list[int]] = {}
    for i in wanted:
        by_contact.setdefault(proposals[i].contact_id, []).append(i)
    for cid, idxs in by_contact.items():
        card = hub.get_card(cid)
        if card is None:
            result.skipped.extend(idxs)
            continue
        taken: set[str] = set()
        done: list[int] = []
        for i in idxs:
            p = proposals[i]
            slot = "photo" if p.is_photo else (f"s:{p.field_kind}" if p.is_scalar else "")
            if slot and slot in taken:
                result.skipped.append(i)
                continue
            if _put(card, p, hub.region):
                done.append(i)
                if slot:
                    taken.add(slot)
            else:
                result.skipped.append(i)
        if not done:
            continue
        # One hub.update per source so each value keeps its own source and receipt.
        # `hub.update(source=...)` stamps every value then on the card, so the rows
        # it over-stamps are removed again below; only what was proposed keeps the source.
        for kind, group in _by_source(proposals, done):
            before_max = _max_source_id(hub, cid)
            card_now = hub.get_card(cid)
            for i in group:
                _put(card_now, proposals[i], hub.region)
            receipt = _joined_receipt(proposals, group)
            hub.update(cid, card_now, actor=actor, action="enrich", batch_id=result.batch_id,
                       source=SOURCE_PREFIX + kind, source_ref=receipt)
            _fix_sources(hub, cid, SOURCE_PREFIX + kind, before_max,
                         {proposal_key(proposals[i], hub.region) for i in group},
                         [proposals[i] for i in group if proposals[i].is_photo])
        result.applied.extend(done)
        result.changed_contacts.append(cid)
    result.applied.sort()
    return result


def _by_source(proposals: Sequence[Proposal], idxs: Sequence[int]) -> list[tuple[str, list[int]]]:
    order: dict[str, list[int]] = {}
    for i in idxs:
        order.setdefault(proposals[i].source, []).append(i)
    return list(order.items())


def _joined_receipt(proposals: Sequence[Proposal], group: Sequence[int]) -> str:
    seen: list[str] = []
    for i in group:
        r = proposals[i].receipt
        if r and r not in seen:
            seen.append(r)
    return "; ".join(seen)[:500]


def _put(card: Card, p: Proposal, region: str) -> bool:
    """Put one proposal into `card`. False when the card already holds it."""
    if p.is_photo:
        if not isinstance(p.new, Photo) or (card.photo is not None and card.photo.sha == p.new.sha):
            return False
        card.photo = p.new
        return True
    if p.is_scalar:
        text = str(p.new).strip()
        if not text or card.get(p.field_kind).strip() == text:
            return False
        card.scalars[p.field_kind] = text
        return True
    f = p.as_field()
    if not f.value.strip() and not any((f.data or {}).values()):
        return False
    if any(field_key(x, region) == field_key(f, region) for x in card.fields):
        return False
    card.fields.append(f)
    return True


def _max_source_id(hub: Any, cid: int) -> int:
    row = hub.conn.execute("SELECT COALESCE(MAX(id), 0) FROM contact_sources WHERE contact_id = ?",
                           (cid,)).fetchone()
    return int(row[0])


def _fix_sources(hub: Any, cid: int, source: str, after_id: int, keep_keys: set[str],
                 photos: list[Proposal]) -> None:
    """Make `contact_sources` say what is true after an enrichment write.

    `ContactHub.update(source=...)` records the source against every value on the
    card, including ones the person typed years ago. For an enrichment that is
    a false receipt, so the rows this write just added for any other value are
    removed, and a photo (which the hub does not key) gets its own row.
    """
    with hub.store.write() as conn:
        rows = conn.execute("SELECT id, field_key FROM contact_sources WHERE contact_id = ? AND source = ? "
                            "AND id > ?", (cid, source, after_id)).fetchall()
        for r in rows:
            if r["field_key"] not in keep_keys:
                conn.execute("DELETE FROM contact_sources WHERE id = ?", (r["id"],))
        if photos and "photo" in keep_keys:
            have = conn.execute("SELECT 1 FROM contact_sources WHERE contact_id = ? AND field_key = 'photo' "
                                "AND source = ?", (cid, source)).fetchone()
            if not have:
                conn.execute("INSERT INTO contact_sources (contact_id, field_key, source, ref, at) "
                             "VALUES (?,?,?,?,?)", (cid, "photo", source, photos[0].receipt[:500], int(time.time())))


# --------------------------------------------------------------------------
# The network guard
# --------------------------------------------------------------------------

class GuardedTransport:
    """Any `Transport`, behind an `AllowedHosts` check that runs first.

    The check is `AllowedHosts.check`: a host not on the list, a non-https URL
    and a non-http(s) scheme all raise `ERR_ACCOUNT_UNREACHABLE` before a request
    is made. `calls` records what was asked, for tests.
    """

    def __init__(self, inner: Transport, allowed: AllowedHosts) -> None:
        self.inner = inner
        self.allowed = allowed
        self.calls: list[str] = []

    def request(self, method: str, url: str, *, headers: Optional[dict[str, str]] = None,
                body: Optional[bytes] = None, timeout: float = 30.0) -> HttpResponse:
        self.allowed.check(url)
        self.calls.append(url)
        return self.inner.request(method, url, headers=headers, body=body, timeout=timeout)


# --------------------------------------------------------------------------
# Images from the internet
# --------------------------------------------------------------------------

MAX_IMAGE_BYTES = 512 * 1024


def normalise_image(data: bytes, *, edge: int = 256, mime: str = "image/png") -> Optional[Photo]:
    """A small decoded image as a `Photo`, or None when `data` is not a real picture.

    Anything fetched is decoded by Pillow and re-encoded, so what is kept is
    always a plain PNG of at most `edge` pixels, whatever the server sent.
    """
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None
    try:
        from PIL import Image  # noqa: PLC0415
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            im = im.convert("RGBA") if mime == "image/png" else im.convert("RGB")
            im.thumbnail((edge, edge))
            out = io.BytesIO()
            im.save(out, "PNG" if mime == "image/png" else "JPEG", **({} if mime == "image/png" else {"quality": 85}))
    except Exception:                                    # noqa: BLE001 - not a picture, or too odd to trust
        return None
    return Photo(mime, out.getvalue())


# --------------------------------------------------------------------------
# Reading mail from the index
# --------------------------------------------------------------------------

def address_of(text: str) -> str:
    """The lower-cased address in `Dave Smith <dave@x.com>`, or `""`."""
    addr = parseaddr(text or "")[1].strip().lower()
    return addr if "@" in addr else ""


def emails_of(card: Card) -> list[str]:
    seen: list[str] = []
    for f in card.values("email"):
        a = f.value.strip().lower()
        if "@" in a and a not in seen:
            seen.append(a)
    return seen


@dataclass(frozen=True)
class MailRow:
    file_id: int
    subject: str
    sender: str
    recipients: tuple[str, ...]
    sent_at: Optional[int]                               # Unix seconds, as the index stores it


def recipients_of(raw: Any) -> tuple[str, ...]:
    if not raw:
        return ()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return tuple(x for x in re.split(r"[;,]", str(raw)) if x.strip())
    if isinstance(data, list):
        return tuple(str(x) for x in data)
    return (str(data),)


def _row(r: Any) -> MailRow:
    return MailRow(int(r["file_id"]), r["subject"] or "", r["sender"] or "", recipients_of(r["recipients"]),
                   r["sent_at"])


def _has_sender_addr(store: Any) -> bool:
    """Schema 41's indexed `sender_addr` is there (it is not on an index measured read-only before it)."""
    known = getattr(store, "_enrich_sender_addr", None)
    if known is None:
        known = any(r[1] == "sender_addr" for r in store.conn.execute("PRAGMA table_info(messages)"))
        try:
            store._enrich_sender_addr = known
        except AttributeError:                               # a store that takes no attributes asks each time
            pass
    return known


def mail_from(store: Any, emails: Sequence[str], *, limit: int = 50, since: Optional[int] = None) -> list[MailRow]:
    """Messages whose sender is one of `emails`, newest first (undated last).

    `since` (Unix seconds) is order 1s's age window, D1: a message sent before it is not read, and an
    undated one is outside every window but "any age" (`since=None`).

    Order 1s 1c: one indexed look-up per address on `sender_addr` (schema 41), where it was a scan of
    every message - about a second a contact on the owner's index. An index without the column (one
    measured read-only before the migration) is still read the old way, so the answer is the same."""
    out: dict[int, MailRow] = {}
    wanted = {e.lower() for e in emails if e}
    indexed = _has_sender_addr(store)
    for e in wanted:
        sql = ("SELECT file_id, subject, sender, recipients, sent_at FROM messages "
               + ("WHERE sender_addr = ? " if indexed else "WHERE instr(lower(sender), ?) > 0 ")
               + ("AND sent_at >= ? " if since is not None else "")
               + "ORDER BY sent_at DESC LIMIT ?")
        params = (e, *(() if since is None else (int(since),)), int(limit) * 4)
        for r in store.conn.execute(sql, params):
            if address_of(r["sender"] or "") in wanted:      # 'joann@x.com' is not 'ann@x.com'
                out[int(r["file_id"])] = _row(r)
    rows = sorted(out.values(), key=lambda m: (m.sent_at is None, -(m.sent_at or 0), m.file_id))
    return rows[:limit]


def mail_with(store: Any, emails: Sequence[str], *, limit: int = 5000,
              since: Optional[int] = None) -> list[MailRow]:
    """Messages from or to any of `emails`, newest first. `since`: the age window, as `mail_from`."""
    out: dict[int, MailRow] = {}
    wanted = {e.lower() for e in emails if e}
    window = "" if since is None else "AND sent_at >= ? "
    for e in wanted:
        params = (e, e, *(() if since is None else (int(since),)), int(limit) * 2)
        for r in store.conn.execute(
                "SELECT file_id, subject, sender, recipients, sent_at FROM messages "
                "WHERE (instr(lower(sender), ?) > 0 OR instr(lower(recipients), ?) > 0) "
                + window + "ORDER BY sent_at DESC LIMIT ?", params):
            row = _row(r)
            people = {address_of(row.sender), *(address_of(x) for x in row.recipients)}
            if people & wanted:
                out[row.file_id] = row
    rows = sorted(out.values(), key=lambda m: (m.sent_at is None, -(m.sent_at or 0), m.file_id))
    return rows[:limit]


def body_text(store: Any, file_id: int) -> str:
    """The indexed body of one message: its passages in order, without the header block.

    The index keeps what `strip_quoted` left, so a signature after a `-- ` line or a
    "Sent from my ..." line is *not* here (it was cut when the message was indexed);
    one written under a sign-off such as "Kind regards" is.
    """
    parts: list[str] = []
    for r in store.conn.execute("SELECT ordinal, text, label FROM chunks WHERE file_id = ? ORDER BY ordinal",
                                (int(file_id),)):
        text = r["text"] or ""
        label = (r["label"] or "") if "label" in r.keys() else ""
        if label == "Headers" or (not label and r["ordinal"] == 0 and text.startswith("Subject:")
                                  and "\nFrom:" in text[:600]):
            continue
        parts.append(text)
    return "\n".join(parts)


def full_text(store: Any, file_id: int, *, read_original: bool = False) -> str:
    """Everything enrichment may read of one message: the indexed body, then the tail the index cut.

    Order 1s section 4. The signature reader (section 3) reads this, never `body_text` directly, so
    a message's cut-off signature (4a, `message_tails`) and, when the person switched it on, the
    original message read again (4b, `read_original`) reach it without the reader knowing where the
    text came from.

    **Per message.** It reads one message; how many a contact's are read is the caller's choice - the
    order's cap for `read_original` is the newest five, because each one opens a file.

    * A kept tail: the indexed body, a newline, the tail. Nothing is opened.
    * No tail and `read_original`: the original's whole body, nothing cut (`app.extract.reread`), read
      only when the index says something was cut (`messages.quoted_removed` is not 0) - opened
      read-only. Where it is not opened (live account mail, an Outlook-read message, a member of an
      mbox) or cannot be, the indexed body.
    * Otherwise the indexed body, as `body_text`.

    The tail and the original may hold a quoted chain - other people's messages and signatures.
    `message_tail(...)[1]` says which the tail was ("signature: ..." or "quoted: ...")."""
    body = body_text(store, file_id)
    tail = _tail(store, file_id)
    if tail:
        return f"{body}\n{tail}" if body else tail
    if read_original:
        place = _place(store, file_id)
        if place is not None and place.get("quoted_removed") != 0:
            from app.extract import reread  # noqa: PLC0415 - only a re-read needs the readers

            original = reread.original_body(place)
            if original:
                return original
    return body


def _tail(store: Any, file_id: int) -> str:
    """The message's kept tail, or "" - also on an index from before schema 42, which `--measure`
    reads read-only and so never migrates."""
    import sqlite3  # noqa: PLC0415

    try:
        found = store.message_tail(file_id)
    except (sqlite3.OperationalError, AttributeError):
        return ""
    return found[0] if found else ""


def _place(store: Any, file_id: int) -> Optional[dict]:
    """Where a message can be read again: its `files` and `messages` columns, or None."""
    row = store.conn.execute(
        "SELECT f.path, f.source_kind, m.store_path, m.entry_id, m.folder_path, m.folder_index, "
        "m.quoted_removed FROM files f LEFT JOIN messages m ON m.file_id = f.id WHERE f.id = ?",
        (int(file_id),)).fetchone()
    return dict(row) if row is not None else None


def window_start(months: int, today: Any = None) -> Optional[int]:
    """Unix seconds at the start of the age window (order 1s D1): `months` calendar months before today, at
    midnight UTC, or None for any age. Calendar months, so six months before 12 October is 12 April."""
    import calendar  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415

    if not months:
        return None
    t = today or datetime.now(timezone.utc).date()
    y, m = divmod(t.month - 1 - int(months), 12)
    year, month = t.year + y, m + 1
    day = min(t.day, calendar.monthrange(year, month)[1])
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


def has_mail_before(store: Any, emails: Sequence[str], since: int) -> bool:
    """Does any message from one of `emails` predate the window? One indexed look-up an address (2c)."""
    for e in {x.lower() for x in emails if x}:
        if _has_sender_addr(store):
            sql = "SELECT sender FROM messages WHERE sender_addr = ? AND sent_at < ? LIMIT 1"
        else:
            sql = "SELECT sender FROM messages WHERE instr(lower(sender), ?) > 0 AND sent_at < ? LIMIT 20"
        for r in store.conn.execute(sql, (e, int(since))):
            if address_of(r["sender"] or "") == e:
                return True
    return False


def format_day(epoch_s: Optional[int]) -> str:
    if not epoch_s:
        return ""
    return time.strftime("%Y-%m-%d", time.gmtime(int(epoch_s)))
