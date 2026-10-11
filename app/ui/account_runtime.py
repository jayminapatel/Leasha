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
        tray = getattr(shell, "tray", None)
        if tray is not None and hasattr(tray, "set_account_actions"):
            tray.set_account_actions(pause=self.pause, sync_now=self.sync_all, open_page=self.open_page)

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
