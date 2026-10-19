"""The sync status chip: what the account sync is doing, in a few words, on the Contacts and Mail pages.

Layer: L5 widget. Words from `app.ui.presenter.sync_chip`; driven by the window's `AccountScheduler` signals.

Review 2026-10-14, CS-8 and IA-5. On the Contacts toolbar it says "Syncing 2 accounts - writing 120 of 1,436"
while a run goes, "Held - look" when a run held something for you, and is hidden when there is nothing to say. On
the Mail page it is always there - it is the Mail page's way to the Sync window - and says what mail is doing
("Mail: fetching 15%", "Mail: up to date"), read from the activity rows every few seconds while the page is on
screen, because a mail fetch sends no progress of its own. A click opens the one Sync window, to look: it starts
nothing.

The scheduler is created after the window's first paint (`AccountRuntime`), later than the pages: the runtime
attaches every chip it finds, and a chip shown later attaches itself. On attaching, the chip reads the accounts'
last runs - the runtime's one read, or its own, on a worker - so a hold from before start-up says "Held - look"
too (`SyncTracker.seed`).
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Optional

from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtWidgets import QToolButton, QWidget

from app.ui.presenter.account_activity import describe
from app.ui.presenter.sync_chip import ChipWords, SyncTracker, mail_words
from app.ui.theme import theme_colours
from app.ui.widgets.icons import icon, icon_size

__all__ = ["SyncChip", "open_sync_status", "POLL_MS"]

#: How often the Mail chip reads the activity rows while it is on screen.
POLL_MS = 8000

_TIP = "What the account sync is doing now. Click to open the Sync window"


def open_sync_status(widget: QWidget) -> Any:
    """The one Sync window, from any page: the window's `AccountRuntime.open_sync`. None before the runtime exists."""
    top = widget.window()
    opener = getattr(getattr(top, "account_runtime", None), "open_sync", None)
    return opener(top) if callable(opener) else None


def _snapshots(factory: Any) -> list:                       # worker
    return factory().activity_snapshot(datetime.now())


