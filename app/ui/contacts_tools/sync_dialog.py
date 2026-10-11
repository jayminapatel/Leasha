r"""The Sync window: every account's sync, live, with Stop and Sync now.

Layer: L5, driving the window's `AccountScheduler`.

Order 1k, the owner (2026-10-11): "when you press sync should open a detailed synch status window, where
you can control and see the detailed progress". One row per account: where it is (reading the account,
writing 120 of 1,436 - about 12 min left), a progress bar, the contact it is on, and what it has
added, changed, deleted and been refused. Under it, the contacts an account refused, and why - they
stay waiting and the next sync tries them again. **Closing the window does not stop the sync**; Stop
does, after the contact it is on, and what was written stays. It is not modal, and there is only ever
one of it (`open_sync_window`).

What the words say is `presenter/sync_status.py`; what a run did and what the accounts last said comes
from `LazyService.sync_overview`, on a worker. This only draws.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHeaderView, QLabel, QProgressBar, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QWidget,
)

from app.ui.contacts_tools._common import ToolDialog, btn, make_table, named
from app.ui.presenter import sync_status as words

__all__ = ["SyncDialog", "open_sync_window"]

COLUMNS = ("Account", "State", "Step", "Progress", "Working on", "So far")
_FAILURES_SHOWN = 200


class SyncDialog(ToolDialog):
    def __init__(self, ctx: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(ctx, parent, title="Sync", glyph="refresh-cw", size=(1020, 600), name="contacts-sync",
                         subtitle="Your contacts here and at every account, brought level. You can close this "
                                  "window while it works.")
        service = getattr(ctx, "service", None)
        get = getattr(service, "scheduler", None)
        self.scheduler: Any = get() if callable(get) else None
        self._row_of: dict[int, int] = {}
        self._started: dict[int, float] = {}
        self._problems: dict[tuple, bool] = {}
        self._running = self._stopping = False

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        named(self.summary, "Sync status", "What the sync is doing and what to do next")
        self.table = make_table(self, list(COLUMNS), "Accounts", "One row for each account that keeps contacts",
                                stretch=4)
        head = self.table.horizontalHeader()
        for column in (0, 1, 2, 3, 5):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMaximumHeight(210)
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
        self.body.addWidget(self.summary)
        self.body.addWidget(self.table)
        self.body.addWidget(self.problem_title)
        self.body.addWidget(self.problems, 1)

        self.sync_button = btn("Sync now", "refresh-cw", "primary",
                               "Bring Leasha and every account level now; each contact is written once", "Sync now")
        self.stop_button = btn("Stop", "square", "secondary",
                               "Stop after the contact it is on. What was written stays, and Sync now carries on",
                               "Stop")
        self.sync_button.clicked.connect(self.start_sync)
        self.stop_button.clicked.connect(self.stop_sync)
        self.add_footer_buttons(self.sync_button, self.stop_button)
        if self.scheduler is not None:
            self.scheduler.progress.connect(self._progress)
            self.scheduler.run_started.connect(self._started_run)
            self.scheduler.run_finished.connect(self._finished_run)
            self.scheduler.run_failed.connect(self._failed_run)
            self._running = bool(self.scheduler.busy)
        self._show_buttons()
        self.reload()

    # -- what the accounts last said ----------------------------------------------------------------

    def reload(self) -> None:
        read = getattr(getattr(self.ctx, "service", None), "sync_overview", None)
        if callable(read):
            self.job(read, self._overview)

    def _overview(self, rows: list) -> None:
        for r in rows:
            row = self._row(int(r["id"]), str(r["name"]))
            if self._running and int(r["id"]) in self._started:
                continue                                   # live progress is newer than the last run
            self._set(row, state=str(r.get("word", "")), step=words.result_words(
                str(r.get("status", "")), str(r.get("error", ""))), now="", sofar=words.counts_words(r))
            self.table.cellWidget(row, 3).setRange(0, 1)
            self.table.cellWidget(row, 3).setValue(0)
            for name, why in r.get("failures", []) or []:
                self._problem(str(r["name"]), str(name), str(why))
        self._summarise()

    # -- live ---------------------------------------------------------------------------------------

    def start_sync(self) -> None:
        if self.scheduler is None:
            return
        if self.scheduler.sync_now():
            self._problems.clear()
            self.problems.clear()
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

    def _progress(self, e: dict) -> None:
        aid = int(e.get("account_id") or 0)
        row = self._row(aid, str(e.get("account", "")))
        self._started.setdefault(aid, time.monotonic())
        elapsed = time.monotonic() - self._started[aid]
        phase = str(e.get("phase", ""))
        total, done = int(e.get("total") or 0), int(e.get("done") or 0)
        bar: QProgressBar = self.table.cellWidget(row, 3)
        if phase == "writing" and total:
            bar.setRange(0, total)
            bar.setValue(done)
        elif phase == "reading":
            bar.setRange(0, 0)                              # no total yet: a moving bar, not a number
        elif phase == "done":
            bar.setRange(0, 1)
            bar.setValue(1 if e.get("status") in ("ok", "partial") else 0)
        self._set(row, state={"reading": "Reading", "writing": "Writing", "done": ""}.get(phase, ""),
                  step=words.step_words(e, elapsed), now=str(e.get("current", "")),
                  sofar=words.counts_words(e) if phase != "reading" else "")
        for name, why in e.get("failures", []) or []:
            self._problem(str(e.get("account", "")), str(name), str(why))
        self._running = phase != "done" or self._running
        self._summarise()

    def _finished_run(self, outcomes: Any) -> None:
        self._running = self._stopping = False
        self._started.clear()
        for out in outcomes or []:
            row = self._row(int(out.account_id), str(out.account_name))
            err = getattr(getattr(out, "error", None), "message", "") or ""
            plan = out.plan
            self._set(row, state=words.STATUS_WORDS.get(out.status, out.status), step=words.result_words(out.status, err),
                      now="", sofar=words.counts_words({"added": plan.add_in + plan.create_out,
                                                        "changed": plan.merge_in + plan.update_out,
                                                        "deleted": plan.delete_out + plan.deleted_in,
                                                        "failed": plan.failed_out}))
            for name, why in plan.failures:
                self._problem(str(out.account_name), str(name), str(why))
        self._show_buttons()
        self._summarise()
        self.reload()

    def _failed_run(self, error: Any) -> None:
        self._running = self._stopping = False
        self._started.clear()
        self._show_buttons()
        self.say(getattr(error, "message", None) or str(error))

    # -- drawing ------------------------------------------------------------------------------------

    def _row(self, account_id: int, name: str) -> int:
        row = self._row_of.get(account_id)
        if row is not None:
            return row
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._row_of[account_id] = row
        self.table.setItem(row, 0, QTableWidgetItem(name))
        bar = QProgressBar()
        bar.setTextVisible(False)
        bar.setMaximumHeight(10)
        bar.setRange(0, 1)
        self.table.setCellWidget(row, 3, bar)
        for column in (1, 2, 4, 5):
            self.table.setItem(row, column, QTableWidgetItem(""))
        return row

    def _set(self, row: int, *, state: str, step: str, now: str, sofar: str) -> None:
        for column, text in ((1, state), (2, step), (4, now), (5, sofar)):
            item = self.table.item(row, column)
            if item is not None and item.text() != text:
                item.setText(text)
                item.setToolTip(text)

    def _problem(self, account: str, contact: str, why: str) -> None:
        key = (account, contact, why)
        if key in self._problems or len(self._problems) >= _FAILURES_SHOWN:
            return
        self._problems[key] = True
        item = QTreeWidgetItem([account, contact, why])
        item.setToolTip(2, why)
        self.problems.addTopLevelItem(item)
        self.problem_title.setText(f"<b>Could not be written ({len(self._problems):,})</b> - the account refused "
                                   "these one by one. The rest was written; Sync now tries them again.")

    def _show_buttons(self) -> None:
        self.sync_button.setEnabled(not self._running)
        self.stop_button.setEnabled(self._running and not self._stopping)

    def _summarise(self) -> None:
        self.summary.setText(words.summary_words(self._running, self._stopping, self.table.rowCount(),
                                                 len(self._problems)))


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
    if start and not window._running:
        window.start_sync()
    return window
