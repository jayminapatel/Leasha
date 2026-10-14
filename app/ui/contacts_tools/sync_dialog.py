r"""The Sync window: what every account's contacts and mail are doing, live, with buttons for what you can do.

Layer: L5, driving the window's `AccountScheduler`.

Order 1k, the owner (2026-10-11): "when you press sync should open a detailed synch status window, where
you can control and see the detailed progress"; and 2026-10-12: contacts and mail sync progress and status for
each account "is a must", with "command buttons there for functions". One table (`ActivityTable`) with a row for
each account's contacts and one for its mail: its status, a bar, a sentence (reading the account, writing 120 of
1,436 - about 12 min left, fetching mail 15% - 4 hours left, waiting for another account's mail), what comes
next, and the buttons that make sense for that state: Sync now, Stop, Pause or Resume the mail fetch, Try again,
Test connection, Reconnect, Settings. Under it a line for everything, and the contacts an account refused, and
why - they stay waiting and the next sync tries them again. **Closing the window does not stop the sync**; Stop
does, after the contact it is on, and what was written stays. It is not modal, and there is only ever one of
it (`open_sync_window`).

The words are `presenter/account_activity.py` (and `sync_status.py` for the line above the table); the data is
`AccountService.activity_snapshot`, read on a worker every few seconds, with the running contact sync's own events
laid over it as they arrive. This only draws and passes a button press on.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QHeaderView, QLabel, QTreeWidget, QTreeWidgetItem, QWidget

from app.ui.contacts_tools._common import ToolDialog, btn, named
from app.ui.presenter import sync_status as words
from app.ui.presenter.account_activity import overall_line
from app.ui.widgets.activity_table import ActivityTable

__all__ = ["SyncDialog", "open_sync_window"]

_FAILURES_SHOWN = 200


class SyncDialog(ToolDialog):
    def __init__(self, ctx: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(ctx, parent, title="Sync", glyph="refresh-cw", size=(1100, 620), name="contacts-sync",
                         subtitle="Your contacts and mail, at every account: what each is doing, how far it has "
                                  "got, and what comes next. You can close this window while it works.")
        service = getattr(ctx, "service", None)
        get = getattr(service, "scheduler", None)
        self.scheduler: Any = get() if callable(get) else None
        self._problems: dict[tuple, bool] = {}
        self._running = self._stopping = False
        self._snaps: list = []
        self._live: dict[int, dict] = {}
        self._asking = False

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        named(self.summary, "Sync status", "What the sync is doing and what to do next")
        self.activity = ActivityTable(self)
        self.activity.action_requested.connect(self.do)
        self.activity_summary = QLabel("")
        self.activity_summary.setWordWrap(True)
        self.activity_summary.setAccessibleName("Summary of every account")
        self.problems = QTreeWidget()
        self.problems.setHeaderLabels(["Account", "Contact", "Why the account refused it"])
        self.problems.setRootIsDecorated(False)
        self.problems.setAlternatingRowColors(True)
        named(self.problems, "Contacts that could not be written",
              "Contacts an account refused one by one. They stay waiting and the next sync tries them again")
        self.problems.header().setStretchLastSection(True)
        self.problems.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.problems.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.problem_title = QLabel("")
        # Order 1t 2c: what was refused stays listed across runs, and can be acted on.
        self.problems.itemDoubleClicked.connect(self._open_problem)
        self.open_problem_button = btn("Open contact", "user", "secondary",
                                       "Open the chosen contact in Contacts, to see what the account would not take",
                                       "Open contact")
        self.retry_button = btn("Retry these", "rotate-cw", "secondary",
                                "Sync again - the one account that refused them, or every account if several did; "
                                "each refused contact is sent again", "Retry these")
        self.open_problem_button.clicked.connect(lambda: self._open_problem(self.problems.currentItem()))
        self.retry_button.clicked.connect(self._retry_problems)
        self.retry_button.setEnabled(False)
        self.body.addWidget(self.summary)
        self.body.addWidget(self.activity, 3)
        self.body.addWidget(self.activity_summary)
        self.body.addWidget(self.problem_title)
        self.body.addWidget(self.problems, 1)

        self.sync_button = btn("Sync now", "refresh-cw", "primary",
                               "Bring Leasha and every account level now; each contact is written once", "Sync now")
        self.stop_button = btn("Stop", "square", "secondary",
                               "Stop after the contact it is on. What was written stays, and Sync now carries on",
                               "Stop")
        self.sync_button.clicked.connect(self.start_sync)
        self.stop_button.clicked.connect(self.stop_sync)
        self.add_footer_buttons(self.sync_button, self.stop_button, self.retry_button, self.open_problem_button)
        if self.scheduler is not None:
            self.scheduler.progress.connect(self._progress)
            self.scheduler.run_started.connect(self._started_run)
            self.scheduler.run_finished.connect(self._finished_run)
            self.scheduler.run_failed.connect(self._failed_run)
            self._running = bool(self.scheduler.busy)
        self._show_buttons()
        self.reload()
        self._poll = QTimer(self)                          # a child of the window: it stops when the window goes
        self._poll.setInterval(4000)
        self._poll.timeout.connect(self._ask_activity)
        self._poll.start()
        self._ask_activity()

    # -- what every account is doing ------------------------------------------------------------------

    def _ask_activity(self) -> None:
        read = getattr(getattr(self.ctx, "service", None), "activity_overview", None)
        if not callable(read) or self._asking:
            return
        self._asking = True
        self.job(read, self._activity_ready, on_fail=lambda _e: setattr(self, "_asking", False))

    def _activity_ready(self, snaps: list) -> None:
        self._asking = False
        self._snaps = list(snaps or [])
        self._draw_activity()

    def _draw_activity(self) -> None:
        shown = self.activity.set_snapshots(self._snaps, time.time(), self._live)
        self.activity_summary.setText(overall_line(shown, self._snaps))
        self._summarise()

    # -- the buttons on a row -------------------------------------------------------------------------

    def do(self, account_id: int, kind: str, action: str) -> None:
        """A row's button. Sync and Stop go to the scheduler; Pause and Resume to the mail syncer (on a worker);
        Test, Reconnect and Settings open the account in Settings > Accounts, where those live."""
        service = getattr(self.ctx, "service", None)
        if action in ("sync", "retry"):
            if self.scheduler is None:
                return
            self._trust_the_scheduler()
            if self.scheduler.sync_now(account_id, kind):
                self._running, self._stopping = True, False
                self._show_buttons()
            else:
                self.say("A sync is already running; this window shows it, and this one starts after it.")
        elif action == "stop":
            self.stop_sync()
        elif action in ("pause", "resume"):
            call = getattr(service, "pause_mail" if action == "pause" else "resume_mail", None)
            if callable(call):
                self.job(lambda: call(account_id), lambda _r: self._ask_activity())
        elif action == "allow":
            if self.scheduler is None:
                return
            self._trust_the_scheduler()
            if self.scheduler.sync_now(account_id, "contacts", allow_brake=True):
                self._running, self._stopping = True, False
                self._show_buttons()
            else:
                self.say("A sync is already running; press Allow again when it has finished.")
        elif action == "fix":
            fix = getattr(service, "fix_check", None)
            if callable(fix) and self.scheduler is not None:
                self.job(lambda: fix(account_id), lambda _r: self.do(account_id, "contacts", "sync"))
        elif action in ("settings", "test", "reconnect"):
            opener = getattr(service, "open_account", None)
            if callable(opener):
                opener(account_id, action)

    # -- what the accounts last refused -----------------------------------------------------------------

    def reload(self) -> None:
        """Ask for each account's last contact run: what it refused. The table above has everything else."""
        read = getattr(getattr(self.ctx, "service", None), "sync_overview", None)
        if callable(read):
            self.job(read, self._overview)
        self._ask_activity()

    def _overview(self, rows: list) -> None:
        for r in rows:
            for entry in r.get("failures", []) or []:
                name, why = entry[0], entry[1]
                cid = entry[2] if len(entry) > 2 else None
                self._problem(str(r["name"]), str(name), str(why), cid, int(r.get("id") or 0))
        self._summarise()

    def _open_problem(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        """Double-click (or Open contact): the contact the account refused, in Contacts."""
        cid = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        opener = getattr(getattr(self.ctx, "service", None), "open_contact", None)
        if cid and callable(opener):
            opener(int(cid))

    def _retry_problems(self) -> None:
        """Try again: a sync of the accounts that refused something; each refused contact is sent again."""
        ids = sorted({int(i.data(1, Qt.ItemDataRole.UserRole) or 0)
                      for i in (self.problems.topLevelItem(n) for n in range(self.problems.topLevelItemCount()))})
        ids = [i for i in ids if i]
        if self.scheduler is None or not ids:
            return
        if len(ids) == 1:
            self.do(ids[0], "contacts", "sync")
        else:
            self.start_sync()

    # -- live ---------------------------------------------------------------------------------------

    def start_sync(self) -> None:
        if self.scheduler is None:
            return
        self._trust_the_scheduler()
        if self.scheduler.sync_now():
            self._problems.clear()
            self.problems.clear()
            self.retry_button.setEnabled(False)
            self._running, self._stopping = True, False
            self._show_buttons()
        else:
            self.say("A sync is already running; this window shows it.")

    def stop_sync(self) -> None:
        if self.scheduler is not None and self._running:
            self._stopping = True
            self.scheduler.stop_run()
            self._show_buttons()
            self._summarise()

    def _started_run(self, _why: Any = "") -> None:
        self._running = True
        self._show_buttons()
        self._summarise()
        self._ask_activity()

    def _progress(self, e: dict) -> None:
        aid = int(e.get("account_id") or 0)
        phase = str(e.get("phase", ""))
        live = self._live.setdefault(aid, {"started": time.time()})
        live.update(e)
        for entry in e.get("failures", []) or []:
            self._problem(str(e.get("account", "")), str(entry[0]), str(entry[1]), entry[2] if len(entry) > 2 else None,
                          aid)
        self._running = phase != "done" or self._running
        if self._snaps:
            self._draw_activity()
        else:
            self._summarise()

    def _finished_run(self, outcomes: Any) -> None:
        self._running = self._stopping = False
        self._live.clear()
        for out in outcomes or []:
            for entry in out.plan.failures:
                self._problem(str(out.account_name), str(entry[0]), str(entry[1]),
                              entry[2] if len(entry) > 2 else None, int(out.account_id or 0))
        self._show_buttons()
        self._summarise()
        self.reload()

    def _failed_run(self, error: Any) -> None:
        self._running = self._stopping = False
        self._live.clear()
        self._show_buttons()
        self.say(getattr(error, "message", None) or str(error))
        self._ask_activity()

    # -- drawing ------------------------------------------------------------------------------------

    def _problem(self, account: str, contact: str, why: str, contact_id: Any = None, account_id: int = 0) -> None:
        key = (account, contact, why)
        if key in self._problems or len(self._problems) >= _FAILURES_SHOWN:
            return
        self._problems[key] = True
        item = QTreeWidgetItem([account, contact, why])
        item.setToolTip(2, why)
        if contact_id:
            item.setData(0, Qt.ItemDataRole.UserRole, int(contact_id))
            item.setToolTip(1, "Double-click to open this contact")
        if account_id:
            item.setData(1, Qt.ItemDataRole.UserRole, int(account_id))
        self.retry_button.setEnabled(True)
        self.problems.addTopLevelItem(item)
        self.problem_title.setText(f"<b>Could not be written ({len(self._problems):,})</b> - the account refused "
                                   "these one by one. The rest was written; Sync now tries them again.")

    def _trust_the_scheduler(self) -> None:
        """What is running is the scheduler's to say. A "running" this window kept after the run ended (a late
        progress note, a window opened mid-run) must not leave Sync now greyed out for good."""
        if self.scheduler is not None and self._running and not self.scheduler.busy:
            self._running = self._stopping = False
            self._live.clear()

    def _show_buttons(self) -> None:
        self._trust_the_scheduler()
        self.sync_button.setEnabled(not self._running)
        self.stop_button.setEnabled(self._running and not self._stopping)

    def _summarise(self) -> None:
        accounts = len({a.account_id for a in self.activity.activities})
        self.summary.setText(words.summary_words(self._running, self._stopping, accounts, len(self._problems)))


def open_sync_window(parent: Any, ctx: Any, *, start: bool = True) -> Any:
    """The one Sync window: shown if it is not, raised if it is, and a sync started if none is going."""
    owner = parent if parent is not None else ctx
    window = getattr(owner, "_sync_window", None)
    try:
        if window is None or not window.isVisible() and window.scheduler is None:
            window = None
        if window is not None:
            window.reload()
    except RuntimeError:                                     # the window was deleted under it
        window = None
    if window is None:
        window = SyncDialog(ctx, parent)
        window.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        try:
            owner._sync_window = window
        except AttributeError:
            pass
    window.show()
    window.raise_()
    window.activateWindow()
    window._trust_the_scheduler()
    window._show_buttons()
    if start and not window._running:
        window.start_sync()
    return window
