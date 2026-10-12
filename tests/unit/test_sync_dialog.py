"""Order 1k: the Sync window - what every account's contacts and mail are doing, live, with buttons.

Layer: L5

The owner (2026-10-11): "when you press sync should open a detailed synch status window, where you can
control and see the detailed progress"; (2026-10-12) contacts and mail status and progress for each account, and
"command buttons there for functions". A fake scheduler stands in for the window's, so each test says exactly
what the worker would send.
"""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal                      # noqa: E402
from PySide6.QtWidgets import QToolButton                       # noqa: E402

from app.ui.contacts_tools.sync_dialog import SyncDialog, open_sync_window   # noqa: E402
from tests.unit.test_contacts_tools_ui_common import open_dialog, unnamed_controls   # noqa: E402

NOW = time.time()


class FakeScheduler(QObject):
    progress = Signal(object)
    run_started = Signal(str)
    run_finished = Signal(object)
    run_failed = Signal(object)

    def __init__(self):
        super().__init__()
        self.busy = False
        self.calls = []

    def sync_now(self, account_id=None, kind=None, **_k):
        self.calls.append(("sync", account_id, kind))
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


def snap(account_id, name, kind, **kw):
    base = {"account_id": account_id, "name": name, "kind": kind, "enabled": True, "live": False, "hold": None,
            "signin_expired": False, "mode": "interval", "next": NOW + 2700, "last_success": NOW - 600, "failures": 0,
            "run": {"status": "ok", "added": 0, "changed": 20, "deleted": 0, "error_text": ""}}
    base.update(kw)
    return base


SNAPS = [snap(1, "GMail Main", "contacts"), snap(1, "GMail Main", "mail", messages=1200),
         snap(2, "MS Main", "contacts", failures=1, run={"status": "partial", "changed": 5, "added": 0, "deleted": 0,
                                                         "error_text": ""}),
         snap(2, "MS Main", "mail", next=NOW - 5, queued_behind="GMail Main's mail")]


@pytest.fixture
def ctx():
    sched = FakeScheduler()
    calls = []
    service = SimpleNamespace(scheduler=lambda: sched, sync_overview=lambda: ROWS, activity_overview=lambda: SNAPS,
                              pause_mail=lambda aid: calls.append(("pause", aid)) or True,
                              resume_mail=lambda aid: calls.append(("resume", aid)) or True,
                              open_account=lambda aid, action="": calls.append(("open", aid, action)))
    return SimpleNamespace(service=service, refresh=lambda: None), sched, calls


def _row(dlg, account, what):
    last = ""
    for r in range(dlg.activity.rowCount()):
        last = dlg.activity.item(r, 0).text() or last              # an account's name is written once
        if last == account and dlg.activity.item(r, 1).text() == what:
            return r
    raise AssertionError((account, what))


def _cell(dlg, account, what, column):
    return dlg.activity.item(_row(dlg, account, what), column).text()


def _buttons(dlg, account, what):
    holder = dlg.activity.cellWidget(_row(dlg, account, what), 6)
    return {b.text(): b for b in holder.findChildren(QToolButton)}


def test_it_opens_with_a_row_for_each_accounts_contacts_and_mail_and_what_it_refused(ctx, qtbot):
    c, _sched, _calls = ctx
    dlg = open_dialog(qtbot, SyncDialog, c)
    qtbot.waitUntil(lambda: dlg.activity.rowCount() == 4 and dlg.problems.topLevelItemCount() == 1, timeout=5000)
    assert _cell(dlg, "GMail Main", "Contacts", 2) == "Up to date"
    assert "20 changed" in _cell(dlg, "GMail Main", "Contacts", 4)
    assert _cell(dlg, "MS Main", "Mail", 2) == "Waiting its turn"
    assert "waiting for GMail Main's mail to finish" in _cell(dlg, "MS Main", "Mail", 4)
    assert dlg.problems.topLevelItem(0).text(1) == "Alaa Arawi" and "HTTP 500" in dlg.problems.topLevelItem(0).text(2)
    assert "finished with 1 problem" in dlg.summary.text()
    assert "3 up to date" in dlg.activity_summary.text() and "1 waiting its turn" in dlg.activity_summary.text()
    assert dlg.sync_button.isEnabled() and not dlg.stop_button.isEnabled()


def test_progress_shows_the_step_the_bar_and_the_contact_it_is_on(ctx, qtbot):
    c, sched, _calls = ctx
    SNAPS_LIVE = [dict(s, live=True) if (s["account_id"], s["kind"]) == (2, "contacts") else s for s in SNAPS]
    c.service.activity_overview = lambda: SNAPS_LIVE
    dlg = open_dialog(qtbot, SyncDialog, c)
    qtbot.waitUntil(lambda: dlg.activity.rowCount() == 4, timeout=5000)
    sched.busy = True                    # a run is going, as it is whenever progress arrives
    sched.run_started.emit("sync-now")
    sched.progress.emit({"account": "MS Main", "account_id": 2, "phase": "reading", "done": 300, "total": 0})
    assert "Reading the account - 300 contacts so far" in _cell(dlg, "MS Main", "Contacts", 4)
    sched.progress.emit({"account": "MS Main", "account_id": 2, "phase": "writing", "done": 120, "total": 1436,
                         "current": "Waqas Khan", "failed": 2, "changed": 118,
                         "failures": [["Alaa Arawi", "HTTP 500"]]})
    row = _row(dlg, "MS Main", "Contacts")
    assert "Writing 120 of 1,436 contacts" in _cell(dlg, "MS Main", "Contacts", 4)
    bar = dlg.activity.cellWidget(row, 3)
    assert (bar.minimum(), bar.maximum(), bar.value()) == (0, 1000, int(120 / 1436 * 1000))
    assert "Waqas Khan" in _cell(dlg, "MS Main", "Contacts", 5)
    assert not dlg.sync_button.isEnabled() and dlg.stop_button.isEnabled()
    assert "Syncing now" in dlg.summary.text() and "close this window" in dlg.summary.text().lower()


