"""Order 1k 10h, 10m: the tray items and the toasts, and the runtime that wires them.

Layer: L5
"""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("keyring")

from tests.unit.accounts_ui_fakes import make_service, qt, settle, wait_until  # noqa: E402


class FakeTray:
    def __init__(self):
        self.messages = []

    def showMessage(self, title, text, icon, ms):
        self.messages.append((title, text))


def tray_with_menu():
    from PySide6.QtWidgets import QMenu
    from app.ui.tray import TrayPresence
    qt()
    tray = TrayPresence(NS())
    tray._tray = FakeTray()
    tray._menu = QMenu()
    tray._menu.addAction("Show Leasha")
    tray._menu.addAction("Quit")
    return tray


def test_the_tray_has_pause_and_sync_items_with_the_same_choices_as_the_page():
    calls = []
    tray = tray_with_menu()
    tray.set_account_actions(pause=lambda c: calls.append(("pause", c)), sync_now=lambda: calls.append("sync"),
                             open_page=lambda: calls.append("open"))
    top = [a.text() for a in tray._menu.actions() if not a.isSeparator()]
    assert "Sync accounts now" in top and top[-1] == "Quit"      # before Quit
    assert any(a.menu() is not None and a.menu().title().startswith("Pause account sync")
               for a in tray._menu.actions())
    sub = tray._account_pause_menu
    assert [a.text() for a in sub.actions() if a.isVisible()] == ["For 1 hour", "Until tomorrow",
                                                                  "Until I turn it back on"]
    for action in sub.actions():
        action.trigger()
    tray._account_sync.trigger()
    assert [c for c in calls if isinstance(c, tuple)] == [("pause", "hour"), ("pause", "tomorrow"),
                                                          ("pause", "forever"), ("pause", "off")]
    assert "sync" in calls


def test_the_tray_says_when_sync_is_paused_and_offers_to_turn_it_back_on():
    tray = tray_with_menu()
    tray.set_account_actions(pause=lambda c: None, sync_now=lambda: None, open_page=lambda: None)
    tray.set_accounts_paused("Paused until 14:05")
    assert tray._account_pause_menu.title() == "Pause account sync (Paused until 14:05)"
    assert tray._account_resume.isVisible()
    tray.set_accounts_paused(None)
    assert not tray._account_resume.isVisible()


def test_a_warning_line_shows_for_an_account_that_needs_attention():
    opened = []
    tray = tray_with_menu()
    tray.set_account_actions(pause=lambda c: None, sync_now=lambda: None, open_page=lambda: opened.append(1))
    tray.set_accounts_warning("Needs attention: Work")
    assert tray._account_warning_action.isVisible() and "Work" in tray._account_warning_action.text()
    tray._account_warning_action.trigger()
    assert opened == [1]
    tray.set_accounts_warning("")
    assert not tray._account_warning_action.isVisible()


def test_toast_goes_through_the_tray_and_is_false_without_one():
    tray = tray_with_menu()
    assert tray.toast("Sync held", "Home: ...") is True
    assert tray._tray.messages == [("Sync held", "Home: ...")]
    tray._tray = None
    assert tray.toast("x", "y") is False


def test_the_runtime_sends_toasts_honouring_the_switches_and_tells_each_once(tmp_path):
    from app.accounts.store import AccountSettings
    from app.ui.account_runtime import AccountRuntime
    qt()
    store, svc = make_service(tmp_path)
    try:
        aid = svc.accounts.add("google", "g@x.com", "Work")
        svc.accounts.update(aid, last_error_code="ERR_ACCOUNT_SIGNIN_EXPIRED", last_error="expired")
        tray = tray_with_menu()
        tray.set_account_actions(pause=lambda c: None, sync_now=lambda: None, open_page=lambda: None)
        shell = NS(_store=store, tray=tray, settings_view=NS(accounts_page=None))
        rt = AccountRuntime(shell)
        from datetime import datetime
        snap = (svc.settings(), svc.status(datetime.now()))
        rt._snapshot(snap, [])
        rt._snapshot(snap, [])
        assert len(tray._tray.messages) == 1 and "Work" in tray._tray.messages[0][1]
        assert tray._account_warning_action.isVisible()
        rt.told.clear()
        off = AccountSettings(notify_signin=False)
        rt._snapshot((off, snap[1]), [])
        assert len(tray._tray.messages) == 1                    # the switch is honoured
    finally:
        store.close()


def test_tray_pause_writes_the_same_setting_as_the_page(tmp_path):
    from app.ui.account_runtime import AccountRuntime
    qt()
    store, svc = make_service(tmp_path)
    try:
        tray = tray_with_menu()
        shell = NS(_store=store, tray=tray, settings_view=NS(accounts_page=None))
        rt = AccountRuntime(shell)
        rt.pause("forever")
        assert wait_until(lambda: svc.settings().paused_until == 9e15)
        rt.pause("off")
        assert wait_until(lambda: svc.settings().paused_until is None)
    finally:
        store.close()


def test_the_runtime_gives_the_page_the_one_scheduler_and_starts_no_sync(tmp_path):
    from app.accounts.store import ScheduleSetting
    from app.ui.account_runtime import AccountRuntime
    from app.ui.accounts_page import AccountsPage
    qt()
    store, svc = make_service(tmp_path)
    try:
        page = AccountsPage(lambda: svc)
        shell = NS(_store=store, tray=tray_with_menu(), settings_view=NS(accounts_page=page))
        svc.accounts.update_settings(contacts=ScheduleSetting("interval", 5))
        svc.accounts.add("carddav", "a@x.com", "Home")
        calls = []
        svc.run_due = lambda *a, **k: calls.append("run_due") or []
        svc.sync_now = lambda *a, **k: calls.append("sync_now") or []
        rt = AccountRuntime(shell)
        assert page._scheduler is rt.scheduler
        rt.start()
        wait_until(lambda: False, 0.3)
        rt.stop()
        assert calls == []                                       # starting the runtime syncs nothing
    finally:
        store.close()
