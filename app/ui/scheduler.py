"""The clock that asks `app/index/schedule.py` whether it is time yet.

Layer: L5 (UI), driving L3

Deliberately almost empty. Every decision - what is due, whether a clock jump
should cause a second run, how long after launch to wait - is in
`app/index/schedule.py`, which is pure arithmetic and fully tested. This is a
`QTimer` and a guard, and there is nothing here worth a test that a person
clicking could not verify in ten seconds.

**One rule that is not in the pure module, because it is about this process:**
a scheduled run never starts while an index run is already going. The single-
instance lock protects the database from a second *process*; nothing protects it
from this application starting a second run over the top of its own. The guard
is `is_running()`, supplied by the caller.

**The tick is one minute.** Not one second - a background timer that wakes 3,600
times an hour to do arithmetic is exactly the sort of thing that shows up in
somebody's battery report and gets the application blamed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal

from app.core.logging import logger
from app.index.schedule import SchedulePolicy, describe, is_due

__all__ = ["IndexScheduler", "TICK_MS"]

_log = logger.bind(component="ui.scheduler")

#: One minute. Fine enough for a schedule expressed in hours, coarse enough that
#: the timer itself never appears in a power report.
TICK_MS = 60_000


class IndexScheduler(QObject):
    """Emits `due` when a scheduled index run should start."""

    due = Signal()
    state_changed = Signal(str)          # a sentence for the status bar

    def __init__(
        self,
        policy: SchedulePolicy,
        *,
        is_running: Callable[[], bool],
        load_last_run: Callable[[], Optional[datetime]],
        save_last_run: Callable[[datetime], None],
        parent: Optional[QObject] = None,
        tick_ms: int = TICK_MS,
    ) -> None:
        """Hold the policy and the three callbacks; the timer starts in `start`."""
        super().__init__(parent)
        self.policy = policy
        self._is_running = is_running
        self._load_last_run = load_last_run
        self._save_last_run = save_last_run
        self._started_at = datetime.now()
        self._last_finished: Optional[datetime] = None
        #: The stored value, read **once**. `status()` runs on every tick - once
        #: a minute, for as long as the window is open - and called
        #: `_load_last_run()` each time, which is a store read on the UI thread
        #: to produce a status-bar string that changes only when a run ends.
        #: After a run `_last_finished` answers without touching anything.
        self._stored_last_run: Optional[datetime] = None
        self._read_stored = False

        self._timer = QTimer(self)
        self._timer.setInterval(tick_ms)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        """Start ticking unless the policy is manual, and say the status."""
        if self.policy.mode != "manual":
            self._timer.start()
        self.state_changed.emit(self.status())

    def stop(self) -> None:
        self._timer.stop()

    def set_policy(self, policy: SchedulePolicy) -> None:
        """Apply a changed setting without restarting the application."""
        self.policy = policy
        self._timer.stop()
        self.start()

    def notify_finished(self, when: Optional[datetime] = None) -> None:
        """Call when an index run ends, however it ended.

        Records the time so the interval counts from a real finish, and feeds
        the minimum-gap backstop. Called on *any* completion, including a failed
        or interrupted one - otherwise a run that fails immediately becomes an
        instant retry loop.
        """
        moment = when or datetime.now()
        self._last_finished = moment
        # **Written on a worker.** Once per run rather than once per minute, so
        # the cost is small - but it is still a store write from a Qt slot, and
        # it lands exactly when an index run has just released the write lock
        # and the vector store is compacting. The status bar already has the
        # value from `_last_finished`; persisting it can take as long as it
        # likes.
        from app.ui.workers import CallableWorker, run as run_worker

        worker = CallableWorker(self._save_last_run, moment,
                                component="ui.scheduler.save")
        worker.signals.failed.connect(
            lambda error: _log.warning(
                "could not record the last index time: {}",
                getattr(error, "message", error)))
        run_worker(QThreadPool.globalInstance(), worker)
        self.state_changed.emit(self.status())

    def status(self) -> str:
        return describe(self.policy, last_run=self._safe_last_run(), now=datetime.now())

    def _safe_last_run(self) -> Optional[datetime]:
        """When indexing last finished. Cached - see `_stored_last_run`."""
        if self._last_finished is not None:
            return self._last_finished        # this session knows better
        if not self._read_stored:
            self._read_stored = True
            try:
                self._stored_last_run = self._load_last_run()
            except Exception:                 # noqa: BLE001
                self._stored_last_run = None
        return self._stored_last_run

    def _tick(self) -> None:
        """Timer slot, UI thread: emit `due` when the policy says so and nothing runs."""
        if self._is_running():
            return                            # never stack a run on top of a run
        now = datetime.now()
        if not is_due(
            self.policy,
            last_run=self._safe_last_run(),
            now=now,
            started_at=self._started_at,
            last_finished=self._last_finished,
        ):
            return
        _log.info("scheduled index run is due ({})", self.policy.mode)
        self.due.emit()