def test_sync_now_starts_a_sync_and_stop_asks_the_scheduler_to_stop(ctx, qtbot):
    c, sched, _calls = ctx
    dlg = open_dialog(qtbot, SyncDialog, c)
    dlg.sync_button.click()
    assert sched.calls == [("sync", None, None)] and not dlg.sync_button.isEnabled() and dlg.stop_button.isEnabled()
    dlg.stop_button.click()
    assert sched.calls[-1] == "stop" and not dlg.stop_button.isEnabled()
    assert "Stopping after the contact it is on" in dlg.summary.text()
    out = SimpleNamespace(account_id=1, account_name="GMail Main", status="stopped", error=None,
                          plan=SimpleNamespace(add_in=0, create_out=0, merge_in=0, update_out=40, delete_out=0,
                                               deleted_in=0, failed_out=0, failures=[]))
    sched.busy = False
    sched.run_finished.emit([out])
    assert dlg.sync_button.isEnabled() and not dlg.stop_button.isEnabled()


def test_a_finished_run_lists_what_each_account_refused(ctx, qtbot):
    c, sched, _calls = ctx
    dlg = open_dialog(qtbot, SyncDialog, c)
    out = SimpleNamespace(account_id=2, account_name="MS Main", status="partial", error=None,
                          plan=SimpleNamespace(add_in=0, create_out=0, merge_in=0, update_out=9, delete_out=0,
                                               deleted_in=0, failed_out=2,
                                               failures=[["Bob Bell", "HTTP 500"], ["Cy Cox", "HTTP 500"]]))
    sched.run_finished.emit([out])
    names = {dlg.problems.topLevelItem(i).text(1) for i in range(dlg.problems.topLevelItemCount())}
    assert {"Bob Bell", "Cy Cox", "Alaa Arawi"} <= names
    assert "Could not be written" in dlg.problem_title.text()


def test_each_row_has_the_buttons_its_state_calls_for_and_they_do_their_thing(ctx, qtbot):
    c, sched, calls = ctx
    dlg = open_dialog(qtbot, SyncDialog, c)
    qtbot.waitUntil(lambda: dlg.activity.rowCount() == 4, timeout=5000)
    assert list(_buttons(dlg, "GMail Main", "Contacts")) == ["Sync now", "Settings"]
    _buttons(dlg, "GMail Main", "Mail")["Settings"].click()
    assert calls[-1] == ("open", 1, "settings")
    _buttons(dlg, "MS Main", "Mail")["Sync now"].click()
    assert sched.calls[-1] == ("sync", 2, "mail")                     # that account, that kind - not everything
    dlg.do(1, "mail", "pause")
    qtbot.waitUntil(lambda: ("pause", 1) in calls, timeout=3000)
    dlg.do(2, "contacts", "test")
    assert calls[-1] == ("open", 2, "test")


def test_a_running_mail_fetch_offers_stop_and_pause_and_a_paused_one_offers_resume(ctx, qtbot):
    c, sched, calls = ctx
    live_mail = snap(1, "GMail Main", "mail", live=True, mail={"fetched": 500, "total": 1000, "rate_per_s": 3,
                                                                 "eta_s": 100})
    paused = snap(2, "MS Main", "mail", paused_by_person=True)
    c.service.activity_overview = lambda: [snap(1, "GMail Main", "contacts"), live_mail,
                                           snap(2, "MS Main", "contacts"), paused]
    dlg = open_dialog(qtbot, SyncDialog, c)
    qtbot.waitUntil(lambda: dlg.activity.rowCount() == 4, timeout=5000)
    assert list(_buttons(dlg, "GMail Main", "Mail")) == ["Stop", "Pause", "Settings"]
    assert list(_buttons(dlg, "MS Main", "Mail")) == ["Resume", "Settings"]
    _buttons(dlg, "MS Main", "Mail")["Resume"].click()
    qtbot.waitUntil(lambda: ("resume", 2) in calls, timeout=3000)


def test_there_is_one_window_closing_it_does_not_stop_the_sync_and_every_control_is_named(ctx, qtbot):
    c, sched, _calls = ctx
    first = open_sync_window(None, c)
    qtbot.addWidget(first)
    assert sched.calls == [("sync", None, None)]                      # opening started one
    first.close()
    assert sched.calls == [("sync", None, None)]                      # closing did not stop it
    second = open_sync_window(None, c)
    assert second is first and sched.calls == [("sync", None, None)]  # reopened; one was going, so none started
    assert unnamed_controls(first) == []
