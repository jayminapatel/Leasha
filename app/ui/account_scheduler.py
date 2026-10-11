"""The clock that asks `AccountService.due_work` whether any account is due.

Layer: L5 (UI), driving L7

Order 1k 10b, 10c. The `app/ui/scheduler.py` pattern: a `QTimer` and a guard, no
decisions. What is due is `AccountService.due_work` (pure arithmetic over the
settings and the saved state, idempotent about the past: a due time that fell
while the machine slept is served once, D11.4; one catch-up after a closed
spell, D16).

**What starts a sync (D11.2): this timer's tick and `sync_now`, nothing else.**
Not app start, not wake, not the network coming back. The first tick is one
interval after `start`, and the catch-up a minute after the window opened is the
service's own rule.

**One run at a time, serial.** A tick, or a "Sync now", that arrives while a run
is going is folded into it (the signal `folded` says so) - never queued twice.
The service folds the same way per account and kind; this gate is the window's.
Contacts go before mail inside `run_due` (10f).

**The tick is 30 seconds.** It is the whole cost: the read happens on a worker.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal

from app.core.logging import logger

__all__ = ["AccountScheduler", "TICK_MS"]

_log = logger.bind(component="ui.account_scheduler")

TICK_MS = 30_000


def _scheduled(factory: Callable[[], Any], window_opened_at: datetime) -> list:
    """Worker: ask the schedule; run what it says is due. Starts nothing else."""
    svc = factory()
    now = datetime.now()
    if not svc.due_work(now, window_opened_at=window_opened_at):
        return []
    return svc.run_due(now, window_opened_at=window_opened_at)


def _now(factory: Callable[[], Any], account_id: Optional[int], kind: Optional[str],
         allow_brake: bool, approve: bool) -> list:
    """Worker: "Sync now" for all accounts, one account or one kind of one account."""
    svc = factory()
    if approve and account_id is not None:
        return svc.approve_first_sync(account_id, allow_brake=allow_brake)
    return svc.sync_now(account_id, kind, allow_brake=allow_brake)


class AccountScheduler(QObject):
    run_started = Signal(str)          # "schedule" | "sync-now"
    run_finished = Signal(object)      # the list of SyncOutcome
    run_failed = Signal(object)        # an AppError
    folded = Signal()                  # a request arrived while a run was going

    def __init__(self, factory: Callable[[], Any], *, tick_ms: int = TICK_MS,
                 window_opened_at: Optional[datetime] = None, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._factory = factory
        self.window_opened_at = window_opened_at or datetime.now()
        self._busy = False
        self._timer = QTimer(self)
        self._timer.setInterval(tick_ms)
        self._timer.timeout.connect(self.tick)

    # -- lifecycle
    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    @property
    def busy(self) -> bool:
        return self._busy

    # -- the two things that start a sync
    def tick(self) -> bool:
        """Timer slot, UI thread: start a worker that asks the schedule. False when a run is going."""
        if self._busy:
            return False
        self._start("schedule", _scheduled, self._factory, self.window_opened_at)
        return True

    def sync_now(self, account_id: Optional[int] = None, kind: Optional[str] = None, *,
                 allow_brake: bool = False, approve: bool = False) -> bool:
        """'Sync now'. False (and `folded`) when a run is already going: it is folded into that one."""
        if self._busy:
            self.folded.emit()
            return False
        self._start("sync-now", _now, self._factory, account_id, kind, allow_brake, approve)
        return True

    def sync_all_now(self) -> bool:
        """'Sync accounts now' from the tray: every account, both kinds."""
        return self.sync_now()

    def _start(self, why: str, fn: Callable[..., Any], *args: Any) -> None:
        from app.ui.workers import CallableWorker, run  # noqa: PLC0415

        self._busy = True
        worker = CallableWorker(fn, *args, component="ui.account_scheduler")
        worker.signals.finished.connect(lambda out, w=why: self._ended(w, out))
        worker.signals.failed.connect(self._failed)
        run(QThreadPool.globalInstance(), worker)
        # A scheduled tick that finds nothing due must not flash "Syncing": the signal is sent
        # from `_ended` only when something ran. A Sync now says so at once.
        if why == "sync-now":
            self.run_started.emit(why)

    def _ended(self, why: str, outcomes: Any) -> None:
        self._busy = False
        outcomes = list(outcomes or [])
        if why == "schedule" and not outcomes:
            return
        _log.info("account sync ended ({}): {} outcome(s)", why, len(outcomes))
        self.run_finished.emit(outcomes)

    def _failed(self, error: Any) -> None:
        self._busy = False
        self.run_failed.emit(error)
