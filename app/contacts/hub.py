"""The contact hub: Leasha's master copy of the people a person knows.

Layer: L7

Order 1k section 2 (D4-D10). SQLite is the authority (rule 6): `contacts`,
`contact_fields` and the tables around them hold everything; an account is a
place a contact is *kept*, tracked by `contact_links`. This module is the only
code that writes those tables, so the journal cannot be bypassed:

* **Every change is a journal entry** holding whole-contact snapshots before and
  after (`contact_journal`), so any change - an edit, a merge, a clean-up, a
  whole sync - is undone by putting a snapshot back, and the accounts follow at
  the next push (`undo_*` returns the accounts to push to).
* **`rev` counts edits.** A link remembers the `rev` it last agreed at
  (`hub_rev`); a link whose `hub_rev` differs from the contact's `rev` may need
  a push. `edited_at` is the *person's* edit time - a sync never moves it past
  the time of the change it applied - because "most recent edit wins" (D6)
  compares it with the account's own edit time.
* **Nothing is removed outright.** A delete goes to the bin for 30 days (D7);
  a merge soft-deletes the contacts it folds in, with `merged_into`.

Times in `contacts`, `contact_journal` and `contact_conflicts` are integer
milliseconds, so two edits in the same second still have an order.
"""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Optional, Sequence

from app.contacts import phone as phones
from app.contacts.card import (SCALARS, Card, Field, Photo, display_name, field_key, units)

__all__ = [
    "BIN_DAYS", "ContactHub", "ContactRow", "ContactFilter", "LinkRow", "JournalRow",
    "ContactHit", "Smart", "UndoResult",
]

#: How long a deleted contact stays in the bin (D7.2).
BIN_DAYS = 30

LINK_STATES = ("synced", "pending_create", "pending_remove", "deleted_remote")


@dataclass
class ContactRow:
    """What the list and the cards show; cheap to build for ten thousand."""

    id: int
    uid: str
    display_name: str
    company: str = ""
    title: str = ""
    email: str = ""
    phone: str = ""
    favourite: bool = False
    rev: int = 1
    edited_at: int = 0
    deleted_at: Optional[int] = None
    conflict_at: Optional[int] = None
    last_contacted_at: Optional[int] = None
    accounts: list[int] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    has_photo: bool = False


@dataclass
class LinkRow:
    id: int
    contact_id: int
    account_id: int
    remote_id: Optional[str]
    version_tag: str
    snapshot: Optional[Card]
    hub_rev: int
    state: str
    last_synced_at: Optional[int]


@dataclass
class JournalRow:
    id: int
    at: int
    contact_id: Optional[int]
    actor: str
    action: str
    sync_id: Optional[int]
    batch_id: Optional[str]
    before: Optional[dict]
    after: Optional[dict]
    detail: Optional[dict]
    undone_at: Optional[int]


@dataclass
class ContactHit:
    id: int
    display_name: str
    company: str
    email: str
    phone: str


@dataclass
class UndoResult:
    undone: int = 0
    skipped: int = 0
    accounts: set[int] = field(default_factory=set)       # accounts to push to afterwards


@dataclass
class UnmergeResult:
    restored: int                     # the contact given back as a person of their own (0: nothing to do)
    survivor: Optional[int] = None    # who they had been folded into
    survivor_rebuilt: bool = False    # False when the survivor was edited since the merge and is left as it is
    accounts: set[int] = field(default_factory=set)


@dataclass
class Smart:
    id: int
    name: str
    rules: list[dict]
    icon: str = "sliders-horizontal"


@dataclass
class ContactFilter:
    """Which contacts the page lists.

    `view`: all | favourites | group | tag | account | conflict | deleted_elsewhere
    | bin | single_account | smart | ids. `arg` is the group/tag/account/smart id
    or, for `ids`, the id list.
    """

    view: str = "all"
    arg: Any = None
    text: str = ""
    sort: str = "name"            # name | company | edited | contacted


def _now_ms() -> int:
    return int(time.time() * 1000)


def _norm_for(f: Field, region: str) -> str:
    if f.kind == "email":
        return f.value.strip().casefold()
    if f.kind == "phone":
        return phones.match_key(f.value, region)
    return ""


def _sort_key(card: Card) -> str:
    key = " ".join(p for p in (card.get("family"), card.get("given"), card.get("company")) if p)
    return (key or display_name(card)).casefold()


