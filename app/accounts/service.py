"""The account service: what the window and the command line both call.

Layer: L7

Order 1k. One object that owns the pieces - the account store, the contact hub,
the secret store, the transport and the sync engine - and offers the few verbs
Settings > Accounts, the Contacts page, the tray and `app.cli accounts` need:
add an account, test it, sign it in again, remove it, "Sync now", "what is due",
undo a sync. Nothing here imports Qt (rule 8: the CLI ships first); the window
runs these on a worker thread (rule 5).

**What starts a sync (D11.2): the schedule or "Sync now", nothing else.**
`due_work` is the schedule's half - a pure read of the clock, the settings and
the saved state, called by the Qt timer in `app/ui/account_scheduler.py` and by
`app.cli accounts schedule`. `sync_now` is the other half. Neither opens an
account on its own; opening Leasha, waking from sleep and the network coming
back start nothing (a due time that fell while asleep is served once, D11.4;
one catch-up after a closed spell, D16).

**Sync now folds.** A second "Sync now" for an account whose sync is running is
refused as already running instead of queued (10c).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable, Optional

from app.accounts import policy as pol
from app.accounts.engine import ContactSyncEngine, SyncOutcome
from app.accounts.http import (AllowedHosts, KeepAliveTransport, Meter, RetryingTransport, UrllibTransport,
                               normalise_server)
from app.accounts.oauth import (AppPasswordSource, OAuthClientConfig, TokenManager, sign_in)
from app.accounts.provider import ContactProvider, Transport
from app.accounts.secrets import SecretStore, account_key, client_secret_key
from app.accounts.store import Account, AccountSettings, AccountStore, ScheduleSetting
from app.contacts.hub import ContactHub, UndoResult
from app.core.errors import AppErrorException, make_error
from app.core.logging import logger

__all__ = ["AccountService", "ConnectionReport", "DueWork", "AccountStatus", "KINDS",
           "IMAP_PRESETS", "STATUS_WORDS"]

_log = logger.bind(component="accounts.service")

KINDS = ("contacts", "mail")

#: Servers the person does not have to type (D17: iCloud is a preset of the same two connectors).
IMAP_PRESETS: dict[str, dict[str, Any]] = {
    "apple": {"server": "imap.mail.me.com", "port": 993, "security": "ssl",
              "carddav": "https://contacts.icloud.com",
              "help": "Make an app-specific password at account.apple.com > Sign-In and Security > "
                      "App-Specific Passwords (two-factor authentication must be on)."},
}

STATUS_WORDS = ("Up to date", "Syncing", "Waiting", "Paused", "Needs sign-in", "Failed")


@dataclass
class ConnectionReport:
    ok: bool
    message: str
    scopes: list[str] = field(default_factory=list)
    error_code: Optional[str] = None
    fix: str = ""


@dataclass
class VerifyReport:
    """What a read-only check of one account's contacts found (`AccountService.verify_contacts`)."""

    account_id: int
    account: str
    remote: int = 0                    # contacts the account holds now
    linked: int = 0                    # contacts here linked to a record of that account
    missing_here: list[str] = field(default_factory=list)    # at the account, not yet here: the next sync adds them
    missing_there: list[str] = field(default_factory=list)   # linked here, gone at the account
    changed_there: int = 0             # the account's record changed since the last sync
    waiting_to_write: int = 0          # changes here not yet written to the account
    error: str = ""

    @property
    def in_sync(self) -> bool:
        return not (self.error or self.missing_here or self.missing_there or self.changed_there
                    or self.waiting_to_write)

    def summary(self) -> str:
        if self.error:
            return f"{self.account}: could not be checked - {self.error}"
        head = f"{self.account}: {self.remote:,} at the account, {self.linked:,} linked here"
        if self.in_sync:
            return head + " - in sync."
        bits = []
        if self.missing_here:
            bits.append(f"{len(self.missing_here):,} at the account not yet here")
        if self.missing_there:
            bits.append(f"{len(self.missing_there):,} here no longer at the account")
        if self.changed_there:
            bits.append(f"{self.changed_there:,} changed at the account")
        if self.waiting_to_write:
            bits.append(f"{self.waiting_to_write:,} changes here waiting to be written")
        return head + " - " + ", ".join(bits) + ". The next sync settles them."


@dataclass
class DueWork:
    account: Account
    kind: str
    reason: str                    # "schedule" | "catch-up"


@dataclass
class AccountStatus:
    account: Account
    word: str                       # one of STATUS_WORDS
    icon: str                       # a glyph from icons.ICON_NAMES
    last_sync: Optional[datetime]
    next_sync: Optional[datetime]
    schedule_text: str
    hold: Optional[str]             # why a scheduled sync is held back (pause, quiet hours)
    warning: str = ""               # failing for 24 hours / sign-in expires soon


