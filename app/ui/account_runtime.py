"""The single `AccountService` of the open index, and the runtime that drives it.

Layer: L5

Order 1k 8, 10b, 10h, 10m. Two halves:

* `get_service(shell)` / `service_factory_for(store)` - the one service for the open
  index, built the first time something on a worker asks for it. **This module's top
  level imports nothing heavy** (no Qt, no `app.accounts`): the window imports it at
  start-up and must not pay for the OAuth, HTTP and sync-engine modules until an
  account is actually used.
* `AccountRuntime` - created once by the window after its first paint
  (`MainWindow._start_background_work`). It owns the `AccountScheduler`, hands it to the
  Accounts page, gives the tray its two items ("Pause account sync", "Sync accounts now"),
  and sends the Windows toasts of 10m through the tray, honouring each switch.

**It starts nothing on its own account**: the scheduler's tick and Sync now are the only
starters (D11.2); this class only wires and reports.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any, Callable, Optional

__all__ = ["LazyService", "get_service", "service_for_store", "service_factory_for", "AccountRuntime", "start_runtime"]

_LOCK = threading.Lock()
_FALLBACK: dict[int, Any] = {}


def service_for_store(store: Any) -> Any:
    """The one `AccountService` for this open store. Call from a worker: the first call imports."""
    with _LOCK:
        svc = getattr(store, "_account_service", None) or _FALLBACK.get(id(store))
        if svc is None:
            from app.accounts.service import AccountService  # noqa: PLC0415 - lazy by design (start-up)
            svc = AccountService(store)
            # Phase B: fetched mail is indexed by the same service. The window leaves the vector
            # stores and the embedder on the store (`_account_mail_deps`) so fetched mail gets its
            # meaning at once; without them it is searchable by its words and the next index run
            # adds the meaning (`mail_sync.attach_mail`).
            from app.accounts.mail_sync import attach_mail  # noqa: PLC0415
            attach_mail(svc, **(getattr(store, "_account_mail_deps", None) or {}))
            try:
                store._account_service = svc
            except AttributeError:
                _FALLBACK[id(store)] = svc
        return svc


def service_factory_for(store: Any) -> Callable[[], Any]:
    return lambda: service_for_store(store)


class LazyService:
    """What the Contacts page is given: the service, built only when a worker first needs it.

    Every method is explicit so that merely *looking* for one (the page asks for hooks with
    `getattr`) never builds the service on the window's thread. `request_sync` is the one
    thing meant for the UI thread: it asks the account scheduler, the starter besides the
    schedule (D11.2).
    """

    def __init__(self, shell: Any) -> None:
        self._shell = shell

    def request_sync(self) -> bool:
        runtime = getattr(self._shell, "account_runtime", None)
        return bool(runtime is not None and runtime.scheduler.sync_all_now())

    def scheduler(self) -> Any:
        """The window's account scheduler (the Sync window listens to it), or None before it exists."""
        return getattr(getattr(self._shell, "account_runtime", None), "scheduler", None)

    def sync_overview(self) -> list:                            # worker
        """One plain dict per account that syncs contacts: its state word and what its last contact run did,
        for the Sync window."""
        svc = get_service(self._shell)
        last: dict[int, Any] = {}
        for run in svc.accounts.runs(kind="contacts", limit=60):            # newest first
            if not run.dry_run:
                last.setdefault(run.account_id, run)
        rows = []
        for st in svc.status(datetime.now()):
            if not st.account.syncs_contacts:
                continue
            run = last.get(st.account.id)
            plan = dict(getattr(run, "plan", None) or {})
            rows.append({
                "id": st.account.id, "name": st.account.name or st.account.address, "word": st.word,
                "status": getattr(run, "status", ""), "error": getattr(run, "error_text", "") or "",
                "added": int(plan.get("add_in", 0)) + int(plan.get("create_out", 0)),
                "changed": int(plan.get("merge_in", 0)) + int(plan.get("update_out", 0)),
                "deleted": int(plan.get("delete_out", 0)) + int(plan.get("deleted_in", 0)),
                "failed": int(plan.get("failed_out", 0)),
                # order 1t 2c: what the account still refuses - kept across runs, with the contact to open
                "failures": [[f.get("name", ""), f.get("reason", ""), f.get("contact_id")]
                             for f in svc.accounts.failures(st.account.id)]})
        return rows

    def fix_check(self, account_id: int) -> None:                # worker
        get_service(self._shell).fix_check(account_id)

    def open_contact(self, contact_id: int) -> None:            # UI thread
        opener = getattr(self._shell, "_open_contact", None)
        if callable(opener) and contact_id:
            opener(int(contact_id))

    def pause_mail(self, account_id: int) -> bool:               # worker
        return get_service(self._shell).pause_mail(account_id)

    def resume_mail(self, account_id: int) -> bool:              # worker
        return get_service(self._shell).resume_mail(account_id)

    def open_account(self, account_id: int, action: str = "") -> None:   # UI thread
        """Show Settings > Accounts with this account selected; "reconnect" and "test" then run that button."""
        runtime = getattr(self._shell, "account_runtime", None)
        if runtime is None:
            return
        runtime.open_page()
        page = getattr(getattr(self._shell, "settings_view", None), "accounts_page", None)
        if page is None:
            return
        page.ensure_built()
        page.account_list.select(int(account_id))
        panel = getattr(page, "panel", None)
        if panel is not None and action in ("reconnect", "test"):
            getattr(panel, "_reconnect" if action == "reconnect" else "_test")()

    def open_contacts_tool(self, name: str) -> None:            # UI thread
        """A Contacts tool from an activity row: "duplicates" (review 2026-10-14 CS-10: the mirror waits until
        safe duplicates are merged) or "conflicts" (contacts changed in two places wait for your choice)."""
        view = getattr(self._shell, "contacts_view", None)
        show = getattr(self._shell, "_show", None)
        if view is None:
            return
        if callable(show):
            show(view)
        page = view.ensure_built() if callable(getattr(view, "ensure_built", None)) else getattr(view, "page", None)
        if page is not None and callable(getattr(page, "open_tool", None)):
            page.open_tool(name)

    def activity_overview(self) -> list:                        # worker
        """What each account's contacts and mail are doing: `AccountService.activity_snapshot`."""
        return get_service(self._shell).activity_snapshot(datetime.now())

    def push_accounts(self, account_ids: Any) -> Any:           # worker
        return get_service(self._shell).push_accounts(account_ids)

    def contact_provider(self, account: Any) -> Any:           # worker
        return get_service(self._shell).contact_provider(account)

    def new_contact(self, card: Any, **kw: Any) -> Any:         # worker
        return get_service(self._shell).new_contact(card, **kw)

    def resolve_conflict(self, contact_id: int, account_id: int, choose: str) -> Any:   # worker
        return get_service(self._shell).resolve_conflict(contact_id, account_id, choose)


