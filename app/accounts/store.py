"""Accounts, their sync state, the sync log and the account settings.

Layer: L7

Order 1k 1b, 10. The rows live in the index database (migration 37). A secret is
never stored here: refresh tokens and app passwords are kept by
`app/accounts/secrets.py` in Windows Credential Manager, and this module has no
column or setting that could hold one.

The settings of Settings > Accounts (section 10) are one JSON document in
`index_state` under `accounts:settings` rather than keys in `.env`: they are the
person's choices about *their accounts*, they are read by the account worker and
the window and by nothing that starts before the index opens, and every one has a
control on the page (rule 11).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Optional

__all__ = [
    "PROVIDERS", "DIRECTIONS", "Account", "AccountStore", "AccountSettings", "ScheduleSetting",
    "SyncState", "SyncRun", "SETTINGS_KEY", "palette_colour",
]

PROVIDERS = ("google", "microsoft", "apple", "imap", "carddav")
DIRECTIONS = ("two-way", "in-only", "off")
SETTINGS_KEY = "accounts:settings"

#: Colours handed to new accounts in turn (the app's own palette tokens would be
#: better but the window's theme is not known down here; these read on both).
_PALETTE = ("#4C8BF5", "#E8710A", "#188038", "#A142F4", "#D93025", "#129EAF", "#B06000", "#C2185B")


def palette_colour(index: int) -> str:
    return _PALETTE[index % len(_PALETTE)]


@dataclass
class Account:
    id: int
    provider: str
    address: str
    name: str
    server: Optional[str] = None
    port: Optional[int] = None
    security: Optional[str] = None
    colour: str = "#4C8BF5"
    mail_on: bool = False
    contacts_on: bool = True
    contacts_direction: str = "two-way"
    position: int = 0
    added_at: int = 0
    last_synced_at: Optional[int] = None
    last_error: Optional[str] = None
    last_error_code: Optional[str] = None
    first_sync_done: bool = False
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def syncs_contacts(self) -> bool:
        return self.contacts_on and self.contacts_direction != "off"

    @property
    def writes_contacts(self) -> bool:
        return self.syncs_contacts and self.contacts_direction == "two-way"

    def option(self, key: str, default: Any = None) -> Any:
        return self.options.get(key, default)


@dataclass
class SyncState:
    account_id: int
    kind: str
    token: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)
    last_success_at: Optional[int] = None
    last_attempt_at: Optional[int] = None
    first_failure_at: Optional[int] = None
    failures: int = 0
    next_retry_at: Optional[int] = None


@dataclass
class SyncRun:
    id: int
    account_id: Optional[int]
    account_name: str
    kind: str
    trigger: str
    started_at: int
    finished_at: Optional[int]
    status: str
    dry_run: bool
    added: int
    changed: int
    deleted: int
    held: int
    error_code: Optional[str]
    error_text: Optional[str]
    plan: Optional[dict]
    undone_at: Optional[int]


@dataclass
class ScheduleSetting:
    """One schedule: `manual`, `interval` (every `minutes`) or `daily` (at `daily_at`)."""

    mode: str = "manual"
    minutes: int = 60                     # the interval once switched to a timed schedule
    daily_at: str = "02:00"


@dataclass
class AccountSettings:
    """Everything at the top of Settings > Accounts, and what the account worker reads."""

    # 2026-10-11, the owner: "as automated as possible, with the manual options still there as
    # options". Mail and contacts sync on their own every hour, and a first sync runs without
    # waiting to be approved; Manual, "Daily", and the dry run first are each one setting away.
    mail: ScheduleSetting = field(default_factory=lambda: ScheduleSetting(mode="interval", minutes=60))
    contacts: ScheduleSetting = field(default_factory=lambda: ScheduleSetting(mode="interval", minutes=60))
    same_for_all: bool = True
    paused_until: Optional[float] = None          # epoch seconds; None: not paused; inf: until turned back on
    quiet_on: bool = False
    quiet_start: str = "22:00"
    quiet_end: str = "07:00"
    first_fetch_kb_s: int = 0                     # 0: no cap
    conflict_policy: str = "newest"               # newest | priority | ask
    brake_count: int = 20
    brake_percent: float = 5.0
    dry_run_first: bool = False                   # True: the first sync of an account waits for Approve
    # 2026-10-11, the owner: one account is the master list - what it deletes is deleted everywhere
    # (to the Bin here, and from the other accounts), within the delete brake.
    master_account_id: Optional[int] = None
    # Keep every contact in every account that takes contacts back, so an add or an edit made in
    # any account reaches all of them through the local master. Off: "Kept in" is chosen per contact.
    mirror_contacts: bool = False
    # A copy of the contact book (a vCard file and a small database of the contact tables) is
    # written before a first sync and before a sync that deletes; the newest `contacts_backup_keep` stay.
    backup_contacts: bool = True
    contacts_backup_keep: int = 10
    notify_brake: bool = True
    notify_conflict: bool = True
    notify_signin: bool = True
    notify_failed_24h: bool = True
    default_account_id: Optional[int] = None
    phone_region: str = "GB"
    maps_site: str = "openstreetmap"
    google_client_id: str = ""
    microsoft_client_id: str = ""
    # enrichment (section 7): local ones on, internet ones off
    enrich_signatures: bool = True
    enrich_company: bool = True
    enrich_faces: bool = True
    enrich_history: bool = True
    enrich_suggest: bool = True
    enrich_gravatar: bool = False
    enrich_logo: bool = False
    enrich_wikidata: bool = False
    # order 1t (owner, 2026-10-12): after every contact sync, a read-only check that each account holds what
    # Leasha thinks it holds ("In step", or the contacts that differ, by name).
    check_after_sync: bool = True
    # order 1t 4c: when more than this share of an account's contacts arrive changed in the same way on one
    # sync, nothing is written back to that account until the person looks (0 turns the guard off).
    churn_guard_percent: int = 50

    @classmethod
    def from_json(cls, raw: Optional[str]) -> "AccountSettings":
        base = cls()
        if not raw:
            return base
        try:
            data = json.loads(raw)
        except ValueError:
            return base
        for f in fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            if f.name in ("mail", "contacts") and isinstance(value, dict):
                value = ScheduleSetting(**{k: v for k, v in value.items()
                                           if k in ScheduleSetting.__dataclass_fields__})
            try:
                setattr(base, f.name, value)
            except Exception:                                   # noqa: BLE001
                continue
        return base

    def to_json(self) -> str:
        d = asdict(self)
        if d.get("paused_until") == float("inf"):
            d["paused_until"] = 9e15
        return json.dumps(d, sort_keys=True)

    def is_paused(self, now: Optional[float] = None) -> bool:
        if self.paused_until is None:
            return False
        return (time.time() if now is None else now) < self.paused_until


class AccountStore:
    """Reads and writes `accounts` and the rows around it on an open `SqliteStore`."""

    def __init__(self, store: Any) -> None:
        self.store = store

    @property
    def conn(self):
        return self.store.conn

    # --------------------------------------------------------------- accounts
    @staticmethod
    def _account(r: Any) -> Account:
        return Account(
            id=r["id"], provider=r["provider"], address=r["address"], name=r["name"], server=r["server"],
            port=r["port"], security=r["security"], colour=r["colour"], mail_on=bool(r["mail_on"]),
            contacts_on=bool(r["contacts_on"]), contacts_direction=r["contacts_direction"],
            position=r["position"], added_at=r["added_at"], last_synced_at=r["last_synced_at"],
            last_error=r["last_error"], last_error_code=r["last_error_code"],
            first_sync_done=bool(r["first_sync_done"]), options=json.loads(r["options"] or "{}"))

    def add(self, provider: str, address: str, name: str = "", *, server: Optional[str] = None,
            port: Optional[int] = None, security: Optional[str] = None, colour: Optional[str] = None,
            mail_on: bool = False, contacts_on: bool = True, direction: str = "two-way",
            options: Optional[dict] = None) -> int:
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider {provider!r}")
        if direction not in DIRECTIONS:
            raise ValueError(f"unknown direction {direction!r}")
        with self.store.write() as conn:
            n = int(conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0])
            pos = int(conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM accounts").fetchone()[0])
            cur = conn.execute(
                "INSERT INTO accounts (provider, address, name, server, port, security, colour, mail_on, "
                "contacts_on, contacts_direction, position, added_at, options) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (provider, address.strip(), (name or address).strip(), server, port, security,
                 colour or palette_colour(n), int(mail_on), int(contacts_on), direction, pos,
                 int(time.time()), json.dumps(options or {})))
            return int(cur.lastrowid)

    def list(self) -> list[Account]:
        return [self._account(r) for r in self.conn.execute("SELECT * FROM accounts ORDER BY position, id")]

    def get(self, account_id: int) -> Optional[Account]:
        r = self.conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return self._account(r) if r else None

    def find(self, ref: str) -> Optional[Account]:
        """By id, name or address (the CLI's `--account X`)."""
        for a in self.list():
            if ref == str(a.id) or ref.casefold() in (a.name.casefold(), a.address.casefold()):
                return a
        return None

    _COLUMNS = {"name", "colour", "mail_on", "contacts_on", "contacts_direction", "server", "port",
                "security", "address", "first_sync_done", "last_synced_at", "last_error", "last_error_code",
                "position"}

    def update(self, account_id: int, **values: Any) -> None:
        opts = values.pop("options", None)
        cols = {k: (int(v) if isinstance(v, bool) else v) for k, v in values.items() if k in self._COLUMNS}
        unknown = set(values) - self._COLUMNS
        if unknown:
            raise ValueError(f"unknown account field(s): {sorted(unknown)}")
        if cols.get("contacts_direction") and cols["contacts_direction"] not in DIRECTIONS:
            raise ValueError("unknown direction")
        with self.store.write() as conn:
            if cols:
                conn.execute(f"UPDATE accounts SET {', '.join(k + ' = ?' for k in cols)} WHERE id = ?",
                             (*cols.values(), account_id))
            if opts is not None:
                row = conn.execute("SELECT options FROM accounts WHERE id = ?", (account_id,)).fetchone()
                cur = json.loads(row[0] or "{}") if row else {}
                cur.update(opts)
                conn.execute("UPDATE accounts SET options = ? WHERE id = ?", (json.dumps(cur), account_id))

    def reorder(self, ids: list[int]) -> None:
        with self.store.write() as conn:
            for pos, aid in enumerate(ids):
                conn.execute("UPDATE accounts SET position = ? WHERE id = ?", (pos, aid))

    def delete_rows(self, account_id: int) -> None:
        """Only the row (and, by cascade, its sync state and links). Use `manage.remove_account`."""
        with self.store.write() as conn:
            conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))

    # -------------------------------------------------------------- sync state
    def state(self, account_id: int, kind: str = "contacts") -> SyncState:
        r = self.conn.execute("SELECT * FROM account_sync_state WHERE account_id = ? AND kind = ?",
                              (account_id, kind)).fetchone()
        if r is None:
            return SyncState(account_id, kind)
        return SyncState(account_id, kind, r["token"], json.loads(r["extra"] or "{}"), r["last_success_at"],
                         r["last_attempt_at"], r["first_failure_at"], r["failures"], r["next_retry_at"])

    def save_token(self, account_id: int, kind: str, token: Optional[str], *, extra: Optional[dict] = None,
                   conn: Any = None) -> None:
        """Called with the page's changes in the same transaction (3a): the token and what it covers
        are saved together or not at all."""
        def run(c: Any) -> None:
            c.execute(
                "INSERT INTO account_sync_state (account_id, kind, token, extra) VALUES (?,?,?,?) "
                "ON CONFLICT(account_id, kind) DO UPDATE SET token = excluded.token, "
                "extra = CASE WHEN ? THEN excluded.extra ELSE account_sync_state.extra END",
                (account_id, kind, token, json.dumps(extra or {}), 1 if extra is not None else 0))
        if conn is not None:
            run(conn)
        else:
            with self.store.write() as c:
                run(c)

    def record_attempt(self, account_id: int, kind: str) -> None:
        with self.store.write() as conn:
            conn.execute(
                "INSERT INTO account_sync_state (account_id, kind, last_attempt_at) VALUES (?,?,?) "
                "ON CONFLICT(account_id, kind) DO UPDATE SET last_attempt_at = excluded.last_attempt_at",
                (account_id, kind, int(time.time())))

    def record_success(self, account_id: int, kind: str) -> None:
        now = int(time.time())
        with self.store.write() as conn:
            conn.execute(
                "INSERT INTO account_sync_state (account_id, kind, last_success_at, last_attempt_at) VALUES (?,?,?,?) "
                "ON CONFLICT(account_id, kind) DO UPDATE SET last_success_at = excluded.last_success_at, "
                "last_attempt_at = excluded.last_attempt_at, failures = 0, first_failure_at = NULL, "
                "next_retry_at = NULL", (account_id, kind, now, now))
            conn.execute("UPDATE accounts SET last_synced_at = ?, last_error = NULL, last_error_code = NULL "
                         "WHERE id = ?", (now, account_id))

    def record_failure(self, account_id: int, kind: str, code: str, text: str, *,
                       next_retry_at: Optional[int] = None) -> None:
        now = int(time.time())
        with self.store.write() as conn:
            conn.execute(
                "INSERT INTO account_sync_state (account_id, kind, last_attempt_at, first_failure_at, failures, "
                "next_retry_at) VALUES (?,?,?,?,1,?) ON CONFLICT(account_id, kind) DO UPDATE SET "
                "last_attempt_at = excluded.last_attempt_at, failures = account_sync_state.failures + 1, "
                "first_failure_at = COALESCE(account_sync_state.first_failure_at, excluded.first_failure_at), "
                "next_retry_at = excluded.next_retry_at", (account_id, kind, now, now, next_retry_at))
            conn.execute("UPDATE accounts SET last_error = ?, last_error_code = ? WHERE id = ?",
                         (text, code, account_id))

    def reset_token(self, account_id: int, kind: str = "contacts") -> None:
        with self.store.write() as conn:
            conn.execute("UPDATE account_sync_state SET token = NULL WHERE account_id = ? AND kind = ?",
                         (account_id, kind))

    # ---------------------------------------------------------------- sync log
    def start_run(self, account: Optional[Account], kind: str, trigger: str, *, dry_run: bool = False) -> int:
        with self.store.write() as conn:
            return int(conn.execute(
                "INSERT INTO sync_runs (account_id, account_name, kind, trigger, started_at, status, dry_run) "
                "VALUES (?,?,?,?,?,?,?)",
                (account.id if account else None, account.name if account else "", kind, trigger,
                 int(time.time()), "running", int(dry_run))).lastrowid)

    def finish_run(self, run_id: int, status: str, *, added: int = 0, changed: int = 0, deleted: int = 0,
                   held: int = 0, error_code: Optional[str] = None, error_text: Optional[str] = None,
                   plan: Optional[dict] = None) -> None:
        with self.store.write() as conn:
            conn.execute(
                "UPDATE sync_runs SET finished_at = ?, status = ?, added = ?, changed = ?, deleted = ?, held = ?, "
                "error_code = ?, error_text = ?, plan = ? WHERE id = ?",
                (int(time.time()), status, added, changed, deleted, held, error_code, error_text,
                 json.dumps(plan) if plan is not None else None, run_id))

    def save_progress(self, run_id: int, plan: dict, *, added: int = 0, changed: int = 0, deleted: int = 0,
                      held: int = 0, conn: Any = None) -> None:
        """What a running run has done so far, written as it goes (order 1t 2a), with a heartbeat (`beat`) so a
        run whose app went away can be told from one still working. Only a run still marked running is touched."""
        plan = dict(plan)
        plan["beat"] = int(time.time())
        sql = ("UPDATE sync_runs SET added = ?, changed = ?, deleted = ?, held = ?, plan = ? "
               "WHERE id = ? AND status = 'running'")
        args = (added, changed, deleted, held, json.dumps(plan), run_id)
        if conn is not None:
            conn.execute(sql, args)
            return
        with self.store.write() as c:
            c.execute(sql, args)

    #: A running run that has not written its progress for this long belongs to an app that went away (a batch
    #: is seconds; the longest wait the transport sleeps through is five minutes).
    STALE_AFTER_S = 30 * 60

    def close_stale_runs(self) -> int:
        """Label as stopped any run still marked running that a later run of the same account and
        kind has replaced: the app closed before it could say how it ended, and only one sync per
        account and kind runs at a time. It never touches the newest run, so a sync that is really
        in progress keeps its label, and a late `finish_run` still overwrites this one."""
        now = int(time.time())
        with self.store.write() as conn:
            closed = conn.execute(
                "UPDATE sync_runs SET status = 'interrupted', finished_at = ?, "
                "error_text = 'The app closed before this sync finished.' "
                "WHERE status = 'running' AND EXISTS (SELECT 1 FROM sync_runs n "
                "WHERE n.account_id IS sync_runs.account_id AND n.kind = sync_runs.kind AND n.id > sync_runs.id)",
                (now,)).rowcount
            # Order 1t 2b: also the newest run, when its heartbeat stopped long ago - its counts, written as it
            # went, stay on the row.
            for r in conn.execute("SELECT id, plan FROM sync_runs WHERE status = 'running' AND plan IS NOT NULL"
                                  ).fetchall():
                try:
                    beat = int((json.loads(r["plan"]) or {}).get("beat") or 0)
                except (ValueError, TypeError, AttributeError):
                    beat = 0
                if beat and now - beat > self.STALE_AFTER_S:
                    closed += conn.execute(
                        "UPDATE sync_runs SET status = 'interrupted', finished_at = ?, "
                        "error_text = 'The app closed before this sync finished.' WHERE id = ? AND status = 'running'",
                        (beat, r["id"])).rowcount
            return closed

    # ------------------------------------------------- refused contacts, and the check (order 1t 2c, 3c)
    #: Kept in `index_state` as one small JSON list per account (no new table, so a database written by this
    #: build still opens in the one before it).
    @staticmethod
    def _failures_key(account_id: int) -> str:
        return f"contacts:failures:{int(account_id)}"

    def failures(self, account_id: int) -> list[dict]:
        """Contacts the account refused one by one and that still wait: contact id, name, what was tried, the
        account's reason, first and last seen, attempts."""
        try:
            data = json.loads(self.store.get_state(self._failures_key(account_id)) or "[]")
            return [d for d in data if isinstance(d, dict)]
        except ValueError:
            return []

    def record_failures(self, account_id: int, failed: list[dict], written: set[int]) -> None:
        """After a push: add or refresh what was refused this time, drop what went through."""
        now = int(time.time())
        by_id = {int(d.get("contact_id") or 0): d for d in self.failures(account_id)}
        for cid in written:
            by_id.pop(int(cid), None)
        for f in failed:
            cid = int(f.get("contact_id") or 0)
            cur = by_id.get(cid) or {"contact_id": cid, "first_seen": now, "attempts": 0}
            cur.update(name=str(f.get("name") or ""), kind=str(f.get("kind") or ""), reason=str(f.get("reason") or ""),
                       last_seen=now, attempts=int(cur.get("attempts") or 0) + 1)
            by_id[cid] = cur
        rows = sorted(by_id.values(), key=lambda d: (-int(d.get("last_seen") or 0), str(d.get("name") or "")))[:500]
        self.store.set_state(self._failures_key(account_id), json.dumps(rows))

    def clear_failures(self, account_id: int) -> None:
        self.store.set_state(self._failures_key(account_id), "[]")

    def save_check(self, account_id: int, result: dict) -> None:
        self.store.set_state(f"contacts:check:{int(account_id)}", json.dumps(result))

    def check(self, account_id: int) -> Optional[dict]:
        """The last check of this account against Leasha (order 1t 3c), or None if it was never checked."""
        raw = self.store.get_state(f"contacts:check:{int(account_id)}")
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    @staticmethod
    def _run(r: Any) -> SyncRun:
        return SyncRun(r["id"], r["account_id"], r["account_name"] or "", r["kind"], r["trigger"],
                       r["started_at"], r["finished_at"], r["status"], bool(r["dry_run"]), r["added"],
                       r["changed"], r["deleted"], r["held"], r["error_code"], r["error_text"],
                       json.loads(r["plan"]) if r["plan"] else None, r["undone_at"])

    def runs(self, *, account_id: Optional[int] = None, kind: Optional[str] = None, limit: int = 200) -> list[SyncRun]:
        where, args = [], []
        if account_id is not None:
            where.append("account_id = ?")
            args.append(account_id)
        if kind:
            where.append("kind = ?")
            args.append(kind)
        sql = "SELECT * FROM sync_runs" + (" WHERE " + " AND ".join(where) if where else "")
        return [self._run(r) for r in self.conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))]

    def run(self, run_id: int) -> Optional[SyncRun]:
        r = self.conn.execute("SELECT * FROM sync_runs WHERE id = ?", (run_id,)).fetchone()
        return self._run(r) if r else None

    def last_run(self, account_id: int, kind: str = "contacts") -> Optional[SyncRun]:
        r = self.conn.execute("SELECT * FROM sync_runs WHERE account_id = ? AND kind = ? AND dry_run = 0 "
                              "ORDER BY id DESC LIMIT 1", (account_id, kind)).fetchone()
        return self._run(r) if r else None

    # ---------------------------------------------------------------- settings
    def settings(self) -> AccountSettings:
        return AccountSettings.from_json(self.store.get_state(SETTINGS_KEY))

    def save_settings(self, settings: AccountSettings) -> None:
        self.store.set_state(SETTINGS_KEY, settings.to_json())

    def update_settings(self, **values: Any) -> AccountSettings:
        cur = self.settings()
        for k, v in values.items():
            if not hasattr(cur, k):
                raise ValueError(f"unknown setting {k!r}")
            setattr(cur, k, v)
        self.save_settings(cur)
        return cur

    def default_account(self) -> Optional[Account]:
        """D5: the chosen account, else the first with contact sync on."""
        s = self.settings()
        if s.default_account_id is not None:
            a = self.get(s.default_account_id)
            if a is not None and a.writes_contacts:        # a default must be somewhere a contact can be written
                return a
        for a in self.list():
            if a.writes_contacts:
                return a
        return None
