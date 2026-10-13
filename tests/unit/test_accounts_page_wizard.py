"""Order 1k 1c, 1e, D3: the Add account wizard and the Sync log, on a real service and a fake server.

Layer: L5
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("keyring")

from tests.unit.accounts_ui_fakes import make_service, qt, settle, wait_until  # noqa: E402


@pytest.fixture()
def world(tmp_path):
    qt()
    store, svc = make_service(tmp_path)
    svc.accounts.update_settings(dry_run_first=False)
    from app.accounts.service import ConnectionReport
    svc.test_connection = lambda acct: ConnectionReport(True, "Connected.")
    from app.ui.accounts_page import AccountsPage
    page = AccountsPage(lambda: svc)
    page.ensure_built()
    page.refresh()
    settle(page)
    yield svc, page
    page.close()
    store.close()


def test_wizard_happy_path_with_an_app_password(world):
    svc, page = world
    dlg = page.open_wizard()
    dlg.choices["apple"].setChecked(True)
    dlg.next.click()
    assert dlg.stack.currentIndex() == 1
    assert "App-Specific Passwords" in dlg.help.text()           # the provider's own help (IMAP_PRESETS)
    dlg.name.setText("iCloud")
    dlg.address.setText("me@icloud.com")
    dlg.password.setText("abcd-efgh-ijkl-mnop")
    dlg.add.click()
    assert wait_until(lambda: dlg.account is not None)
    acct = svc.accounts.get(dlg.account.id)
    assert (acct.provider, acct.name, acct.address) == ("apple", "iCloud", "me@icloud.com")
    assert acct.server == "imap.mail.me.com" and acct.options.get("signed_in_at")
    settle(page)
    assert page.account_list.topLevelItemCount() == 1
    assert page.account_list.currentItem().text(0) == "iCloud"
    assert "dry run" in page.notice.text()
    # the password is in the keyring only
    from app.accounts.secrets import account_key
    assert svc.secrets.get(account_key(acct.id, "app_password")) == "abcd-efgh-ijkl-mnop"


def test_wizard_needs_the_address_and_password_first(world):
    svc, page = world
    dlg = page.open_wizard()
    dlg.choices["imap"].setChecked(True)
    dlg.next.click()
    dlg.add.click()
    assert "app password" in dlg.notice.text() and svc.accounts.list() == []


def test_a_failed_test_connection_shows_the_error_with_its_fix_and_saves_nothing(world):
    from app.accounts.service import ConnectionReport
    svc, page = world
    svc.test_connection = lambda acct: ConnectionReport(False, "the server said no")
    dlg = page.open_wizard()
    dlg.choices["carddav"].setChecked(True)
    dlg.next.click()
    dlg.address.setText("me@x.com")
    dlg.password.setText("pw-pw-pw-pw")
    dlg.server.setText("dav.example.com")
    dlg.add.click()
    assert wait_until(lambda: dlg.add.isEnabled() and "refused the request" in dlg.notice.text())
    assert "Test connection" in dlg.notice.text()                 # the fix (rule 2)
    assert svc.accounts.list() == [] and dlg.account is None


def test_microsoft_admin_consent_error_is_shown_plainly(world):
    from app.core.errors import AppErrorException, make_error
    svc, page = world

    def refuse(provider, name="", **kw):
        kw["on_url"]("https://login.example/auth")
        raise AppErrorException(make_error("ERR_ACCOUNT_ADMIN_CONSENT", "accounts.oauth", account="Work"))
    svc.add_oauth_account = refuse
    dlg = page.open_wizard()
    dlg.choices["microsoft"].setChecked(True)
    dlg.next.click()
    assert dlg.add.text() == "Sign in and add"
    dlg.name.setText("Work")
    dlg.add.click()
    assert wait_until(lambda: "approved Leasha" in dlg.notice.text())
    assert "Ask your IT administrator" in dlg.notice.text() and "Mail.Read" in dlg.notice.text()
    assert dlg.add.isEnabled() and svc.accounts.list() == []


def test_google_oauth_path_shows_the_sign_in_address_and_adds(world):
    svc, page = world

    def sign_in(provider, name="", *, mail, contacts, login_hint, on_url, **kw):
        on_url("https://accounts.example/auth?x=1")
        aid = svc.accounts.add(provider, "g@gmail.com", name or "g@gmail.com", mail_on=mail, contacts_on=contacts)
        return svc.accounts.get(aid)
    svc.add_oauth_account = sign_in
    dlg = page.open_wizard()
    assert dlg.choices["google"].isChecked()
    dlg.next.click()
    dlg.mail.setChecked(True)
    dlg.add.click()
    assert wait_until(lambda: dlg.account is not None)
    assert svc.accounts.get(dlg.account.id).mail_on is True


def test_gmail_over_imap_choice_says_app_passwords_are_personal_only_and_saves_an_imap_account(world):
    svc, page = world
    dlg = page.open_wizard()
    choice = dlg.choices["google-imap"]
    assert choice.text() == "Read Gmail over IMAP instead"
    choice.setChecked(True)
    dlg.next.click()
    assert "personal Google accounts" in dlg.help.text() and "Work and school" in dlg.help.text()
    dlg.address.setText("me@gmail.com")
    dlg.password.setText("abcdabcdabcdabcd")
    dlg.add.click()
    assert wait_until(lambda: dlg.account is not None)
    a = svc.accounts.get(dlg.account.id)
    assert (a.provider, a.server, a.port, a.security) == ("imap", "imap.gmail.com", 993, "ssl")
    assert a.mail_on and not a.contacts_on


def test_every_provider_choice_has_its_own_help_and_a_guide_section(world):
    from app.ui.accounts import logic
    for key, (title, glyph, line, help_) in logic.PROVIDER_TEXT.items():
        assert title and line
        from app.ui.widgets.icons import ICON_NAMES
        assert glyph in ICON_NAMES                              # a glyph, never a provider logo
    assert set(logic.GUIDE_ANCHORS.values()) == {"accounts-google-setup", "accounts-microsoft-setup",
                                                 "accounts-icloud-password", "accounts-imap"}


# ------------------------------------------------------------------------------ sync log
def _run(svc, acct, status, *, added=0, changed=0, deleted=0, held=0, dry=False, code=None, text=None, plan=None,
         trigger="sync-now", kind="contacts"):
    rid = svc.accounts.start_run(acct, kind, trigger, dry_run=dry)
    svc.accounts.finish_run(rid, status, added=added, changed=changed, deleted=deleted, held=held,
                            error_code=code, error_text=text, plan=plan)
    return rid


@pytest.mark.slow
def test_the_log_lists_runs_filters_and_shows_the_fix(world):
    svc, page = world
    a = svc.accounts.get(svc.accounts.add("carddav", "a@x.com", "Home"))
    b = svc.accounts.get(svc.accounts.add("carddav", "b@x.com", "Work"))
    _run(svc, a, "ok", added=3, changed=1)
    _run(svc, b, "failed", code="ERR_ACCOUNT_UNREACHABLE",
         text="Work could not be reached (timeout). FIX: Check the internet connection.")
    _run(svc, b, "ok", kind="mail", trigger="schedule")
    page.refresh()
    settle(page)
    log = page.log
    assert log.tree.topLevelItemCount() == 3
    problems = [log.tree.topLevelItem(i).text(10) for i in range(3)]
    assert any("FIX: Check the internet connection." in t for t in problems)
    log.account_filter.setCurrentIndex(log.account_filter.findData(a.id))
    assert log.tree.topLevelItemCount() == 1 and log.tree.topLevelItem(0).text(4) == "3"
    log.account_filter.setCurrentIndex(0)
    log.kind_filter.setCurrentIndex(log.kind_filter.findData("mail"))
    assert log.tree.topLevelItemCount() == 1 and log.tree.topLevelItem(0).text(3) == "Schedule"


def test_undo_this_sync_confirms_then_calls_the_service_on_a_worker(world, monkeypatch):
    from app.ui.accounts import parts
    svc, page = world
    a = svc.accounts.get(svc.accounts.add("carddav", "a@x.com", "Home"))
    rid = _run(svc, a, "ok", added=3)
    undone = []
    monkeypatch.setattr(svc, "undo_run", lambda r: undone.append(r))
    page.refresh()
    settle(page)
    page.log.tree.setCurrentItem(page.log.tree.topLevelItem(0))
    assert page.log.undo.isEnabled()
    monkeypatch.setattr(parts, "confirm", lambda *a, **k: False)
    page.log.undo.click()
    settle(page)
    assert undone == []
    asked = []
    monkeypatch.setattr(parts, "confirm", lambda p, t, text, ok, **k: asked.append(text) or True)
    page.log.undo.click()
    assert wait_until(lambda: undone == [rid])
    assert "3 added" in asked[0]


def test_export_log_writes_a_csv(world, monkeypatch, tmp_path):
    from app.ui.accounts import sync_log
    svc, page = world
    a = svc.accounts.get(svc.accounts.add("carddav", "a@x.com", "Home"))
    _run(svc, a, "ok", added=3)
    page.refresh()
    settle(page)
    out = tmp_path / "log.csv"
    monkeypatch.setattr(sync_log, "ask_save_path", lambda parent: str(out))
    page.log.export.click()
    assert wait_until(out.exists) and wait_until(lambda: "Saved" in page.log.notice.text())
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("Time,Account,Kind,Started by,Added,Changed,Deleted,Held,Took,Result")
    assert "Home" in lines[1] and ",3," in lines[1]


def test_a_dry_run_shows_its_plan_and_approve_runs_the_first_sync_through_the_scheduler(world, monkeypatch):
    svc, page = world
    a = svc.accounts.get(svc.accounts.add("carddav", "a@x.com", "Home"))
    _run(svc, a, "needs-approval", dry=True, plan={"add_in": 12, "merge_in": 3, "create_out": 4,
                                                   "names": {"add_in": ["Ada", "Grace"]}},
         code="ERR_SYNC_NEEDS_APPROVAL", text="The first sync is a dry run. FIX: Press Approve.")
    calls = []
    monkeypatch.setattr(svc, "approve_first_sync", lambda aid, **kw: calls.append((aid, kw)) or [])
    page.refresh()
    settle(page)
    page.log.tree.setCurrentItem(page.log.tree.topLevelItem(0))
    assert "add 12 to Leasha" in page.log.plan.text() and "Ada" in page.log.plan.text()
    assert not page.log.approve.isHidden()
    page.log.approve.click()
    # the scheduler also hands the run its progress and stop hooks; only what the person chose matters here
    assert wait_until(lambda: [(c[0], c[1].get("allow_brake")) for c in calls] == [(a.id, False)])


def test_a_held_run_offers_allow_which_syncs_with_allow_brake(world, monkeypatch):
    svc, page = world
    a = svc.accounts.get(svc.accounts.add("carddav", "a@x.com", "Home"))
    _run(svc, a, "held", held=30, plan={"delete_out": 30}, code="ERR_SYNC_DELETE_BRAKE",
         text="This sync would delete 30 contacts from Home.")
    calls = []
    monkeypatch.setattr(svc, "sync_now", lambda aid=None, kind=None, **kw: calls.append((aid, kind, kw)) or [])
    page.refresh()
    settle(page)
    page.log.tree.setCurrentItem(page.log.tree.topLevelItem(0))
    assert not page.log.allow.isHidden() and "delete 30 from the account" in page.log.plan.text()
    assert "FIX" in page.log.tree.topLevelItem(0).text(10)       # the fix is added for a held run
    page.log.allow.click()
    assert wait_until(lambda: [(c[0], c[1], c[2].get("allow_brake")) for c in calls] == [(a.id, "contacts", True)])


def test_two_sync_nows_fold_through_the_ui(world, monkeypatch):
    import threading
    svc, page = world
    svc.accounts.add("carddav", "a@x.com", "Home")
    gate, calls = threading.Event(), []

    def slow(aid=None, kind=None, **kw):
        calls.append(aid)
        gate.wait(5)
        return []
    monkeypatch.setattr(svc, "sync_now", slow)
    assert page.request_sync(0, "") is True
    assert page.request_sync(0, "") is False                      # folded, not queued
    assert "folded" in page.notice.text()
    gate.set()
    assert wait_until(lambda: not page._scheduler.busy)
    assert calls == [None]
    assert page.request_sync(0, "") is True                       # free again
    assert wait_until(lambda: not page._scheduler.busy)