class ContactHub:
    """Reads and writes the contact tables of an open `SqliteStore`."""

    def __init__(self, store: Any, *, clock_ms: Callable[[], int] = _now_ms,
                 region: str = phones.DEFAULT_REGION) -> None:
        self.store = store
        self._now = clock_ms
        self.region = region

    # ------------------------------------------------------------------ reads
    @property
    def conn(self):
        return self.store.conn

    def count(self, *, include_bin: bool = False) -> int:
        sql = "SELECT COUNT(*) FROM contacts" + ("" if include_bin else " WHERE deleted_at IS NULL")
        return int(self.conn.execute(sql).fetchone()[0])

    def get_card(self, cid: int, conn: Any = None) -> Optional[Card]:
        conn = conn or self.conn
        row = conn.execute(f"SELECT {', '.join(SCALARS)}, favourite FROM contacts WHERE id = ?",
                           (cid,)).fetchone()
        if row is None:
            return None
        card = Card(scalars={k: row[k] for k in SCALARS if row[k]}, favourite=bool(row["favourite"]))
        for fr in conn.execute("SELECT kind, label, value, data, is_main FROM contact_fields "
                               "WHERE contact_id = ? ORDER BY position, id", (cid,)):
            card.fields.append(Field(fr["kind"], fr["value"], fr["label"],
                                     json.loads(fr["data"] or "{}"), bool(fr["is_main"])))
        card.groups = [g["name"] for g in conn.execute(
            "SELECT g.name FROM contact_group_members m JOIN contact_groups g ON g.id = m.group_id "
            "WHERE m.contact_id = ? ORDER BY g.name", (cid,))]
        ph = conn.execute("SELECT mime, data FROM contact_photos WHERE contact_id = ?", (cid,)).fetchone()
        if ph is not None:
            card.photo = Photo(ph["mime"], bytes(ph["data"]))
        return card

    def get_row(self, cid: int) -> Optional[ContactRow]:
        rows = self._rows(f"WHERE c.id = {int(cid)}")
        return rows[0] if rows else None

    def exists(self, cid: int) -> bool:
        return self.conn.execute("SELECT 1 FROM contacts WHERE id = ?", (cid,)).fetchone() is not None

    def tags_of(self, cid: int) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT t.name FROM contact_tag_members m JOIN contact_tags t ON t.id = m.tag_id "
            "WHERE m.contact_id = ? ORDER BY t.name", (cid,))]

    def kept_in(self, cid: int) -> list[int]:
        """Accounts this contact is kept in (D4): linked, or waiting to be written."""
        return [r[0] for r in self.conn.execute(
            "SELECT account_id FROM contact_links WHERE contact_id = ? "
            "AND state IN ('synced','pending_create','deleted_remote') ORDER BY account_id", (cid,))]

    def links(self, cid: int) -> list[LinkRow]:
        return [self._link(r) for r in self.conn.execute(
            "SELECT * FROM contact_links WHERE contact_id = ? ORDER BY account_id", (cid,))]

    def link(self, cid: int, account_id: int) -> Optional[LinkRow]:
        r = self.conn.execute("SELECT * FROM contact_links WHERE contact_id = ? AND account_id = ?",
                              (cid, account_id)).fetchone()
        return self._link(r) if r else None

    def link_by_remote(self, account_id: int, remote_id: str) -> Optional[LinkRow]:
        r = self.conn.execute("SELECT * FROM contact_links WHERE account_id = ? AND remote_id = ?",
                              (account_id, remote_id)).fetchone()
        return self._link(r) if r else None

    @staticmethod
    def _link(r: Any) -> LinkRow:
        snap = json.loads(r["snapshot"] or "{}")
        return LinkRow(r["id"], r["contact_id"], r["account_id"], r["remote_id"], r["version_tag"],
                       Card.from_json(snap) if snap else None, r["hub_rev"], r["state"],
                       r["last_synced_at"])

    def contact_meta(self, cid: int) -> Optional[dict]:
        r = self.conn.execute("SELECT rev, edited_at, updated_at, deleted_at, conflict_at "
                              "FROM contacts WHERE id = ?", (cid,)).fetchone()
        return dict(r) if r else None

    def all_cards(self, *, include_bin: bool = False) -> list[tuple[int, Card]]:
        """Every contact as a card (for the duplicate scan and exports)."""
        ids = [r[0] for r in self.conn.execute(
            "SELECT id FROM contacts" + ("" if include_bin else " WHERE deleted_at IS NULL") + " ORDER BY sort_key")]
        return [(i, c) for i in ids if (c := self.get_card(i)) is not None]

    # ----------------------------------------------------------------- writes
    def _write(self):
        return self.store.write()

    def _record(self, conn: Any, cid: Optional[int], actor: str, action: str, *, before: Any = None,
                after: Any = None, sync_id: Optional[int] = None, batch_id: Optional[str] = None,
                detail: Any = None) -> int:
        cur = conn.execute(
            "INSERT INTO contact_journal (at, contact_id, actor, action, sync_id, batch_id, before, after, detail) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (self._now(), cid, actor, action, sync_id, batch_id,
             json.dumps(before) if before is not None else None,
             json.dumps(after) if after is not None else None,
             json.dumps(detail) if detail is not None else None))
        return int(cur.lastrowid)

    def snapshot(self, cid: int, conn: Any = None, *, with_links: bool = False) -> Optional[dict]:
        conn = conn or self.conn
        card = self.get_card(cid, conn)
        if card is None:
            return None
        meta = conn.execute("SELECT deleted_at, edited_at FROM contacts WHERE id = ?", (cid,)).fetchone()
        snap: dict[str, Any] = {
            "card": card.to_json(),
            "photo": ({"mime": card.photo.mime, "b64": base64.b64encode(card.photo.data).decode("ascii")}
                      if card.photo else None),
            "tags": [r[0] for r in conn.execute(
                "SELECT t.name FROM contact_tag_members m JOIN contact_tags t ON t.id = m.tag_id "
                "WHERE m.contact_id = ?", (cid,))],
            "kept_in": [r[0] for r in conn.execute(
                "SELECT account_id FROM contact_links WHERE contact_id = ? "
                "AND state IN ('synced','pending_create','deleted_remote')", (cid,))],
            "deleted": meta["deleted_at"] is not None,
        }
        if with_links:
            snap["links"] = [dict(r) for r in conn.execute(
                "SELECT account_id, remote_id, version_tag, snapshot, hub_rev, state, last_synced_at "
                "FROM contact_links WHERE contact_id = ?", (cid,))]
        return snap

    def create(self, card: Card, *, actor: str = "user", kept_in: Optional[Iterable[int]] = None,
               sync_id: Optional[int] = None, batch_id: Optional[str] = None,
               edited_at: Optional[int] = None, source: str = "", source_ref: str = "",
               uid: str = "") -> int:
        now = self._now()
        with self._write() as conn:
            cur = conn.execute(
                "INSERT INTO contacts (uid, created_at, edited_at, updated_at) VALUES (?,?,?,?)",
                (uid or str(uuid.uuid4()), now, edited_at or now, now))
            cid = int(cur.lastrowid)
            self._store_card(conn, cid, card)
            for acc in (kept_in or []):
                self._ensure_link(conn, cid, int(acc), "pending_create")
            if source:
                self._set_sources(conn, cid, card, source, source_ref)
            self._record(conn, cid, actor, "create", after=self.snapshot(cid, conn),
                         sync_id=sync_id, batch_id=batch_id)
        return cid

    def update(self, cid: int, card: Card, *, actor: str = "user", action: str = "update",
               sync_id: Optional[int] = None, batch_id: Optional[str] = None,
               edited_at: Optional[int] = None, touch_edit: bool = True,
               source: str = "", source_ref: str = "", only_if_changed: bool = True) -> bool:
        """Replace the contact's content. Returns False when nothing differs."""
        with self._write() as conn:
            before = self.snapshot(cid, conn)
            if before is None:
                return False
            if only_if_changed and before["card"] == card.to_json() and \
                    (before["photo"] or {}).get("b64") == (base64.b64encode(card.photo.data).decode("ascii")
                                                           if card.photo else None):
                return False
            self._store_card(conn, cid, card)
            now = self._now()
            if touch_edit:
                conn.execute("UPDATE contacts SET edited_at = ? WHERE id = ?", (edited_at or now, cid))
            if source:
                # only what this update added or changed is credited to `source`
                was = units(Card.from_json(before["card"]), region=self.region)
                now_units = units(card, region=self.region)
                fresh = {k for k, v in now_units.items() if was.get(k) != v}
                self._set_sources(conn, cid, card, source, source_ref, only=fresh)
            self._record(conn, cid, actor, action, before=before, after=self.snapshot(cid, conn),
                         sync_id=sync_id, batch_id=batch_id)
        return True

    def delete(self, cid: int, *, actor: str = "user", sync_id: Optional[int] = None,
               batch_id: Optional[str] = None) -> bool:
        """To the bin (D7.2); the accounts it is kept in lose it at the next push."""
        with self._write() as conn:
            before = self.snapshot(cid, conn)
            if before is None or before["deleted"]:
                return False
            now = self._now()
            conn.execute("UPDATE contacts SET deleted_at = ?, updated_at = ?, rev = rev + 1 WHERE id = ?",
                         (now, now, cid))
            self._leave_accounts(conn, cid)
            conn.execute("DELETE FROM contacts_fts WHERE rowid = ?", (cid,))
            self._record(conn, cid, actor, "delete", before=before, after=self.snapshot(cid, conn),
                         sync_id=sync_id, batch_id=batch_id)
        return True

    def restore(self, cid: int, *, actor: str = "user", sync_id: Optional[int] = None,
                batch_id: Optional[str] = None) -> bool:
        """Out of the bin. Writes it back to the accounts it was kept in (D7.2)."""
        with self._write() as conn:
            row = conn.execute("SELECT deleted_at FROM contacts WHERE id = ?", (cid,)).fetchone()
            if row is None or row["deleted_at"] is None:
                return False
            before = self.snapshot(cid, conn)
            last_delete = conn.execute(
                "SELECT before FROM contact_journal WHERE contact_id = ? AND action = 'delete' "
                "ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
            was_in = json.loads(last_delete["before"])["kept_in"] if last_delete and last_delete["before"] else []
            now = self._now()
            conn.execute("UPDATE contacts SET deleted_at = NULL, merged_into = NULL, updated_at = ?, "
                         "rev = rev + 1 WHERE id = ?", (now, cid))
            # a link not yet pushed as a delete simply goes back to normal
            conn.execute("UPDATE contact_links SET state = 'synced', hub_rev = 0 "
                         "WHERE contact_id = ? AND state = 'pending_remove'", (cid,))
            for acc in was_in:
                self._ensure_link(conn, cid, int(acc), "pending_create")
            self._reindex(conn, cid)
            self._record(conn, cid, actor, "restore", before=before, after=self.snapshot(cid, conn),
                         sync_id=sync_id, batch_id=batch_id)
        return True

    def purge_bin(self, *, older_than_days: int = BIN_DAYS) -> int:
        """Remove for good what has been in the bin longer than `older_than_days`
        and is no longer waiting to leave an account. Returns how many."""
        cutoff = self._now() - older_than_days * 86_400_000
        with self._write() as conn:
            ids = [r[0] for r in conn.execute(
                "SELECT c.id FROM contacts c WHERE c.deleted_at IS NOT NULL AND c.deleted_at < ? "
                "AND NOT EXISTS (SELECT 1 FROM contact_links l WHERE l.contact_id = c.id "
                "AND l.state = 'pending_remove')", (cutoff,))]
            for cid in ids:
                conn.execute("DELETE FROM contacts WHERE id = ?", (cid,))
        return len(ids)

    def empty_bin(self) -> int:
        return self.purge_bin(older_than_days=-1)

    def bin_waiting(self) -> int:
        """How many contacts in the bin are still waiting to be removed from an account. They stay in the bin
        until a sync has done that: deleting them here first would lose the instruction, and the next sync
        would bring them back from the account."""
        return int(self.conn.execute(
            "SELECT COUNT(DISTINCT c.id) FROM contacts c JOIN contact_links l ON l.contact_id = c.id "
            "WHERE c.deleted_at IS NOT NULL AND c.merged_into IS NULL AND l.state = 'pending_remove'").fetchone()[0])

    # -- low-level row writers (call inside a write transaction) ---------------
    def _store_card(self, conn: Any, cid: int, card: Card) -> None:
        sets = {k: card.get(k) for k in SCALARS}
        sets["display_name"] = display_name(card)
        sets["sort_key"] = _sort_key(card)
        sets["favourite"] = 1 if card.favourite else 0
        now = self._now()
        assigns = ", ".join(f"{k} = ?" for k in sets)
        conn.execute(f"UPDATE contacts SET {assigns}, updated_at = ?, rev = rev + 1 WHERE id = ?",
                     (*sets.values(), now, cid))
        conn.execute("DELETE FROM contact_fields WHERE contact_id = ?", (cid,))
        seen_main: set[str] = set()
        for pos, f in enumerate(card.fields):
            if not f.value.strip() and not any((f.data or {}).values()):
                continue
            main = 1 if f.primary and f.kind not in seen_main else 0
            if main:
                seen_main.add(f.kind)
            conn.execute(
                "INSERT INTO contact_fields (contact_id, kind, label, value, norm, data, position, is_main) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (cid, f.kind, f.label, f.value, _norm_for(f, self.region),
                 json.dumps(f.data or {}), pos, main))
        conn.execute("DELETE FROM contact_group_members WHERE contact_id = ?", (cid,))
        for name in dict.fromkeys(g.strip() for g in card.groups if g.strip()):
            gid = self._group_id(conn, name)
            conn.execute("INSERT OR IGNORE INTO contact_group_members (contact_id, group_id) VALUES (?,?)",
                         (cid, gid))
        if card.photo is None:
            conn.execute("DELETE FROM contact_photos WHERE contact_id = ?", (cid,))
        else:
            conn.execute("INSERT INTO contact_photos (contact_id, mime, data, sha) VALUES (?,?,?,?) "
                         "ON CONFLICT(contact_id) DO UPDATE SET mime = excluded.mime, "
                         "data = excluded.data, sha = excluded.sha",
                         (cid, card.photo.mime, card.photo.data, card.photo.sha))
        self._reindex(conn, cid)

    def _group_id(self, conn: Any, name: str) -> int:
        row = conn.execute("SELECT id FROM contact_groups WHERE name = ?", (name,)).fetchone()
        if row:
            return int(row[0])
        return int(conn.execute("INSERT INTO contact_groups (name) VALUES (?)", (name,)).lastrowid)

    def _ensure_link(self, conn: Any, cid: int, account_id: int, state: str) -> None:
        conn.execute(
            "INSERT INTO contact_links (contact_id, account_id, state, hub_rev) VALUES (?,?,?,0) "
            "ON CONFLICT(contact_id, account_id) DO UPDATE SET state = CASE "
            "WHEN contact_links.state = 'pending_remove' THEN 'synced' ELSE contact_links.state END",
            (cid, account_id, state))

    def _reindex(self, conn: Any, cid: int) -> None:
        row = conn.execute("SELECT display_name, company, title, notes, deleted_at FROM contacts "
                           "WHERE id = ?", (cid,)).fetchone()
        try:
            conn.execute("DELETE FROM contacts_fts WHERE rowid = ?", (cid,))
            if row is None or row["deleted_at"] is not None:
                return
            emails = " ".join(r[0] for r in conn.execute(
                "SELECT value FROM contact_fields WHERE contact_id = ? AND kind = 'email'", (cid,)))
            phone_text = " ".join(
                f"{r['value']} {r['norm'] or ''}" for r in conn.execute(
                    "SELECT value, norm FROM contact_fields WHERE contact_id = ? AND kind = 'phone'", (cid,)))
            conn.execute(
                "INSERT INTO contacts_fts (rowid, name, company, title, notes, emails, phones) "
                "VALUES (?,?,?,?,?,?,?)",
                (cid, row["display_name"], row["company"], row["title"], row["notes"], emails, phone_text))
        except Exception:                                    # noqa: BLE001 - search is a convenience
            pass                                              # FTS5 missing: list and filters still work

    def _set_sources(self, conn: Any, cid: int, card: Card, source: str, ref: str,
                     only: Optional[set] = None) -> None:
        now = int(time.time())
        keys = [f"s:{k}" for k in SCALARS if card.get(k)] + ["f:" + field_key(f, self.region)
                                                              for f in card.fields]
        if card.photo is not None:
            keys.append("photo")
        if only is not None:
            keys = [k for k in keys if k in only]
        for key in keys:
            have = conn.execute("SELECT 1 FROM contact_sources WHERE contact_id = ? AND field_key = ? "
                                "AND source = ?", (cid, key, source)).fetchone()
            if not have:
                conn.execute("INSERT INTO contact_sources (contact_id, field_key, source, ref, at) "
                             "VALUES (?,?,?,?,?)", (cid, key, source, ref, now))

    def sources(self, cid: int) -> dict[str, list[tuple[str, str]]]:
        out: dict[str, list[tuple[str, str]]] = {}
        for r in self.conn.execute("SELECT field_key, source, ref FROM contact_sources "
                                   "WHERE contact_id = ? ORDER BY id", (cid,)):
            out.setdefault(r["field_key"], []).append((r["source"], r["ref"]))
        return out

    # ------------------------------------------------------------------ links
    def set_link(self, cid: int, account_id: int, *, remote_id: Optional[str] = None,
                 version_tag: str = "", snapshot: Optional[Card] = None, hub_rev: Optional[int] = None,
                 state: str = "synced", conn: Any = None) -> None:
        """Create or update the link, recording what was last agreed (the merge base)."""
        assert state in LINK_STATES
        own = conn is None
        ctx = self._write() if own else _Null(conn)
        with ctx as c:
            if hub_rev is None:
                row = c.execute("SELECT rev FROM contacts WHERE id = ?", (cid,)).fetchone()
                hub_rev = int(row[0]) if row else 0
            c.execute(
                "INSERT INTO contact_links (contact_id, account_id, remote_id, version_tag, snapshot, "
                "hub_rev, state, last_synced_at) VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT(contact_id, account_id) DO UPDATE SET remote_id = excluded.remote_id, "
                "version_tag = excluded.version_tag, snapshot = excluded.snapshot, "
                "hub_rev = excluded.hub_rev, state = excluded.state, last_synced_at = excluded.last_synced_at",
                (cid, account_id, remote_id, version_tag,
                 snapshot.dumps() if snapshot is not None else "{}", hub_rev, state, int(time.time())))

    def mirror_to(self, account_ids: Iterable[int]) -> int:
        """Keep every live contact in each of these accounts: a contact with no link to one gets a
        `pending_create` link, written there at the next push. A link in any state counts as a
        link - one the account deleted (`deleted_remote`) or the person removed is not brought
        back. Returns how many links were added."""
        added = 0
        with self._write() as conn:
            for aid in {int(a) for a in account_ids}:
                for r in conn.execute(
                        "SELECT c.id FROM contacts c WHERE c.deleted_at IS NULL AND NOT EXISTS "
                        "(SELECT 1 FROM contact_links l WHERE l.contact_id = c.id AND l.account_id = ?)",
                        (aid,)).fetchall():
                    self._ensure_link(conn, int(r[0]), aid, "pending_create")
                    added += 1
        return added

    def drop_link(self, cid: int, account_id: int, *, conn: Any = None) -> None:
        ctx = self._write() if conn is None else _Null(conn)
        with ctx as c:
            c.execute("DELETE FROM contact_links WHERE contact_id = ? AND account_id = ?", (cid, account_id))

    def set_kept_in(self, cid: int, account_ids: Iterable[int], *, actor: str = "user",
                    confirmed_removals: bool = False, batch_id: Optional[str] = None) -> dict[str, list[int]]:
        """Change where the contact is kept (D4). Adding writes it there at the next push; removing deletes
        it from that account, and only when `confirmed_removals` is True."""
        want = {int(a) for a in account_ids}
        added: list[int] = []
        removed: list[int] = []
        with self._write() as conn:
            before = self.snapshot(cid, conn)
            if before is None:
                return {"added": [], "removed": [], "unconfirmed": []}
            have = {r["account_id"]: r["state"] for r in conn.execute(
                "SELECT account_id, state FROM contact_links WHERE contact_id = ?", (cid,))}
            unconfirmed: list[int] = []
            for acc in want:
                if have.get(acc) in (None, "pending_remove"):
                    if acc in have and have[acc] == "pending_remove":
                        conn.execute("UPDATE contact_links SET state = 'synced', hub_rev = 0 WHERE "
                                     "contact_id = ? AND account_id = ?", (cid, acc))
                    else:
                        self._ensure_link(conn, cid, acc, "pending_create")
                    added.append(acc)
            for acc, state in have.items():
                if acc in want or state == "pending_remove":
                    continue
                if not confirmed_removals:
                    unconfirmed.append(acc)
                    continue
                if state == "pending_create":
                    conn.execute("DELETE FROM contact_links WHERE contact_id = ? AND account_id = ?", (cid, acc))
                else:
                    conn.execute("UPDATE contact_links SET state = 'pending_remove' "
                                 "WHERE contact_id = ? AND account_id = ?", (cid, acc))
                removed.append(acc)
            if added or removed:
                conn.execute("UPDATE contacts SET rev = rev + 1, updated_at = ? WHERE id = ?", (self._now(), cid))
                self._record(conn, cid, actor, "kept_in", before=before, after=self.snapshot(cid, conn),
                             batch_id=batch_id, detail={"added": added, "removed": removed})
        return {"added": added, "removed": removed, "unconfirmed": unconfirmed}

    # ----------------------------------------------------------------- groups
    def groups(self) -> list[tuple[int, str, int]]:
        return [(r[0], r[1], r[2]) for r in self.conn.execute(
            "SELECT g.id, g.name, COUNT(c.id) FROM contact_groups g "
            "LEFT JOIN contact_group_members m ON m.group_id = g.id "
            "LEFT JOIN contacts c ON c.id = m.contact_id AND c.deleted_at IS NULL "
            "GROUP BY g.id ORDER BY g.name")]

    def add_to_group(self, cids: Iterable[int], name: str, *, actor: str = "user",
                     batch_id: Optional[str] = None) -> int:
        return self._bulk_card(cids, lambda c: c.groups.append(name) if name.casefold() not in
                               {g.casefold() for g in c.groups} else None, actor, "group", batch_id)

    def remove_from_group(self, cids: Iterable[int], name: str, *, actor: str = "user",
                          batch_id: Optional[str] = None) -> int:
        def edit(c: Card) -> None:
            c.groups = [g for g in c.groups if g.casefold() != name.casefold()]
        return self._bulk_card(cids, edit, actor, "group", batch_id)

    def _bulk_card(self, cids: Iterable[int], edit: Callable[[Card], Any], actor: str, action: str,
                   batch_id: Optional[str]) -> int:
        batch = batch_id or uuid.uuid4().hex
        n = 0
        for cid in cids:
            card = self.get_card(cid)
            if card is None:
                continue
            edit(card)
            if self.update(cid, card, actor=actor, action=action, batch_id=batch):
                n += 1
        return n

    def set_company(self, cids: Iterable[int], company: str, *, actor: str = "user",
                    batch_id: Optional[str] = None) -> int:
        return self._bulk_card(cids, lambda c: c.scalars.__setitem__("company", company), actor,
                               "update", batch_id)

    def set_favourite(self, cids: Iterable[int], value: bool, *, actor: str = "user",
                      batch_id: Optional[str] = None) -> int:
        def edit(c: Card) -> None:
            c.favourite = value
        return self._bulk_card(cids, edit, actor, "favourite", batch_id)

    def rename_group(self, group_id: int, name: str) -> None:
        with self._write() as conn:
            conn.execute("UPDATE contact_groups SET name = ? WHERE id = ?", (name, group_id))
            for (cid,) in conn.execute("SELECT contact_id FROM contact_group_members WHERE group_id = ?",
                                        (group_id,)).fetchall():
                conn.execute("UPDATE contacts SET rev = rev + 1 WHERE id = ?", (cid,))

    # ------------------------------------------------------------------- tags
    def tags(self) -> list[tuple[int, str, str, int]]:
        return [tuple(r) for r in self.conn.execute(
            "SELECT t.id, t.name, t.colour, COUNT(c.id) FROM contact_tags t "
            "LEFT JOIN contact_tag_members m ON m.tag_id = t.id "
            "LEFT JOIN contacts c ON c.id = m.contact_id AND c.deleted_at IS NULL "
            "GROUP BY t.id ORDER BY t.name")]

    def create_tag(self, name: str, colour: str = "#4C8BF5") -> int:
        with self._write() as conn:
            row = conn.execute("SELECT id FROM contact_tags WHERE name = ?", (name,)).fetchone()
            if row:
                conn.execute("UPDATE contact_tags SET colour = ? WHERE id = ?", (colour, row[0]))
                return int(row[0])
            return int(conn.execute("INSERT INTO contact_tags (name, colour) VALUES (?,?)",
                                    (name, colour)).lastrowid)

    def delete_tag(self, tag_id: int) -> None:
        with self._write() as conn:
            conn.execute("DELETE FROM contact_tags WHERE id = ?", (tag_id,))

    def tag(self, cids: Iterable[int], name: str, *, on: bool = True, actor: str = "user",
            batch_id: Optional[str] = None) -> int:
        """Tags are Leasha's own (6s) and never sent to an account; changes are still journalled."""
        tag_id = self.create_tag(name, self._tag_colour(name))
        batch = batch_id or uuid.uuid4().hex
        n = 0
        with self._write() as conn:
            for cid in cids:
                before = self.snapshot(cid, conn)
                if before is None:
                    continue
                if on:
                    cur = conn.execute("INSERT OR IGNORE INTO contact_tag_members (contact_id, tag_id) VALUES (?,?)",
                                       (cid, tag_id))
                else:
                    cur = conn.execute("DELETE FROM contact_tag_members WHERE contact_id = ? AND tag_id = ?",
                                       (cid, tag_id))
                if cur.rowcount:
                    n += 1
                    self._record(conn, cid, actor, "tag", before=before, after=self.snapshot(cid, conn),
                                 batch_id=batch)
        return n

    def _tag_colour(self, name: str) -> str:
        row = self.conn.execute("SELECT colour FROM contact_tags WHERE name = ?", (name,)).fetchone()
        return row[0] if row else "#4C8BF5"

    # ---------------------------------------------------------------- journal
    def history(self, cid: int, limit: int = 200) -> list[JournalRow]:
        return [self._journal(r) for r in self.conn.execute(
            "SELECT * FROM contact_journal WHERE contact_id = ? ORDER BY id DESC LIMIT ?", (cid, limit))]

    def journal_entry(self, jid: int) -> Optional[JournalRow]:
        r = self.conn.execute("SELECT * FROM contact_journal WHERE id = ?", (jid,)).fetchone()
        return self._journal(r) if r else None

    @staticmethod
    def _journal(r: Any) -> JournalRow:
        def load(x):
            return json.loads(x) if x else None
        return JournalRow(r["id"], r["at"], r["contact_id"], r["actor"], r["action"], r["sync_id"],
                          r["batch_id"], load(r["before"]), load(r["after"]), load(r["detail"]), r["undone_at"])

    def undo_entry(self, jid: int, *, actor: str = "undo", _batch: Optional[str] = None) -> UndoResult:
        res = UndoResult()
        self._undo_ids([jid], actor, res, _batch)
        return res

    def unmerge(self, folded: int, *, actor: str = "user") -> UnmergeResult:
        """Give a folded contact back as a person of their own (order 1k, a wrong merge).

        The folded contact returns with its own card and its own account records. The contact
        it had been folded into is rebuilt from its card as it was before the merge plus every
        *other* contact still folded into it, so what the merge brought from this person leaves
        and what it brought from the rest stays. A survivor edited since the merge is not
        rebuilt - its edits are the person's - and the result says so."""
        row = self.conn.execute(
            "SELECT id, detail FROM contact_journal WHERE contact_id = ? AND action = 'merged' "
            "AND undone_at IS NULL ORDER BY id DESC LIMIT 1", (folded,)).fetchone()
        if row is None:
            return UnmergeResult(0)
        survivor = (json.loads(row["detail"] or "{}") or {}).get("into")
        res = self.undo_entry(int(row["id"]), actor=actor)
        if survivor is None:
            return UnmergeResult(folded, None, False, res.accounts)
        survivor = int(survivor)
        self._recopy_folded_sources(survivor)
        rebuilt = self.rebuild_merged(survivor, actor=actor)
        return UnmergeResult(folded, survivor, rebuilt, res.accounts)

    def _recopy_folded_sources(self, survivor: int) -> None:
        """After one person is given back, a source the survivor held only through them may also be
        held by a contact still folded in (the copy was not doubled). Copy those back, journalled on
        that fold and on its merge, so a later undo still removes exactly what merges copied."""
        with self._write() as conn:
            for r in conn.execute(
                    "SELECT id, contact_id, batch_id, detail FROM contact_journal WHERE action = 'merged' "
                    "AND undone_at IS NULL ORDER BY id").fetchall():
                detail = json.loads(r["detail"] or "{}") or {}
                if detail.get("into") != survivor:
                    continue
                made = self._copy_sources(conn, int(r["contact_id"]), survivor)
                if not made:
                    continue
                detail["sources"] = list(detail.get("sources") or []) + made
                conn.execute("UPDATE contact_journal SET detail = ? WHERE id = ?", (json.dumps(detail), r["id"]))
                m = conn.execute("SELECT id, detail FROM contact_journal WHERE contact_id = ? AND action = 'merge' "
                                 "AND batch_id IS ? AND undone_at IS NULL ORDER BY id DESC LIMIT 1",
                                 (survivor, r["batch_id"])).fetchone()
                if m is not None:
                    md = json.loads(m["detail"] or "{}") or {}
                    md["sources"] = list(md.get("sources") or []) + made
                    conn.execute("UPDATE contact_journal SET detail = ? WHERE id = ?", (json.dumps(md), m["id"]))

    def merge_batches(self) -> list[str]:
        """The merges still standing, newest first: each is one "Merge" (or "Merge all safe") batch."""
        return [r[0] for r in self.conn.execute(
            "SELECT batch_id FROM contact_journal WHERE action IN ('merge', 'merged') AND undone_at IS NULL "
            "AND batch_id IS NOT NULL GROUP BY batch_id ORDER BY MIN(id) DESC")]

    def undo_all_merges(self, *, actor: str = "user") -> UndoResult:
        """Back to square one: every standing merge undone, newest first, so each contact is again the
        record its account sent, linked to that account alone."""
        total = UndoResult()
        for batch in self.merge_batches():
            res = self.undo_batch(batch, actor=actor)
            total.undone += res.undone
            total.skipped += res.skipped
            total.accounts |= res.accounts
        return total

    def rebuild_merged(self, survivor: int, *, actor: str = "user") -> bool:
        """Rebuild a merge survivor's card from the card it had before the merge plus the
        contacts that are *still* folded into it (a person given back no longer counts).

        Only while nobody has edited it since: the card must still be what the merge wrote or
        what the last rebuild wrote. Returns whether it rebuilt."""
        from app.contacts import dupes  # noqa: PLC0415
        current = self.get_card(survivor)
        merge = self.conn.execute(
            "SELECT id, before, after FROM contact_journal WHERE contact_id = ? AND action = 'merge' "
            "AND undone_at IS NULL ORDER BY id", (survivor,)).fetchall()
        if current is None or not merge:
            return False
        last_rebuild = self.conn.execute(
            "SELECT after FROM contact_journal WHERE contact_id = ? AND action = 'unmerge' "
            "ORDER BY id DESC LIMIT 1", (survivor,)).fetchone()
        written = [Card.from_json(json.loads(m["after"])["card"]).to_json() for m in merge[-1:] if m["after"]]
        if last_rebuild is not None and last_rebuild["after"]:
            written.append(Card.from_json(json.loads(last_rebuild["after"])["card"]).to_json())
        if current.to_json() not in written:
            return False
        base = Card.from_json(json.loads(merge[0]["before"])["card"])
        others = []
        for r in self.conn.execute(
                "SELECT contact_id, detail, before FROM contact_journal WHERE action = 'merged' "
                "AND undone_at IS NULL ORDER BY id"):
            if (json.loads(r["detail"] or "{}") or {}).get("into") == survivor and r["before"]:
                others.append(Card.from_json(json.loads(r["before"])["card"]))
        self.update(survivor, dupes.merge_cards([base] + others).card, actor=actor, action="unmerge")
        return True

    def undo_batch(self, batch_id: str, *, actor: str = "undo") -> UndoResult:
        ids = [r[0] for r in self.conn.execute(
            "SELECT id FROM contact_journal WHERE batch_id = ? AND undone_at IS NULL "
            "AND action NOT IN ('undo','conflict') ORDER BY id DESC", (batch_id,))]
        res = UndoResult()
        self._undo_ids(ids, actor, res, batch_id)
        return res

    def undo_sync(self, sync_id: int, *, actor: str = "undo") -> UndoResult:
        ids = [r[0] for r in self.conn.execute(
            "SELECT id FROM contact_journal WHERE sync_id = ? AND undone_at IS NULL "
            "AND action NOT IN ('undo','conflict') ORDER BY id DESC", (sync_id,))]
        res = UndoResult()
        self._undo_ids(ids, actor, res, f"undo-sync-{sync_id}")
        with self._write() as conn:
            conn.execute("UPDATE sync_runs SET undone_at = ? WHERE id = ?", (int(time.time()), sync_id))
        return res

    def _undo_ids(self, ids: Sequence[int], actor: str, res: UndoResult, batch: Optional[str]) -> None:
        batch = batch or uuid.uuid4().hex
        for jid in ids:
            e = self.journal_entry(jid)
            if e is None or e.undone_at is not None:
                res.skipped += 1
                continue
            with self._write() as conn:
                accounts = self._apply_undo(conn, e, actor, batch)
                conn.execute("UPDATE contact_journal SET undone_at = ? WHERE id = ?", (self._now(), jid))
            if accounts is None:
                res.skipped += 1
            else:
                res.undone += 1
                res.accounts |= accounts

    def _apply_undo(self, conn: Any, e: JournalRow, actor: str, batch: str) -> Optional[set[int]]:
        cid = e.contact_id
        if cid is None:
            return None
        cur_snap = self.snapshot(cid, conn, with_links=True)
        accounts: set[int] = set()
        if e.action == "create" and e.before is None:
            if cur_snap is None:
                return None
            self._soft_delete(conn, cid)
            accounts |= {r["account_id"] for r in conn.execute(
                "SELECT account_id FROM contact_links WHERE contact_id = ? AND state = 'pending_remove'", (cid,))}
        elif e.before is not None:
            if cur_snap is None:
                return None
            self._apply_snapshot(conn, cid, e.before, links=e.detail.get("links") if e.detail else None)
            self._drop_merged_sources(conn, e)
            accounts |= {r["account_id"] for r in conn.execute(
                "SELECT account_id FROM contact_links WHERE contact_id = ?", (cid,))}
        else:
            return None
        self._record(conn, cid, actor, "undo", before=cur_snap, after=self.snapshot(cid, conn),
                     batch_id=batch, detail={"of": e.id})
        return accounts

    @staticmethod
    def _drop_merged_sources(conn: Any, e: JournalRow) -> None:
        """Undoing a merge (`merge` on the survivor) or one fold (`merged` on the folded contact)
        takes off the survivor the source rows that merge copied there, and only those."""
        if e.action not in ("merge", "merged") or not e.detail:
            return
        ids = [int(i) for i in e.detail.get("sources") or []]
        owner = e.contact_id if e.action == "merge" else e.detail.get("into")
        if not ids or owner is None:
            return
        conn.executemany("DELETE FROM contact_sources WHERE id = ? AND contact_id = ?",
                         [(i, int(owner)) for i in ids])

    def _soft_delete(self, conn: Any, cid: int) -> None:
        now = self._now()
        conn.execute("UPDATE contacts SET deleted_at = ?, updated_at = ?, rev = rev + 1 WHERE id = ?",
                     (now, now, cid))
        self._leave_accounts(conn, cid)
        conn.execute("DELETE FROM contacts_fts WHERE rowid = ?", (cid,))

    @staticmethod
    def _leave_accounts(conn: Any, cid: int) -> None:
        """A binned contact leaves every account that still holds it. A copy the
        account already deleted, or one never written there, simply goes."""
        conn.execute("UPDATE contact_links SET state = 'pending_remove' WHERE contact_id = ? "
                     "AND remote_id IS NOT NULL AND state = 'synced'", (cid,))
        conn.execute("DELETE FROM contact_links WHERE contact_id = ? AND "
                     "(remote_id IS NULL OR state IN ('pending_create','deleted_remote'))", (cid,))

    def _apply_snapshot(self, conn: Any, cid: int, snap: dict, *, links: Optional[list] = None) -> None:
        card = Card.from_json(snap["card"])
        if snap.get("photo"):
            card.photo = Photo(snap["photo"]["mime"], base64.b64decode(snap["photo"]["b64"]))
        self._store_card(conn, cid, card)
        conn.execute("UPDATE contacts SET deleted_at = ?, merged_into = NULL WHERE id = ?",
                     (self._now() if snap.get("deleted") else None, cid))
        if snap.get("deleted"):
            self._soft_delete(conn, cid)
        else:
            self._reindex(conn, cid)
        # tags
        conn.execute("DELETE FROM contact_tag_members WHERE contact_id = ?", (cid,))
        for name in snap.get("tags", []):
            row = conn.execute("SELECT id FROM contact_tags WHERE name = ?", (name,)).fetchone()
            if row:
                conn.execute("INSERT OR IGNORE INTO contact_tag_members VALUES (?,?)", (cid, row[0]))
        # where it is kept
        want = set(snap.get("kept_in", []))
        have = {r["account_id"]: r["state"] for r in conn.execute(
            "SELECT account_id, state FROM contact_links WHERE contact_id = ?", (cid,))}
        if not snap.get("deleted"):
            for acc in want:
                if have.get(acc) in (None, "pending_remove"):
                    if have.get(acc) == "pending_remove":
                        conn.execute("UPDATE contact_links SET state='synced', hub_rev=0 WHERE contact_id=? "
                                     "AND account_id=?", (cid, acc))
                    else:
                        self._ensure_link(conn, cid, acc, "pending_create")
            for acc, state in have.items():
                if acc not in want and state != "pending_remove":
                    if state == "pending_create":
                        conn.execute("DELETE FROM contact_links WHERE contact_id=? AND account_id=?", (cid, acc))
                    else:
                        conn.execute("UPDATE contact_links SET state='pending_remove' WHERE contact_id=? "
                                     "AND account_id=?", (cid, acc))
        for l in links or []:
            if l["remote_id"] is not None:
                # A merge moved this link to the surviving contact; undoing the fold gives it back to
                # the person it belongs to, so the survivor's inherited row goes (an account holds a
                # record under one contact only).
                conn.execute("DELETE FROM contact_links WHERE account_id = ? AND remote_id = ? "
                             "AND contact_id <> ?", (l["account_id"], l["remote_id"], cid))
            conn.execute(
                "INSERT INTO contact_links (contact_id, account_id, remote_id, version_tag, snapshot, hub_rev, "
                "state, last_synced_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(contact_id, account_id) DO UPDATE SET "
                "remote_id=excluded.remote_id, version_tag=excluded.version_tag, snapshot=excluded.snapshot, "
                "hub_rev=0, state=excluded.state",
                (cid, l["account_id"], l["remote_id"], l["version_tag"], l["snapshot"], 0, l["state"],
                 l["last_synced_at"]))

    def note(self, cid: int, actor: str, action: str, *, sync_id: Optional[int] = None,
             detail: Any = None, conn: Any = None) -> None:
        """A journal line with no content change (a copy deleted at an account, say)."""
        ctx = self._write() if conn is None else _Null(conn)
        with ctx as c:
            self._record(c, cid, actor, action, sync_id=sync_id, detail=detail)

    def flag_deleted_remote(self, cid: int, account_id: int, *, sync_id: Optional[int] = None,
                            conn: Any = None) -> None:
        """The account no longer has this contact (D7.1): Leasha and the other accounts keep it."""
        ctx = self._write() if conn is None else _Null(conn)
        with ctx as c:
            c.execute("UPDATE contact_links SET state = 'deleted_remote' WHERE contact_id = ? AND account_id = ?",
                      (cid, account_id))
            self._record(c, cid, f"account:{account_id}", "deleted_elsewhere", sync_id=sync_id)

    def restore_to_account(self, cid: int, account_id: int, *, actor: str = "user") -> None:
        """'Restore' on a Deleted-elsewhere contact: write it back to that account as a new record."""
        with self._write() as conn:
            conn.execute("UPDATE contact_links SET state = 'pending_create', remote_id = NULL, version_tag = '', "
                         "snapshot = '{}', hub_rev = 0 WHERE contact_id = ? AND account_id = ?", (cid, account_id))
            self._record(conn, cid, actor, "restore_to_account", detail={"account": account_id})

    def confirm_deleted_elsewhere(self, cid: int, *, actor: str = "user") -> bool:
        """'Confirm': the deletion stands, so the contact goes to the bin and leaves the other accounts too."""
        return self.delete(cid, actor=actor)

    # -------------------------------------------------------------- conflicts
    def add_conflict(self, cid: int, account_id: Optional[int], unit: str, local: Any, remote: Any,
                     winner: str, *, conn: Any = None) -> int:
        ctx = self._write() if conn is None else _Null(conn)
        with ctx as c:
            c.execute("UPDATE contacts SET conflict_at = ? WHERE id = ?", (self._now(), cid))
            return int(c.execute(
                "INSERT INTO contact_conflicts (contact_id, account_id, unit, local_value, remote_value, "
                "winner, at) VALUES (?,?,?,?,?,?,?)",
                (cid, account_id, unit, json.dumps(local, default=str), json.dumps(remote, default=str),
                 winner, self._now())).lastrowid)

    def open_conflicts(self, cid: Optional[int] = None) -> list[dict]:
        sql = "SELECT * FROM contact_conflicts WHERE resolved_at IS NULL"
        args: tuple = ()
        if cid is not None:
            sql += " AND contact_id = ?"
            args = (cid,)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY id", args)]

    def resolve_conflict_row(self, conflict_id: int) -> None:
        """One clash is dealt with: it leaves the list, and the contact's marker goes when it was the last."""
        with self._write() as conn:
            row = conn.execute("SELECT contact_id FROM contact_conflicts WHERE id = ?", (int(conflict_id),)).fetchone()
            if row is None:
                return
            conn.execute("UPDATE contact_conflicts SET resolved_at = ? WHERE id = ? AND resolved_at IS NULL",
                         (self._now(), int(conflict_id)))
            if not conn.execute("SELECT 1 FROM contact_conflicts WHERE contact_id = ? AND resolved_at IS NULL",
                                (row[0],)).fetchone():
                conn.execute("UPDATE contacts SET conflict_at = NULL WHERE id = ?", (row[0],))

    def acknowledge_conflicts(self, cid: int) -> None:
        """The person has looked: the marker goes (D6)."""
        with self._write() as conn:
            conn.execute("UPDATE contact_conflicts SET resolved_at = ? WHERE contact_id = ? AND resolved_at IS NULL",
                         (self._now(), cid))
            conn.execute("UPDATE contacts SET conflict_at = NULL WHERE id = ?", (cid,))

    # ------------------------------------------------------------------ merge
    def merge(self, survivor: int, others: Sequence[int], card: Optional[Card] = None, *,
              actor: str = "user", batch_id: Optional[str] = None) -> str:
        """Fold `others` into `survivor` (D9, 6g). Returns the batch id that undoes the merge.

        The survivor takes `card` (or keeps its own), inherits each account link
        it lacks, and a surplus link to an account the survivor is already in
        stays on the folded-in contact in the bin as `pending_remove`, so the
        duplicate leaves that account at the next push.

        The survivor also gains a copy of each folded contact's sources (where its
        details came from) that it does not already hold; the folded contact keeps
        its own. The ids of the copies are journalled (`detail["sources"]`) so the
        undo removes exactly them.
        """
        batch = batch_id or uuid.uuid4().hex
        with self._write() as conn:
            s_before = self.snapshot(survivor, conn, with_links=True)
            if s_before is None:
                return batch
            have = {r["account_id"] for r in conn.execute(
                "SELECT account_id FROM contact_links WHERE contact_id = ?", (survivor,))}
            folded: list[int] = []
            copied: list[int] = []
            for oid in others:
                if oid == survivor:
                    continue
                o_before = self.snapshot(oid, conn, with_links=True)
                if o_before is None:
                    continue
                for l in conn.execute("SELECT * FROM contact_links WHERE contact_id = ?", (oid,)).fetchall():
                    if l["account_id"] in have:
                        if l["state"] == "pending_create":
                            conn.execute("DELETE FROM contact_links WHERE id = ?", (l["id"],))
                        elif l["remote_id"] is not None:
                            conn.execute("UPDATE contact_links SET state = 'pending_remove' WHERE id = ?", (l["id"],))
                    else:
                        conn.execute("UPDATE contact_links SET contact_id = ?, hub_rev = 0 WHERE id = ?",
                                     (survivor, l["id"]))
                        have.add(l["account_id"])
                o_sources = self._copy_sources(conn, oid, survivor)
                copied += o_sources
                now = self._now()
                conn.execute("UPDATE contacts SET deleted_at = ?, merged_into = ?, updated_at = ?, "
                             "rev = rev + 1 WHERE id = ?", (now, survivor, now, oid))
                conn.execute("DELETE FROM contacts_fts WHERE rowid = ?", (oid,))
                self._record(conn, oid, actor, "merged", before=o_before, after=self.snapshot(oid, conn),
                             batch_id=batch, detail={"into": survivor, "links": o_before.get("links", []),
                                                     "sources": o_sources})
                folded.append(oid)
            if card is not None:
                self._store_card(conn, survivor, card)
                conn.execute("UPDATE contacts SET edited_at = ? WHERE id = ?", (self._now(), survivor))
            # every account link of the survivor must be re-agreed after the content changed
            conn.execute("UPDATE contacts SET rev = rev + 1 WHERE id = ?", (survivor,))
            self._record(conn, survivor, actor, "merge", before=s_before,
                         after=self.snapshot(survivor, conn, with_links=True), batch_id=batch,
                         detail={"folded": folded, "links": s_before.get("links", []), "sources": copied})
        return batch

    @staticmethod
    def _copy_sources(conn: Any, src: int, dst: int) -> list[int]:
        """Copy `src`'s sources onto `dst`, skipping any `dst` already holds (same value, source and
        record). Returns the ids of the new rows, for the journal."""
        made: list[int] = []
        for r in conn.execute("SELECT field_key, source, ref, at FROM contact_sources WHERE contact_id = ? "
                              "ORDER BY id", (src,)).fetchall():
            if conn.execute("SELECT 1 FROM contact_sources WHERE contact_id = ? AND field_key = ? AND source = ? "
                            "AND ref = ?", (dst, r["field_key"], r["source"], r["ref"])).fetchone():
                continue
            made.append(int(conn.execute(
                "INSERT INTO contact_sources (contact_id, field_key, source, ref, at) VALUES (?,?,?,?,?)",
                (dst, r["field_key"], r["source"], r["ref"], r["at"])).lastrowid))
        return made

    # --------------------------------------------------------------- rejected
    def reject_pair(self, a: int, b: int) -> None:
        """"Not a duplicate". What the two also hold in common - an office number, a family
        mailbox - is remembered as shared, so it never matches anyone to anyone again."""
        from app.contacts import dupes  # noqa: PLC0415
        a, b = sorted((int(a), int(b)))
        with self._write() as conn:
            conn.execute("INSERT OR IGNORE INTO contact_rejected (a_id, b_id, at) VALUES (?,?,?)",
                         (a, b, self._now()))
        ca, cb = self.get_card(a), self.get_card(b)
        if ca is not None and cb is not None:
            keys = dupes.shared_keys(ca, cb, self.region)
            if keys:
                self.mark_shared(keys)

    SHARED_KEY = "contacts:shared_values"

    def shared_values(self) -> set[str]:
        """Emails and numbers held by different people (`email:x@y`, `phone:+44...`)."""
        try:
            return {str(k) for k in json.loads(self.store.get_state(self.SHARED_KEY, "[]") or "[]")}
        except (ValueError, TypeError):
            return set()

    def mark_shared(self, keys: Iterable[str]) -> None:
        self.store.set_state(self.SHARED_KEY, json.dumps(sorted(self.shared_values() | set(keys))))

    def unmark_shared(self, keys: Iterable[str]) -> None:
        self.store.set_state(self.SHARED_KEY, json.dumps(sorted(self.shared_values() - set(keys))))

    def unreject_pair(self, a: int, b: int) -> None:
        a, b = sorted((int(a), int(b)))
        with self._write() as conn:
            conn.execute("DELETE FROM contact_rejected WHERE a_id = ? AND b_id = ?", (a, b))

    def rejected_pairs(self) -> set[tuple[int, int]]:
        return {(r[0], r[1]) for r in self.conn.execute("SELECT a_id, b_id FROM contact_rejected")}

    # ------------------------------------------------------------ smart groups
    def smart_groups(self) -> list[Smart]:
        return [Smart(r["id"], r["name"], json.loads(r["rules"] or "[]"), r["icon"])
                for r in self.conn.execute("SELECT * FROM contact_smart_groups ORDER BY name")]

    def save_smart_group(self, name: str, rules: list[dict], *, smart_id: Optional[int] = None,
                         icon: str = "sliders-horizontal") -> int:
        with self._write() as conn:
            if smart_id is not None:
                conn.execute("UPDATE contact_smart_groups SET name=?, rules=?, icon=? WHERE id=?",
                             (name, json.dumps(rules), icon, smart_id))
                return smart_id
            return int(conn.execute("INSERT INTO contact_smart_groups (name, rules, icon) VALUES (?,?,?)",
                                    (name, json.dumps(rules), icon)).lastrowid)

    def delete_smart_group(self, smart_id: int) -> None:
        with self._write() as conn:
            conn.execute("DELETE FROM contact_smart_groups WHERE id = ?", (smart_id,))

    def ids_for_rules(self, rules: list[dict], *, now_s: Optional[float] = None) -> set[int]:
        """Contacts matching every rule. Rule: {"field": ..., "op": ..., "value": ...}.

        Fields: company, title, name, email, phone, group, tag, account, notes, favourite,
        last_contacted. Ops: is, is_not, contains, empty, not_empty, older_than_days (last_contacted).
        """
        now_s = time.time() if now_s is None else now_s
        rows = {r.id: r for r in self._rows("WHERE c.deleted_at IS NULL")}
        out = set(rows)
        for rule in rules:
            keep = {cid for cid, r in rows.items() if cid in out and self._rule_matches(r, rule, now_s)}
            out &= keep
        return out

    def _rule_matches(self, r: ContactRow, rule: dict, now_s: float) -> bool:
        fld, op, val = rule.get("field", ""), rule.get("op", "is"), str(rule.get("value", "") or "")
        mine: Any
        if fld == "company":
            mine = r.company
        elif fld == "title":
            mine = r.title
        elif fld == "name":
            mine = r.display_name
        elif fld == "email":
            mine = r.email
        elif fld == "phone":
            mine = r.phone
        elif fld == "group":
            mine = r.groups
        elif fld == "tag":
            mine = r.tags
        elif fld == "account":
            mine = [str(a) for a in r.accounts]
        elif fld == "favourite":
            mine = "yes" if r.favourite else ""
        elif fld == "last_contacted":
            if op == "older_than_days":
                return r.last_contacted_at is None or (now_s - r.last_contacted_at / 1000) > float(val or 0) * 86400
            mine = r.last_contacted_at or ""
        else:
            return False
        if isinstance(mine, list):
            low = [m.casefold() for m in mine]
            if op == "empty":
                return not mine
            if op == "not_empty":
                return bool(mine)
            if op == "is_not":
                return val.casefold() not in low
            return val.casefold() in low
        text = str(mine or "").casefold()
        v = val.casefold()
        return {"is": text == v, "is_not": text != v, "contains": v in text,
                "empty": not text, "not_empty": bool(text)}.get(op, False)

    # ------------------------------------------------------------------- list
    def _rows(self, where: str = "", args: Sequence[Any] = ()) -> list[ContactRow]:
        conn = self.conn
        rows: dict[int, ContactRow] = {}
        for r in conn.execute(
                "SELECT c.id, c.uid, c.display_name, c.company, c.title, c.favourite, c.rev, c.edited_at, "
                "c.deleted_at, c.conflict_at, c.last_contacted_at, "
                "EXISTS (SELECT 1 FROM contact_photos p WHERE p.contact_id = c.id) AS has_photo "
                f"FROM contacts c {where} ORDER BY c.sort_key, c.id", tuple(args)):
            rows[r["id"]] = ContactRow(r["id"], r["uid"], r["display_name"], r["company"], r["title"], "", "",
                                       bool(r["favourite"]), r["rev"], r["edited_at"], r["deleted_at"],
                                       r["conflict_at"], r["last_contacted_at"], has_photo=bool(r["has_photo"]))
        if not rows:
            return []
        small = len(rows) <= 200
        id_clause = f"AND contact_id IN ({','.join(str(i) for i in rows)})" if small else ""
        for r in conn.execute("SELECT contact_id, kind, value FROM contact_fields "
                              f"WHERE kind IN ('email','phone') {id_clause} ORDER BY is_main DESC, position, id"):
            row = rows.get(r["contact_id"])
            if row is None:
                continue
            attr = "email" if r["kind"] == "email" else "phone"
            if not getattr(row, attr):
                setattr(row, attr, r["value"])
        for r in conn.execute("SELECT contact_id, account_id FROM contact_links WHERE "
                              f"state IN ('synced','pending_create','deleted_remote') {id_clause} ORDER BY account_id"):
            if r["contact_id"] in rows:
                rows[r["contact_id"]].accounts.append(r["account_id"])
        for r in conn.execute("SELECT m.contact_id, g.name FROM contact_group_members m "
                              "JOIN contact_groups g ON g.id = m.group_id "
                              f"WHERE 1 {id_clause.replace('contact_id', 'm.contact_id')} ORDER BY g.name"):
            if r["contact_id"] in rows:
                rows[r["contact_id"]].groups.append(r["name"])
        for r in conn.execute("SELECT m.contact_id, t.name FROM contact_tag_members m "
                              "JOIN contact_tags t ON t.id = m.tag_id "
                              f"WHERE 1 {id_clause.replace('contact_id', 'm.contact_id')} ORDER BY t.name"):
            if r["contact_id"] in rows:
                rows[r["contact_id"]].tags.append(r["name"])
        return list(rows.values())

    def list_rows(self, f: Optional[ContactFilter] = None) -> list[ContactRow]:
        f = f or ContactFilter()
        where, args = ["c.deleted_at IS NULL"], []
        if f.view == "bin":
            where = ["c.deleted_at IS NOT NULL", "c.merged_into IS NULL"]
        elif f.view == "favourites":
            where.append("c.favourite = 1")
        elif f.view == "group":
            where.append("c.id IN (SELECT contact_id FROM contact_group_members WHERE group_id = ?)")
            args.append(int(f.arg))
        elif f.view == "tag":
            where.append("c.id IN (SELECT contact_id FROM contact_tag_members WHERE tag_id = ?)")
            args.append(int(f.arg))
        elif f.view == "account":
            where.append("c.id IN (SELECT contact_id FROM contact_links WHERE account_id = ? "
                         "AND state IN ('synced','pending_create','deleted_remote'))")
            args.append(int(f.arg))
        elif f.view == "conflict":
            where.append("c.conflict_at IS NOT NULL")
        elif f.view == "deleted_elsewhere":
            where.append("c.id IN (SELECT contact_id FROM contact_links WHERE state = 'deleted_remote')")
        elif f.view == "single_account":
            where.append("(SELECT COUNT(*) FROM contact_links l WHERE l.contact_id = c.id "
                         "AND l.state IN ('synced','pending_create','deleted_remote')) = 1")
        elif f.view == "ids":
            ids = [int(i) for i in (f.arg or [])] or [-1]
            where.append(f"c.id IN ({','.join(str(i) for i in ids)})")
        elif f.view == "smart":
            sg = next((s for s in self.smart_groups() if s.id == int(f.arg)), None)
            ids = sorted(self.ids_for_rules(sg.rules)) if sg else []
            where.append(f"c.id IN ({','.join(str(i) for i in ids) or '-1'})")
        from app.contacts.slash import split

        free, rules = split(f.text)                           # `/company acme` and friends
        if rules:
            ids = sorted(self.ids_for_rules(rules))
            where.append(f"c.id IN ({','.join(str(i) for i in ids) or '-1'})")
        if free.strip():
            hits = self.search_ids(free, limit=100_000)
            where.append(f"c.id IN ({','.join(str(i) for i in hits) or '-1'})")
        rows = self._rows("WHERE " + " AND ".join(where), args)
        if f.sort == "company":
            rows.sort(key=lambda r: (r.company.casefold() or "￿", r.display_name.casefold()))
        elif f.sort == "edited":
            rows.sort(key=lambda r: -r.edited_at)
        elif f.sort == "contacted":
            rows.sort(key=lambda r: -(r.last_contacted_at or 0))
        return rows

    def account_counts(self) -> dict[int, dict[str, int]]:
        """Per account, the one set of numbers every screen shows (order 1t 3a, D3):

        * `kept` - live contacts kept in the account: the ones its entry in the Contacts list shows;
        * `linked` - of those, written there and agreed (what Leasha believes the account holds now);
        * `waiting_create` - kept, not written there yet;
        * `waiting_delete` - in the Bin here and still at the account, to be removed at the next push;
        * `deleted_there` - live here, deleted at the account (flagged, never deleted here on a hunch);
        * `not_kept` - live contacts the account does not keep at all.
        """
        out: dict[int, dict[str, int]] = {}
        alive = int(self.conn.execute("SELECT COUNT(*) FROM contacts WHERE deleted_at IS NULL").fetchone()[0])
        for r in self.conn.execute(
                "SELECT l.account_id AS a, l.state AS s, c.deleted_at IS NOT NULL AS binned, COUNT(*) AS n "
                "FROM contact_links l JOIN contacts c ON c.id = l.contact_id GROUP BY 1, 2, 3"):
            d = out.setdefault(int(r["a"]), {"kept": 0, "linked": 0, "waiting_create": 0, "waiting_delete": 0,
                                             "deleted_there": 0, "not_kept": 0})
            n, state, binned = int(r["n"]), r["s"], bool(r["binned"])
            if binned:
                if state in ("synced", "pending_remove"):
                    d["waiting_delete"] += n
                continue
            if state == "pending_remove":
                d["waiting_delete"] += n
                continue
            d["kept"] += n
            if state == "synced":
                d["linked"] += n
            elif state == "pending_create":
                d["waiting_create"] += n
            elif state == "deleted_remote":
                d["deleted_there"] += n
        for d in out.values():
            d["not_kept"] = max(0, alive - d["kept"])
        return out

    def counts(self) -> dict[str, int]:
        c = self.conn
        one = lambda sql: int(c.execute(sql).fetchone()[0])   # noqa: E731
        return {
            "all": one("SELECT COUNT(*) FROM contacts WHERE deleted_at IS NULL"),
            "favourites": one("SELECT COUNT(*) FROM contacts WHERE deleted_at IS NULL AND favourite = 1"),
            "conflict": one("SELECT COUNT(*) FROM contacts WHERE deleted_at IS NULL AND conflict_at IS NOT NULL"),
            "deleted_elsewhere": one("SELECT COUNT(DISTINCT contact_id) FROM contact_links l JOIN contacts c "
                                     "ON c.id = l.contact_id WHERE l.state = 'deleted_remote' AND c.deleted_at IS NULL"),
            "bin": one("SELECT COUNT(*) FROM contacts WHERE deleted_at IS NOT NULL AND merged_into IS NULL"),
            "single_account": one(
                "SELECT COUNT(*) FROM contacts c WHERE c.deleted_at IS NULL AND (SELECT COUNT(*) FROM "
                "contact_links l WHERE l.contact_id = c.id AND l.state IN ('synced','pending_create','deleted_remote')) = 1"),
        }

    # ----------------------------------------------------------------- search
    def search_ids(self, text: str, *, limit: int = 50) -> list[int]:
        q = _fts_query(text)
        if q:
            try:
                return [r[0] for r in self.conn.execute(
                    "SELECT rowid FROM contacts_fts WHERE contacts_fts MATCH ? ORDER BY rank LIMIT ?", (q, limit))]
            except Exception:                                  # noqa: BLE001 - fall back to LIKE below
                pass
        like = f"%{text.strip().casefold()}%"
        return [r[0] for r in self.conn.execute(
            "SELECT id FROM contacts WHERE deleted_at IS NULL AND (lower(display_name) LIKE ? OR lower(company) LIKE ? "
            "OR id IN (SELECT contact_id FROM contact_fields WHERE lower(value) LIKE ?)) LIMIT ?",
            (like, like, like, limit))]

    def search(self, text: str, *, limit: int = 20) -> list[ContactHit]:
        ids = self.search_ids(text, limit=limit)
        by_id = {r.id: r for r in self._rows(f"WHERE c.id IN ({','.join(str(i) for i in ids) or '-1'}) "
                                              "AND c.deleted_at IS NULL")}
        return [ContactHit(i, by_id[i].display_name, by_id[i].company, by_id[i].email, by_id[i].phone)
                for i in ids if i in by_id]

    def find_by_email(self, email: str) -> list[int]:
        key = email.strip().casefold()
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT f.contact_id FROM contact_fields f JOIN contacts c ON c.id = f.contact_id "
            "WHERE f.kind = 'email' AND f.norm = ? AND c.deleted_at IS NULL", (key,))]

    def find_by_phone(self, number: str) -> list[int]:
        key = phones.match_key(number, self.region)
        if not key:
            return []
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT f.contact_id FROM contact_fields f JOIN contacts c ON c.id = f.contact_id "
            "WHERE f.kind = 'phone' AND f.norm = ? AND c.deleted_at IS NULL", (key,))]


class _Null:
    """Lets a helper run inside the caller's open write transaction."""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def __enter__(self) -> Any:
        return self.conn

    def __exit__(self, *exc: Any) -> bool:
        return False


def _fts_query(text: str) -> str:
    tokens = [t for t in re.findall(r"\w+", text or "", flags=re.UNICODE) if t]
    return " ".join(f'"{t}"*' for t in tokens)
