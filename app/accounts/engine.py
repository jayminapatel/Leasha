"""The contact sync engine: inbound merge, outbound writes, dry run, brake, undo, and the check after a sync.

Layer: L7

Order 1k section 3 (D4-D10), and order 1t (2026-10-12: fast, and trusted). One engine for every provider: it
speaks only `ContactProvider` and `Card`, so the same code runs against Google, Microsoft, CardDAV and the fake
the tests use.

**A sync of several accounts has two phases** - pull from every account, then push to every account - so a
change made in one account reaches the others within the one sync (A2). Pulls run one after another (two
accounts bringing in the same new person at once must not both create it); **pushes run at the same time**, one
worker per account (1t D5: different hosts, different failure domains). Each account is its own failure domain
(3g): an account that fails is recorded and the rest carry on.

**Batched writes (1t 5b).** A provider that offers `create_many` / `update_many` / `delete_many` is written a
batch at a time (Google 200, Microsoft 20), and the answer to a write is the read-back when it carries the saved
record. One contact's conflict or refusal never fails its batch: a conflict is re-read, re-merged and retried on
its own (3c); a refusal is recorded (`AccountStore.record_failures`) and stays waiting. Each batch's links and
the run's progress are one transaction.

**Resumable (rule 4).** Each page of changes is applied and its token saved in one transaction, so a crash costs
at most the page in flight. The run's counts are written as it goes (1t 2a), so a run whose app went away still
says what it did.

**Never overwrites blind (3c).** An update carries the version tag it was based on; if the account refuses it
as stale, the engine re-reads that record, merges it again and retries, a few times, before giving up on that
one contact. A contact binned while its write was in flight stays on its way out (1t: six binned contacts were
marked synced again by a push that was writing them, and stayed at Google).

**Nothing is deleted on a hunch (D7).** A contact an account deleted is only *flagged*; deletes past the brake
(a count, or a share of the book) are held and named, **and everything else is still written** (1t D2).

**Nothing spreads by accident (1t 4c).** When one account returns most of its contacts changed in the same way,
nothing is written to any account until the person looks and presses Allow.

**The first sync of an account can be a dry run (D8).** It reads, plans and writes nothing until approved.

**A check after every sync (1t D4).** Read-only: the account's own list of ids and versions against the links,
stored as "in step" or the contacts that differ, by name.
"""

from __future__ import annotations

import json
import math
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.accounts.http import retry_at_of
from app.accounts.provider import (ContactProvider, RemoteContact, TokenExpired, VersionConflict, WriteResult)
from app.accounts.store import Account, AccountSettings, AccountStore
from app.contacts.card import Card, merge, project, units
from app.contacts.hub import ContactHub
from app.core.errors import AppError, AppErrorException, make_error, to_app_error
from app.core.logging import logger

__all__ = ["ContactSyncEngine", "SyncOutcome", "Plan", "RACE_RETRIES", "sample", "estimate_requests"]

_log = logger.bind(component="accounts.engine")

#: How many times a write that lost a race is re-read and re-merged before that
#: one contact is left for the next sync (3c). Three is enough for two clients
#: editing at once; more would be a loop against a server that never settles.
RACE_RETRIES = 3

_SAMPLE = 50

#: The churn guard (1t 4c) looks only at a sync where at least this many linked contacts changed: a handful of
#: edits made the same way is a person tidying, not a server rewriting every card.
_CHURN_MIN = 50

#: Requests per contact written one at a time, by kind - for `contacts sync --measure` (1t 1b). A connector that
#: says otherwise (`one_by_one_cost`) wins.
_ONE_BY_ONE = {"create": 2, "update": 2, "delete": 2}


def sample(names: list[str], name: str) -> None:
    if len(names) < _SAMPLE:
        names.append(name)


def estimate_requests(provider: Any, creates: int, updates: int, deletes: int) -> dict:
    """How many requests the waiting writes cost one at a time, and in batches (1t 1b). An estimate from the
    connector's own figures, not a measurement - `Plan.timing` is the measurement."""
    one = dict(_ONE_BY_ONE, **(getattr(provider, "one_by_one_cost", None) or {}))
    before = creates * one["create"] + updates * one["update"] + deletes * one["delete"]
    size = int(getattr(provider, "batch_size", 0) or 0)
    if size > 1 and callable(getattr(provider, "update_many", None)):
        cost = dict({"create": 1, "update": 1, "delete": 1}, **(getattr(provider, "batch_cost", None) or {}))
        after = sum(math.ceil(n / size) * cost[k] for k, n in
                    (("create", creates), ("update", updates), ("delete", deletes)) if n)
    else:
        after = before
    return {"one_by_one": before, "batched": after, "batch_size": size}


@dataclass
class Plan:
    """What a sync did - or, for a dry run, would do. Counts, with a few names."""

    add_in: int = 0               # new contacts in Leasha from the account
    merge_in: int = 0             # contacts already linked whose account side changed
    possible_duplicates: int = 0  # new from the account but matching a contact already here
    flagged_deleted: int = 0      # deleted at the account (kept here, D7.1)
    deleted_in: int = 0           # deleted at the master account, so deleted here and at the others
    create_out: int = 0           # written to the account for the first time
    update_out: int = 0
    delete_out: int = 0
    conflicts: int = 0
    held_conflicts: int = 0
    failed_out: int = 0           # contacts the account refused one by one (the sync went on without them)
    held_deletes: int = 0         # deletes past the brake, waiting for Allow (1t D2)
    failures: list[list] = field(default_factory=list)         # [name, why, contact id], the first few
    names: dict[str, list[str]] = field(default_factory=dict)
    timing: dict = field(default_factory=dict)                 # 1t 1a: seconds by phase, and the transport's meter

    def note(self, bucket: str, name: str) -> None:
        sample(self.names.setdefault(bucket, []), name)

    def fail(self, name: str, why: str, contact_id: Optional[int] = None) -> None:
        """One contact the account refused. Its id rides along (review CS-12), so the Sync window's Open contact
        works while the sync is still running, not only after a reload."""
        self.failed_out += 1
        if len(self.failures) < 50:
            self.failures.append([name, why] + ([int(contact_id)] if contact_id is not None else []))

    def to_json(self) -> dict:
        return {k: getattr(self, k) for k in (
            "add_in", "merge_in", "possible_duplicates", "flagged_deleted", "deleted_in", "create_out",
            "update_out", "delete_out", "conflicts", "held_conflicts", "failed_out", "held_deletes")} | {
                "names": self.names, "failures": self.failures, "timing": self.timing}

    @property
    def added(self) -> int:
        return self.add_in + self.create_out

    @property
    def changed(self) -> int:
        return self.merge_in + self.update_out

    @property
    def deleted(self) -> int:
        return self.delete_out + self.deleted_in

    @property
    def held(self) -> int:
        return self.flagged_deleted + self.held_conflicts + self.held_deletes


@dataclass
class SyncOutcome:
    account_id: int
    account_name: str
    run_id: int
    status: str                    # ok | partial | stopped | needs-approval | held | failed | skipped
    plan: Plan = field(default_factory=Plan)
    error: Optional[AppError] = None
    accounts_to_push: set[int] = field(default_factory=set)
    needs_approval: bool = False
    check: Optional[dict] = None   # 1t 3c: the check after the sync, when it ran


@dataclass
class _Ctx:
    run_id: int
    plan: Plan
    dry: bool
    policy: str
    trigger: str
    cancel: Callable[[], bool]
    full_seen: set[str] = field(default_factory=set)
    progress: Optional[Callable[[dict], None]] = None         # told what is happening, for the Sync window
    account: str = ""
    master: bool = False                                      # this account is the master list
    master_gone: set[int] = field(default_factory=set)        # contacts the master no longer has
    churn: Counter = field(default_factory=Counter)           # 1t 4c: how linked contacts changed at the account
    churned: int = 0                                          # linked contacts that arrived changed
    saved_at: float = 0.0                                     # when the run's progress was last written


