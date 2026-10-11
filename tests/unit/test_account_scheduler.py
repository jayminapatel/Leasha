"""Order 1k 10b, 10c, D11.2, D11.4, D16: the Qt clock that asks the schedule.

Layer: L5

It ONLY asks `AccountService.due_work` and starts `run_due` on a worker; one run at a time.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("keyring")

from tests.unit.accounts_ui_fakes import make_service, qt, wait_until  # noqa: E402


@pytest.fixture()
def svc(tmp_path):
    qt()
    store, service = make_service(tmp_path)
    service.accounts.update_settings(dry_run_first=False)
    yield service
    store.close()


def make(svc, **kw):
    from app.ui.account_scheduler import AccountScheduler
    return AccountScheduler(lambda: svc, **kw)


def test_start_alone_starts_nothing_and_no_tick_means_no_sync(svc, monkeypatch):
    calls = []
    for name in ("run_due", "sync_now", "due_work"):
        monkeypatch.setattr(svc, name, lambda *a, _n=name, **k: calls.append(_n) or [])
    s = make(svc, tick_ms=60_000)
    s.start()
    wait_until(lambda: False, 0.3)
    assert calls == []                                           # not at app start (D11.2)
    s.stop()


def test_a_manual_schedule_runs_nothing_even_with_accounts(svc):
    from app.accounts.store import ScheduleSetting
    # 2026-10-11: the default is hourly now; Manual is the person's choice, so it is chosen here.
    svc.accounts.update_settings(mail=ScheduleSetting("manual"), contacts=ScheduleSetting("manual"))
    svc.accounts.add("carddav", "a@x.com", "Home")
    s = make(svc)
    done = []
    s.run_finished.connect(done.append)
    assert s.tick() is True
    assert wait_until(lambda: not s.busy)
    assert done == []                                            # nothing due: no run, no signal


def test_a_due_schedule_runs_once_and_ticks_while_busy_are_folded(svc, monkeypatch):
    from app.accounts.service import DueWork
    from app.accounts.store import ScheduleSetting
    a = svc.accounts.get(svc.accounts.add("carddav", "a@x.com", "Home"))
    svc.accounts.update_settings(contacts=ScheduleSetting("interval", 5))
    gate, ran = threading.Event(), []
    monkeypatch.setattr(svc, "run_due", lambda now=None, **kw: (ran.append(kw), gate.wait(5), ["outcome"])[2])
    s = make(svc)
    finished = []
    s.run_finished.connect(finished.append)
    assert s.tick() is True
    assert wait_until(lambda: len(ran) == 1)
    assert s.tick() is False and s.busy                          # serial: one run at a time
    gate.set()
    assert wait_until(lambda: finished == [["outcome"]])
    assert len(ran) == 1
    assert isinstance(DueWork(a, "contacts", "schedule"), DueWork)


def test_the_window_opened_time_is_passed_so_the_catch_up_works(svc, monkeypatch):
    seen = {}
    monkeypatch.setattr(svc, "due_work", lambda now=None, window_opened_at=None: seen.update(w=window_opened_at) or [1])
    monkeypatch.setattr(svc, "run_due", lambda now=None, window_opened_at=None, **kw: [])
    opened = datetime.now() - timedelta(minutes=2)
    s = make(svc, window_opened_at=opened)
    s.tick()
    assert wait_until(lambda: not s.busy)
    assert seen["w"] == opened


def test_a_machine_that_slept_for_a_fortnight_syncs_once(svc):
    """Real service + schedule arithmetic through the scheduler's own worker body."""
    from app.accounts.store import ScheduleSetting
    from app.ui.account_scheduler import _scheduled
    aid = svc.accounts.add("carddav", "a@x.com", "Home")
    svc.accounts.update_settings(contacts=ScheduleSetting("interval", 60))
    with svc.store.write() as conn:
        conn.execute("INSERT INTO account_sync_state (account_id, kind, last_success_at, last_attempt_at) "
                     "VALUES (?,?,?,?)", (aid, "contacts", int((datetime.now() - timedelta(days=14)).timestamp()),
                                          int((datetime.now() - timedelta(days=14)).timestamp())))
    due = svc.due_work(datetime.now())
    assert [(w.account.id, w.kind) for w in due] == [(aid, "contacts")]    # once, not once per missed hour
    calls = []
    svc.run_due = lambda now=None, **kw: calls.append(1) or []
    _scheduled(lambda: svc, datetime.now())
    assert calls == [1]


def test_sync_now_for_one_account_and_one_kind_reaches_the_service(svc, monkeypatch):
    seen = []
    monkeypatch.setattr(svc, "sync_now", lambda aid=None, kind=None, **kw: seen.append((aid, kind, kw)) or [])
    s = make(svc)
    assert s.sync_now(7, "mail") is True
    assert wait_until(lambda: not s.busy)
    assert s.sync_now() is True
    assert wait_until(lambda: not s.busy)
    assert seen == [(7, "mail", {"allow_brake": False}), (None, None, {"allow_brake": False})]


def test_two_sync_nows_fold_into_one_run(svc, monkeypatch):
    gate, seen = threading.Event(), []
    monkeypatch.setattr(svc, "sync_now", lambda *a, **k: (seen.append(1), gate.wait(5), [])[2])
    s = make(svc)
    folded = []
    s.folded.connect(lambda: folded.append(1))
    assert s.sync_now() is True and s.sync_now() is False
    assert folded == [1]
    gate.set()
    assert wait_until(lambda: not s.busy)
    assert seen == [1]


def test_a_failing_run_reports_and_frees_the_gate(svc, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(svc, "sync_now", boom)
    s = make(svc)
    errors = []
    s.run_failed.connect(errors.append)
    s.sync_now()
    assert wait_until(lambda: errors and not s.busy)
    assert s.sync_now() is True                                  # not stuck busy
    wait_until(lambda: not s.busy)


def test_the_tick_is_coarse():
    from app.ui.account_scheduler import TICK_MS
    assert 15_000 <= TICK_MS <= 60_000
