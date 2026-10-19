"""Review 2026-10-14: the sync status where people are - CS-1/CS-2 on both hosts and the sync log, CS-8 the chip and
the tray line, CS-9 the link state on each "Kept in" account, IA-5 the Sync window from Mail and Settings.

Layer: L5 (offscreen Qt), with fake schedulers standing in for the window's.
"""

from __future__ import annotations

import os
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("keyring")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal                                   # noqa: E402
from PySide6.QtWidgets import QLabel, QWidget                                # noqa: E402

from app.ui.presenter.sync_chip import SyncTracker, mail_words              # noqa: E402
from tests.unit.accounts_ui_fakes import make_service, qt, settle, wait_until   # noqa: E402

HELD_PLAN = {"held_deletes": 3, "names": {"held_deletes": ["Ann Lee", "Bob Ray", "Cy Dee"]}}


class FakeScheduler(QObject):
    progress = Signal(object)
    run_started = Signal(str)
    run_finished = Signal(object)
    run_failed = Signal(object)
    folded = Signal()

    def __init__(self):
        super().__init__()
        self.busy = False
        self.calls = []

    def sync_now(self, account_id=None, kind=None, **kw):
        self.calls.append((account_id, kind, kw))
        return True

    def stop_run(self):
        self.calls.append("stop")


@pytest.fixture
def world(tmp_path):
    qt()
    store, svc = make_service(tmp_path)
    svc.accounts.update_settings(dry_run_first=False)
    pages = []
    yield store, svc, pages
    for p in pages:
        p.deleteLater()
    store.close()


def _page(svc, pages):
    from app.ui.accounts_page import AccountsPage
    p = AccountsPage(lambda: svc)
    p.ensure_built()
    p.refresh()
    settle(p)
    pages.append(p)
    return p


# ------------------------------------------------------------------- CS-1 / CS-2: the panel's Allow and Fix
@pytest.mark.slow
def test_the_panels_allow_asks_naming_the_held_contacts_and_then_syncs_with_the_brake_off(world, monkeypatch):
    from app.ui.accounts import parts
    store, svc, pages = world
    aid = svc.accounts.add("carddav", "a@x.com", "Home")
    p = _page(svc, pages)
    p.account_list.select(aid)
    wait_until(lambda: p.panel._id == aid, 2)
    asked, synced = [], []
    monkeypatch.setattr(p, "request_sync", lambda *a, **k: synced.append((a, k)) or True)
    p.panel._snaps = [{"account_id": aid, "kind": "contacts", "name": "Home",
                       "run": {"status": "held", "plan": HELD_PLAN, "error_code": "ERR_SYNC_DELETE_BRAKE"}}]
    monkeypatch.setattr(parts, "ask_allow", lambda parent, q: asked.append(q) or False)
    p.panel._row_action(aid, "contacts", "allow")
    assert asked and asked[0].verb == "Allow and delete 3" and "Ann Lee, Bob Ray, Cy Dee" in asked[0].informative
    assert synced == []                                               # Cancel: nothing ran
    monkeypatch.setattr(parts, "ask_allow", lambda parent, q: True)
    p.panel._row_action(aid, "contacts", "allow")
    assert synced == [((aid, "contacts"), {"allow_brake": True})]


@pytest.mark.slow
def test_the_panels_fix_these_resets_the_check_and_syncs(world, monkeypatch):
    store, svc, pages = world
    aid = svc.accounts.add("carddav", "a@x.com", "Home")
    fixed, synced = [], []
    monkeypatch.setattr(svc, "fix_check", lambda i: fixed.append(i))
    p = _page(svc, pages)
    p.account_list.select(aid)
    wait_until(lambda: p.panel._id == aid, 2)
    monkeypatch.setattr(p, "request_sync", lambda *a, **k: synced.append(a) or True)
    p.panel._row_action(aid, "contacts", "fix")
    assert wait_until(lambda: synced == [(aid, "contacts")])
    assert fixed == [aid]