class ContactSyncEngine:
    def __init__(self, store: Any, hub: ContactHub, accounts: AccountStore,
                 provider_for: Callable[[Account], ContactProvider], *,
                 clock: Callable[[], float] = time.time, parallel: bool = True) -> None:
        self.store = store
        self.hub = hub
        self.accounts = accounts
        self.provider_for = provider_for
        self._clock = clock
        self._parallel = parallel
        self._failed_lock = threading.Lock()

    # ------------------------------------------------------------------- API
    def sync(self, account_ids: Optional[list[int]] = None, *, trigger: str = "sync-now",
             approve_first: bool = False, allow_brake: bool = False, dry_run: bool = False,
             cancel: Callable[[], bool] = lambda: False,
             progress: Optional[Callable[[dict], None]] = None) -> list[SyncOutcome]:
        """Pull from then push to the accounts (all with contact sync on, by default).

        `progress` is called with a dict - `account`, `phase` (reading, writing, checking, done), `done`, `total`,
        `current`, `failed`, and the counts so far - as work goes on, from whichever thread runs that account.

        `allow_brake` is the person's Allow: deletes past the brake, and changes the churn guard held, go ahead.
        `dry_run` plans without writing, for any account (`contacts sync --dry-run`);
        the first sync of an account is a dry run by itself until approved (D8)."""
        settings = self.accounts.settings()
        chosen = [a for a in self.accounts.list() if a.syncs_contacts
                  and (account_ids is None or a.id in account_ids)]
        outcomes: dict[int, SyncOutcome] = {}
        providers: dict[int, ContactProvider] = {}
        if not dry_run and chosen:
            self._repair_vendor_fields()
        churn_hold: Optional[AppError] = None
        if not dry_run:
            if allow_brake:
                self._set_churn_hold(None)                    # the person looked and pressed Allow
            else:
                churn_hold = self._churn_hold()               # a hold from an earlier sync stands until Allow
        for acct in chosen:                                  # phase 1: pull, one account after another
            first_pending = (not acct.first_sync_done) and settings.dry_run_first and not approve_first
            dry = first_pending or dry_run
            run_id = self.accounts.start_run(acct, "contacts", trigger, dry_run=dry)
            out = SyncOutcome(acct.id, acct.name, run_id, "running")
            outcomes[acct.id] = out
            ctx = _Ctx(run_id, out.plan, dry, settings.conflict_policy, trigger, cancel,
                       progress=progress, account=acct.name, master=acct.id == settings.master_account_id)
            out.needs_approval = first_pending
            self._tell(ctx, "reading", 0, 0, acct.id)
            started = time.monotonic()
            try:
                self.accounts.record_attempt(acct.id, "contacts")
                provider = self.provider_for(acct)
                providers[acct.id] = provider
                self._pull(acct, provider, ctx, settings)
                if ctx.master and not ctx.dry:
                    self._apply_master_deletes(acct, ctx, settings, allow_brake)
                if ctx.dry:
                    self._plan_outbound(acct, provider, ctx)
                    out.status = "needs-approval" if first_pending else "dry-run"
                elif churn_hold is None and not allow_brake:
                    churn_hold = self._churn(acct, ctx, settings)
                    if churn_hold is not None:
                        self._set_churn_hold(churn_hold)
            except _Held as held:
                out.status, out.error = "held", held.error
            except Exception as exc:                          # noqa: BLE001 - one account never stops the rest
                self._fail(acct, out, exc)
            out.plan.timing["pull_s"] = round(time.monotonic() - started, 2)
            self._save(out, force=True)
        if settings.mirror_contacts and not dry_run and churn_hold is None:
            targets = [a.id for a in chosen if a.writes_contacts and outcomes[a.id].status == "running"
                       and (a.first_sync_done or not settings.dry_run_first or approve_first)]
            waiting = self._safe_duplicate_groups() if len(targets) > 1 else 0
            if waiting:
                # The same person held twice would be written to every account twice. Wait until they
                # are merged (Contacts > Find duplicates > Merge all safe matches, or
                # `leasha contacts merge-safe`); nothing is lost by waiting.
                _log.warning("mirror waits: {} group(s) of safe duplicates are not merged yet", waiting)
                for out in outcomes.values():
                    out.plan.note("mirror_waiting", f"{waiting} duplicate groups to merge first")
            elif len(targets) > 1:
                self.hub.mirror_to(targets)                   # every contact, in every account that takes them
        if churn_hold is not None:
            for acct in chosen:
                out = outcomes[acct.id]
                if out.status == "running":
                    out.status, out.error = "held", churn_hold
            _log.warning("churn guard: nothing written - {}", churn_hold.message)

        def push_and_check(acct: Account) -> None:        # phase 2, one worker per account
            out = outcomes[acct.id]
            if out.status == "running" and acct.writes_contacts:
                ctx = _Ctx(out.run_id, out.plan, False, settings.conflict_policy, trigger, cancel,
                           progress=progress, account=acct.name)
                started = time.monotonic()
                try:
                    completed = self._push(acct, providers[acct.id], ctx, settings, allow_brake=allow_brake)
                    if out.plan.held_deletes:
                        out.status = "held"
                        out.error = make_error("ERR_SYNC_DELETE_BRAKE", "accounts.engine",
                                               count=out.plan.held_deletes, account=acct.name)
                    else:
                        out.status = ("stopped" if not completed else "partial" if out.plan.failed_out else "ok")
                except _Held as held:
                    out.status, out.error = "held", held.error
                except Exception as exc:                      # noqa: BLE001
                    self._fail(acct, out, exc)
                out.plan.timing["push_s"] = round(time.monotonic() - started, 2)
            if out.status == "running":                       # in-only account: nothing to push
                out.status = "ok"
            if (settings.check_after_sync and acct.id in providers and out.status in ("ok", "partial", "held")
                    and not cancel()):
                ctx = _Ctx(out.run_id, out.plan, False, "", trigger, cancel, progress=progress, account=acct.name)
                started = time.monotonic()
                self._tell(ctx, "checking", 0, 0, acct.id)
                self._save(out)                               # the heartbeat, before the account is read
                out.check = self.check(acct, providers[acct.id])
                self._save(out)
                out.plan.timing["check_s"] = round(time.monotonic() - started, 2)

        busy = [a for a in chosen if not outcomes[a.id].status in ("needs-approval", "dry-run")]
        if self._parallel and len(busy) > 1:
            with ThreadPoolExecutor(max_workers=len(busy), thread_name_prefix="contacts-push") as pool:
                for f in [pool.submit(push_and_check, a) for a in busy]:
                    f.result()
        else:
            for acct in busy:
                push_and_check(acct)
        for acct in chosen:
            out = outcomes[acct.id]
            if out.status == "running":
                out.status = "ok"
            meter = getattr(providers.get(acct.id), "meter", None)
            if meter is not None and hasattr(meter, "snapshot"):
                out.plan.timing["transport"] = meter.snapshot()
            self._finish(acct, out)
            self._tell(_Ctx(out.run_id, out.plan, False, "", "", cancel, progress=progress, account=acct.name),
                       "done", 0, 0, acct.id, status=out.status)
        for p in providers.values():
            try:
                p.close()
            except Exception:                                 # noqa: BLE001 - closing is best effort
                _log.debug("provider close failed", exc_info=True)
        return list(outcomes.values())

    def push(self, account_ids: set[int], *, trigger: str = "undo", allow_brake: bool = False) -> list[SyncOutcome]:
        """Push only (no pull): what an undo needs, to put the old values back at the accounts."""
        settings = self.accounts.settings()
        out_list: list[SyncOutcome] = []
        for acct in self.accounts.list():
            if acct.id not in account_ids or not acct.writes_contacts:
                continue
            run_id = self.accounts.start_run(acct, "contacts", trigger)
            out = SyncOutcome(acct.id, acct.name, run_id, "running")
            ctx = _Ctx(run_id, out.plan, False, settings.conflict_policy, trigger, lambda: False)
            try:
                provider = self.provider_for(acct)
                try:
                    self._push(acct, provider, ctx, settings, allow_brake=allow_brake)
                    if out.plan.held_deletes:
                        out.status = "held"
                        out.error = make_error("ERR_SYNC_DELETE_BRAKE", "accounts.engine",
                                               count=out.plan.held_deletes, account=acct.name)
                    else:
                        out.status = "ok"
                finally:
                    provider.close()
            except _Held as held:
                out.status, out.error = "held", held.error
            except Exception as exc:                          # noqa: BLE001
                self._fail(acct, out, exc)
            self._finish(acct, out)
            out_list.append(out)
        return out_list

    # ----------------------------------------------------------------- check
    def check(self, acct: Account, provider: ContactProvider) -> Optional[dict]:
        """Read-only (1t 3c): the account's own list of ids and versions against what is linked here. Stored, and
        returned: the account's count, and by name what is at the account but not linked here, linked here but
        not at the account, and changed at the account since it was last read. None when the connector cannot
        list (it is then never claimed to be in step)."""
        lister = getattr(provider, "list_ids", None)
        if not callable(lister):
            return None
        try:
            remote = lister()
        except Exception as exc:                              # noqa: BLE001 - a check never fails a sync
            err = exc.error if isinstance(exc, AppErrorException) else None
            _log.warning("check of {} could not read the account: {}", acct.name, err.code if err else exc)
            result = {"at": int(self._clock()), "error": (err.message if err else str(exc))[:200]}
            self.accounts.save_check(acct.id, result)
            return result
        versions = {rid: (v[0] if isinstance(v, (tuple, list)) else str(v or "")) for rid, v in remote.items()}
        names = {rid: (v[1] if isinstance(v, (tuple, list)) and len(v) > 1 else "") for rid, v in remote.items()}
        rows = self.store.conn.execute(
            "SELECT l.contact_id, l.remote_id, l.state, l.version_tag, c.display_name, c.deleted_at "
            "FROM contact_links l JOIN contacts c ON c.id = l.contact_id WHERE l.account_id = ?",
            (acct.id,)).fetchall()
        linked_ids = {r["remote_id"] for r in rows if r["remote_id"]}
        missing_there: list[str] = []
        changed_there: list[str] = []
        for r in rows:
            if r["state"] != "synced" or not r["remote_id"] or r["deleted_at"] is not None:
                continue
            if r["remote_id"] not in versions:
                missing_there.append(r["display_name"] or str(r["contact_id"]))
            elif r["version_tag"] and versions[r["remote_id"]] and versions[r["remote_id"]] != r["version_tag"]:
                changed_there.append(r["display_name"] or str(r["contact_id"]))
        not_here = [names.get(rid) or rid for rid in versions if rid not in linked_ids]
        counts = self.hub.account_counts().get(acct.id, {})
        waiting = int(counts.get("waiting_create", 0)) + int(counts.get("waiting_delete", 0))
        result = {
            "at": int(self._clock()), "count": len(versions), "kept": int(counts.get("kept", 0)),
            "linked": int(counts.get("linked", 0)), "waiting_create": int(counts.get("waiting_create", 0)),
            "waiting_delete": int(counts.get("waiting_delete", 0)),
            "missing_there_n": len(missing_there), "missing_there": sorted(missing_there)[:_SAMPLE],
            "not_here_n": len(not_here), "not_here": sorted(not_here)[:_SAMPLE],
            "changed_there_n": len(changed_there), "changed_there": sorted(changed_there)[:_SAMPLE],
        }
        result["in_step"] = not (missing_there or not_here or changed_there or waiting)
        self.accounts.save_check(acct.id, result)
        return result

    # ------------------------------------------------------------------ pull
    def _pull(self, acct: Account, provider: ContactProvider, ctx: _Ctx, settings: AccountSettings) -> None:
        state = self.accounts.state(acct.id, "contacts")
        token = None if ctx.dry else state.token
        extra = dict(state.extra)
        full = token is None
        if full and not ctx.dry:
            ctx.full_seen = set(extra.get("seen", [])) if extra.get("full_in_progress") else set()
        while True:
            try:
                completed = self._pull_pages(acct, provider, ctx, token, full)
                break
            except TokenExpired:
                _log.info("sync token for {} expired - pulling everything again", acct.name)
                token, full, ctx.full_seen = None, True, set()
                if not ctx.dry:
                    self.accounts.reset_token(acct.id, "contacts")
        if completed and full and not ctx.dry:
            self._flag_unseen(acct, ctx)

    def _pull_pages(self, acct: Account, provider: ContactProvider, ctx: _Ctx, token: Optional[str],
                    full: bool) -> bool:
        read = 0
        for page in provider.pull(token):
            if ctx.cancel():
                return False
            read += len(page.changes)
            self._tell(ctx, "reading", read, 0, acct.id)
            if ctx.dry:
                for rc in page.changes:
                    self._inbound(acct, provider, rc, ctx)
                continue
            with self.store.batch() as conn:
                for rc in page.changes:
                    if full:
                        ctx.full_seen.add(rc.remote_id)
                    self._inbound(acct, provider, rc, ctx)
                extra = ({"full_in_progress": not page.done, "seen": sorted(ctx.full_seen) if not page.done else []}
                         if full else {})
                self.accounts.save_token(acct.id, "contacts", page.next_token,
                                         extra=extra if full else None, conn=conn)
                self._save_ctx(ctx, conn=conn)
        return True

    def _flag_unseen(self, acct: Account, ctx: _Ctx) -> None:
        """After a complete full pull, links the account did not return are deleted there (D7.1)."""
        with self.store.batch() as conn:
            for r in conn.execute("SELECT contact_id, remote_id FROM contact_links WHERE account_id = ? "
                                  "AND state = 'synced' AND remote_id IS NOT NULL", (acct.id,)).fetchall():
                if r["remote_id"] not in ctx.full_seen:
                    if ctx.master:
                        ctx.master_gone.add(r["contact_id"])
                        continue
                    self.hub.flag_deleted_remote(r["contact_id"], acct.id, sync_id=ctx.run_id, conn=conn)
                    ctx.plan.flagged_deleted += 1
            self.accounts.save_token(acct.id, "contacts", self.accounts.state(acct.id, "contacts").token,
                                     extra={"full_in_progress": False, "seen": []}, conn=conn)

    def _apply_master_deletes(self, acct: Account, ctx: _Ctx, settings: AccountSettings,
                              allow_brake: bool) -> None:
        """What the master account no longer holds is deleted here (to the Bin, so it can be put
        back) and, at the next push, at the other accounts.

        The delete brake stands in front of it: a master that suddenly returns nothing - an outage,
        a revoked permission - must not empty the book. Past the limit the contacts are only
        flagged "deleted elsewhere" and the run is held; pressing Allow carries them out, because
        a flagged contact of the master counts again on every later sync."""
        hub = self.hub
        gone = set(ctx.master_gone)
        for r in self.store.conn.execute(
                "SELECT l.contact_id FROM contact_links l JOIN contacts c ON c.id = l.contact_id "
                "WHERE l.account_id = ? AND l.state = 'deleted_remote' AND c.deleted_at IS NULL", (acct.id,)):
            gone.add(int(r[0]))
        alive = sorted(c for c in gone if (m := hub.contact_meta(c)) is not None and m["deleted_at"] is None)
        if not alive:
            return
        if _too_many(len(alive), max(1, hub.count(include_bin=True)), settings) and not allow_brake:
            for cid in alive:
                hub.flag_deleted_remote(cid, acct.id, sync_id=ctx.run_id)
            ctx.plan.flagged_deleted += len(alive)
            err = make_error("ERR_SYNC_DELETE_BRAKE", "accounts.engine", count=len(alive), account=acct.name)
            self.accounts.finish_run(ctx.run_id, "held", error_code=err.code, error_text=err.message,
                                     plan=ctx.plan.to_json(), held=len(alive))
            raise _Held(err)
        for cid in alive:
            ctx.plan.note("deleted_in", self._name(cid))
            hub.drop_link(cid, acct.id)                       # already gone there: nothing to delete
            hub.delete(cid, actor=f"account:{acct.id}", sync_id=ctx.run_id)
            ctx.plan.deleted_in += 1

    _CHURN_KEY = "contacts:churn-hold"

    def _churn_hold(self) -> Optional[AppError]:
        """A churn hold from an earlier sync, until Allow: the changes it held are already in the book, so a sync
        that simply carried on would spread them (CodeRabbit on #70)."""
        try:
            raw = self.store.get_state(self._CHURN_KEY)
            ctx = json.loads(raw) if raw else None
        except Exception:                                     # noqa: BLE001 - no hold readable: none
            return None
        if not ctx:
            return None
        return make_error("ERR_SYNC_CHANGES_HELD", "accounts.engine", **{k: str(v) for k, v in ctx.items()})

    def _set_churn_hold(self, err: Optional[AppError]) -> None:
        try:
            self.store.set_state(self._CHURN_KEY, json.dumps(
                {k: err.context.get(k) for k in ("account", "count", "linked", "what")} if err is not None else {}))
        except Exception:                                     # noqa: BLE001
            _log.debug("churn hold not saved", exc_info=True)

    def _churn(self, acct: Account, ctx: _Ctx, settings: AccountSettings) -> Optional[AppError]:
        """1t 4c: did this account return most of its linked contacts changed in one and the same way?"""
        pct = int(getattr(settings, "churn_guard_percent", 0) or 0)
        if pct <= 0 or ctx.churned < _CHURN_MIN or not ctx.churn:
            return None
        linked = int(self.store.conn.execute(
            "SELECT COUNT(*) FROM contact_links WHERE account_id = ? AND state = 'synced'", (acct.id,)).fetchone()[0])
        what, n = ctx.churn.most_common(1)[0]
        if linked <= 0 or n * 100 < pct * linked:
            return None
        return make_error("ERR_SYNC_CHANGES_HELD", "accounts.engine", account=acct.name, count=f"{n:,}",
                          linked=f"{linked:,}", what=what)

    def _inbound(self, acct: Account, provider: ContactProvider, rc: RemoteContact, ctx: _Ctx) -> None:
        caps = provider.caps
        hub = self.hub
        link = hub.link_by_remote(acct.id, rc.remote_id)
        actor = f"account:{acct.id}"
        if rc.card is None:                                   # deleted at the account
            if link is None:
                return
            if link.state == "pending_remove":                # our own delete, echoed back
                if not ctx.dry:
                    hub.drop_link(link.contact_id, acct.id)
                return
            if link.state == "deleted_remote":
                return
            if ctx.master:                                    # the master's delete is everyone's
                if ctx.dry:
                    ctx.plan.deleted_in += 1
                    ctx.plan.note("deleted_in", self._name(link.contact_id))
                else:
                    ctx.master_gone.add(link.contact_id)
                return
            ctx.plan.flagged_deleted += 1
            ctx.plan.note("flagged_deleted", self._name(link.contact_id))
            if not ctx.dry:
                hub.flag_deleted_remote(link.contact_id, acct.id, sync_id=ctx.run_id)
            return
        remote_card = project(rc.card, caps, hub.region)
        remote_ms = int(rc.edited_at * 1000) if rc.edited_at else int(self._clock() * 1000)
        if link is None:
            same = self._same_person(remote_card, acct)
            if same is not None:
                # The master already holds this person (the same email or mobile, and a name that
                # agrees): the account's record becomes one more link of that contact, not a second
                # contact. Anything less certain is added as a new contact and left to the
                # Duplicates screen (D9).
                ctx.plan.merge_in += 1
                ctx.plan.note("merge_in", _label(remote_card))
                if ctx.dry:
                    return
                hub.set_link(same, acct.id, remote_id=rc.remote_id, version_tag=rc.version, snapshot=None,
                             hub_rev=-1, state="synced")
                link = hub.link_by_remote(acct.id, rc.remote_id)
        if link is None:                                      # new here: a new contact, never auto-merged (D9)
            ctx.plan.add_in += 1
            ctx.plan.note("add_in", _label(remote_card))
            dupes = self._lookalikes(remote_card)
            if dupes:
                ctx.plan.possible_duplicates += 1
            if ctx.dry:
                return
            cid = hub.create(rc.card, actor=actor, sync_id=ctx.run_id, edited_at=remote_ms,
                             source=actor, source_ref=rc.remote_id)
            hub.set_link(cid, acct.id, remote_id=rc.remote_id, version_tag=rc.version,
                         snapshot=remote_card, hub_rev=hub.contact_meta(cid)["rev"], state="synced")
            return
        if link.state in ("pending_remove",):
            return
        cid = link.contact_id
        meta = hub.contact_meta(cid)
        if meta is None:
            return
        if link.state == "deleted_remote":                    # it is back at the account
            link.snapshot = None
        base = link.snapshot
        if base is not None:
            was, now = units(base, caps, hub.region), units(remote_card, caps, hub.region)
            if was == now:
                if not ctx.dry and rc.version and rc.version != link.version_tag:
                    hub.set_link(cid, acct.id, remote_id=rc.remote_id, version_tag=rc.version,
                                 snapshot=base, hub_rev=link.hub_rev, state="synced")
                return                                        # the account side did not change
            ctx.churned += 1
            ctx.churn[_change_shape(was, now)] += 1
        local = hub.get_card(cid)
        assert local is not None
        res = merge(base, local, remote_card, caps, local_time=meta["edited_at"] / 1000.0,
                    remote_time=remote_ms / 1000.0, policy=_policy(ctx.policy), region=hub.region)
        ctx.plan.merge_in += 1
        ctx.plan.note("merge_in", _label(local))
        ctx.plan.conflicts += len(res.conflicts)
        if res.held:
            ctx.plan.held_conflicts += sum(1 for c in res.conflicts if c.winner == "held")
        if ctx.dry:
            return
        for c in res.conflicts:
            self._record_conflict(cid, acct, c, ctx)
        if res.held:
            return                                            # leave the base alone: asked again next time
        if res.hub_changed:
            # the content now carries the account's edit, so its edit time is the account's
            hub.update(cid, res.card, actor=actor, action="sync", sync_id=ctx.run_id,
                       edited_at=max(meta["edited_at"], remote_ms))
        rev = hub.contact_meta(cid)["rev"]
        hub.set_link(cid, acct.id, remote_id=rc.remote_id, version_tag=rc.version, snapshot=remote_card,
                     hub_rev=-1 if res.push_needed else rev, state="synced")

    def _record_conflict(self, cid: int, acct: Account, c: Any, ctx: _Ctx) -> None:
        exists = self.store.conn.execute(
            "SELECT 1 FROM contact_conflicts WHERE contact_id = ? AND account_id = ? AND unit = ? "
            "AND resolved_at IS NULL", (cid, acct.id, c.unit)).fetchone()
        if exists:
            return
        self.hub.add_conflict(cid, acct.id, c.unit, c.local, c.remote, c.winner)
        self.hub.note(cid, f"account:{acct.id}", "conflict", sync_id=ctx.run_id,
                      detail={"unit": c.unit, "local": _jsonable(c.local), "remote": _jsonable(c.remote),
                              "winner": c.winner})

    def _safe_duplicate_groups(self) -> int:
        """How many groups of contacts are certainly one person held more than once."""
        from app.contacts import dupes  # noqa: PLC0415
        groups = dupes.scan_duplicates(self.hub.all_cards(), self.hub.rejected_pairs(), self.hub.region,
                                       self.hub.shared_values())
        return len(dupes.safe_groups(groups))

    def _same_person(self, card: Card, acct: Account) -> Optional[int]:
        """The one contact in the master that is certainly this person, or None.

        Certain means: it shares an email address or a mobile number with `card`, that value is
        not one marked as shared, the names agree, the account holds no record for it yet, and
        no other contact qualifies as well (two candidates is a doubt, so nothing is joined)."""
        from app.contacts import dupes, phone as phones  # noqa: PLC0415
        hub = self.hub
        shared = hub.shared_values()
        found: set[int] = set()
        for f in card.fields:
            if f.kind == "email" and f.value and f"email:{f.value.strip().casefold()}" not in shared:
                found.update(hub.find_by_email(f.value))
            elif f.kind == "phone" and f.value and phones.is_mobile(f.value, hub.region):
                e164 = phones.normalise(f.value, hub.region)
                if e164 and f"phone:{e164}" not in shared:
                    found.update(hub.find_by_phone(f.value))
        good: list[int] = []
        for cid in sorted(found):
            if hub.link(cid, acct.id) is not None:
                continue
            other = hub.get_card(cid)
            if other is not None and dupes.names_compatible(other, card):
                good.append(cid)
        return good[0] if len(good) == 1 else None

    def _lookalikes(self, card: Card) -> list[int]:
        out: list[int] = []
        for f in card.fields:
            if f.kind == "email" and f.value:
                out += self.hub.find_by_email(f.value)
            elif f.kind == "phone" and f.value:
                out += self.hub.find_by_phone(f.value)
        return sorted(set(out))

    def _name(self, cid: int) -> str:
        row = self.hub.get_row(cid)
        return row.display_name if row else str(cid)

    # ---------------------------------------------------- vendor fields (1t 4b)
    _REPAIR_KEY = "contacts:repair:vendor-fields"

    def _repair_vendor_fields(self) -> int:
        """Once: take out of the book the custom values that were a server's own bookkeeping (`X-FC-CLUSTER-ID`
        read as "fc cluster id" on every contact, 2026-10-12). One journal batch, so the sync log's Undo puts
        them back. The next push removes them from the accounts that were sent them."""
        try:
            if self.store.get_state(self._REPAIR_KEY):
                return 0
        except Exception:                                     # noqa: BLE001 - a store without index_state
            return 0
        from app.contacts.card import is_server_internal as internal  # noqa: PLC0415

        batch = f"repair-vendor-fields-{uuid.uuid4().hex[:8]}"
        fixed = 0
        for (cid,) in self.store.conn.execute(
                "SELECT DISTINCT contact_id FROM contact_fields WHERE kind = 'custom'").fetchall():
            card = self.hub.get_card(int(cid))
            if card is None:
                continue
            keep = [f for f in card.fields if not (f.kind == "custom" and internal(f.label))]
            if len(keep) == len(card.fields):
                continue
            card.fields = keep
            if self.hub.update(int(cid), card, actor="repair", action="update", batch_id=batch, touch_edit=False):
                fixed += 1
        # An account that shows custom values to the person (Google) was sent them: its base is forgotten for
        # those contacts, so the next push writes each once and they leave the account too. A CardDAV server's own
        # property stays where it is - the server adds it again, and it is no longer read.
        shown = [a.id for a in self.accounts.list() if a.provider == "google"]
        cleared = 0
        for aid in shown:
            rows = self.store.conn.execute(
                "SELECT contact_id, snapshot FROM contact_links WHERE account_id = ? AND state = 'synced' "
                "AND snapshot LIKE '%\"custom\"%'", (aid,)).fetchall()
            stale = []
            for r in rows:
                try:
                    snap = Card.from_json(json.loads(r["snapshot"] or "{}"))
                except (ValueError, TypeError, KeyError):
                    continue
                if any(f.kind == "custom" and internal(f.label) for f in snap.fields):
                    stale.append(int(r["contact_id"]))
            if stale:
                with self.store.write() as conn:
                    conn.executemany("UPDATE contact_links SET snapshot = '{}', hub_rev = -1 WHERE contact_id = ? "
                                     "AND account_id = ?", [(c, aid) for c in stale])
                cleared += len(stale)
        self.store.set_state(self._REPAIR_KEY, str(int(self._clock())))
        if cleared:
            _log.info("{} contacts at Google carry a server's bookkeeping value; each is written once to remove it",
                      cleared)
        if fixed:
            _log.info("removed server bookkeeping values from {} contacts (journal batch {})", fixed, batch)
        return fixed

    # ------------------------------------------------------------------ push
    def _plan_outbound(self, acct: Account, provider: ContactProvider, ctx: _Ctx) -> None:
        if not acct.writes_contacts:
            return
        n = Counter()
        for kind, link, cid in self._dirty(acct, heal=False):
            bucket = {"create": "create_out", "update": "update_out", "delete": "delete_out"}[kind]
            setattr(ctx.plan, bucket, getattr(ctx.plan, bucket) + 1)
            ctx.plan.note(bucket, self._name(cid))
            n[kind] += 1
        ctx.plan.timing["estimate"] = estimate_requests(provider, n["create"], n["update"], n["delete"])

    def _dirty(self, acct: Account, *, heal: bool = True) -> list[tuple[str, Any, int]]:
        """What would be written to `acct`: (kind, link, contact id).

        A link still marked synced to a contact in the Bin is on its way out: binning leaves every account
        (`ContactHub._leave_accounts`), and a write that finished after the bin once marked it synced again
        (1t). It is put back to `pending_remove` here, so it is deleted like any other."""
        out: list[tuple[str, Any, int]] = []
        rows = self.store.conn.execute(
            "SELECT l.id, l.contact_id, l.state, l.hub_rev, l.remote_id, c.rev, c.deleted_at FROM contact_links l "
            "JOIN contacts c ON c.id = l.contact_id WHERE l.account_id = ? "
            "AND l.state IN ('pending_create','synced','pending_remove')", (acct.id,)).fetchall()
        healed = [r["contact_id"] for r in rows
                  if r["state"] == "synced" and r["deleted_at"] is not None and r["remote_id"]]
        if healed and heal:                                   # a dry run plans them as deletes, writes nothing
            with self.store.write() as conn:
                conn.executemany("UPDATE contact_links SET state = 'pending_remove' WHERE contact_id = ? "
                                 "AND account_id = ? AND state = 'synced'", [(c, acct.id) for c in healed])
            _log.info("{}: {} binned contact(s) still marked as kept there - they are removed at this push",
                      acct.name, len(healed))
        healed_set = set(healed)
        for r in rows:
            link = self.hub.link(r["contact_id"], acct.id)
            if r["state"] == "pending_remove" or r["contact_id"] in healed_set:
                out.append(("delete", link, r["contact_id"]))
            elif r["deleted_at"] is not None:
                continue
            elif r["state"] == "pending_create":
                out.append(("create", link, r["contact_id"]))
            elif r["hub_rev"] != r["rev"]:
                out.append(("update", link, r["contact_id"]))
        return out

    def _push(self, acct: Account, provider: ContactProvider, ctx: _Ctx, settings: AccountSettings, *,
              allow_brake: bool) -> bool:
        """Write what is waiting. Returns False when the person stopped it part-way.

        **One contact the account refuses never stops the rest** (2026-10-11). It is listed in the plan and
        in the account's failures with the account's reason, stays waiting, and the next sync tries it again;
        only a failure that is the account's as a whole (sign-in, rate limit, no connection), or fifteen
        refusals in a row, ends the run.

        **Deletes past the brake are held, and only they** (1t D2): creates and updates are written, the
        deletes wait - named in the plan - until the person presses Allow."""
        work = self._dirty(acct)
        deletes = [w for w in work if w[0] == "delete"]
        if deletes and _too_many(len(deletes), max(1, self.hub.count(include_bin=True)), settings) \
                and not allow_brake:
            ctx.plan.held_deletes = len(deletes)
            for _k, _l, cid in deletes:
                ctx.plan.note("held_deletes", self._name(cid))
            work = [w for w in work if w[0] != "delete"]
            _log.warning("{}: {} deletes held by the brake; the rest is written", acct.name, len(deletes))
        creates = [w for w in work if w[0] == "create"]
        updates = [w for w in work if w[0] == "update"]
        deletes = [w for w in work if w[0] == "delete"]
        p = _Pusher(self, acct, provider, ctx, total=len(work))
        try:
            batched = int(getattr(provider, "batch_size", 0) or 0) > 1
            for kind, items in (("create", creates), ("update", updates), ("delete", deletes)):
                if not items:
                    continue
                many = getattr(provider, f"{kind}_many", None)
                if batched and callable(many):
                    if not p.batched(kind, items, many):
                        return False
                elif not p.one_by_one(kind, items):
                    return False
            self._tell(ctx, "writing", len(work), len(work), acct.id)
            return True
        finally:
            p.close()

    def _tell(self, ctx: _Ctx, phase: str, done: int, total: int, account_id: int, *, current: str = "",
              status: str = "") -> None:
        """Say what is happening to whoever listens (the Sync window). Never raises, never waits."""
        if ctx.progress is None:
            return
        now = time.monotonic()
        last = getattr(ctx, "_told_at", 0.0)
        if phase == "writing" and 0 < done < total and now - last < 0.25:
            return                                            # a window needs a few updates a second, not thousands
        ctx._told_at = now                                    # type: ignore[attr-defined]
        p = ctx.plan
        try:
            ctx.progress({"account": ctx.account, "account_id": account_id, "phase": phase, "done": done,
                          "total": total, "current": current, "status": status, "failed": p.failed_out,
                          "added": p.add_in + p.create_out, "changed": p.merge_in + p.update_out,
                          "deleted": p.delete_out + p.deleted_in, "held_deletes": p.held_deletes,
                          "failures": list(p.failures[-5:])})
        except Exception:                                     # noqa: BLE001 - a listener never breaks a sync
            _log.debug("progress listener failed", exc_info=True)

    def _save_ctx(self, ctx: _Ctx, *, conn: Any = None, force: bool = False) -> None:
        """The run's counts so far, written as it goes (1t 2a): at most every two seconds, unless forced."""
        if ctx.dry:
            return
        now = time.monotonic()
        if not force and now - ctx.saved_at < 2.0:
            return
        ctx.saved_at = now
        p = ctx.plan
        try:
            self.accounts.save_progress(ctx.run_id, p.to_json(), added=p.added, changed=p.changed,
                                        deleted=p.deleted, held=p.held, conn=conn)
        except Exception:                                     # noqa: BLE001 - a label; never stops a sync
            _log.debug("progress of run {} not saved", ctx.run_id, exc_info=True)

    def _save(self, out: SyncOutcome, *, force: bool = False) -> None:
        if out.status not in ("running",):
            return
        p = out.plan
        try:
            self.accounts.save_progress(out.run_id, p.to_json(), added=p.added, changed=p.changed,
                                        deleted=p.deleted, held=p.held)
        except Exception:                                     # noqa: BLE001
            _log.debug("progress of run {} not saved", out.run_id, exc_info=True)

    def _settle(self, cid: int, acct: Account, *, remote_id: Optional[str], version_tag: str,
                snapshot: Optional[Card], hub_rev: int, conn: Any = None) -> None:
        """Record a write as agreed - unless the contact went to the Bin while it was being written, in which
        case it stays on its way out of the account (1t: a push marked six binned contacts synced again)."""
        c = conn or self.store.conn
        row = c.execute("SELECT c.deleted_at, l.state FROM contacts c LEFT JOIN contact_links l "
                        "ON l.contact_id = c.id AND l.account_id = ? WHERE c.id = ?", (acct.id, cid)).fetchone()
        leaving = row is not None and (row["deleted_at"] is not None or row["state"] == "pending_remove")
        self.hub.set_link(cid, acct.id, remote_id=remote_id, version_tag=version_tag, snapshot=snapshot,
                          hub_rev=hub_rev, state="pending_remove" if leaving else "synced", conn=conn)

    def _remote_delete(self, acct: Account, provider: ContactProvider, link: Any, ctx: _Ctx) -> None:
        if link.remote_id:
            version = link.version_tag
            for attempt in range(RACE_RETRIES + 1):
                try:
                    provider.delete(link.remote_id, version)
                    break
                except VersionConflict:
                    # The record's version moved. If what it holds is still what Leasha last saw, only the
                    # version changed (the account re-saved it): delete with the new one. If its content
                    # changed, rule 3c holds - nothing that changed meanwhile is deleted blind.
                    fresh = provider.get(link.remote_id)
                    if fresh is None or fresh.card is None:
                        break                                       # already gone
                    # A link joined by a merge-in has no base (2026-10-13: six binned contacts stayed in Gmail,
                    # their etag's field mask moved while their content did not). Without one, the contact as
                    # Leasha holds it is the measure: an account holding exactly that has nothing to lose.
                    held = link.snapshot
                    if held is None:
                        own = self.hub.get_card(link.contact_id)
                        held = project(own, provider.caps, self.hub.region) if own is not None else None
                    now = project(fresh.card, provider.caps, self.hub.region)
                    same = held is not None and units(now, provider.caps, self.hub.region) == units(
                        held, provider.caps, self.hub.region)
                    if not same or attempt == RACE_RETRIES:
                        raise
                    version = fresh.version
        self.hub.drop_link(link.contact_id, acct.id)
        ctx.plan.delete_out += 1

    def _remote_create(self, acct: Account, provider: ContactProvider, link: Any, cid: int, ctx: _Ctx) -> None:
        card = self.hub.get_card(cid)
        if card is None:
            return
        rev = self.hub.contact_meta(cid)["rev"]
        sent = project(card, provider.caps, self.hub.region)
        remote_id, version = provider.create(sent)
        self._settle(cid, acct, remote_id=remote_id, version_tag=version,
                     snapshot=self._held_after_write(provider, remote_id, sent), hub_rev=rev)
        ctx.plan.create_out += 1

    def _held_after_write(self, provider: ContactProvider, remote_id: str, sent: Card) -> Card:
        """What the account now holds for the record just written, as the merge base.

        An account silently keeps less than it was sent (Outlook holds three emails and one web
        page; a primary flag is not stored). Taking `sent` as the base would make the next pull
        read the missing values as *deleted at the account* and take them out of Leasha. The
        read-back keeps the base honest: Leasha keeps the extra, the account never had it."""
        try:
            fresh = provider.get(remote_id)
            if fresh is not None and fresh.card is not None:
                return project(fresh.card, provider.caps, self.hub.region)
        except Exception:                                     # noqa: BLE001 - the base falls back to what was sent
            _log.debug("read-back of {} failed; using what was sent", remote_id, exc_info=True)
        return sent

    def _remote_update(self, acct: Account, provider: ContactProvider, link: Any, cid: int, ctx: _Ctx) -> None:
        caps = provider.caps
        hub = self.hub
        if self._waiting_for_choice(cid, acct):
            return                                            # waiting for the person's choice
        for attempt in range(RACE_RETRIES + 1):
            local = hub.get_card(cid)
            link = hub.link(cid, acct.id)
            if local is None or link is None or link.remote_id is None:
                return
            sent = project(local, caps, hub.region)
            rev = hub.contact_meta(cid)["rev"]
            if link.snapshot is not None and units(sent, caps, hub.region) == units(link.snapshot, caps, hub.region):
                self._settle(cid, acct, remote_id=link.remote_id, version_tag=link.version_tag,
                             snapshot=link.snapshot, hub_rev=rev)
                return
            try:
                version = provider.update(link.remote_id, link.version_tag, sent)
            except VersionConflict:
                if attempt == RACE_RETRIES:
                    raise
                fresh = provider.get(link.remote_id)             # 3c: re-read, re-merge, retry
                if fresh is None or fresh.card is None:
                    hub.flag_deleted_remote(cid, acct.id, sync_id=ctx.run_id)
                    return
                self._inbound(acct, provider, fresh, ctx)
                continue
            self._settle(cid, acct, remote_id=link.remote_id, version_tag=version,
                         snapshot=self._held_after_write(provider, link.remote_id, sent), hub_rev=rev)
            ctx.plan.update_out += 1
            return

    def _waiting_for_choice(self, cid: int, acct: Account) -> bool:
        return self.store.conn.execute("SELECT 1 FROM contact_conflicts WHERE contact_id = ? AND account_id = ? "
                                       "AND resolved_at IS NULL AND winner = 'held'", (cid, acct.id)).fetchone() \
            is not None

    # ---------------------------------------------------------------- finish
    def _fail(self, acct: Account, out: SyncOutcome, exc: Exception) -> None:
        if isinstance(exc, AppErrorException):
            err = exc.error
        else:
            err = to_app_error(exc, "accounts.engine", "ERR_SYNC_FAILED", account=acct.name,
                               reason=f"{type(exc).__name__}: {exc}")
        err.context.setdefault("account", acct.name)
        out.status, out.error = "failed", err
        _log.warning("sync of {} failed: {}", acct.name, err.code)

    def _finish(self, acct: Account, out: SyncOutcome) -> None:
        p = out.plan
        if out.status == "held":
            run = self.accounts.run(out.run_id)
            if run is not None and run.status == "held":
                return                                        # `_apply_master_deletes` already closed the run
            err = out.error or make_error("ERR_SYNC_DELETE_BRAKE", "accounts.engine", count=p.held_deletes,
                                          account=acct.name)
            self.accounts.finish_run(out.run_id, "held", added=p.added, changed=p.changed, deleted=p.deleted,
                                     held=p.held, error_code=err.code, error_text=err.message, plan=p.to_json())
            if err.code == "ERR_SYNC_DELETE_BRAKE":
                # Everything else was written: as far as the schedule goes, this account synced.
                self.accounts.record_success(acct.id, "contacts")
            return
        if out.status == "failed" and out.error is not None:
            # A provider that said how long to wait is waited for that long, not 1, 2, 4 minutes (review CS-5).
            self.accounts.record_failure(acct.id, "contacts", out.error.code, out.error.message,
                                         next_retry_at=retry_at_of(out.error, self._clock()))
            self.accounts.finish_run(out.run_id, "failed", added=p.added, changed=p.changed, deleted=p.deleted,
                                     held=p.held, error_code=out.error.code, error_text=_with_fix(out.error),
                                     plan=p.to_json())
            return
        if out.status == "dry-run":
            self.accounts.finish_run(out.run_id, "dry-run", added=p.added, changed=p.changed,
                                     deleted=p.deleted, held=p.held, plan=p.to_json())
            return
        if out.status == "needs-approval":
            err = make_error("ERR_SYNC_NEEDS_APPROVAL", "accounts.engine", account=acct.name)
            out.error = err
            self.accounts.finish_run(out.run_id, "needs-approval", added=p.added, changed=p.changed,
                                     deleted=p.deleted, held=p.held, error_code=err.code,
                                     error_text=_with_fix(err), plan=p.to_json())
            return
        if out.status == "stopped":
            self.accounts.finish_run(
                out.run_id, "interrupted", added=p.added, changed=p.changed, deleted=p.deleted, held=p.held,
                error_text="You stopped this sync. What was written stays; the next sync carries on from there.",
                plan=p.to_json())
            return
        self.accounts.record_success(acct.id, "contacts")
        if not acct.first_sync_done:
            self.accounts.update(acct.id, first_sync_done=True)
        if out.status == "partial":
            first = "; ".join(f"{f[0]}: {f[1]}" for f in p.failures[:3])
            self.accounts.finish_run(
                out.run_id, "partial", added=p.added, changed=p.changed, deleted=p.deleted, held=p.held,
                error_text=f"{p.failed_out} contact(s) could not be written ({first}). They stay waiting; "
                           "the next sync tries them again.", plan=p.to_json())
            return
        self.accounts.finish_run(out.run_id, "ok", added=p.added, changed=p.changed, deleted=p.deleted,
                                 held=p.held, plan=p.to_json())

    # --------------------------------------------------------- conflict choice
    def resolve_conflict(self, contact_id: int, account_id: int, choose: str, *,
                         trigger: str = "conflict") -> SyncOutcome:
        """The person's choice for a clash held under 'ask me every time': `local` or `remote`."""
        acct = self.accounts.get(account_id)
        assert acct is not None and choose in ("local", "remote")
        link = self.hub.link(contact_id, account_id)
        provider = self.provider_for(acct)
        run_id = self.accounts.start_run(acct, "contacts", trigger)
        out = SyncOutcome(acct.id, acct.name, run_id, "running")
        ctx = _Ctx(run_id, out.plan, False, choose, trigger, lambda: False)
        try:
            fresh = provider.get(link.remote_id) if link and link.remote_id else None
            if fresh is not None and fresh.card is not None:
                with self.store.write() as conn:
                    conn.execute("UPDATE contact_conflicts SET resolved_at = ? WHERE contact_id = ? "
                                 "AND account_id = ? AND resolved_at IS NULL", (int(self._clock() * 1000),
                                                                              contact_id, account_id))
                self._inbound(acct, provider, fresh, ctx)
            self.hub.acknowledge_conflicts(contact_id)
            self._push(acct, provider, ctx, self.accounts.settings(), allow_brake=False)
            out.status = "held" if out.plan.held_deletes else "ok"
        except Exception as exc:                              # noqa: BLE001
            self._fail(acct, out, exc)
        finally:
            provider.close()
        self._finish(acct, out)
        return out