class SyncChip(QToolButton):
    def __init__(self, kind: str = "contacts", parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.tracker = SyncTracker()
        self.scheduler: Any = None
        self._asking = False
        self._acts: list = []
        self._factory: Any = None
        self.setObjectName("syncChip")
        self.setAutoRaise(True)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.setIconSize(icon_size("toolbar"))
        self.setToolTip(_TIP)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.clicked.connect(lambda _c=False: open_sync_status(self))
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._ask)
        self.words = ChipWords("", _TIP, "refresh-cw", "text_dim")
        self.draw()

    # -- the scheduler
    def attach(self, scheduler: Any, factory: Any = None) -> None:
        """Follow the scheduler's signals, and read the last runs once when given `factory` (the runtime's service
        factory). The runtime attaches the chips it finds without one: its own read seeds them (`seed`)."""
        if scheduler is None or scheduler is self.scheduler:
            return
        self.scheduler = scheduler
        self._factory = factory
        scheduler.run_started.connect(lambda why="": self._then(self.tracker.started, why))
        scheduler.progress.connect(lambda e: self._then(self.tracker.progress, e))
        scheduler.run_finished.connect(lambda out: self._then(self.tracker.finished, out, ask=True))
        scheduler.run_failed.connect(lambda err: self._then(self.tracker.failed_run, err, ask=True))
        self.tracker.running = bool(getattr(scheduler, "busy", False))
        self.draw()
        self._read_last_runs()

    def _then(self, step: Any, value: Any, *, ask: bool = False) -> None:
        try:
            step(value)
            self.draw()
            if ask:
                self._ask()
        except RuntimeError:                                 # the chip was deleted under a late signal
            pass

    def _runtime(self) -> Any:
        return getattr(self.window(), "account_runtime", None)

    # -- a hold from before start-up
    def _read_last_runs(self) -> None:
        """Once, on a worker: the activity rows, for `SyncTracker.seed`. Never read on the window's thread."""
        if self._factory is None or self.tracker.heard:
            return
        from app.ui.later import when_done  # noqa: PLC0415
        from app.ui.workers import CallableWorker, run  # noqa: PLC0415
        from PySide6.QtCore import QThreadPool  # noqa: PLC0415

        worker = CallableWorker(_snapshots, self._factory, component="ui.sync_chip")
        when_done(self, worker, finished=self.seed, failed=lambda _e: None)
        run(QThreadPool.globalInstance(), worker)

    def seed(self, snaps: Any) -> None:
        if self.tracker.seed(snaps or []):
            self.draw()

    # -- the Mail chip reads the rows
    def _ask(self) -> None:
        factory = self._factory or getattr(self._runtime(), "factory", None)
        if self.kind != "mail" or factory is None or self._asking or not self.isVisible():
            return
        from app.ui.later import when_done  # noqa: PLC0415
        from app.ui.workers import CallableWorker, run  # noqa: PLC0415
        from PySide6.QtCore import QThreadPool  # noqa: PLC0415

        self._asking = True
        worker = CallableWorker(_snapshots, factory, component="ui.sync_chip")
        when_done(self, worker, finished=self.set_snapshots, failed=lambda _e: setattr(self, "_asking", False))
        run(QThreadPool.globalInstance(), worker)

    def set_snapshots(self, snaps: Any) -> None:
        self._asking = False
        self.tracker.seed(snaps or [])                       # the first read also tells of an older hold
        now = time.time()
        self._acts = [describe(s, now) for s in (snaps or []) if s.get("enabled", True)]
        self.draw()

    # -- drawing
    def draw(self) -> None:
        if self.kind == "mail":
            self.words = mail_words(self._acts, running=self.tracker.running)
            if self.tracker.held or self.tracker.failed:
                held = self.tracker.words()
                self.words = ChipWords(held.text, held.tip, held.glyph, held.tone)
        else:
            self.words = self.tracker.words()
        colours = theme_colours()
        # Its name is its words, as on every bar button (`test_ui_c_contacts`). Hidden while idle, it still keeps
        # a name - "Sync status" - so it is never a nameless control.
        said = self.words.text or "Sync status"
        self.setText(said)
        self.setAccessibleName(said)
        self.setToolTip(self.words.tip or _TIP)
        self.setIcon(icon(self.words.glyph, colours.get(self.words.tone, colours.get("text_dim", "#888888"))))
        # Never shown before it has a parent: a parentless widget made visible opens as a window of its
        # own (test_window_opens). The wish is kept, and applied when the chip is put in its bar.
        self._wanted = bool(self.words.text)
        if self.parentWidget() is not None:
            self.setVisible(self._wanted)

    def event(self, event: Any) -> bool:  # noqa: D102 - Qt's
        if event.type() == QEvent.Type.ParentChange and self.parentWidget() is not None:
            self.setVisible(getattr(self, "_wanted", False))
        return super().event(event)

    # -- Qt
    def try_attach(self) -> None:
        """Attach to the window's scheduler if it exists by now. The Contacts chip is hidden while idle - it gets
        no show event - so its toolbar calls this when it is shown."""
        runtime = self._runtime()
        if runtime is None:
            return
        rows = getattr(runtime, "last_runs", None)
        # The runtime's one read of the last runs, when it has come back; else this chip reads them itself.
        self.attach(getattr(runtime, "scheduler", None), None if rows is not None else getattr(runtime, "factory", None))
        if rows is not None:
            self.seed(rows)

    def showEvent(self, event: Any) -> None:                                 # noqa: N802 - Qt
        super().showEvent(event)
        self.try_attach()
        if self.kind == "mail":
            self._poll.start()
            self._ask()

    def hideEvent(self, event: Any) -> None:                                 # noqa: N802 - Qt
        super().hideEvent(event)
        self._poll.stop()

    def changeEvent(self, event: Any) -> None:                               # noqa: N802 - Qt
        super().changeEvent(event)
        if event.type() == QEvent.Type.StyleChange:                         # a theme change: repaint the icon
            colours = theme_colours()
            self.setIcon(icon(self.words.glyph, colours.get(self.words.tone, colours.get("text_dim", "#888888"))))
