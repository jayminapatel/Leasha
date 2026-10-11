"""The contact sync engine: inbound merge, outbound writes, dry run, brake, undo.

Layer: L7

Order 1k section 3 (D4-D10). One engine for every provider: it speaks only
`ContactProvider` and `Card`, so the same code runs against Google, Microsoft,
CardDAV and the fake the tests use.

**A sync of several accounts has two phases** - pull from every account, then push
to every account - so a change made in one account reaches the others within the
one sync (A2). Each account is its own failure domain (3g): an account that fails
is recorded and the rest carry on.

**Resumable (rule 4).** Each page of changes is applied and its token saved in one
transaction (`store.batch()`), so a crash costs at most the page in flight and the
next run resumes from the last token saved. A full re-pull (first sync, or an
expired token) also saves the ids seen so far, so the deletions it infers at the
end are right even after a crash in the middle.

**Never overwrites blind (3c).** An update carries the version tag it was based
on; if the account refuses it as stale, the engine re-reads that record, merges it
again and retries, a few times, before giving up on that one contact.

**Nothing is deleted on a hunch (D7).** A contact an account deleted is only
*flagged*; a delete Leasha would make at an account is counted, and more than the
brake allows (20, or 5% of the book) stops the push and asks.

**The first sync of an account is a dry run (D8).** It reads, plans and writes
nothing - not the hub, not the account, not even the token - until the person
approves.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from app.accounts.provider import (ContactProvider, RemoteContact, TokenExpired, VersionConflict)
from app.accounts.store import Account, AccountSettings, AccountStore
from app.contacts.card import Card, merge, project, units
from app.contacts.hub import ContactHub
from app.core.errors import AppError, AppErrorException, make_error, to_app_error
from app.core.logging import logger

__all__ = ["ContactSyncEngine", "SyncOutcome", "Plan", "RACE_RETRIES", "sample"]

_log = logger.bind(component="accounts.engine")

#: How many times a write that lost a race is re-read and re-merged before that
#: one contact is left for the next sync (3c). Three is enough for two clients
#: editing at once; more would be a loop against a server that never settles.
RACE_RETRIES = 3

_SAMPLE = 50


def sample(names: list[str], name: str) -> None:
    if len(names) < _SAMPLE:
        names.append(name)


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
    names: dict[str, list[str]] = field(default_factory=dict)

    def note(self, bucket: str, name: str) -> None:
        sample(self.names.setdefault(bucket, []), name)

    def to_json(self) -> dict:
        return {k: getattr(self, k) for k in (
            "add_in", "merge_in", "possible_duplicates", "flagged_deleted", "deleted_in", "create_out",
            "update_out", "delete_out", "conflicts", "held_conflicts")} | {"names": self.names}

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
        return self.flagged_deleted + self.held_conflicts


@dataclass
class SyncOutcome:
    account_id: int
    account_name: str
    run_id: int
    status: str                    # ok | needs-approval | held | failed | skipped
    plan: Plan = field(default_factory=Plan)
    error: Optional[AppError] = None
    accounts_to_push: set[int] = field(default_factory=set)
    needs_approval: bool = False


@dataclass
class _Ctx:
    run_id: int
    plan: Plan
    dry: bool
    policy: str
    trigger: str
    cancel: Callable[[], bool]
    full_seen: set[str] = field(default_factory=set)
    master: bool = False                                      # this account is the master list
    master_gone: set[int] = field(default_factory=set)        # contacts the master no longer has


class ContactSyncEngine:
    def __init__(self, store: Any, hub: ContactHub, accounts: AccountStore,
                 provider_for: Callable[[Account], ContactProvider], *,
                 clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.hub = hub
        self.accounts = accounts
        self.provider_for = provider_for
        self._clock = clock

    # ------------------------------------------------------------------- API
    def sync(self, account_ids: Optional[list[int]] = None, *, trigger: str = "sync-now",
             approve_first: bool = False, allow_brake: bool = False, dry_run: bool = False,
             cancel: Callable[[], bool] = lambda: False) -> list[SyncOutcome]:
        """Pull from then push to the accounts (all with contact sync on, by default).

        `dry_run` plans without writing, for any account (`contacts sync --dry-run`);
        the first sync of an account is a dry run by itself until approved (D8)."""
        settings = self.accounts.settings()
        chosen = [a for a in self.accounts.list() if a.syncs_contacts
                  and (account_ids is None or a.id in account_ids)]
        outcomes: dict[int, SyncOutcome] = {}
        providers: dict[int, ContactProvider] = {}
        for acct in chosen:                                  # phase 1: pull
            first_pending = (not acct.first_sync_done) and settings.dry_run_first and not approve_first
            dry = first_pending or dry_run
            run_id = self.accounts.start_run(acct, "contacts", trigger, dry_run=dry)
            out = SyncOutcome(acct.id, acct.name, run_id, "running")
            outcomes[acct.id] = out
            ctx = _Ctx(run_id, out.plan, dry, settings.conflict_policy, trigger, cancel,
                       master=acct.id == settings.master_account_id)
            out.needs_approval = first_pending
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
            except _Held as held:
                out.status, out.error = "held", held.error
            except Exception as exc:                          # noqa: BLE001 - one account never stops the rest
                self._fail(acct, out, exc)
        if settings.mirror_contacts and not dry_run:
            targets = [a.id for a in chosen if a.writes_contacts and outcomes[a.id].status == "running"
                       and (a.first_sync_done or not settings.dry_run_first or approve_first)]
            if len(targets) > 1:
                self.hub.mirror_to(targets)                   # every contact, in every account that takes them
        for acct in chosen:                                  # phase 2: push
            out = outcomes[acct.id]
            if out.status != "running" or not acct.writes_contacts:
                continue
            ctx = _Ctx(out.run_id, out.plan, False, settings.conflict_policy, trigger, cancel)
            try:
                self._push(acct, providers[acct.id], ctx, settings, allow_brake=allow_brake)
                out.status = "ok"
            except _Held as held:
                out.status, out.error = "held", held.error
            except Exception as exc:                          # noqa: BLE001
                self._fail(acct, out, exc)
        for acct in chosen:
            out = outcomes[acct.id]
            if out.status == "running":                       # in-only account: nothing to push
                out.status = "ok"
            self._finish(acct, out)
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

    # ------------------------------------------------------------------ pull
    def _pull(self, acct: Account, provider: ContactProvider, ctx: _Ctx, settings: AccountSettings) -> None:
        state = self.accounts.state(acct.id, "contacts")
        token = None if ctx.dry else state.token
        if ctx.dry:
            token = None
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
        for page in provider.pull(token):
            if ctx.cancel():
                return False
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
        book = max(1, hub.count(include_bin=True))
        too_many = len(alive) > settings.brake_count or (len(alive) > 1 and len(alive) > book * settings.brake_percent / 100.0)
        if too_many and not allow_brake:
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
        if base is not None and units(base, caps, hub.region) == units(remote_card, caps, hub.region):
            if not ctx.dry and rc.version and rc.version != link.version_tag:
                hub.set_link(cid, acct.id, remote_id=rc.remote_id, version_tag=rc.version,
                             snapshot=base, hub_rev=link.hub_rev, state="synced")
            return                                            # the account side did not change
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

    # ------------------------------------------------------------------ push
    def _plan_outbound(self, acct: Account, provider: ContactProvider, ctx: _Ctx) -> None:
        if not acct.writes_contacts:
            return
        for kind, link, cid in self._dirty(acct):
            bucket = {"create": "create_out", "update": "update_out", "delete": "delete_out"}[kind]
            setattr(ctx.plan, bucket, getattr(ctx.plan, bucket) + 1)
            ctx.plan.note(bucket, self._name(cid))

    def _dirty(self, acct: Account) -> list[tuple[str, Any, int]]:
        """What would be written to `acct`: (kind, link, contact id)."""
        out: list[tuple[str, Any, int]] = []
        rows = self.store.conn.execute(
            "SELECT l.id, l.contact_id, l.state, l.hub_rev, c.rev, c.deleted_at FROM contact_links l "
            "JOIN contacts c ON c.id = l.contact_id WHERE l.account_id = ? "
            "AND l.state IN ('pending_create','synced','pending_remove')", (acct.id,)).fetchall()
        for r in rows:
            link = self.hub.link(r["contact_id"], acct.id)
            if r["state"] == "pending_remove":
                out.append(("delete", link, r["contact_id"]))
            elif r["deleted_at"] is not None:
                continue
            elif r["state"] == "pending_create":
                out.append(("create", link, r["contact_id"]))
            elif r["hub_rev"] != r["rev"]:
                out.append(("update", link, r["contact_id"]))
        return out

    def _push(self, acct: Account, provider: ContactProvider, ctx: _Ctx, settings: AccountSettings, *,
              allow_brake: bool) -> None:
        work = self._dirty(acct)
        deletes = [w for w in work if w[0] == "delete"]
        # D7.3: more than N contacts, or more than P% of the book. A single
        # delete is never a mass deletion, whatever the size of a tiny book.
        book = max(1, self.hub.count(include_bin=True))
        too_many = len(deletes) > settings.brake_count or (
            len(deletes) > 1 and len(deletes) > book * settings.brake_percent / 100.0)
        if deletes and too_many and not allow_brake:
            err = make_error("ERR_SYNC_DELETE_BRAKE", "accounts.engine", count=len(deletes), account=acct.name)
            self.accounts.finish_run(ctx.run_id, "held", error_code=err.code, error_text=err.message,
                                     plan=ctx.plan.to_json(), held=len(deletes))
            raise _Held(err)
        for kind, link, cid in work:
            if ctx.cancel():
                return
            try:
                if kind == "delete":
                    self._remote_delete(acct, provider, link, ctx)
                elif kind == "create":
                    self._remote_create(acct, provider, link, cid, ctx)
                else:
                    self._remote_update(acct, provider, link, cid, ctx)
            except VersionConflict:
                _log.warning("gave up on {} for {}: it keeps changing; next sync tries again", cid, acct.name)

    def _remote_delete(self, acct: Account, provider: ContactProvider, link: Any, ctx: _Ctx) -> None:
        if link.remote_id:
            provider.delete(link.remote_id, link.version_tag)
        self.hub.drop_link(link.contact_id, acct.id)
        ctx.plan.delete_out += 1

    def _remote_create(self, acct: Account, provider: ContactProvider, link: Any, cid: int, ctx: _Ctx) -> None:
        card = self.hub.get_card(cid)
        if card is None:
            return
        sent = project(card, provider.caps, self.hub.region)
        remote_id, version = provider.create(sent)
        self.hub.set_link(cid, acct.id, remote_id=remote_id, version_tag=version,
                          snapshot=self._held_after_write(provider, remote_id, sent),
                          hub_rev=self.hub.contact_meta(cid)["rev"], state="synced")
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
        if self.store.conn.execute("SELECT 1 FROM contact_conflicts WHERE contact_id = ? AND account_id = ? "
                                   "AND resolved_at IS NULL AND winner = 'held'", (cid, acct.id)).fetchone():
            return                                            # waiting for the person's choice
        for attempt in range(RACE_RETRIES + 1):
            local = hub.get_card(cid)
            link = hub.link(cid, acct.id)
            if local is None or link is None or link.remote_id is None:
                return
            sent = project(local, caps, hub.region)
            rev = hub.contact_meta(cid)["rev"]
            if link.snapshot is not None and units(sent, caps, hub.region) == units(link.snapshot, caps, hub.region):
                hub.set_link(cid, acct.id, remote_id=link.remote_id, version_tag=link.version_tag,
                             snapshot=link.snapshot, hub_rev=rev, state="synced")
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
            hub.set_link(cid, acct.id, remote_id=link.remote_id, version_tag=version,
                         snapshot=self._held_after_write(provider, link.remote_id, sent),
                         hub_rev=rev, state="synced")
            ctx.plan.update_out += 1
            return

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
            return                                            # `_push` already closed the run
        if out.status == "failed" and out.error is not None:
            self.accounts.record_failure(acct.id, "contacts", out.error.code, out.error.message)
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
        self.accounts.record_success(acct.id, "contacts")
        if not acct.first_sync_done:
            self.accounts.update(acct.id, first_sync_done=True)
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
            out.status = "ok"
        except Exception as exc:                              # noqa: BLE001
            self._fail(acct, out, exc)
        finally:
            provider.close()
        self._finish(acct, out)
        return out


class _Held(Exception):
    def __init__(self, error: AppError) -> None:
        self.error = error
        super().__init__(error.message)


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