class _Pusher:
    """One account's writes for one push: one at a time, or a batch at a time (1t 5b). Records what the account
    refused (and what went through) in the account's failures list when it closes."""

    def __init__(self, engine: ContactSyncEngine, acct: Account, provider: ContactProvider, ctx: _Ctx, *,
                 total: int) -> None:
        self.e, self.acct, self.provider, self.ctx, self.total = engine, acct, provider, ctx, total
        self.done = 0
        self.in_a_row = 0
        self.refused: list[dict] = []
        self.written: set[int] = set()

    # -- the bookkeeping every write shares
    def _refuse(self, kind: str, cid: int, err: AppError, *, defer: bool = False,
                record_only: bool = False) -> Optional[AppError]:
        """Record a refusal. A refusal that ends the run (the account's as a whole, or too many in a row) is raised -
        or, with `defer`, returned, so a batch first commits what the account already accepted. `record_only`: the
        batch has already given up; the rest of its refusals are listed, nothing more (review of #70)."""
        if record_only:
            if err.code not in _ACCOUNT_WIDE:
                self._record(kind, cid, err)
            return None
        if err.code in _ACCOUNT_WIDE or self.in_a_row >= _GIVE_UP_AFTER:
            if defer:
                return err
            raise AppErrorException(err)
        self.in_a_row += 1
        self._record(kind, cid, err)
        return None

    def _record(self, kind: str, cid: int, err: AppError) -> None:
        name = self.e._name(cid)
        self.ctx.plan.fail(name, err.message, cid)
        self.refused.append({"contact_id": cid, "name": name, "kind": kind, "reason": err.message})
        _log.warning("{} could not be written to {}: {}", cid, self.acct.name, err.code)

    def _wrote(self, cid: int) -> None:
        self.in_a_row = 0
        self.written.add(cid)

    def close(self) -> None:
        if self.refused or self.written:
            try:
                self.e.accounts.record_failures(self.acct.id, self.refused, self.written)
            except Exception:                                 # noqa: BLE001 - a list; never fails a sync
                _log.debug("failures of {} not recorded", self.acct.name, exc_info=True)

    def _step(self, cid: int) -> bool:
        if self.ctx.cancel():
            return False
        self.e._tell(self.ctx, "writing", self.done, self.total, self.acct.id, current=self.e._name(cid))
        return True

    def _one(self, kind: str, link: Any, cid: int) -> None:
        """One contact on its own: the 1k path, and the fallback for a conflict inside a batch."""
        e = self.e
        try:
            if kind == "delete":
                e._remote_delete(self.acct, self.provider, link, self.ctx)
            elif kind == "create":
                e._remote_create(self.acct, self.provider, link, cid, self.ctx)
            else:
                e._remote_update(self.acct, self.provider, link, cid, self.ctx)
            self._wrote(cid)
        except VersionConflict:
            _log.warning("gave up on {} for {}: it keeps changing; next sync tries again", cid, self.acct.name)
            # Listed, so the run says "partial" with the name rather than "ok" over a write that never happened.
            self._record(kind, cid, make_error("ERR_ACCOUNT_REJECTED", "accounts.engine", account=self.acct.name,
                                               reason="it changed at the account since Leasha last read it"))
        except AppErrorException as exc:
            self._refuse(kind, cid, exc.error)

    def one_by_one(self, kind: str, items: list) -> bool:
        for _kind, link, cid in items:
            if not self._step(cid):
                return False
            self._one(kind, link, cid)
            self.done += 1
            self.e._save_ctx(self.ctx)
        return True

    # -- batches
    def batched(self, kind: str, items: list, many: Callable) -> bool:
        e, provider, caps, region = self.e, self.provider, self.provider.caps, self.e.hub.region
        size = max(1, int(getattr(provider, "batch_size", 1)))
        for start in range(0, len(items), size):
            chunk = items[start:start + size]
            if not self._step(chunk[0][2]):
                return False
            jobs: list[tuple[Any, int, Any, int]] = []           # (link, cid, payload, rev)
            for _k, link, cid in chunk:
                if kind == "delete":
                    if link is None or not link.remote_id:
                        e.hub.drop_link(cid, self.acct.id)
                        self._wrote(cid)
                        continue
                    jobs.append((link, cid, (link.remote_id, link.version_tag), 0))
                    continue
                if kind == "update" and e._waiting_for_choice(cid, self.acct):
                    continue
                card = e.hub.get_card(cid)
                meta = e.hub.contact_meta(cid)
                if card is None or meta is None:
                    continue
                sent = project(card, caps, region)
                if kind == "update":
                    if link is None or not link.remote_id:
                        continue
                    if link.snapshot is not None and units(sent, caps, region) == units(link.snapshot, caps, region):
                        e._settle(cid, self.acct, remote_id=link.remote_id, version_tag=link.version_tag,
                                  snapshot=link.snapshot, hub_rev=meta["rev"])
                        continue
                    jobs.append((link, cid, (link.remote_id, link.version_tag, sent), meta["rev"]))
                else:
                    jobs.append((link, cid, sent, meta["rev"]))
            if jobs:
                results: list[WriteResult] = many([j[2] for j in jobs])
                if len(results) != len(jobs):
                    raise AppErrorException(make_error(
                        "ERR_SYNC_FAILED", "accounts.engine", account=self.acct.name,
                        reason=f"the account answered {len(results)} of {len(jobs)} writes in a batch"))
                self._apply(kind, jobs, results)
            self.done += len(chunk)
            e._tell(self.ctx, "writing", self.done, self.total, self.acct.id)
        return True

    def _apply(self, kind: str, jobs: list, results: list[WriteResult]) -> None:
        e, provider, caps, region = self.e, self.provider, self.provider.caps, self.e.hub.region
        # What the account holds now, for the writes whose answer did not say: one read for all, if it can.
        need = [r.remote_id or j[0].remote_id for j, r in zip(jobs, results)
                if r.ok and kind != "delete" and r.card is None]
        fetched: dict[str, Any] = {}
        get_many = getattr(provider, "get_many", None)
        if need and callable(get_many):
            try:
                fetched = get_many(need) or {}
            except Exception:                                 # noqa: BLE001 - falls back to one read each
                _log.debug("read-back of a batch failed", exc_info=True)
        # The merge base of each write, worked out before the write lock is taken (a read-back is a request).
        bases: list[Optional[Card]] = []
        for (link, _cid, payload, _rev), r in zip(jobs, results):
            if not r.ok or kind == "delete":
                bases.append(None)
                continue
            sent = payload[2] if kind == "update" else payload
            rid = r.remote_id or (link.remote_id if link is not None else "")
            if r.card is not None:
                bases.append(project(r.card, caps, region))
            elif fetched.get(rid) is not None and fetched[rid].card is not None:
                bases.append(project(fetched[rid].card, caps, region))
            elif callable(get_many):
                bases.append(sent)                             # the batch read failed: what was sent
            else:
                bases.append(e._held_after_write(provider, rid, sent))
        retry: list[tuple[Any, int]] = []
        give_up: Optional[AppError] = None
        with e.store.batch() as conn:
            for (link, cid, payload, rev), r, held in zip(jobs, results, bases):
                if r.conflict:
                    retry.append((link, cid))
                    continue
                if not r.ok:
                    err = r.error if r.error is not None else make_error(
                        "ERR_ACCOUNT_REJECTED", "accounts.engine", account=self.acct.name, reason="refused")
                    if give_up is None:
                        give_up = self._refuse(kind, cid, err, defer=True)
                    else:                                     # already giving up: still listed
                        self._refuse(kind, cid, err, record_only=True)
                    continue
                if kind == "delete":
                    e.hub.drop_link(cid, self.acct.id, conn=conn)
                    self.ctx.plan.delete_out += 1
                    self._wrote(cid)
                    continue
                rid = r.remote_id or (link.remote_id if link is not None else "")
                e._settle(cid, self.acct, remote_id=rid, version_tag=r.version or "", snapshot=held, hub_rev=rev,
                          conn=conn)
                if kind == "create":
                    self.ctx.plan.create_out += 1
                else:
                    self.ctx.plan.update_out += 1
                self._wrote(cid)
            e._save_ctx(self.ctx, conn=conn, force=True)
        if give_up is not None:                               # what the account accepted is committed first
            raise AppErrorException(give_up)
        for link, cid in retry:                               # 3c, on its own: re-read, re-merge, retry
            self._one(kind, e.hub.link(cid, self.acct.id) or link, cid)