_WORD_ICON = {"Up to date": "circle-check", "Syncing": "refresh-cw", "Waiting": "clock", "Paused": "pause",
              "Needs sign-in": "key-round", "Failed": "circle-alert"}


def _dt(ts: Optional[int | float]) -> Optional[datetime]:
    return datetime.fromtimestamp(ts) if ts else None


class AccountService:
    def __init__(self, store: Any, *, secrets: Optional[SecretStore] = None,
                 transport: Optional[Transport] = None, clock: Callable[[], float] = time.time,
                 provider_for: Optional[Callable[[Account], ContactProvider]] = None) -> None:
        self.store = store
        self.accounts = AccountStore(store)
        self.hub = ContactHub(store)
        self.secrets = secrets or SecretStore()
        self._clock = clock
        self.allowed = AllowedHosts()
        self._transport = transport
        self._token_managers: dict[int, TokenManager] = {}
        self.engine = ContactSyncEngine(store, self.hub, self.accounts, provider_for or self.contact_provider,
                                        clock=clock)
        #: Phase B plugs the mail syncer in here: callable(account, trigger) -> SyncOutcome-like.
        self.mail_sync: Optional[Callable[..., Any]] = None
        self._lock = threading.Lock()
        self._running: set[tuple[int, str]] = set()
        #: The latest progress event of each account's running contact sync (`_track`).
        self._live_progress: dict[int, dict] = {}
        try:
            self.accounts.close_stale_runs()          # a run the last session never finished says so
        except Exception:                             # noqa: BLE001 - a label; never stops the service
            pass

    # ---------------------------------------------------------------- plumbing
    def settings(self) -> AccountSettings:
        return self.accounts.settings()

    def transport_for(self, account: Optional[Account] = None) -> Transport:
        if self._transport is not None:
            return self._transport
        if account is not None and account.server:
            self.allowed.allow_server(account.server)
        return RetryingTransport(UrllibTransport(self.allowed, account=account.name if account else ""))

    def oauth_config(self, provider: str) -> OAuthClientConfig:
        s = self.settings()
        client_id = s.google_client_id if provider == "google" else s.microsoft_client_id
        return OAuthClientConfig.from_store(provider, client_id, self.secrets)

    def token_source(self, account: Account) -> Callable[[], str]:
        if account.provider in ("google", "microsoft"):
            tm = self._token_managers.get(account.id)
            if tm is None:
                tm = TokenManager(account.id, self.secrets, self.transport_for(account),
                                  self.oauth_config(account.provider), account_label=account.name,
                                  clock=self._clock)
                self._token_managers[account.id] = tm
            return tm
        return AppPasswordSource(self.secrets, account_key(account.id, "app_password"), account=account.name)

    def contact_transport(self, account: Account) -> Transport:
        """The transport a contact connector talks over: one kept connection per host, counted by a `Meter` the
        engine reads into the run's plan (order 1t 1a, 5a). Tests that hand the service a transport get theirs."""
        if self._transport is not None:
            return self._transport
        if account.server:
            self.allowed.allow_server(account.server)
        meter = Meter()
        return RetryingTransport(KeepAliveTransport(self.allowed, account=account.name, meter=meter),
                                 account=account.name, meter=meter)

    def contact_provider(self, account: Account) -> ContactProvider:
        """The connector for `account` (4a-4e), with its transport's `meter` and `close` reachable from it."""
        transport = self.contact_transport(account)
        provider = self._contact_provider(account, transport)
        try:
            provider.meter = getattr(transport, "meter", None)            # type: ignore[attr-defined]
        except AttributeError:                                           # a provider with __slots__: no meter
            pass
        return provider

    def _contact_provider(self, account: Account, transport: Transport) -> ContactProvider:
        from app.accounts.providers.carddav import CardDavProvider, basic_auth_header  # noqa: PLC0415
        if account.provider == "google":
            from app.accounts.providers.google_people import GooglePeopleProvider  # noqa: PLC0415
            return GooglePeopleProvider(transport, self.token_source(account), account=account.name)
        if account.provider == "microsoft":
            from app.accounts.providers.graph_contacts import GraphContactsProvider  # noqa: PLC0415
            return GraphContactsProvider(transport, self.token_source(account), account=account.name)
        if account.provider == "apple":
            return CardDavProvider.icloud(transport, account.address, self.token_source(account),
                                          account=account.name)
        if account.provider == "carddav":
            url = account.option("addressbook_url") or ""
            book_name = ""
            if url and "://" not in url:
                # Not an address: the NAME of an address book ("Default Address Book/"), which discovery matches.
                url, book_name = "", url.strip().strip("/")
            server = account.option("server_url") or (f"https://{account.server}" if account.server else "")
            if account.server:
                self.allowed.allow_server(account.server)
            return CardDavProvider(transport, basic_auth_header(account.address, self.token_source(account)),
                                   server_url=server, addressbook_url=url, addressbook_name=book_name,
                                   account=account.name)
        raise AppErrorException(make_error("ERR_ACCOUNT_REJECTED", "accounts.service", account=account.name,
                                           reason=f"{account.provider} accounts have no contacts connector"))

    # ----------------------------------------------------------------- contacts
    def new_contact(self, card: Any, *, actor: str = "user", kept_in: Optional[Iterable[int]] = None) -> int:
        """A contact made in Leasha (D4, D5): it goes to the default account unless the caller says where.

        The default is the account chosen in Settings, else the first with two-way
        contact sync on. With no such account the contact simply lives in Leasha."""
        if kept_in is None:
            default = self.accounts.default_account()
            kept_in = [default.id] if default is not None and default.writes_contacts else []
        return self.hub.create(card, actor=actor, kept_in=kept_in)

    # ----------------------------------------------------------------- accounts
    def add_oauth_account(self, provider: str, name: str = "", *, mail: bool = False, contacts: bool = True,
                          direction: str = "two-way", login_hint: str = "",
                          open_browser: Optional[Callable[[str], object]] = None,
                          on_url: Optional[Callable[[str], None]] = None, timeout_s: float = 300.0) -> Account:
        """Google or Microsoft: browser sign-in (PKCE, loopback). Blocks: run it on a worker."""
        config = self.oauth_config(provider)
        config.validate()
        transport = self.transport_for()
        kwargs: dict[str, Any] = {}
        if open_browser is not None:
            kwargs["open_browser"] = open_browser
        placeholder = login_hint or f"pending-{int(self._clock())}"
        aid = self.accounts.add(provider, placeholder, name or login_hint or provider.title(),
                                mail_on=mail, contacts_on=contacts, direction=direction)
        try:
            tokens, manager = sign_in(config, transport, self.secrets, account_id=aid, mail=mail,
                                      contacts=contacts, login_hint=login_hint, account=name or provider,
                                      on_url=on_url, timeout_s=timeout_s, clock=self._clock, **kwargs)
        except BaseException:
            self.accounts.delete_rows(aid)
            raise
        address = tokens.email or login_hint or placeholder
        self.accounts.update(aid, address=address, name=name or address)
        self._token_managers[aid] = manager
        self.resolve_address(self.accounts.get(aid))      # type: ignore[arg-type] - Microsoft gives no id_token email
        return self.accounts.get(aid)  # type: ignore[return-value]

    def resolve_address(self, account: Account) -> str:
        """Fill in a Microsoft account's address when sign-in left the placeholder (`pending-...`).

        Microsoft's sign-in carries no email in what Leasha asks for, so the address is read once
        from the signed-in profile (`User.Read` is already granted). Returns the address now held;
        a failure leaves the placeholder and never stops what the caller was doing."""
        if account.provider != "microsoft" or not account.address.startswith("pending-"):
            return account.address
        try:
            resp = self.transport_for(account).request(
                "GET", "https://graph.microsoft.com/v1.0/me?$select=mail,userPrincipalName",
                headers={"Authorization": f"Bearer {self.token_source(account)()}", "Accept": "application/json"})
            data = (resp.json() or {}) if resp.status == 200 else {}
        except Exception:                                  # noqa: BLE001 - the address is a label
            return account.address
        address = str(data.get("mail") or data.get("userPrincipalName") or "")
        if address:
            self.accounts.update(account.id, address=address)
            return address
        return account.address

    def _name_pending(self, account_id: Optional[int]) -> None:
        """Give every Microsoft account still holding the `pending-...` placeholder its real address, from the
        signed-in profile. Asked before a sync, on the worker that runs it, so an account added before the address was
        read on sign-in is named by its next sync and the list stops saying "pending-1791709065" (the owner,
        2026-10-12). A failure leaves the placeholder; it never stops the sync."""
        for acct in self.accounts.list():
            if (acct.provider == "microsoft" and acct.address.startswith("pending-")
                    and account_id in (None, acct.id)):
                try:
                    self.resolve_address(acct)
                except Exception:                          # noqa: BLE001 - a label
                    pass

    def add_password_account(self, provider: str, address: str, password: str, name: str = "", *,
                             server: Optional[str] = None, port: Optional[int] = None,
                             security: Optional[str] = None, mail: bool = False, contacts: bool = True,
                             direction: str = "two-way", options: Optional[dict] = None,
                             test: bool = True) -> Account:
        """Apple, any IMAP or any CardDAV server: an app password, kept only in Credential Manager."""
        preset = IMAP_PRESETS.get(provider, {})
        opts = dict(options or {})
        if server:
            # A pasted address (https://dav.example.com, host:port/path) is taken apart: the host is the server, the
            # port and a path are kept where they belong.
            host, found_port, path = normalise_server(server)
            server, port = host or server, port or found_port
            if path and provider == "carddav":
                opts.setdefault("server_url", f"https://{host}{path}")
        server = server or preset.get("server")
        port = port or preset.get("port")
        security = security or preset.get("security")
        if provider == "apple":
            opts.setdefault("server_url", preset.get("carddav"))
        aid = self.accounts.add(provider, address, name or address, server=server, port=port, security=security,
                                mail_on=mail, contacts_on=contacts, direction=direction, options=opts)
        try:
            self.secrets.put(account_key(aid, "app_password"), password)
            acct = self.accounts.get(aid)
            assert acct is not None
            if test:
                report = self.test_connection(acct)
                if not report.ok:
                    raise AppErrorException(make_error("ERR_ACCOUNT_REJECTED", "accounts.service",
                                                       account=acct.name, reason=report.message))
        except BaseException:
            self.remove_account(aid, keep_contacts=True, remove_mail=True)
            raise
        return self.accounts.get(aid)  # type: ignore[return-value]

    def test_connection(self, account: Account) -> ConnectionReport:
        """'Test connection' (10p.3): reaches the server and reports what was granted."""
        self.resolve_address(account)
        try:
            if account.contacts_on:
                provider = self.contact_provider(account)
                try:
                    for _ in provider.pull(None):
                        break                                  # one page proves sign-in and permission
                finally:
                    provider.close()
            scopes = []
            if account.provider in ("google", "microsoft"):
                scopes = ["contacts"] + (["mail (read only)"] if account.mail_on else [])
            return ConnectionReport(True, "Connected.", scopes)
        except AppErrorException as exc:
            return ConnectionReport(False, exc.error.message, error_code=exc.error.code,
                                    fix=exc.error.suggestion)

    def reconnect(self, account_id: int, **kw: Any) -> Account:
        acct = self.accounts.get(account_id)
        assert acct is not None
        if acct.provider not in ("google", "microsoft"):
            raise AppErrorException(make_error("ERR_ACCOUNT_REJECTED", "accounts.service", account=acct.name,
                                               reason="enter the new app password instead"))
        config = self.oauth_config(acct.provider)
        tokens, manager = sign_in(config, self.transport_for(), self.secrets, account_id=acct.id,
                                  mail=acct.mail_on, contacts=acct.contacts_on, login_hint=acct.address,
                                  account=acct.name, clock=self._clock, **kw)
        self._token_managers[acct.id] = manager
        self.accounts.update(acct.id, last_error=None, last_error_code=None)
        return self.accounts.get(acct.id)  # type: ignore[return-value]

    def set_app_password(self, account_id: int, password: str) -> None:
        self.secrets.put(account_key(account_id, "app_password"), password)
        self.accounts.update(account_id, last_error=None, last_error_code=None)

    def remove_account(self, account_id: int, *, keep_contacts: bool = True, remove_mail: bool = True) -> None:
        """1f: sign out, delete the secret, and (the person's choice) keep the contacts
        in Leasha (default) and take the mail out of the index (default)."""
        acct = self.accounts.get(account_id)
        if acct is None:
            return
        for kind in ("refresh_token", "app_password"):
            try:
                self.secrets.delete(account_key(account_id, kind))
            except AppErrorException:
                _log.warning("could not delete the {} of account {}", kind, account_id)
        self._token_managers.pop(account_id, None)
        with self.store.batch() as conn:
            only_here = [r[0] for r in conn.execute(
                "SELECT l.contact_id FROM contact_links l WHERE l.account_id = ? AND NOT EXISTS "
                "(SELECT 1 FROM contact_links o WHERE o.contact_id = l.contact_id AND o.account_id <> ? "
                "AND o.state IN ('synced','pending_create','deleted_remote'))", (account_id, account_id))]
            conn.execute("DELETE FROM contact_links WHERE account_id = ?", (account_id,))
        if not keep_contacts:
            for cid in only_here:
                self.hub.delete(cid, actor="account-removed")
        if remove_mail and self.remove_mail_hook is not None:
            self.remove_mail_hook(account_id)
        if self.settings().master_account_id == account_id:
            self.accounts.update_settings(master_account_id=None)      # no account is the master any more
        self.accounts.delete_rows(account_id)

    #: Phase B: removes an account's mail from the index (set by the mail syncer).
    remove_mail_hook: Optional[Callable[[int], Any]] = None

    # --------------------------------------------------------------------- sync
    # ---------------------------------------------------------------- backup
    def backup_folder(self) -> Any:
        from pathlib import Path  # noqa: PLC0415
        db = Path(getattr(self.store, "db_path", "."))
        data = db.parent.parent if db.parent.name == "fts" else db.parent
        return data / "backups" / "contacts"

    def backup_contacts(self, reason: str = "manual") -> Any:
        """Write a copy of the contact book now (a vCard file and the contact tables)."""
        from app.contacts import backup  # noqa: PLC0415
        return backup.write_backup(self.store, self.hub, self.backup_folder(), reason=reason,
                                   keep=max(1, int(self.settings().contacts_backup_keep or 10)))

    def _backup_before_sync(self, ids: list[int]) -> None:
        """A copy before an account's first sync and at most once a day otherwise. Never blocks a sync."""
        settings = self.settings()
        if not settings.backup_contacts or not ids:
            return
        try:
            from app.contacts import backup  # noqa: PLC0415
            first = any(not a.first_sync_done for a in self.accounts.list() if a.id in ids)
            age = backup.newest_age_s(self.backup_folder(), self._clock())
            if first or age is None or age > 20 * 3600:
                self.backup_contacts("first-sync" if first else "daily")
        except Exception:                                  # noqa: BLE001 - a copy is a courtesy to the sync, not a gate
            _log.warning("the contact backup before a sync failed", exc_info=True)

    # ----------------------------------------------------------------- verify
    def verify_contacts(self, account: Account) -> VerifyReport:
        """Read-only: compare what the account holds with what is linked here. Writes nothing."""
        rep = VerifyReport(account.id, account.name or account.address)
        try:
            provider = self.engine.provider_for(account)
        except AppErrorException as exc:
            rep.error = exc.error.message
            return rep
        remote: dict[str, tuple[str, str]] = {}               # remote id -> (version, name)
        caps = getattr(provider, "caps", None)
        try:
            for page in provider.pull(None):
                for rc in page.changes:
                    if rc.card is not None:
                        from app.contacts.card import display_name  # noqa: PLC0415
                        remote[rc.remote_id] = (rc.version or "", display_name(rc.card) or "(no name)")
        except AppErrorException as exc:
            rep.error = exc.error.message
            return rep
        finally:
            try:
                provider.close()
            except Exception:                              # noqa: BLE001
                pass
        rep.remote = len(remote)
        rows = self.store.conn.execute(
            "SELECT l.contact_id, l.remote_id, l.version_tag, l.state, l.hub_rev, c.rev, c.display_name FROM contact_links l "
            "JOIN contacts c ON c.id = l.contact_id WHERE l.account_id = ? AND c.deleted_at IS NULL",
            (account.id,)).fetchall()
        known: set[str] = set()
        for r in rows:
            if r["state"] in ("pending_create",) or r["remote_id"] is None:
                rep.waiting_to_write += 1
                continue
            known.add(r["remote_id"])
            if r["state"] == "pending_remove":
                rep.waiting_to_write += 1
                continue
            rep.linked += 1
            if r["remote_id"] not in remote:
                if r["state"] != "deleted_remote":
                    rep.missing_there.append(r["display_name"] or "(no name)")
            else:
                if r["state"] == "synced" and remote[r["remote_id"]][0] and r["version_tag"] != remote[r["remote_id"]][0]:
                    rep.changed_there += 1
                if r["state"] == "synced" and r["hub_rev"] != r["rev"] and self._differs_from_account(
                        r["contact_id"], account.id, caps):
                    rep.waiting_to_write += 1
        rep.missing_here = [name for rid, (_v, name) in remote.items() if rid not in known]
        return rep

    def _differs_from_account(self, cid: int, account_id: int, caps: Any) -> bool:
        """Would a push write anything for this contact? The same test the sync makes: what would be
        sent, as the account can hold it, against what was last agreed with it."""
        from app.contacts.card import ALL_CAPS, project, units  # noqa: PLC0415
        link = self.hub.link(cid, account_id)
        card = self.hub.get_card(cid)
        if link is None or card is None or link.snapshot is None:
            return True
        caps = caps or ALL_CAPS
        region = self.hub.region
        return units(project(card, caps, region), caps, region) != units(link.snapshot, caps, region)

    def sync_now(self, account_id: Optional[int] = None, kind: Optional[str] = None, *,
                 trigger: str = "sync-now", approve_first: bool = False, allow_brake: bool = False,
                 cancel: Callable[[], bool] = lambda: False,
                 progress: Optional[Callable[[dict], None]] = None) -> list[SyncOutcome]:
        """'Sync now' for all accounts, one account, or one kind of one account (10c)."""
        kinds = (kind,) if kind else KINDS
        outcomes: list[SyncOutcome] = []
        self._name_pending(account_id)
        if "contacts" in kinds:
            ids = [a.id for a in self.accounts.list() if a.syncs_contacts and (account_id in (None, a.id))]
            claimed = self._claim([(i, "contacts") for i in ids])
            try:
                if claimed:
                    self._backup_before_sync([i for i, _ in claimed])
                    outcomes += self.engine.sync([i for i, _ in claimed], trigger=trigger,
                                                 approve_first=approve_first, allow_brake=allow_brake,
                                                 cancel=cancel, progress=self._track(progress))
            finally:
                self._release(claimed)
        if "mail" in kinds and self.mail_sync is not None:
            for acct in self.accounts.list():
                if not acct.mail_on or account_id not in (None, acct.id):
                    continue
                claimed = self._claim([(acct.id, "mail")])
                try:
                    if claimed:
                        outcomes.append(self.mail_sync(acct, trigger=trigger, cancel=cancel))
                finally:
                    self._release(claimed)
        return outcomes

    def _claim(self, items: list[tuple[int, str]]) -> list[tuple[int, str]]:
        with self._lock:
            free = [i for i in items if i not in self._running]
            self._running.update(free)
            return free

    def _release(self, items: Iterable[tuple[int, str]]) -> None:
        with self._lock:
            self._running.difference_update(items)

    def is_running(self, account_id: int, kind: str = "contacts") -> bool:
        with self._lock:
            return (account_id, kind) in self._running

    def undo_run(self, run_id: int) -> UndoResult:
        """Undo a whole sync (3e): the hub is put back and the accounts are written to match."""
        res = self.hub.undo_sync(run_id)
        if res.accounts:
            self.engine.push(res.accounts, trigger="undo")
        return res

    def push_accounts(self, account_ids: Iterable[int]) -> list[SyncOutcome]:
        """Write the hub's current state to these accounts (after an undo from the Contacts page).

        A push, not a sync: nothing is read from the accounts, and it is not started by the
        schedule - the person pressed Undo, which is the same as pressing Sync now for them."""
        return self.engine.push(set(int(a) for a in account_ids), trigger="undo")

    def resolve_conflict(self, contact_id: int, account_id: int, choose: str) -> Optional[SyncOutcome]:
        """The person's choice for a clash held under 'ask me every time' (10k): `local` keeps
        Leasha's value, `remote` takes the account's. Returns None when that account is mid-sync
        (nothing was changed; ask again when it ends)."""
        if choose not in ("local", "remote"):
            raise ValueError("choose must be 'local' or 'remote'")
        claimed = self._claim([(int(account_id), "contacts")])
        if not claimed:
            return None
        try:
            return self.engine.resolve_conflict(int(contact_id), int(account_id), choose)
        finally:
            self._release(claimed)

    def approve_first_sync(self, account_id: int, **kw: Any) -> list[SyncOutcome]:
        return self.sync_now(account_id, "contacts", approve_first=True, trigger="approved", **kw)

    # ----------------------------------------------------------------- schedule
    def effective_schedule(self, account: Account, kind: str, settings: Optional[AccountSettings] = None) -> ScheduleSetting:
        s = settings or self.settings()
        base = s.contacts if kind == "contacts" else s.mail
        if s.same_for_all:
            return base
        own = account.options.get(f"{kind}_schedule")
        if isinstance(own, dict):
            return ScheduleSetting(**{k: v for k, v in own.items() if k in ScheduleSetting.__dataclass_fields__})
        return base

    def policy_for(self, account: Account, kind: str, settings: Optional[AccountSettings] = None) -> pol.SyncPolicy:
        sc = self.effective_schedule(account, kind, settings)
        return pol.SyncPolicy.from_values(sc.mode, sc.minutes, sc.daily_at)

    def _state(self, account: Account, kind: str) -> pol.AccountSyncState:
        st = self.accounts.state(account.id, kind)
        return pol.AccountSyncState(last_success=_dt(st.last_success_at), last_attempt=_dt(st.last_attempt_at),
                                    failures=st.failures, first_failure_at=_dt(st.first_failure_at),
                                    retry_after_until=_dt(st.next_retry_at))

    def hold_reason(self, now: datetime, settings: Optional[AccountSettings] = None) -> Optional[str]:
        s = settings or self.settings()
        until = None
        if s.paused_until is not None:
            until = datetime.max if s.paused_until >= 9e15 else datetime.fromtimestamp(s.paused_until)
        return pol.hold_reason(now, quiet_start=pol.parse_hhmm(s.quiet_start) if s.quiet_on else None,
                               quiet_end=pol.parse_hhmm(s.quiet_end) if s.quiet_on else None, paused_until=until)

    def due_work(self, now: Optional[datetime] = None, *, window_opened_at: Optional[datetime] = None) -> list[DueWork]:
        """What the schedule says to start now - and nothing else (D11.2). Contacts before mail (10f)."""
        now = now or datetime.fromtimestamp(self._clock())
        s = self.settings()
        if self.hold_reason(now, s) is not None:
            return []
        out: list[DueWork] = []
        for kind in KINDS:
            for acct in self.accounts.list():
                if kind == "contacts" and not acct.syncs_contacts:
                    continue
                if kind == "mail" and not acct.mail_on:
                    continue
                if acct.last_error_code == "ERR_ACCOUNT_SIGNIN_EXPIRED":
                    continue                                   # nothing to retry until the person reconnects
                policy = self.policy_for(acct, kind, s)
                if policy.mode == "manual":
                    continue
                st = self._state(acct, kind)
                nxt = pol.next_attempt_at(policy, st, now)
                if nxt is not None and nxt <= now and pol.is_due(policy, st.last_success, now,
                                                                 last_attempt=st.last_attempt):
                    out.append(DueWork(acct, kind, "schedule"))
                    continue
                if window_opened_at is not None:
                    cu = pol.catch_up_due(policy, st.last_success, now, window_opened_at)
                    if cu is not None and cu <= now:
                        out.append(DueWork(acct, kind, "catch-up"))
        return out

    def run_due(self, now: Optional[datetime] = None, *, window_opened_at: Optional[datetime] = None,
                cancel: Callable[[], bool] = lambda: False,
                progress: Optional[Callable[[dict], None]] = None) -> list[SyncOutcome]:
        work = self.due_work(now, window_opened_at=window_opened_at)
        outcomes: list[SyncOutcome] = []
        for w in work[:1]:
            self._name_pending(None)
        contacts = [w for w in work if w.kind == "contacts"]
        if contacts:
            trigger = "schedule" if any(w.reason == "schedule" for w in contacts) else "schedule (catch-up)"
            ids = [w.account.id for w in contacts]
            claimed = self._claim([(i, "contacts") for i in ids])
            try:
                if claimed:
                    self._backup_before_sync([i for i, _ in claimed])
                    outcomes += self.engine.sync([i for i, _ in claimed], trigger=trigger, cancel=cancel,
                                                 progress=self._track(progress))
            finally:
                self._release(claimed)
        for w in work:
            if w.kind == "mail" and self.mail_sync is not None:
                claimed = self._claim([(w.account.id, "mail")])
                try:
                    if claimed:
                        outcomes.append(self.mail_sync(w.account, trigger="schedule", cancel=cancel))
                finally:
                    self._release(claimed)
        return outcomes

    # ---------------------------------------------------------------- activity
    def activity_snapshot(self, now: Optional[datetime] = None) -> list[dict]:
        """One plain dict for each account and kind (contacts, mail): everything `presenter.account_activity.describe`
        needs to say what that line of the account is doing - live or not, how its last run ended, what it waits
        for, when it runs next, how far a mail fetch has got and what of it is searchable. Reads only; cheap enough
        to ask every few seconds (a worker's job)."""
        now = now or datetime.fromtimestamp(self._clock())
        ts = now.timestamp()
        s = self.settings()
        hold = self.hold_reason(now, s)
        accounts = self.accounts.list()
        live = {(a.id, k): self.is_running(a.id, k) for a in accounts for k in KINDS}
        out: list[dict] = []
        waiting_meaning = self._meaning_waiting() if any(a.mail_on for a in accounts) else None
        for a in accounts:
            for kind in KINDS:
                enabled = (kind == "contacts" and a.syncs_contacts) or (kind == "mail" and a.mail_on)
                snap: dict = {"account_id": a.id, "name": a.name or a.address, "kind": kind, "enabled": bool(enabled),
                              "live": live[(a.id, kind)], "hold": hold if enabled else None,
                              "signin_expired": a.last_error_code == "ERR_ACCOUNT_SIGNIN_EXPIRED"}
                out.append(snap)
                if not enabled:
                    continue
                policy = self.policy_for(a, kind, s)
                st = self.accounts.state(a.id, kind)
                nxt = pol.next_attempt_at(policy, self._state(a, kind), now)
                run = next((r for r in self.accounts.runs(account_id=a.id, kind=kind, limit=4) if not r.dry_run), None)
                status = run.status if run else ""
                if status == "running" and not snap["live"]:
                    status = "interrupted"                 # a run the last session never finished
                if kind == "contacts" and snap["live"]:
                    snap["live_progress"] = dict(self._live_progress.get(a.id) or {})
                snap.update(
                    mode=policy.mode, next=nxt.timestamp() if nxt else None, last_success=st.last_success_at,
                    failures=st.failures,
                    run=({"status": status, "added": run.added, "changed": run.changed, "deleted": run.deleted,
                          "error_text": run.error_text or "", "finished_at": run.finished_at,
                          "started_at": run.started_at, "error_code": run.error_code or ""} if run else None))
                if kind == "contacts":
                    snap["check"] = self.accounts.check(a.id)     # order 1t 3c: the last check, or None
                if kind == "mail":
                    others = [o for o in accounts if o.id != a.id and live[(o.id, "mail")]]
                    if others and not snap["live"]:
                        snap["queued_behind"] = f"{others[0].name or others[0].address}'s mail"
                    snap["paused_by_person"] = bool((st.extra or {}).get("paused"))
                    prog = getattr(self.mail_sync, "progress", None)
                    if callable(prog):
                        try:
                            p = prog(a.id)
                            snap["mail"] = {"fetched": p.fetched, "total": p.total, "rate_per_s": p.rate_per_s,
                                            "eta_s": p.eta_s, "state": p.state}
                        except Exception:                  # noqa: BLE001 - a label; never stops the window
                            pass
                    try:
                        snap["messages"] = int(self.store.conn.execute(
                            "SELECT COUNT(*) FROM messages WHERE account_id = ?", (a.id,)).fetchone()[0])
                    except Exception:                      # noqa: BLE001
                        snap["messages"] = None
                    snap["meaning_waiting"] = waiting_meaning
        return out

    def fix_check(self, account_id: int) -> None:
        """"Fix these" (order 1t 3c): forget where the account's contacts were last read, so the next sync reads
        them all again - which adds what the account has and Leasha lacks, flags what Leasha links and the
        account no longer has, and re-merges what changed - and forget the stale check. Writes no contact."""
        self.accounts.reset_token(int(account_id), "contacts")
        self.accounts.save_check(int(account_id), {})

    def contact_failures(self, account_id: int) -> list[dict]:
        """The contacts this account refused and that still wait (order 1t 2c)."""
        return self.accounts.failures(int(account_id))

    def pause_mail(self, account_id: int) -> bool:
        """Pause an account's mail fetch (what the Pause button and the table's Pause do). The syncer's own pause:
        every mail sync of the account answers `paused` until `resume_mail`."""
        syncer = self.mail_sync
        if syncer is None or not hasattr(syncer, "pause"):
            return False
        syncer.pause(int(account_id))
        return True

    def resume_mail(self, account_id: int) -> bool:
        syncer = self.mail_sync
        if syncer is None or not hasattr(syncer, "resume"):
            return False
        syncer.resume(int(account_id))
        return True

    def _track(self, progress: Optional[Callable[[dict], None]]) -> Callable[[dict], None]:
        """A progress callback that also remembers each account's latest contact-sync event, so a view that was not
        listening when the run started (an account's panel, the Accounts list) can still say "writing 120 of
        1,436". `activity_snapshot` reads it back while the run is live."""
        def wrapped(event: dict) -> None:
            try:
                aid = int(event.get("account_id") or 0)
                if aid:
                    if event.get("phase") == "done":
                        self._live_progress.pop(aid, None)
                    else:
                        cur = self._live_progress.setdefault(aid, {"started": time.time()})
                        cur.update(event)
            except Exception:                              # noqa: BLE001 - a label; never stops a sync
                pass
            if progress is not None:
                progress(event)
        return wrapped

    def _meaning_waiting(self) -> Optional[int]:
        """Passages still waiting for the meaning model - read from the partial index that holds only them, and
        kept for a minute so a window asking every few seconds does not read it every time."""
        cached = getattr(self, "_meaning_cache", None)
        now = time.monotonic()
        if cached is not None and now - cached[0] < 60:
            return cached[1]
        try:
            n = int(self.store.conn.execute("SELECT COUNT(*) FROM chunks WHERE embedded = 0").fetchone()[0])
        except Exception:                                  # noqa: BLE001
            n = None
        self._meaning_cache = (now, n)
        return n

    # ------------------------------------------------------------------- status
    def status(self, now: Optional[datetime] = None) -> list[AccountStatus]:
        """One row per account for Settings > Accounts, the tray and `accounts status` (10o)."""
        now = now or datetime.fromtimestamp(self._clock())
        s = self.settings()
        hold = self.hold_reason(now, s)
        rows: list[AccountStatus] = []
        for acct in self.accounts.list():
            kinds = [k for k in KINDS if (k == "contacts" and acct.syncs_contacts) or (k == "mail" and acct.mail_on)]
            running = any(self.is_running(acct.id, k) for k in KINDS)
            last = _dt(acct.last_synced_at)
            nexts, texts = [], []
            for k in kinds:
                policy = self.policy_for(acct, k, s)
                st = self._state(acct, k)
                n = pol.next_attempt_at(policy, st, now)
                if n is not None:
                    nexts.append(n)
                texts.append(f"{k.title()}: {pol.describe(policy, st.last_success, now)}")
            warn = ""
            for k in kinds:
                st = self._state(acct, k)
                if pol.failing_for_24h(st.first_failure_at, now):
                    warn = "Has been failing for more than 24 hours."
            if acct.last_error_code == "ERR_ACCOUNT_SIGNIN_EXPIRED":
                word = "Needs sign-in"
            elif running:
                word = "Syncing"
            elif acct.last_error_code:
                word = "Failed"
            elif hold is not None and nexts:
                word = "Paused"
            elif last is None or (nexts and min(nexts) <= now):
                word = "Waiting"
            elif not nexts:
                word = "Up to date" if last else "Waiting"
            else:
                word = "Up to date"
            rows.append(AccountStatus(acct, word, _WORD_ICON[word], last, min(nexts) if nexts else None,
                                      " | ".join(texts), hold, warn))
        return rows