@pytest.mark.slow
def test_the_panel_passes_find_duplicates_on(world):
    store, svc, pages = world
    aid = svc.accounts.add("carddav", "a@x.com", "Home")
    p = _page(svc, pages)
    seen = []
    p.contacts_tool_requested.connect(seen.append)
    p.panel._row_action(aid, "contacts", "duplicates")
    assert seen == ["duplicates"]


@pytest.mark.slow
def test_settings_accounts_has_open_sync_status(world):
    store, svc, pages = world
    p = _page(svc, pages)
    seen = []
    p.sync_window_requested.connect(lambda: seen.append(1))
    assert p.sync_window_button.text() == "Open sync status" and p.sync_window_button.toolTip()
    p.sync_window_button.click()
    assert seen == [1]


@pytest.mark.slow
def test_the_sync_logs_allow_asks_first(world, monkeypatch):
    from app.ui.accounts import parts
    store, svc, pages = world
    aid = svc.accounts.add("carddav", "a@x.com", "Home")
    run = svc.accounts.start_run(svc.accounts.get(aid), "contacts", "sync-now")
    svc.accounts.finish_run(run, "held", held=3, error_code="ERR_SYNC_DELETE_BRAKE", plan=HELD_PLAN)
    p = _page(svc, pages)
    log = p.log
    log.tree.setCurrentItem(log.tree.topLevelItem(0))
    log.tree.topLevelItem(0).setSelected(True)
    emitted, asked = [], []
    log.allow_requested.connect(emitted.append)
    monkeypatch.setattr(parts, "ask_allow", lambda parent, q: asked.append(q.verb) or False)
    log.allow.click()
    assert asked == ["Allow and delete 3"] and emitted == []
    monkeypatch.setattr(parts, "ask_allow", lambda parent, q: True)
    log.allow.click()
    assert emitted == [aid]


def test_the_sync_windows_allow_asks_first(qtbot, monkeypatch):
    from app.ui.accounts import parts
    from app.ui.contacts_tools.sync_dialog import SyncDialog
    sched = FakeScheduler()
    service = NS(scheduler=lambda: sched, sync_overview=lambda: [], activity_overview=lambda: [])
    dlg = SyncDialog(NS(service=service, refresh=lambda: None))
    qtbot.addWidget(dlg)
    dlg._snaps = [{"account_id": 5, "kind": "contacts", "name": "Work",
                   "run": {"status": "held", "error_code": "ERR_SYNC_CHANGES_HELD",
                           "error_text": "Work returned 60 of its 80 contacts changed in the same way (phone)"}}]
    asked = []
    monkeypatch.setattr(parts, "ask_allow", lambda parent, q: asked.append(q) or False)
    dlg.do(5, "contacts", "allow")
    assert asked[0].verb == "Allow and sync" and "(phone)" in asked[0].informative
    assert sched.calls == []
    monkeypatch.setattr(parts, "ask_allow", lambda parent, q: True)
    dlg.do(5, "contacts", "allow")
    assert sched.calls == [(5, "contacts", {"allow_brake": True})]
    opened = []
    service.open_contacts_tool = opened.append
    dlg.do(5, "contacts", "duplicates")
    dlg.do(5, "contacts", "retry_refused")
    assert opened == ["duplicates"] and sched.calls[-1] == (5, "contacts", {})


def test_one_sync_window_for_every_page_of_a_window(qtbot):
    from app.ui.contacts_tools.sync_dialog import open_sync_window
    sched = FakeScheduler()
    c = NS(service=NS(scheduler=lambda: sched, sync_overview=lambda: [], activity_overview=lambda: []))
    top = QWidget()
    qtbot.addWidget(top)
    a, b = QWidget(top), QWidget(top)
    first = open_sync_window(a, c, start=False)
    second = open_sync_window(b, c, start=False)
    assert first is second and top._sync_window is first
    assert sched.calls == []                                           # start=False: it only looks
    first.close()


