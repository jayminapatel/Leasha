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
from app.accounts.http import AllowedHosts, RetryingTransport, UrllibTransport
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

    def contact_provider(self, account: Account) -> ContactProvider:
        """The connector for `account` (4a-4e)."""
        from app.accounts.providers.carddav import CardDavProvider, basic_auth_header  # noqa: PLC0415
        transport = self.transport_for(account)
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
            server = account.option("server_url") or (f"https://{account.server}" if account.server else "")
            if account.server:
                self.allowed.allow_server(account.server)
            return CardDavProvider(transport, basic_auth_header(account.address, self.token_source(account)),
                                   server_url=server, addressbook_url=url, account=account.name)
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

    def add_password_account(self, provider: str, address: str, password: str, name: str = "", *,
                             server: Optional[str] = None, port: Optional[int] = None,
                             security: Optional[str] = None, mail: bool = False, contacts: bool = True,
                             direction: str = "two-way", options: Optional[dict] = None,
                             test: bool = True) -> Account:
        """Apple, any IMAP or any CardDAV server: an app password, kept only in Credential Manager."""
        preset = IMAP_PRESETS.get(provider, {})
        server = server or preset.get("server")
        port = port or preset.get("port")
        security = security or preset.get("security")
        opts = dict(options or {})
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
        self.accounts.delete_rows(account_id)

    #: Phase B: removes an account's mail from the index (set by the mail syncer).
    remove_mail_hook: Optional[Callable[[int], Any]] = None

    # --------------------------------------------------------------------- sync
    def sync_now(self, account_id: Optional[int] = None, kind: Optional[str] = None, *,
                 trigger: str = "sync-now", approve_first: bool = False, allow_brake: bool = False,
                 cancel: Callable[[], bool] = lambda: False) -> list[SyncOutcome]:
        """'Sync now' for all accounts, one account, or one kind of one account (10c)."""
        kinds = (kind,) if kind else KINDS
        outcomes: list[SyncOutcome] = []
        if "contacts" in kinds:
            ids = [a.id for a in self.accounts.list() if a.syncs_contacts and (account_id in (None, a.id))]
            claimed = self._claim([(i, "contacts") for i in ids])
            try:
                if claimed:
                    outcomes += self.engine.sync([i for i, _ in claimed], trigger=trigger,
                                                 approve_first=approve_first, allow_brake=allow_brake,
                                                 cancel=cancel)
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
                cancel: Callable[[], bool] = lambda: False) -> list[SyncOutcome]:
        work = self.due_work(now, window_opened_at=window_opened_at)
        outcomes: list[SyncOutcome] = []
        contacts = [w for w in work if w.kind == "contacts"]
        if contacts:
            trigger = "schedule" if any(w.reason == "schedule" for w in contacts) else "schedule (catch-up)"
            ids = [w.account.id for w in contacts]
            claimed = self._claim([(i, "contacts") for i in ids])
            try:
                if claimed:
                    outcomes += self.engine.sync([i for i, _ in claimed], trigger=trigger, cancel=cancel)
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
