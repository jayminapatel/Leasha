"""What the person told enrichment never to suggest again (order 1s 7c, D5).

Layer: L7

"Never suggest these" in Enrich contacts records `(contact, field key, value hash)` in
`enrich_declines` (schema 43). `base.drop_declined` - the one call the run makes - then leaves
those values out, whichever source found them, so a signature and Wikidata offering the same
title are both held back.

**The field key is the slot, never the value**: `s:title`, `f:phone`, `photo`. `card.field_key`
would not do, because for an email it *is* the address.

**The value hash is a salted SHA-256** of the slot and the normalised value. Normalised: a scalar
by its words in any case and spacing, a repeatable value by `card.field_key` (an address in any
letter case, a phone number in any format), a photo by its own hash. Salted with 16 random bytes
made the first time anything is turned down and kept in `index_state` (`SALT_KEY`): a plain
SHA-256 of an email address is the Gravatar key and is found by any list of addresses, which
would make "never reversible" untrue. The salt lives in the same file, so it stops a ready-made
table, not someone holding the file and guessing one address at a time - nothing that can be
compared locally can.

**Looking writes nothing.** Before anything is turned down there is no salt, and `drop_declined`
returns at once without making one (order 1s 9d: a run leaves the book byte-for-byte the same).
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Sequence

from app.contacts.card import SCALARS, field_key

__all__ = ["SALT_KEY", "Declined", "slot_of", "value_hash", "decline", "drop", "for_contact", "undo", "count",
           "clear"]

#: `index_state` key of the salt. Clearing the declines keeps it: nothing else is hashed with it.
SALT_KEY = "enrich:decline_salt"


@dataclass(frozen=True)
class Declined:
    """One row of "Suggestions you turned down", for the contact's page."""

    contact_id: int
    field_key: str
    value_hash: str
    label: str
    shown: str                                           # "" for an email address: it is not kept
    source: str
    at: int


def _has_table(store: Any) -> bool:
    try:
        return store.conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' "
                                  "AND name = 'enrich_declines'").fetchone() is not None
    except Exception:                                    # noqa: BLE001 - a store that cannot say has none
        return False


def slot_of(p: Any) -> str:
    kind = str(getattr(p, "field_kind", "") or "")
    if kind == "photo":
        return "photo"
    return f"s:{kind}" if kind in SCALARS else f"f:{kind}"


def _normal(p: Any, region: str) -> str:
    kind = str(getattr(p, "field_kind", "") or "")
    new = getattr(p, "new", "")
    if kind == "photo":
        return str(getattr(new, "sha", "") or "")
    if kind in SCALARS:
        return " ".join(str(new or "").split()).casefold()
    return field_key(p.as_field(), region) if hasattr(p, "as_field") else f"{kind}:{str(new).strip().casefold()}"


def value_hash(salt: str, p: Any, region: str = "GB") -> str:
    data = f"{salt}\0{slot_of(p)}\0{_normal(p, region)}".encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _shown(p: Any) -> str:
    """What the contact's page lists. Nothing for an email address (the reason the value is hashed)."""
    kind = str(getattr(p, "field_kind", "") or "")
    if kind == "email":
        return ""
    if kind == "photo":
        return "(a photo)"
    return " ".join(str(getattr(p, "new", "") or "").split())[:200]


def _salt(store: Any, make: bool) -> Optional[str]:
    salt = store.get_state(SALT_KEY)
    if salt or not make:
        return salt or None
    salt = secrets.token_hex(16)
    store.set_state(SALT_KEY, salt)
    return salt


def decline(store: Any, proposals: Iterable[Any], *, region: str = "GB", now: Optional[float] = None) -> int:
    """Remember each proposal's value as turned down for its contact. Returns how many rows are new."""
    chosen = [p for p in proposals if getattr(p, "contact_id", None) is not None]
    if not chosen or not _has_table(store):
        return 0
    salt = _salt(store, make=True)
    at = int(now if now is not None else time.time())
    added = 0
    with store.write() as conn:
        for p in chosen:
            label = str(getattr(p, "field_label", "") or getattr(p, "field_kind", "") or "")
            cur = conn.execute("INSERT OR IGNORE INTO enrich_declines (contact_id, field_key, value_hash, label, "
                               "shown, source, at) VALUES (?,?,?,?,?,?,?)",
                               (int(p.contact_id), slot_of(p), value_hash(salt, p, region), label[:100],
                                _shown(p), str(getattr(p, "source", "") or "")[:50], at))
            added += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    return added


def drop(store: Any, proposals: Sequence[Any], *, region: str = "GB") -> list:
    """`proposals` without the values turned down for their contact. Reads only; never makes the salt."""
    out = list(proposals)
    if not out or not _has_table(store):
        return out
    salt = _salt(store, make=False)
    if not salt:
        return out                                       # nothing was ever turned down
    ids = sorted({int(p.contact_id) for p in out if getattr(p, "contact_id", None) is not None})
    held: set[tuple[int, str, str]] = set()
    for start in range(0, len(ids), 500):                # SQLite's variable limit, with room to spare
        part = ids[start:start + 500]
        marks = ",".join("?" * len(part))
        for r in store.conn.execute(f"SELECT contact_id, field_key, value_hash FROM enrich_declines "
                                    f"WHERE contact_id IN ({marks})", part):
            held.add((int(r[0]), str(r[1]), str(r[2])))
    if not held:
        return out
    return [p for p in out
            if (int(p.contact_id), slot_of(p), value_hash(salt, p, region)) not in held]


def for_contact(store: Any, contact_id: int) -> list[Declined]:
    if not _has_table(store):
        return []
    rows = store.conn.execute("SELECT contact_id, field_key, value_hash, label, shown, source, at "
                              "FROM enrich_declines WHERE contact_id = ? ORDER BY at DESC, label",
                              (int(contact_id),)).fetchall()
    return [Declined(int(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]), str(r[5]), int(r[6])) for r in rows]


def undo(store: Any, contact_id: int, slot: str, digest: str) -> int:
    """Forget one turned-down value: it may be suggested again. Returns rows removed."""
    if not _has_table(store):
        return 0
    with store.write() as conn:
        cur = conn.execute("DELETE FROM enrich_declines WHERE contact_id = ? AND field_key = ? AND value_hash = ?",
                           (int(contact_id), slot, digest))
        return max(cur.rowcount or 0, 0)


def count(store: Any) -> int:
    if not _has_table(store):
        return 0
    return int(store.conn.execute("SELECT COUNT(*) FROM enrich_declines").fetchone()[0])


def clear(store: Any) -> int:
    """Forget every turned-down value (Settings > Accounts > Enrichment > Clear). Returns rows removed."""
    if not _has_table(store):
        return 0
    with store.write() as conn:
        cur = conn.execute("DELETE FROM enrich_declines")
        return max(cur.rowcount or 0, 0)