# ----------------------------------------------------------------------------------------- CS-8: the chip
def test_the_tracker_words_a_run_a_hold_and_idle():
    t = SyncTracker()
    assert t.words().text == "" and t.tray_line() == ""
    t.started("sync-now")
    t.progress({"account_id": 1, "phase": "writing", "done": 100, "total": 1000})
    t.progress({"account_id": 2, "phase": "writing", "done": 20, "total": 436})
    assert t.words().text == "Syncing 2 accounts - writing 120 of 1,436"
    assert t.tray_line() == "Accounts: syncing 2 accounts - writing 120 of 1,436"
    t.finished([NS(status="held", account_name="Work"), NS(status="ok", account_name="Home")])
    w = t.words()
    assert w.text == "Held - look" and "Work" in w.tip and w.tone == "warning"
    t.started()
    t.finished([])
    assert t.words().text == ""


def test_the_contacts_chip_hides_when_idle_and_opens_the_sync_window(qtbot):
    from app.ui.widgets.sync_chip import SyncChip
    opened = []
    top = QWidget()
    top.account_runtime = NS(open_sync=lambda parent: opened.append(parent), scheduler=None, factory=None)
    chip = SyncChip("contacts", top)
    qtbot.addWidget(top)
    top.show()
    sched = FakeScheduler()
    chip.attach(sched)
    assert chip.isHidden()
    sched.run_started.emit("sync-now")
    sched.progress.emit({"account_id": 3, "phase": "writing", "done": 120, "total": 1436})
    assert not chip.isHidden() and chip.text() == "Syncing 1 account - writing 120 of 1,436" and chip.toolTip()
    sched.run_finished.emit([NS(status="held", account_name="Work")])
    assert chip.text() == "Held - look"
    chip.click()
    assert opened == [top]


def _row(status, *, kind="contacts", code="", text="", name="Work", enabled=True):
    return {"account_id": 1, "name": name, "kind": kind, "enabled": enabled,
            "run": {"status": status, "error_code": code, "error_text": text}}


def test_the_tracker_is_seeded_from_holds_and_failures_before_start_up():
    t = SyncTracker()
    assert t.seed([_row("held", code="ERR_SYNC_DELETE_BRAKE"), _row("ok", name="Home")])
    w = t.words()
    assert w.text == "Held - look" and "Work" in w.tip and w.tone == "warning"
    clash = SyncTracker()
    clash.seed([_row("partial", code="ERR_SYNC_CONFLICT_HELD", name="Home")])
    assert clash.words().text == "Held - look" and "Home" in clash.words().tip
    failed = SyncTracker()
    failed.seed([_row("failed", text="The server said no."), _row("held", kind="mail")])
    assert failed.words().text == "Sync failed - look" and "The server said no." in failed.words().tip
    quiet = SyncTracker()
    quiet.seed([_row("held", enabled=False), _row("ok"), {"kind": "contacts", "enabled": True, "run": None}])
    assert quiet.words().text == ""


def test_a_seed_never_overrides_what_this_sessions_signals_said():
    t = SyncTracker()
    t.finished([NS(status="ok", account_name="Work")])
    assert not t.seed([_row("held")]) and t.words().text == ""
    once = SyncTracker()
    once.seed([])
    assert not once.seed([_row("held")])                               # read once
    held = SyncTracker()
    held.seed([_row("held")])
    held.started("sync-now")                                           # Allow starts a sync: the hold clears
    held.finished([NS(status="ok", account_name="Work")])
    assert held.words().text == ""


def test_a_hold_from_before_start_up_shows_on_the_chip_read_on_a_worker(world):
    import threading
    from app.ui.widgets.sync_chip import SyncChip
    store, svc, pages = world
    aid = svc.accounts.add("carddav", "a@x.com", "Work")
    run = svc.accounts.start_run(svc.accounts.get(aid), "contacts", "schedule")
    svc.accounts.finish_run(run, "held", held=3, error_code="ERR_SYNC_DELETE_BRAKE", plan=HELD_PLAN)
    read_on = []

    def factory():
        read_on.append(threading.current_thread())
        return svc

    top = QWidget()
    top.account_runtime = NS(open_sync=lambda parent: None, scheduler=None, factory=None)
    chip = SyncChip("contacts", top)
    top.show()
    sched = FakeScheduler()
    try:
        chip.attach(sched, factory)
        assert wait_until(lambda: chip.text() == "Held - look")
        assert not chip.isHidden() and "Work" in chip.toolTip()
        assert read_on and threading.main_thread() not in read_on      # on a worker, never the window's thread
        sched.run_started.emit("sync-now")
        sched.run_finished.emit([NS(status="ok", account_name="Work")])  # a later run that went through
        assert chip.isHidden() and chip.text() == "Sync status"
        wait_until(lambda: False, 0.2)
        assert len(read_on) == 1                                         # once, not again after the run
    finally:
        top.deleteLater()


