"""The mail syncer: fetch an account's mail, resumably, read only, into the index.

Layer: L7

Order 1k 5a-5c (wiring), 5e, 5f, 10f, 10j, 10q. `MailSyncer` is what
`AccountService.mail_sync` calls - `syncer(account, trigger=..., cancel=...)` - and
what `attach_mail(service)` installs. It builds the account's `MailProvider`, pulls
its pages and hands each to `mail_index.MailIndexer`; it owns the sync log row, the
resume token and the first-fetch progress, and nothing else.

**Resumable (rule 4).** A page's messages and its resume token are written in ONE
`store.batch()` (`MailIndexer.apply_page(in_transaction=...)`), so a crash costs at
most the page in flight and the next sync carries on from the last token saved. A
first fetch is recorded in `account_sync_state.extra["first_fetch"]` (its window,
how many fetched, the server's total, the rate), kept after it finishes as a record.

**One page ahead.** The provider's pages are read on a thread of their own
(`_ReadAhead`), one page ahead of the page being parsed, written and embedded, so the
network and the embedder work at the same time instead of in turn. Only the page
being applied has its token saved; a page read ahead and not applied (Stop, giving
way to contacts, a failure) is simply read again by the next sync.

**Read only (rule 10).** The provider is wrapped in `_ReadOnly`, which has `pull`,
`list_folders` and `close` and nothing else; the syncer cannot reach a verb that
could change a mailbox. The connectors are policed by their own tests
(`test_mail_readonly.py`), the syncer by `test_mail_sync.py`.

**Gone is followed (5e).** A `RemoteMessageGone` removes the message, its
attachments and their vectors from the index. Narrowing the chosen folders or
labels removes what is outside them; widening them (or the window, or switching
Spam and Trash on) starts the fetch again from the beginning, which re-reads
everything and writes what is new. A change token the provider has dropped
(`TokenExpired`) does the same, up to twice.

**Budget and courtesy.**
  * `AccountSettings.first_fetch_kb_s` (10j) caps a first fetch by bytes per second;
    the pause is `sleep` (injectable), taken in slices of at most a second so Stop
    is still heard.
  * A first fetch gives way between pages to a due contact sync (10f):
    `should_yield()` (default: the service says a contacts sync is due) is asked after
    each page and a True answer ends the run `paused`, its token saved, to carry on
    at the next mail sync. Stop (`cancel()`) ends it the same way.
  * "Pause" and "Resume" (10q.6) set `extra["paused"]`, which makes every mail sync of
    the account return `paused` at once until Resume.

**The options (`Account.options`, written by Settings > Accounts > the account's
mail panel; read by `MailOptions.from_account`):**

  | key | meaning (10q) | default |
  |---|---|---|
  | `mail_folders` | folder or label names to fetch; absent or empty means all | all |
  | `mail_years` | first fetch only the last N years (D12) | everything |
  | `mail_since` | first fetch only mail since this date (epoch seconds or `YYYY-MM-DD`); wins over `mail_years` | everything |
  | `mail_include_spam_trash` | also fetch Spam and Trash | false |
  | `mail_attachments` | index attachments (false: only their names, in the message text) | true |
  | `mail_attachment_limit_mb` | read attachments up to this size; a bigger one is kept by name | the index's own 64 MB |
  | `gmail_over_imap` | a Google account read over IMAP with an app password (D3 route 2) | false |
  | `imap_username` | the IMAP login if it is not the address | the address |
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Iterator, Optional

from app.accounts.engine import SyncOutcome, _with_fix  # noqa: PLC2701 - one wording for a fix in the log
from app.accounts.http import retry_at_of
from app.accounts.mail_index import MailIndexer, MailSpace, PreparedMessage
from app.accounts.provider import (MailPage, MailProvider, RemoteMessage, RemoteMessageGone,
                                   TokenExpired)
from app.accounts.store import Account
from app.core.errors import AppErrorException, make_error, to_app_error
from app.core.logging import logger

__all__ = ["MailSyncer", "MailOptions", "MailProgress", "attach_mail", "build_mail_provider",
           "GMAIL_IMAP_HOST"]

_log = logger.bind(component="accounts.mail_sync")

GMAIL_IMAP_HOST = "imap.gmail.com"
_YEAR_S = 365.25 * 86400
#: Restarts of one sync after the provider drops its change token. Two is enough for a
#: token that expired; more would be a loop against a server that never settles.
_TOKEN_RESTARTS = 2
#: A first fetch gives way to a due contact sync only after it has had this long to work. Without it, contacts that
#: never finish (held by the delete brake, so always due) stopped the mail after its first page, every time.
MIN_SLICE_S = 90.0


# --------------------------------------------------------------------------
# The options of one account's mail, and its first-fetch progress
# --------------------------------------------------------------------------

@dataclass
class MailOptions:
    folders: Optional[list[str]] = None
    since: Optional[float] = None
    include_spam_trash: bool = False
    attachments: bool = True
    attachment_limit: Optional[int] = None             # bytes; None: the index's own

    @classmethod
    def from_account(cls, account: Account, *, now: Optional[float] = None) -> "MailOptions":
        o = account.options or {}
        folders = [str(f) for f in (o.get("mail_folders") or []) if str(f).strip()] or None
        since = _since_of(o.get("mail_since"))
        if since is None and o.get("mail_years"):
            try:
                since = (time.time() if now is None else now) - float(o["mail_years"]) * _YEAR_S
            except (TypeError, ValueError):
                since = None
        limit = None
        if o.get("mail_attachment_limit_mb"):
            try:
                limit = int(float(o["mail_attachment_limit_mb"]) * 1024 * 1024)
            except (TypeError, ValueError):
                limit = None
        return cls(folders=folders, since=since, include_spam_trash=bool(o.get("mail_include_spam_trash")),
                   attachments=bool(o.get("mail_attachments", True)), attachment_limit=limit)


def _since_of(value: Any) -> Optional[float]:
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return None


@dataclass
class MailProgress:
    """First-fetch progress for the account panel (10q.6)."""
    state: str = "idle"                  # idle | fetching | paused | done
    fetched: int = 0
    total: Optional[int] = None
    rate_per_s: float = 0.0
    kb_per_s: float = 0.0
    eta_s: Optional[float] = None
    since: Optional[float] = None
    paused: bool = False                 # the person pressed Pause


# --------------------------------------------------------------------------
# The provider, as the syncer may use it
# --------------------------------------------------------------------------

class _ReadAhead:
    """Iterate `pages` on a thread of its own, at most `depth` pages ahead of the reader.

    The provider's generator runs, fails and is closed on that thread only; a failure
    there is raised here, in order, after the pages before it. `close()` stops it.
    """

    _END = object()

    def __init__(self, pages: Iterator[MailPage], depth: int = 1) -> None:
        self._pages = pages
        self._q: queue.Queue[Any] = queue.Queue(maxsize=max(1, depth))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="mail-read-ahead", daemon=True)
        self._thread.start()

    def _put(self, item: Any) -> bool:
        while not self._stop.is_set():
            try:
                self._q.put(item, timeout=0.2)
                return True
            except queue.Full:
                continue
        return False

    def _run(self) -> None:
        try:
            for page in self._pages:
                if not self._put(page):
                    return
            self._put(self._END)
        except BaseException as exc:                              # noqa: BLE001 - handed to the reader
            self._put(exc)
        finally:
            close = getattr(self._pages, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:                                 # noqa: BLE001 - closing is best effort
                    _log.debug("closing the mail pages failed", exc_info=True)

    def __iter__(self) -> _ReadAhead:
        return self

    def __next__(self) -> MailPage:
        item = self._q.get()
        if item is self._END:
            raise StopIteration
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self._stop.set()
        while self._thread.is_alive():
            try:                                                  # free a put that is waiting
                self._q.get_nowait()
            except queue.Empty:
                pass
            self._thread.join(timeout=0.2)


class _ReadOnly:
    """What the syncer holds of a provider: three verbs, none of which writes."""

    __slots__ = ("_p",)

    def __init__(self, provider: MailProvider) -> None:
        self._p = provider

    def pull(self, token: Optional[str], **kw: Any) -> Iterator[MailPage]:
        return self._p.pull(token, **kw)

    def list_folders(self) -> list[str]:
        return self._p.list_folders()

    def close(self) -> None:
        self._p.close()


def build_mail_provider(service: Any, account: Account) -> MailProvider:
    """The connector for `account` (5a-5c): Gmail API, Graph, or IMAP (iCloud and Gmail are presets)."""
    from app.accounts.oauth import AppPasswordSource
    from app.accounts.secrets import account_key

    p = account.provider
    if p == "google" and not account.option("gmail_over_imap"):
        from app.accounts.providers.gmail_api import GmailApiMailProvider

        return GmailApiMailProvider(service.transport_for(account), service.token_source(account),
                                    account_name=account.name)
    if p == "microsoft":
        from app.accounts.providers.graph_mail import GraphMailProvider

        # Kept connections (one per download thread): a new TLS connection cost about
        # 240 ms a message against 50 ms on a kept one, measured 2026-10-13.
        return GraphMailProvider(service.contact_transport(account), service.token_source(account),
                                 account_name=account.name)
    from app.accounts.providers.imap_mail import ImapMailProvider

    username = str(account.option("imap_username") or account.address)
    if p == "google":                                           # D3 route 2: Gmail over IMAP, an app password
        source = AppPasswordSource(service.secrets, account_key(account.id, "app_password"),
                                   account=account.name)
        return ImapMailProvider(GMAIL_IMAP_HOST, port=993, security="ssl", username=username,
                                token_source=source, account_name=account.name, gmail=True)
    if p == "apple":
        return ImapMailProvider.icloud(username=username, token_source=service.token_source(account),
                                       account_name=account.name)
    if p == "imap":
        if not account.server:
            raise AppErrorException(make_error("ERR_ACCOUNT_NOT_CONFIGURED", "accounts.mail_sync",
                                               account=account.name))
        return ImapMailProvider(account.server, port=int(account.port or 993),
                                security=str(account.security or "ssl"), username=username,
                                token_source=service.token_source(account), account_name=account.name)
    raise AppErrorException(make_error("ERR_ACCOUNT_REJECTED", "accounts.mail_sync", account=account.name,
                                       reason=f"{p} accounts have no mail connector"))


# --------------------------------------------------------------------------
# The syncer
# --------------------------------------------------------------------------

class MailSyncer:
    def __init__(self, service: Any, *, indexer: Optional[MailIndexer] = None,
                 provider_for: Optional[Callable[[Account], MailProvider]] = None,
                 should_yield: Optional[Callable[[], bool]] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic,
                 clock: Callable[[], float] = time.time) -> None:
        self.service = service
        self.accounts = service.accounts
        self.indexer = indexer or MailIndexer(service.store)
        self.provider_for = provider_for or (lambda account: build_mail_provider(service, account))
        self._should_yield = should_yield or self._contacts_due
        self._min_slice_s = MIN_SLICE_S
        self._sleep = sleep
        self._mono = monotonic
        self._clock = clock

    # ------------------------------------------------------------------ call
    def __call__(self, account: Account, trigger: str = "sync-now",
                 cancel: Callable[[], bool] = lambda: False) -> SyncOutcome:
        """One mail sync of `account`. Never raises: a failure is recorded and returned (one
        account failing never stops the others)."""
        acc = self.accounts
        run_id = acc.start_run(account, "mail", trigger)
        out = SyncOutcome(account.id, account.name, run_id, "running")
        provider: Optional[_ReadOnly] = None
        try:
            acc.record_attempt(account.id, "mail")
            if (acc.state(account.id, "mail").extra or {}).get("paused"):
                out.status = "paused"                             # the person's Pause: no connection is made
            else:
                provider = _ReadOnly(self.provider_for(account))
                self._sync(account, provider, out, cancel)
        except Exception as exc:                                  # noqa: BLE001 - see the docstring
            err = (exc.error if isinstance(exc, AppErrorException) else
                   to_app_error(exc, "accounts.mail_sync", "ERR_SYNC_FAILED", account=account.name,
                                reason=f"{type(exc).__name__}: {exc}"))
            err.context.setdefault("account", account.name)
            out.status, out.error = "failed", err
            _log.warning("mail sync of {} failed: {}", account.name, err.code)
        finally:
            if provider is not None:
                try:
                    provider.close()
                except Exception:                                 # noqa: BLE001 - closing is best effort
                    _log.debug("mail provider close failed", exc_info=True)
        self._finish(account, out)
        return out

    def _finish(self, account: Account, out: SyncOutcome) -> None:
        p = out.plan
        counts = dict(added=p.added, changed=p.changed, deleted=p.deleted, held=p.held, plan=p.to_json())
        if out.status == "failed" and out.error is not None:
            # A provider that said how long to wait is waited for that long (review CS-5).
            self.accounts.record_failure(account.id, "mail", out.error.code, out.error.message,
                                         next_retry_at=retry_at_of(out.error, self._clock()))
            self.accounts.finish_run(out.run_id, "failed", error_code=out.error.code,
                                     error_text=_with_fix(out.error), **counts)
        elif out.status == "paused":
            self.accounts.finish_run(out.run_id, "paused", **counts)
        else:
            out.status = "ok"
            self.accounts.record_success(account.id, "mail")
            self.accounts.finish_run(out.run_id, "ok", **counts)

    # ------------------------------------------------------------------ sync
    def _sync(self, account: Account, provider: _ReadOnly, out: SyncOutcome,
              cancel: Callable[[], bool]) -> None:
        acc = self.accounts
        opts = MailOptions.from_account(account, now=self._clock())
        state = acc.state(account.id, "mail")
        extra: dict[str, Any] = dict(state.extra or {})
        token = self._follow_option_changes(account, opts, state.token, extra, out)
        restarts = 0
        while True:
            try:
                self._pull(account, provider, out, cancel, opts, token, extra)
                return
            except TokenExpired:
                restarts += 1
                _log.info("the change token of {} expired - fetching again from the start", account.name)
                if restarts > _TOKEN_RESTARTS:
                    raise
                acc.reset_token(account.id, "mail")
                token = None
                extra.pop("first_fetch", None)

    def _pull(self, account: Account, provider: _ReadOnly, out: SyncOutcome, cancel: Callable[[], bool],
              opts: MailOptions, token: Optional[str], extra: dict[str, Any]) -> None:
        acc, plan = self.accounts, out.plan
        settings = acc.settings()
        ff = extra.get("first_fetch")
        resuming = token is not None and isinstance(ff, dict) and bool(ff.get("active"))
        first = token is None or resuming
        since: Optional[float] = None
        base_elapsed = 0.0
        if first:
            if not resuming:                                        # a fetch from the beginning
                ff = {"active": True, "state": "fetching", "since": opts.since, "fetched": 0, "total": None,
                      "bytes": 0, "elapsed_s": 0.0, "started_at": self._clock()}
            since = ff.get("since")                                 # fixed for the whole fetch, resumes included
            base_elapsed = float(ff.get("elapsed_s", 0.0))
            ff["state"] = "fetching"
            extra["first_fetch"] = ff
            extra["applied"] = self._applied(opts, since)
        cap_kb_s = int(settings.first_fetch_kb_s or 0)
        run_started = self._mono()
        bytes_run = 0
        last_token = token
        pages = _ReadAhead(provider.pull(token, folders=opts.folders, since=since,
                                         include_spam_trash=opts.include_spam_trash))
        try:
            for page in pages:
                prepared: list[PreparedMessage] = []
                gone: list[str] = []
                fetched = fetched_bytes = 0
                for change in page.changes:
                    if isinstance(change, RemoteMessageGone):
                        gone.append(change.remote_id)
                    elif isinstance(change, RemoteMessage):
                        fetched += 1
                        fetched_bytes += len(change.raw)
                        message = self._prepare(account, change, opts)
                        if message is not None:
                            prepared.append(message)
                if first:
                    ff["fetched"] += fetched
                    ff["bytes"] += fetched_bytes
                    if page.total_hint:
                        ff["total"] = int(page.total_hint)
                    self._rates(ff, base_elapsed + (self._mono() - run_started))
                    ff["active"] = not page.done
                    ff["state"] = "done" if page.done else "fetching"
                    if page.done:
                        ff["finished_at"] = self._clock()
                saved = {k: v for k, v in extra.items() if v is not None}
                result = self.indexer.apply_page(
                    account.id, prepared, gone,
                    in_transaction=lambda conn, t=page.next_token, e=saved: acc.save_token(
                        account.id, "mail", t, extra=e, conn=conn))
                last_token = page.next_token
                plan.add_in += result.added
                plan.merge_in += result.changed
                plan.delete_out += result.deleted
                for p in prepared[:5]:
                    plan.note("add_in", str(p.pieces[0].meta.get("subject") or "(no subject)"))
                bytes_run += fetched_bytes
                if page.done:
                    break
                if first and cap_kb_s > 0:
                    self._throttle(bytes_run, cap_kb_s, run_started, cancel)
                if cancel():
                    self._pause(account, out, ff if first else None, extra, last_token, "stopped")
                    return
                if first and self._mono() - run_started >= self._min_slice_s and self._should_yield():
                    self._pause(account, out, ff, extra, last_token, "a contact sync is due")
                    return
        finally:
            pages.close()

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _applied(opts: MailOptions, since: Optional[float]) -> dict[str, Any]:
        return {"folders": opts.folders, "since": since, "spam": opts.include_spam_trash}

    def _prepare(self, account: Account, change: RemoteMessage, opts: MailOptions) -> Optional[PreparedMessage]:
        try:
            return self.indexer.prepare(account.id, change, attachments=opts.attachments,
                                        attachment_limit=opts.attachment_limit)
        except Exception as exc:                                  # noqa: BLE001 - one bad message costs itself (rule 3)
            _log.warning("could not read message {} of {}: {}", change.remote_id, account.name, exc)
            return None

    @staticmethod
    def _rates(ff: dict[str, Any], elapsed: float) -> None:
        """Rate and time left from what this fetch has done so far, across its resumes
        (`elapsed` is the fetch's whole running time: earlier runs' plus this one's)."""
        ff["elapsed_s"] = round(elapsed, 3)
        ff["rate_per_s"] = round(ff["fetched"] / elapsed, 3) if elapsed > 0 else 0.0
        ff["kb_per_s"] = round(ff["bytes"] / 1024 / elapsed, 2) if elapsed > 0 else 0.0
        total = ff.get("total")
        ff["eta_s"] = (round(max(0, total - ff["fetched"]) / ff["rate_per_s"], 1)
                       if total and ff["rate_per_s"] else None)

    def _throttle(self, bytes_run: int, kb_s: int, started: float, cancel: Callable[[], bool]) -> None:
        """Hold the first fetch to `kb_s` kilobytes a second on average (10j)."""
        wait = bytes_run / (kb_s * 1024.0) - (self._mono() - started)
        while wait > 0:
            step = min(1.0, wait)
            self._sleep(step)
            wait -= step
            if cancel():
                return

    def _pause(self, account: Account, out: SyncOutcome, ff: Optional[dict], extra: dict[str, Any],
               token: Optional[str], why: str) -> None:
        """End the run `paused`, the first-fetch record saying so; the token is already saved."""
        if ff is not None and ff.get("active"):
            ff["state"] = "paused"
            extra["first_fetch"] = ff
            self.accounts.save_token(account.id, "mail", token, extra={k: v for k, v in extra.items()
                                                                        if v is not None})
        out.status = "paused"
        _log.info("mail sync of {} paused: {}", account.name, why)

    def _follow_option_changes(self, account: Account, opts: MailOptions, token: Optional[str],
                               extra: dict[str, Any], out: SyncOutcome) -> Optional[str]:
        """Narrowing the chosen folders takes the rest out of the index; widening anything
        starts the fetch again (it re-reads, and writes what is new). 5e, 5f."""
        applied = extra.get("applied")
        ff = extra.get("first_fetch") or {}
        if token is None or not isinstance(applied, dict) or ff.get("active"):
            return token
        old_f, new_f = applied.get("folders"), opts.folders
        old_since = applied.get("since")
        widen = False
        if new_f is not None:
            narrowed = old_f is None or bool({x.casefold() for x in old_f} - {x.casefold() for x in new_f})
            if old_f is not None and {x.casefold() for x in new_f} - {x.casefold() for x in old_f}:
                widen = True
            if narrowed:
                removed = self.indexer.prune_unchosen(account.id, new_f)
                out.plan.delete_out += removed
        elif old_f is not None:
            widen = True
        if opts.include_spam_trash and not applied.get("spam"):
            widen = True
        if old_since is not None and (opts.since is None or opts.since < float(old_since)):
            widen = True
        if widen:
            self.accounts.reset_token(account.id, "mail")
            extra.pop("first_fetch", None)
            extra["applied"] = None
            return None
        extra["applied"] = self._applied(opts, old_since)
        return token

    def _contacts_due(self) -> bool:
        """Is a contact sync due now? Then a first fetch gives way to it (10f)."""
        try:
            return any(w.kind == "contacts" for w in self.service.due_work())
        except Exception:                                         # noqa: BLE001 - never stop a fetch for a question
            return False

    # ------------------------------------------------------- for the window
    def progress(self, account_id: int) -> MailProgress:
        """The first fetch as the account panel shows it (10q.6)."""
        extra = self.accounts.state(account_id, "mail").extra or {}
        ff = extra.get("first_fetch")
        if not isinstance(ff, dict):
            return MailProgress(paused=bool(extra.get("paused")))
        state = "paused" if extra.get("paused") else str(ff.get("state") or "fetching")
        return MailProgress(state=state, fetched=int(ff.get("fetched", 0)), total=ff.get("total"),
                            rate_per_s=float(ff.get("rate_per_s", 0.0)), kb_per_s=float(ff.get("kb_per_s", 0.0)),
                            eta_s=ff.get("eta_s"), since=ff.get("since"), paused=bool(extra.get("paused")))

    def pause(self, account_id: int) -> None:
        """Pause (10q.6): every mail sync of the account returns `paused` until `resume`."""
        self._set_paused(account_id, True)

    def resume(self, account_id: int) -> None:
        self._set_paused(account_id, False)

    def _set_paused(self, account_id: int, paused: bool) -> None:
        state = self.accounts.state(account_id, "mail")
        extra = dict(state.extra or {})
        if paused:
            extra["paused"] = True
            if isinstance(extra.get("first_fetch"), dict) and extra["first_fetch"].get("active"):
                extra["first_fetch"]["state"] = "paused"
        else:
            extra.pop("paused", None)
            if isinstance(extra.get("first_fetch"), dict) and extra["first_fetch"].get("active"):
                extra["first_fetch"]["state"] = "fetching"
        self.accounts.save_token(account_id, "mail", state.token, extra=extra)

    def space_used(self, account_id: int) -> MailSpace:
        """Space this account's mail takes in the index (10q.5)."""
        return self.indexer.space_used(account_id)

    def remove_account_mail(self, account_id: int) -> int:
        """Take this account's mail out of the index (10q.5, 1f) and forget how far it had got, so
        a later fetch starts from the beginning. `AccountService.remove_mail_hook`."""
        count = self.indexer.remove_account(account_id)
        try:
            self.accounts.save_token(account_id, "mail", None, extra={})
        except Exception as exc:                                  # noqa: BLE001 - the account may be going too
            _log.debug("mail state of account {} not reset: {}", account_id, exc)
        return count


def attach_mail(service: Any, *, indexer: Optional[MailIndexer] = None, vectors: Any = None,
                image_vectors: Any = None, embed: Optional[Callable[[list[str]], Any]] = None,
                **syncer_options: Any) -> MailSyncer:
    """Install the mail syncer on an `AccountService`: `service.mail_sync` and `service.remove_mail_hook`.

    The shell passes the pipeline's vector stores and `embedder_function(pipeline.embedder)` so
    fetched mail gets its meaning; without them it is searchable by its words and the next index
    run adds the meaning. Returns the syncer (for `progress`, `pause`, `resume`, `space_used`).
    """
    indexer = indexer or MailIndexer(service.store, vectors=vectors, image_vectors=image_vectors, embed=embed)
    syncer = MailSyncer(service, indexer=indexer, **syncer_options)
    service.mail_sync = syncer
    service.remove_mail_hook = syncer.remove_account_mail
    return syncer
