"""Order 1k: the Sync window - every account's sync live, with Stop and Sync now.

Layer: L5

The owner (2026-10-11): "when you press sync should open a detailed synch status window, where you can
control and see the detailed progress". A fake scheduler stands in for the window's, so each test says
exactly what the worker would send.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal                      # noqa: E402

from app.ui.contacts_tools.sync_dialog import SyncDialog, open_sync_window   # noqa: E402
from tests.unit.test_contacts_tools_ui_common import open_dialog, unnamed_controls   # noqa: E402


class FakeScheduler(QObject):
    progress = Signal(object)
    run_started = Signal(str)
    run_finished = Signal(object)
    run_failed = Signal(object)

    def __init__(self):
        super().__init__()
        self.busy = False
        self.calls = []

    def sync_now(self, *a, **k):
        self.calls.append("sync")
        if self.busy:
            return False
        self.busy = True
        self.run_started.emit("sync-now")
        return True

    def stop_run(self):
        self.calls.append("stop")


ROWS = [
    {"id": 1, "name": "GMail Main", "word": "Up to date", "status": "ok", "error": "", "added": 0, "changed": 20,
     "deleted": 0, "failed": 0, "failures": []},
    {"id": 2, "name": "MS Main", "word": "Failed", "status": "partial", "error": "", "added": 0, "changed": 5,
     "deleted": 0, "failed": 1, "failures": [["Alaa Arawi", "MS Main refused the request (update a contact: HTTP 500)."]]},
]


@pytest.fixture
def ctx():
    sched = FakeScheduler()
    service = SimpleNamespace(scheduler=lambda: sched, sync_overview=lambda: ROWS)
    return SimpleNamespace(service=service, refresh=lambda: None), sched


def _cell(dlg, account, column):
    for r in range(dlg.table.rowCount()):
        if dlg.table.item(r, 0).text() == account:
            return dlg.table.item(r, column).text()
    raise AssertionError(account)


def test_it_opens_with_each_accounts_last_result_and_what_it_refused(ctx, qtbot):
    c, _sched = ctx
    dlg = open_dialog(qtbot, SyncDialog, c)
    qtbot.waitUntil(lambda: dlg.table.rowCount() == 2, timeout=5000)
    assert _cell(dlg, "GMail Main", 2) == "Done" and "20 changed" in _cell(dlg, "GMail Main", 5)
    assert _cell(dlg, "MS Main", 2) == "Done, with problems" and "1 problem" in _cell(dlg, "MS Main", 5)
    assert dlg.problems.topLevelItemCount() == 1
    assert dlg.problems.topLevelItem(0).text(1) == "Alaa Arawi" and "HTTP 500" in dlg.problems.topLevelItem(0).text(2)
    assert "Idle" in dlg.summary.text() or "finished with 1 problem" in dlg.summary.text()
    assert dlg.sync_button.isEnabled() and not dlg.stop_button.isEnabled()


def test_progress_shows_the_step_the_bar_and_the_contact_it_is_on(ctx, qtbot):
    c, sched = ctx
    dlg = open_dialog(qtbot, SyncDialog, c)
    sched.run_started.emit("sync-now")
    sched.progress.emit({"account": "MS Main", "account_id": 2, "phase": "reading", "done": 300, "total": 0})
    assert "Reading the account - 300 so far" in _cell(dlg, "MS Main", 2)
    assert dlg.table.cellWidget(0 if _cell(dlg, "MS Main", 0) else 1, 3) is not None
    sched.progress.emit({"account": "MS Main", "account_id": 2, "phase": "writing", "done": 120, "total": 1436,
                         "current": "Waqas Khan", "failed": 2, "changed": 118,
                         "failures": [["Alaa Arawi", "HTTP 500"]]})
    row = next(r for r in range(dlg.table.rowCount()) if dlg.table.item(r, 0).text() == "MS Main")
    assert "Writing 120 of 1,436" in _cell(dlg, "MS Main", 2)
    bar = dlg.table.cellWidget(row, 3)
    assert (bar.minimum(), bar.maximum(), bar.value()) == (0, 1436, 120)
    assert _cell(dlg, "MS Main", 4) == "Waqas Khan" and "2 problems" in _cell(dlg, "MS Main", 5)
    assert not dlg.sync_button.isEnabled() and dlg.stop_button.isEnabled()
    assert "Syncing now" in dlg.summary.text() and "close this window" in dlg.summary.text().lower()


def test_sync_now_starts_a_sync_and_stop_asks_the_scheduler_to_stop(ctx, qtbot):
    c, sched = ctx
    dlg = open_dialog(qtbot, SyncDialog, c)
    dlg.sync_button.click()
    assert sched.calls == ["sync"] and not dlg.sync_button.isEnabled() and dlg.stop_button.isEnabled()
    dlg.stop_button.click()
    assert sched.calls == ["sync", "stop"] and not dlg.stop_button.isEnabled()
    assert "Stopping after the contact it is on" in dlg.summary.text()
    out = SimpleNamespace(account_id=1, account_name="GMail Main", status="stopped", error=None,
                          plan=SimpleNamespace(add_in=0, create_out=0, merge_in=0, update_out=40, delete_out=0,
                                               deleted_in=0, failed_out=0, failures=[]))
    sched.busy = False
    sched.run_finished.emit([out])
    assert _cell(dlg, "GMail Main", 2) == "Stopped" and "40 changed" in _cell(dlg, "GMail Main", 5)
    assert dlg.sync_button.isEnabled() and not dlg.stop_button.isEnabled()


def test_a_finished_run_lists_what_each_account_refused(ctx, qtbot):
    c, sched = ctx
    dlg = open_dialog(qtbot, SyncDialog, c)
    out = SimpleNamespace(account_id=2, account_name="MS Main", status="partial", error=None,
                          plan=SimpleNamespace(add_in=0, create_out=0, merge_in=0, update_out=9, delete_out=0,
                                               deleted_in=0, failed_out=2,
                                               failures=[["Bob Bell", "HTTP 500"], ["Cy Cox", "HTTP 500"]]))
    sched.run_finished.emit([out])
    names = {dlg.problems.topLevelItem(i).text(1) for i in range(dlg.problems.topLevelItemCount())}
    assert {"Bob Bell", "Cy Cox", "Alaa Arawi"} <= names
    assert "Could not be written" in dlg.problem_title.text()


def test_there_is_one_window_closing_it_does_not_stop_the_sync_and_every_control_is_named(ctx, qtbot):
    c, sched = ctx
    first = open_sync_window(None, c)
    qtbot.addWidget(first)
    assert sched.calls == ["sync"]                                    # opening started one
    first.close()
    assert sched.calls == ["sync"]                                    # closing did not stop it
    second = open_sync_window(None, c)
    assert second is first and sched.calls == ["sync"]                # reopened; one was going, so none started
    assert unnamed_controls(first) == []