def test_the_runtime_reads_the_last_runs_once_for_the_tray_and_every_chip(world):
    import threading
    from PySide6.QtWidgets import QMenu
    from app.ui.account_runtime import AccountRuntime
    from app.ui.tray import TrayPresence
    from app.ui.widgets.sync_chip import SyncChip

    class Shell(QWidget):
        pass

    store, svc, pages = world
    aid = svc.accounts.add("carddav", "a@x.com", "Work")
    run = svc.accounts.start_run(svc.accounts.get(aid), "contacts", "schedule")
    svc.accounts.finish_run(run, "held", held=3, error_code="ERR_SYNC_DELETE_BRAKE", plan=HELD_PLAN)
    tray = TrayPresence(NS())
    tray._tray = NS(setToolTip=lambda t: None, showMessage=lambda *a: None)
    tray._menu = QMenu()
    tray._menu.addAction("Quit")
    shell = Shell()
    shell._store, shell.tray, shell.settings_view = store, tray, NS(accounts_page=None)
    chip = SyncChip("contacts", shell)
    read_on = []
    try:
        rt = AccountRuntime(shell)
        assert chip.scheduler is rt.scheduler and chip._factory is None     # the runtime's read seeds it
        rt.factory = lambda: read_on.append(threading.current_thread()) or svc
        rt._read_last_runs()
        assert wait_until(lambda: tray._account_activity_action.text() == "Accounts: held - look")
        assert chip.text() == "Held - look"
        assert len(read_on) == 1 and threading.main_thread() not in read_on   # one read, on a worker
        shell.account_runtime = rt
        late = SyncChip("contacts", shell)                                  # a chip built after the read
        late.try_attach()
        assert late.text() == "Held - look" and len(read_on) == 1          # the same rows, not a second read
        rt.scheduler.run_started.emit("sync-now")
        rt.scheduler.run_finished.emit([NS(status="ok", account_name="Work", error=None)])
        assert not tray._account_activity_action.isVisible() and chip.isHidden()
    finally:
        shell.deleteLater()


def test_a_clash_in_this_session_is_held_until_a_run_without_it():
    t = SyncTracker()
    t.started("schedule")
    t.finished([NS(status="partial", account_name="Home", error=NS(code="ERR_SYNC_CONFLICT_HELD"))])
    w = t.words()
    assert w.text == "Held - look" and "Home" in w.tip
    t.started("schedule")
    t.finished([NS(status="ok", account_name="Home", error=None)])
    assert t.words().text == ""
    t.finished([NS(status="ok", account_name="Home", error=NS(code="ERR_SYNC_CONFLICT_HELD"))])
    assert t.words().text == "Held - look"
    t.finished([NS(status="partial", account_name="Home", error=NS(code="ERR_ACCOUNT_TROUBLE"))])
    assert t.words().text == ""


def test_the_mail_chip_takes_a_hold_from_its_first_read():
    from app.ui.widgets.sync_chip import SyncChip
    qt()
    top = QWidget()
    chip = SyncChip("mail", top)
    try:
        chip.set_snapshots([_row("held", code="ERR_SYNC_CHANGES_HELD"), _row("ok", kind="mail")])
        assert chip.text() == "Held - look"
    finally:
        top.deleteLater()


def test_the_mail_chip_says_what_mail_is_doing():
    from app.ui.presenter.account_activity import Activity
    def act(state, frac=None):
        return Activity(1, "Gmail", "mail", state, "Fetching mail: 15 of about 100 (15%)", "", frac, False)
    assert mail_words([act("running", 0.15)]).text == "Mail: fetching 15%"
    assert mail_words([act("idle")]).text == "Mail: up to date"
    assert mail_words([act("failed")]).text == "Mail: needs you"
    assert mail_words([]).text == "Sync status"