#: Failures that are the account's as a whole: the run ends and says so, instead of listing every contact.
_ACCOUNT_WIDE = frozenset({"ERR_ACCOUNT_SIGNIN_EXPIRED", "ERR_PROVIDER_LIMIT", "ERR_ACCOUNT_UNREACHABLE",
                           "ERR_ACCOUNT_ADMIN_CONSENT", "ERR_ACCOUNT_NOT_CONFIGURED", "ERR_ACCOUNT_TROUBLE"})
#: Fifteen contacts refused one after another means the account is not working, not that fifteen records are bad.
_GIVE_UP_AFTER = 15


class _Held(Exception):
    def __init__(self, error: AppError) -> None:
        self.error = error
        super().__init__(error.message)


def _too_many(n: int, book: int, settings: AccountSettings) -> bool:
    """D7.3: more than N contacts, or more than P% of the book. A single delete is never a mass deletion,
    whatever the size of a tiny book."""
    return n > settings.brake_count or (n > 1 and n > book * settings.brake_percent / 100.0)


def _change_shape(was: dict, now: dict) -> str:
    """How a contact changed at the account, in a few words: the kind of each value added, removed or altered
    ("custom 'fc cluster id' added", "phone changed") - the same words for the same change on every contact."""
    bits: list[str] = []
    for key in sorted(set(was) | set(now)):
        if was.get(key) == now.get(key):
            continue
        kind = key[2:].split(":")[0] if key.startswith("f:") else key.split(":")[0]
        verb = "added" if key not in was else "removed" if key not in now else "changed"
        if kind == "custom" and isinstance(now.get(key) or was.get(key), tuple):
            kind = f"custom '{(now.get(key) or was.get(key))[0]}'"
        elif key.startswith("s:"):
            kind = key[2:]
        bits.append(f"{kind} {verb}")
    return ", ".join(dict.fromkeys(bits)) or "changed"


def _policy(setting: str) -> str:
    return setting if setting in ("newest", "ask", "local", "remote") else "newest"


def _label(card: Card) -> str:
    from app.contacts.card import display_name  # noqa: PLC0415
    return display_name(card) or "(no name)"


def _jsonable(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return v if isinstance(v, (str, int, float, bool, type(None))) else str(v)


def _with_fix(err: AppError) -> str:
    return f"{err.message} FIX: {err.suggestion}" if err.suggestion else err.message