def get_service(shell: Any) -> Any:
    """The service for the window's open index (`shell._store`)."""
    return service_for_store(shell._store)


def _snapshot(factory: Callable[[], Any]) -> tuple[Any, list]:
    """Worker: the settings and the statuses, for the tray and the toasts."""
    svc = factory()
    return svc.settings(), svc.status(datetime.now())


def _last_runs(factory: Callable[[], Any]) -> list:
    """Worker: the activity rows, whose last runs say whether a sync was held or failed before start-up."""
    return factory().activity_snapshot(datetime.now())


def _set_pause(factory: Callable[[], Any], choice: str) -> None:
    from app.ui.accounts.logic import pause_value  # noqa: PLC0415
    value = None if choice == "off" else pause_value(choice, datetime.now())
    factory().accounts.update_settings(paused_until=value)


class AccountRuntime:
    def __init__(self, shell: Any) -> None:
        from PySide6.QtCore import QObject  # noqa: PLC0415

        from app.ui.account_scheduler import AccountScheduler  # noqa: PLC0415

        self.shell = shell
        engine = getattr(shell, "_engine", None)
        shell._store._account_mail_deps = {
            "vectors": getattr(shell, "_vectors", None),
            "image_vectors": getattr(shell, "_image_vectors", None),
            "embed": (lambda texts: list(engine.embedder.embed_all(texts))) if engine is not None
                     and hasattr(engine, "embedder") else None,
        }
        self.factory = service_factory_for(shell._store)
        self._owner = QObject(shell if isinstance(shell, QObject) else None)
        self.scheduler = AccountScheduler(self.factory, parent=self._owner)
        self.told: set[str] = set()
        self.page: Any = getattr(getattr(shell, "settings_view", None), "accounts_page", None)
        self.scheduler.run_finished.connect(self._after_run)
        if self.page is not None:
            self.page.attach_scheduler(self.scheduler)
            self.page.state_loaded.connect(self._on_state)
            # Review 2026-10-14 IA-5 / CS-10: Settings > Accounts opens the Sync window and Find duplicates.
            if hasattr(self.page, "sync_window_requested"):
                self.page.sync_window_requested.connect(lambda: self.open_sync())
                self.page.contacts_tool_requested.connect(lambda n: LazyService(self.shell).open_contacts_tool(n))
        tray = getattr(shell, "tray", None)
        if tray is not None and hasattr(tray, "set_account_actions"):
            tray.set_account_actions(pause=self.pause, sync_now=self.sync_all, open_page=self.open_page,
                                     open_sync=self.open_sync)
        # CS-8: the ambient status - the tray's line, and every sync chip already built (the rest attach when shown).
        from app.ui.presenter.sync_chip import SyncTracker  # noqa: PLC0415
        self.tracker = SyncTracker()
        for signal, slot in ((self.scheduler.run_started, self.tracker.started),
                             (self.scheduler.progress, self.tracker.progress),
                             (self.scheduler.run_finished, self.tracker.finished),
                             (self.scheduler.run_failed, self.tracker.failed_run)):
            signal.connect(lambda value=None, s=slot: (s(value), self._tell_tray()))
        # A hold or failure from before start-up: `start` reads the last runs once and seeds the tray's tracker and
        # every chip from that one read; a chip attached later takes the same rows (`last_runs`).
        self.last_runs: Optional[list] = None
        if isinstance(shell, QObject):
            from app.ui.widgets.sync_chip import SyncChip  # noqa: PLC0415
            for chip in shell.findChildren(SyncChip):
                chip.attach(self.scheduler)

    # -- the tray's two items
    def pause(self, choice: str) -> None:
        """'hour' | 'tomorrow' | 'forever' | 'off' (turn sync back on)."""
        from app.ui.workers import CallableWorker, run  # noqa: PLC0415
        from PySide6.QtCore import QThreadPool  # noqa: PLC0415

        worker = CallableWorker(_set_pause, self.factory, choice, component="ui.account_runtime")
        worker.signals.finished.connect(lambda _r: self._refresh_all())
        run(QThreadPool.globalInstance(), worker)

    def sync_all(self) -> None:
        self.scheduler.sync_all_now()

    def open_sync(self, parent: Any = None) -> Any:
        """The one Sync window, to look - it starts no sync (IA-5: from Mail, Settings > Accounts, the tray and the
        status chips)."""
        from types import SimpleNamespace  # noqa: PLC0415

        from app.ui.contacts_tools.sync_dialog import open_sync_window  # noqa: PLC0415

        tray = getattr(self.shell, "tray", None)
        if parent is None and tray is not None and hasattr(tray, "restore"):
            tray.restore()
        host = parent if parent is not None else self.shell
        from PySide6.QtWidgets import QWidget  # noqa: PLC0415
        if not isinstance(host, QWidget):
            host = None
        return open_sync_window(host, SimpleNamespace(service=LazyService(self.shell)), start=False)

    def _read_last_runs(self) -> None:
        from app.ui.workers import CallableWorker, run  # noqa: PLC0415
        from PySide6.QtCore import QThreadPool  # noqa: PLC0415

        worker = CallableWorker(_last_runs, self.factory, component="ui.account_runtime")
        worker.signals.finished.connect(self._seed)
        worker.signals.failed.connect(lambda _e: None)
        run(QThreadPool.globalInstance(), worker)

    def _seed(self, snaps: Any) -> None:
        """UI thread, no I/O: the last runs, to the tray's tracker and every chip (each takes them once)."""
        self.last_runs = list(snaps or [])
        if self.tracker.seed(self.last_runs):
            self._tell_tray()
        from PySide6.QtCore import QObject  # noqa: PLC0415
        if isinstance(self.shell, QObject):
            from app.ui.widgets.sync_chip import SyncChip  # noqa: PLC0415
            for chip in self.shell.findChildren(SyncChip):
                chip.seed(self.last_runs)

    def _tell_tray(self) -> None:
        tray = getattr(self.shell, "tray", None)
        if tray is not None and hasattr(tray, "set_accounts_activity"):
            tray.set_accounts_activity(self.tracker.tray_line())

    def open_page(self) -> None:
        tray = getattr(self.shell, "tray", None)
        if tray is not None:
            tray.restore()
        view = getattr(self.shell, "settings_view", None)
        if view is not None:
            self.shell._show(view)
            from app.ui.accounts_page import CATEGORY_ACCOUNTS  # noqa: PLC0415
            view._nav.show_category(CATEGORY_ACCOUNTS, persist=False)

    # -- life
    def start(self) -> None:
        """Start the 30 s tick, and read once so a sign-in about to expire is noticed at start."""
        self.scheduler.start()
        self._refresh_all(read_only=True)
        self._read_last_runs()

    def stop(self) -> None:
        self.scheduler.stop()

    # -- reporting
    def _refresh_all(self, read_only: bool = False) -> None:
        from app.ui.workers import CallableWorker, run  # noqa: PLC0415
        from PySide6.QtCore import QThreadPool  # noqa: PLC0415

        if self.page is not None and not read_only:
            self.page.refresh()
        worker = CallableWorker(_snapshot, self.factory, component="ui.account_runtime")
        worker.signals.finished.connect(lambda snap: self._snapshot(snap, []))
        worker.signals.failed.connect(lambda _e: None)
        run(QThreadPool.globalInstance(), worker)

    def _after_run(self, outcomes: Any) -> None:
        from app.ui.workers import CallableWorker, run  # noqa: PLC0415
        from PySide6.QtCore import QThreadPool  # noqa: PLC0415

        worker = CallableWorker(_snapshot, self.factory, component="ui.account_runtime")
        worker.signals.finished.connect(lambda snap, o=list(outcomes or []): self._snapshot(snap, o))
        worker.signals.failed.connect(lambda _e: None)
        run(QThreadPool.globalInstance(), worker)

    def _on_state(self, state: Any) -> None:
        self._snapshot((state.settings, state.statuses), [])

    def _snapshot(self, snap: Any, outcomes: list) -> None:
        """UI thread, no I/O: the tray's pause state and warning, and the toasts."""
        from app.ui.accounts import logic  # noqa: PLC0415

        settings, statuses = snap
        now = datetime.now()
        tray = getattr(self.shell, "tray", None)
        if tray is None:
            return
        paused = logic.hold_text(settings, now) if (
            settings.paused_until is not None and (settings.paused_until >= 9e15
                                                   or settings.paused_until > now.timestamp())) else None
        if hasattr(tray, "set_accounts_paused"):
            tray.set_accounts_paused(paused)
        attention = [s.account.name for s in statuses if s.word in ("Needs sign-in", "Failed") or s.warning]
        if hasattr(tray, "set_accounts_warning"):
            tray.set_accounts_warning(
                ("Needs attention: " + ", ".join(attention)) if attention else "")
        for toast in logic.toasts_for(settings, statuses, outcomes, now, self.told):
            if hasattr(tray, "toast"):
                tray.toast(toast.title, toast.text)


def start_runtime(shell: Any) -> Optional[AccountRuntime]:
    """Create and start the runtime; called once, after the window's first paint."""
    runtime = AccountRuntime(shell)
    runtime.start()
    return runtime