def test_the_tray_shows_the_sync_line_and_its_click_opens_the_sync_window():
    from PySide6.QtWidgets import QMenu
    from app.ui.tray import TrayPresence
    qt()
    tray = TrayPresence(NS())
    tips = []
    tray._tray = NS(setToolTip=tips.append, showMessage=lambda *a: None)
    tray._menu = QMenu()
    tray._menu.addAction("Quit")
    opened = []
    tray.set_account_actions(pause=lambda c: None, sync_now=lambda: None, open_page=lambda: None,
                             open_sync=lambda: opened.append(1))
    tray.set_status("Up to date")
    assert tips[-1] == "Leasha — Up to date"                           # unchanged while idle
    tray.set_accounts_activity("Accounts: syncing 1 account")
    assert tray._account_activity_action.isVisible()
    assert tips[-1] == "Leasha — Up to date\nAccounts: syncing 1 account"
    tray._account_activity_action.trigger()
    assert opened == [1]
    tray.set_accounts_activity("")
    assert not tray._account_activity_action.isVisible() and tips[-1] == "Leasha — Up to date"


def test_the_runtime_keeps_the_tray_line_from_the_schedulers_signals(world):
    from PySide6.QtWidgets import QMenu
    from app.ui.account_runtime import AccountRuntime
    from app.ui.tray import TrayPresence
    store, svc, pages = world
    tray = TrayPresence(NS())
    tray._tray = NS(setToolTip=lambda t: None, showMessage=lambda *a: None)
    tray._menu = QMenu()
    tray._menu.addAction("Quit")
    rt = AccountRuntime(NS(_store=store, tray=tray, settings_view=NS(accounts_page=None)))
    rt.scheduler.run_started.emit("sync-now")
    rt.scheduler.progress.emit({"account_id": 1, "phase": "reading"})
    assert tray._account_activity_action.text() == "Accounts: syncing 1 account - reading the accounts"
    rt.scheduler.run_finished.emit([])
    assert not tray._account_activity_action.isVisible()


# ------------------------------------------------------------------------------- CS-9: the link state marks
def test_the_hub_keeps_each_links_state_apart(tmp_path):
    from tests.unit.test_contacts_view_world import World, card
    qt()
    w = World(tmp_path)
    try:
        cid = w.add(card("Ann", "Lee"), kept=[w.a_home, w.a_work])
        w.hub.set_link(cid, w.a_home, remote_id="r1", state="synced")
        assert w.hub.kept_in_states(cid) == {w.a_home: "synced", w.a_work: "pending_create"}
        assert w.hub.kept_in(cid) == [w.a_home, w.a_work]                # its meaning is unchanged
    finally:
        w.close()


@pytest.mark.slow
def test_the_reader_and_editor_mark_each_account(tmp_path):
    from tests.unit.test_contacts_view_world import World, card, settle
    qt()
    w = World(tmp_path)
    try:
        cid = w.add(card("Ann", "Lee"), kept=[w.a_home, w.a_work, w.a_fast])
        w.hub.set_link(cid, w.a_home, remote_id="r1", state="synced")
        w.hub.set_link(cid, w.a_fast, remote_id="r3", state="deleted_remote")
        w.accounts.record_failures(w.a_work, [{"contact_id": cid, "name": "Ann Lee", "reason": "Phone too long"}],
                                   set())
        page = w.page()
        page.select_ids([cid])
        settle(page)
        tips = {lbl.toolTip() for lbl in page.reader.findChildren(QLabel) if lbl.toolTip()}
        assert any(t.startswith("Synced: Home Google") for t in tips)
        assert any("Work refused this contact: Phone too long" in t for t in tips)
        assert any(t.startswith("Deleted at Fastmail") for t in tips)
        assert "waiting to be written" not in page.reader.text()
        marks = page.editor.kept_marks
        assert marks[w.a_work].toolTip().startswith("Work refused this contact")
        assert not marks[w.a_work].isHidden()
    finally:
        w.close()
