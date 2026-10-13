"""Order 1k 6l, section 10: Settings > Accounts, the page.

Layer: L5

Offscreen Qt over a real `AccountService` and a temp index. **Rule 11 (10u.7)**: every
control saves and reloads - each test changes a control, waits for the worker, builds a
fresh page on the same store and reads the control back.
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
    pages = []

    def page():
        from app.ui.accounts_page import AccountsPage
        p = AccountsPage(lambda: svc)
        p.ensure_built()
        p.refresh()
        settle(p)
        pages.append(p)
        return p

    yield svc, page
    for p in pages:
        p.close()
        p.deleteLater()
    store.close()


def reload(world_page):
    return world_page()


@pytest.mark.slow
def test_the_page_starts_empty_and_builds_on_first_use(tmp_path):
    qt()
    store, svc = make_service(tmp_path)
    from app.ui.accounts_page import AccountsPage
    p = AccountsPage(lambda: svc)
    assert not p._built and p.layout().count() == 0
    p.ensure_built()
    assert p._built and p.layout().count() > 3
    store.close()


# ------------------------------------------------------------------ rule 11: save and reload
def _saved(world, change, read):
    svc, page = world
    p = page()
    change(p)
    settle(p)
    q = page()
    return read(q), svc.settings()


def test_schedules_and_same_for_all_save_and_reload(world):
    from app.accounts.store import ScheduleSetting
    svc, page = world
    p = page()
    p.globals.mail_schedule.mode.setCurrentIndex(1)          # Every ...
    assert p.globals.mail_schedule.every.currentData() == 60  # D11.3: starts at 1 hour
    p.globals.mail_schedule.every.setCurrentIndex(p.globals.mail_schedule.every.findData(15))
    p.globals.contacts_schedule.mode.setCurrentIndex(2)      # Daily
    from PySide6.QtCore import QTime
    p.globals.contacts_schedule.daily_at.setTime(QTime(3, 30))
    p.globals.same_for_all.setCurrentIndex(1)
    p.globals.flush()
    settle(p)
    s = svc.settings()
    assert (s.mail.mode, s.mail.minutes) == ("interval", 15)
    assert (s.contacts.mode, s.contacts.daily_at) == ("daily", "03:30")
    assert s.same_for_all is False
    q = page()
    assert q.globals.mail_schedule.mode.currentData() == "interval"
    assert q.globals.mail_schedule.every.currentData() == 15
    assert q.globals.contacts_schedule.daily_at.time().toString("HH:mm") == "03:30"
    assert q.globals.same_for_all.currentData() is False
    assert isinstance(s.mail, ScheduleSetting)


@pytest.mark.slow
def test_pause_choices_save_and_reload_and_turn_back_on(world):
    svc, page = world
    p = page()
    p.globals.pause_for.setCurrentIndex(p.globals.pause_for.findData("forever"))
    p.globals.pause.setChecked(True)
    settle(p)
    assert svc.settings().paused_until >= 9e15
    q = page()
    assert q.globals.pause.isChecked()
    assert "turn it back on" in q.globals.pause_note.text()
    q.globals.pause.setChecked(False)
    settle(q)
    assert svc.settings().paused_until is None
    p2 = page()
    p2.globals.pause_for.setCurrentIndex(p2.globals.pause_for.findData("hour"))
    p2.globals.pause.setChecked(True)
    settle(p2)
    import time
    assert 3000 < svc.settings().paused_until - time.time() <= 3600


def test_quiet_hours_cap_conflict_and_safety_save_and_reload(world):
    from PySide6.QtCore import QTime
    svc, page = world
    p = page()
    g = p.globals
    g.quiet.setChecked(True)
    g.quiet_start.setTime(QTime(23, 15))
    g.quiet_end.setTime(QTime(6, 45))
    g.cap.setValue(500)
    g.conflict.setCurrentIndex(g.conflict.findData("ask"))
    g.brake_count.setValue(42)
    g.brake_percent.setValue(9.5)
    settle(p)
    s = svc.settings()
    assert (s.quiet_on, s.quiet_start, s.quiet_end) == (True, "23:15", "06:45")
    assert (s.first_fetch_kb_s, s.conflict_policy, s.brake_count, s.brake_percent) == (500, "ask", 42, 9.5)
    q = page().globals
    assert q.quiet.isChecked() and q.quiet_start.time().toString("HH:mm") == "23:15"
    assert q.cap.value() == 500 and q.conflict.currentData() == "ask"
    assert q.brake_count.value() == 42 and q.brake_percent.value() == 9.5


@pytest.mark.slow
def test_turning_the_dry_run_off_is_a_confirm_flow(world, monkeypatch):
    from app.ui.accounts import parts
    svc, page = world
    svc.accounts.update_settings(dry_run_first=True)
    p = page()
    asked = []
    monkeypatch.setattr(parts, "confirm", lambda *a, **k: asked.append(a[2]) or False)
    p.globals.dry_run.setChecked(False)
    settle(p)
    assert asked and "before you have seen the list" in asked[0]
    assert p.globals.dry_run.isChecked() and svc.settings().dry_run_first is True   # refused: unchanged
    monkeypatch.setattr(parts, "confirm", lambda *a, **k: True)
    p.globals.dry_run.setChecked(False)
    settle(p)
    assert svc.settings().dry_run_first is False
    assert page().globals.dry_run.isChecked() is False
    # turning it on needs no question
    monkeypatch.setattr(parts, "confirm", lambda *a, **k: pytest.fail("asked"))
    p.globals.dry_run.setChecked(True)
    settle(p)
    assert svc.settings().dry_run_first is True


@pytest.mark.slow
def test_notifications_region_maps_and_client_ids_save_and_reload(world):
    svc, page = world
    p = page()
    g = p.globals
    for box in (g.notify_brake, g.notify_conflict, g.notify_signin, g.notify_failed):
        box.setChecked(False)
    g.phone_region.setText("us")
    g.phone_region.editingFinished.emit()
    g.maps_site.setCurrentIndex(g.maps_site.findData("apple"))
    g.google_id.setText("gid.apps.googleusercontent.com")
    g.google_id.editingFinished.emit()
    g.microsoft_id.setText("ms-client-id")
    g.microsoft_id.editingFinished.emit()
    g.google_secret.setText("shh-secret")
    g.google_secret.editingFinished.emit()
    settle(p)
    s = svc.settings()
    assert not any((s.notify_brake, s.notify_conflict, s.notify_signin, s.notify_failed_24h))
    assert (s.phone_region, s.maps_site) == ("US", "apple")
    assert (s.google_client_id, s.microsoft_client_id) == ("gid.apps.googleusercontent.com", "ms-client-id")
    from app.accounts.secrets import client_secret_key
    assert svc.secrets.get(client_secret_key("google")) == "shh-secret"
    q = page().globals
    assert not q.notify_signin.isChecked() and q.phone_region.text() == "US"
    assert q.maps_site.currentData() == "apple" and q.google_id.text().startswith("gid")
    assert q.google_secret.text() == "" and "Saved" in q.google_secret.placeholderText()


@pytest.mark.slow
def test_a_bad_region_is_put_back(world):
    svc, page = world
    p = page()
    p.globals.phone_region.setText("1x")
    p.globals.phone_region.editingFinished.emit()
    settle(p)
    assert p.globals.phone_region.text() == "GB" and svc.settings().phone_region == "GB"


def test_default_account_saves_and_reloads(world):
    svc, page = world
    a = svc.accounts.add("carddav", "a@x.com", "Home")
    b = svc.accounts.add("carddav", "b@x.com", "Work")
    p = page()
    d = p.globals.default_account
    assert d.count() == 3
    d.setCurrentIndex(d.findData(b))
    settle(p)
    assert svc.settings().default_account_id == b
    assert page().globals.default_account.currentData() == b


def test_every_global_control_has_a_name_and_a_sentence(world):
    from PySide6.QtWidgets import QComboBox, QDoubleSpinBox, QLineEdit, QSpinBox, QTimeEdit
    svc, page = world
    p = page()
    for kind in (QComboBox, QSpinBox, QDoubleSpinBox, QTimeEdit, QLineEdit):
        for w in p.globals.findChildren(kind):
            if w.objectName().startswith("qt_"):
                continue                                   # a spin box's own inner editor
            assert w.accessibleName() or (isinstance(w, QLineEdit) and w.placeholderText()), w
    hints = p.globals.findChildren(type(p.notice), "accountsHint")
    assert len(hints) >= 14


# ---------------------------------------------------------------------------- the list
def test_the_list_shows_status_words_and_dates_in_the_window_format(world):
    from app.core import date_format
    from app.accounts.store import ScheduleSetting
    svc, page = world
    svc.accounts.update_settings(contacts=ScheduleSetting("manual"), mail=ScheduleSetting("manual"))   # hourly is the default now
    a = svc.accounts.add("carddav", "a@x.com", "Home", options={"server_url": "https://x"})
    b = svc.accounts.add("google", "b@x.com", "Work")
    svc.accounts.update(b, last_error_code="ERR_ACCOUNT_SIGNIN_EXPIRED", last_error="expired")
    svc.accounts.update(a, last_synced_at=1_800_000_000)
    date_format.set_date_format("dd/mm/yyyy hh:nn")
    try:
        p = page()
        tree = p.account_list
        rows = {tree.topLevelItem(i).text(0): tree.topLevelItem(i) for i in range(tree.topLevelItemCount())}
        assert rows["Work"].text(3) == "Needs sign-in"
        assert rows["Home"].text(5) == date_format.format_seconds(1_800_000_000)
        assert "/" in rows["Home"].text(5)
        assert rows["Home"].text(6) == "Manual"
        assert rows["Home"].text(4)                       # what it is doing, in a few words (the Doing column)
        assert rows["Work"].text(7)                       # says what to do
        assert not rows["Work"].icon(3).isNull() and not rows["Home"].icon(1).isNull()
    finally:
        date_format.set_date_format("")


def test_every_status_word_is_shown(world):
    from app.ui.accounts import logic
    from app.accounts.service import STATUS_WORDS
    svc, page = world
    svc.accounts.add("carddav", "a@x.com", "Home")
    p = page()
    row = logic.row_view(p._state.statuses[0])
    assert row.word in STATUS_WORDS
    svc.accounts.update_settings(paused_until=9e15)
    svc.accounts.update_settings(contacts=__import__("app.accounts.store", fromlist=["x"]).ScheduleSetting("interval", 60))
    p.refresh()
    settle(p)
    assert p.account_list.topLevelItem(0).text(3) == "Paused"
    assert "turn it back on" in p.account_list.topLevelItem(0).text(7)


def test_quiet_hours_are_said_on_the_row(world):
    from datetime import datetime
    from app.accounts.store import ScheduleSetting
    svc, page = world
    svc.accounts.add("carddav", "a@x.com", "Home")
    now = datetime.now()
    svc.accounts.update_settings(contacts=ScheduleSetting("interval", 60), quiet_on=True,
                                 quiet_start=f"{(now.hour - 1) % 24:02d}:00", quiet_end=f"{(now.hour + 1) % 24:02d}:00")
    p = page()
    assert "Quiet hours" in p.account_list.topLevelItem(0).text(7)


def test_dragging_reorders_and_saves(world):
    svc, page = world
    ids = [svc.accounts.add("carddav", f"{n}@x.com", n) for n in ("One", "Two", "Three")]
    p = page()
    p.account_list.reordered.emit([ids[2], ids[0], ids[1]])
    settle(p)
    assert [a.id for a in svc.accounts.list()] == [ids[2], ids[0], ids[1]]
    q = page()
    assert [q.account_list.topLevelItem(i).text(0) for i in range(3)] == ["Three", "One", "Two"]


# --------------------------------------------------------------------------------- the panel
def _select(p, aid):
    p.account_list.select(aid)
    wait_until(lambda: p.panel._id == aid, 2)
    return p.panel


def test_the_panel_saves_name_colour_idle_and_options_and_reloads(world, monkeypatch):
    from app.ui.accounts import panel as pm
    svc, page = world
    aid = svc.accounts.add("imap", "me@x.com", "Home", server="imap.x.com", mail_on=True)
    p = page()
    pn = _select(p, aid)
    pn.name.setText("Holiday")
    pn.name.editingFinished.emit()
    monkeypatch.setattr(pm, "pick_colour", lambda *a: "#112233")
    pn.colour_button.click()
    pn.idle.setChecked(True)
    pn.attachments.setChecked(False)
    pn.attachment_mb.setValue(25)
    pn.back.setCurrentIndex(pn.back.findData("5y"))
    settle(p)
    a = svc.accounts.get(aid)
    assert (a.name, a.colour) == ("Holiday", "#112233")
    assert a.options["idle"] is True and a.options["mail_attachments"] is False
    assert a.options["mail_attachment_mb"] == 25 and a.options["mail_back"] == "5y"
    q = page()
    pn = _select(q, aid)
    assert pn.name.text() == "Holiday" and pn.idle.isChecked() and not pn.attachments.isChecked()
    assert pn.attachment_mb.value() == 25 and pn.back.currentData() == "5y"


def test_idle_switch_is_off_by_default_and_only_for_imap(world):
    svc, page = world
    imap = svc.accounts.add("imap", "me@x.com", "Mail", server="s")
    dav = svc.accounts.add("carddav", "me@x.com", "Dav")
    p = page()
    pn = _select(p, imap)
    assert not pn.idle.isChecked() and not pn.idle.isHidden()
    pn = _select(p, dav)
    assert pn.idle.isHidden()


@pytest.mark.slow
def test_direction_into_leasha_only_persists_through_the_radio(world):
    svc, page = world
    aid = svc.accounts.add("carddav", "me@x.com", "Dav")
    assert svc.accounts.get(aid).contacts_direction == "two-way"          # the default
    p = page()
    pn = _select(p, aid)
    pn.direction.setCurrentIndex(pn.direction.findData("in-only"))
    settle(p)
    assert svc.accounts.get(aid).contacts_direction == "in-only"
    assert not svc.accounts.get(aid).writes_contacts
    q = page()
    assert _select(q, aid).direction.currentData() == "in-only"
    pn = _select(q, aid)
    pn.direction.setCurrentIndex(pn.direction.findData("off"))
    settle(q)
    assert not svc.accounts.get(aid).syncs_contacts


@pytest.mark.slow
def test_per_account_schedules_show_only_when_each_account_has_its_own(world):
    from app.accounts.store import ScheduleSetting
    svc, page = world
    a = svc.accounts.add("carddav", "a@x.com", "A")
    b = svc.accounts.add("carddav", "b@x.com", "B")
    p = page()
    assert _select(p, a).schedules.isHidden()
    p.globals.same_for_all.setCurrentIndex(1)
    settle(p)
    pn = _select(p, a)
    assert not pn.schedules.isHidden()
    pn.own_contacts.mode.setCurrentIndex(1)
    pn.own_contacts.every.setCurrentIndex(pn.own_contacts.every.findData(5))
    pn.own_contacts.flush()
    settle(p)
    pn = _select(p, b)
    pn.own_contacts.mode.setCurrentIndex(1)
    pn.own_contacts.every.setCurrentIndex(pn.own_contacts.every.findData(120))
    pn.own_contacts.flush()
    settle(p)
    assert svc.effective_schedule(svc.accounts.get(a), "contacts").minutes == 5
    assert svc.effective_schedule(svc.accounts.get(b), "contacts").minutes == 120
    q = page()
    assert _select(q, a).own_contacts.every.currentData() == 5
    assert isinstance(svc.effective_schedule(svc.accounts.get(a), "mail"), ScheduleSetting)


def test_test_connection_reports_the_granted_scopes_and_errors_with_fix(world, monkeypatch):
    from app.accounts.service import ConnectionReport
    svc, page = world
    aid = svc.accounts.add("google", "g@x.com", "G", mail_on=True)
    monkeypatch.setattr(svc, "test_connection", lambda acct: ConnectionReport(
        True, "Connected.", ["contacts", "mail (read only)"]))
    p = page()
    pn = _select(p, aid)
    pn.test_button.click()
    assert wait_until(lambda: "Granted" in pn.notice.text())
    assert "contacts, mail (read only)" in pn.notice.text()
    monkeypatch.setattr(svc, "test_connection", lambda acct: ConnectionReport(
        False, "Your organisation has not approved Leasha.", error_code="ERR_ACCOUNT_ADMIN_CONSENT",
        fix="Ask your IT administrator to approve it."))
    pn.test_button.click()
    assert wait_until(lambda: "IT administrator" in pn.notice.text())


def test_remove_account_is_the_1f_flow(world, monkeypatch):
    from app.ui.accounts import panel as pm
    svc, page = world
    aid = svc.accounts.add("carddav", "me@x.com", "Dav")
    p = page()
    pn = _select(p, aid)
    calls = []
    monkeypatch.setattr(svc, "remove_account", lambda i, **kw: calls.append((i, kw)) or svc.accounts.delete_rows(i))
    monkeypatch.setattr(pm, "ask_remove", lambda *a: None)
    pn.remove_button.click()
    settle(p)
    assert not calls and svc.accounts.get(aid) is not None                # cancelled: nothing
    monkeypatch.setattr(pm, "ask_remove", lambda *a: (True, True))
    pn.remove_button.click()
    assert wait_until(lambda: svc.accounts.get(aid) is None)
    assert calls == [(aid, {"keep_contacts": True, "remove_mail": True})]
    settle(p)
    assert p.account_list.topLevelItemCount() == 0 and not p.panel.isVisibleTo(p)


def test_the_remove_dialog_defaults_keep_contacts_and_remove_mail(world):
    from app.ui.accounts.panel import _RemoveDialog
    svc, page = world
    aid = svc.accounts.add("carddav", "me@x.com", "Dav")
    d = _RemoveDialog(svc.accounts.get(aid))
    assert d.keep_contacts.isChecked() and d.remove_mail.isChecked()
    assert d.keep_contacts.text() == "Keep this account's contacts in Leasha"
    assert d.remove_mail.text() == "Remove this account's mail from the index"


@pytest.mark.slow
def test_mail_tab_says_read_only_and_folder_choice_persists(world, monkeypatch):
    from app.ui.accounts import panel as pm
    svc, page = world
    aid = svc.accounts.add("imap", "me@x.com", "Mail", server="s", mail_on=True)
    p = page()
    pn = _select(p, aid)
    assert pn.findChild(type(pn.notice), "accountsMailHead").text() == \
        "Read only - Leasha never changes this mailbox"
    pn.folders_some.setChecked(True)
    settle(p)
    assert svc.accounts.get(aid).options["mail_folders"] == []
    svc.list_folders = lambda acct: ["INBOX", "Sent", "Spam"]
    monkeypatch.setattr(pm, "choose_from", lambda parent, title, names, cur: ["INBOX", "Sent"])
    pn.choose_folders.click()
    assert wait_until(lambda: svc.accounts.get(aid).options.get("mail_folders") == ["INBOX", "Sent"])
    q = page()
    assert _select(q, aid).folders_some.isChecked()
    pn.folders_all.setChecked(True)
    settle(p)
    assert svc.accounts.get(aid).options["mail_folders"] is None


def test_remove_this_accounts_mail_is_a_confirm_flow(world, monkeypatch):
    from app.ui.accounts import parts
    svc, page = world
    aid = svc.accounts.add("imap", "me@x.com", "Mail", server="s", mail_on=True)
    removed = []
    svc.remove_mail_hook = lambda i: removed.append(i)
    p = page()
    pn = _select(p, aid)
    monkeypatch.setattr(parts, "confirm", lambda *a, **k: False)
    pn.drop_mail.click()
    settle(p)
    assert removed == []
    monkeypatch.setattr(parts, "confirm", lambda *a, **k: True)
    pn.drop_mail.click()
    assert wait_until(lambda: removed == [aid])


def test_contacts_tab_shows_the_count_kept_here(world):
    svc, page = world
    aid = svc.accounts.add("carddav", "me@x.com", "Dav")
    p = page()
    assert _select(p, aid).kept.text() == "0 contacts"


# ------------------------------------------------------------------------------ guide links
def test_set_up_buttons_open_exactly_the_guide_sections(world, monkeypatch):
    from app.ui.accounts import parts
    svc, page = world
    opened = []
    monkeypatch.setattr(parts, "open_guide", lambda anchor: opened.append(anchor) or True)
    g = page().globals
    for b in (g.google_help, g.microsoft_help, g.apple_help, g.imap_help):
        b.click()
    assert opened == ["accounts-google-setup", "accounts-microsoft-setup", "accounts-icloud-password",
                      "accounts-imap"]


def test_open_guide_builds_a_file_url_with_the_anchor(monkeypatch):
    from PySide6.QtGui import QDesktopServices
    from app.ui.accounts import parts
    seen = []
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda url: seen.append(url.toString()) or True))
    assert parts.open_guide("accounts-imap") is True
    assert seen[0].startswith("file:") and seen[0].endswith("USER_GUIDE.html#accounts-imap")


# ------------------------------------------------------------ the Settings category (6l)
@pytest.fixture()
def settings_view(tmp_path):
    from app.core.config import load_settings
    from app.storage.sqlite_store import SqliteStore
    from tests.unit.test_pages_reorg import ENV
    qt()
    env = tmp_path / ".env"
    env.write_text(ENV.format(d=tmp_path.as_posix()), encoding="utf-8")
    settings = load_settings(env)
    store = SqliteStore(settings.fts_db).connect()
    from app.ui.settings_view import SettingsView
    view = SettingsView(settings, store)
    yield view, store
    view.close()
    store.close()


@pytest.mark.slow
def test_settings_has_an_accounts_category_with_an_icon_that_builds_when_first_shown(settings_view):
    from app.ui.widgets.category_nav import CategoryNav
    view, store = settings_view
    assert view._nav.category_names()[-1] == "Accounts"
    assert "Accounts" in CategoryNav.ICONS
    page = view.accounts_page
    assert view._nav.page("Accounts") is page and not page._built      # costs nothing until shown
    view.resize(900, 700)
    view.show()
    view._nav.show_category("Accounts", persist=False)
    assert wait_until(lambda: page._built and page._state is not None)
    assert page.globals.same_for_all.currentData() is True
    view.hide()


def test_every_action_button_on_the_page_is_in_the_button_system(world):
    from PySide6.QtWidgets import QPushButton
    from app.ui.widgets.buttons import ICON_PX, ROLES, is_exempt
    svc, page = world
    svc.accounts.add("imap", "me@x.com", "Mail", server="s", mail_on=True)
    p = page()
    p.resize(900, 4200)                       # tall: in the window it sits in a scroll area
    p.show()
    _select(p, svc.accounts.list()[0].id)
    # 2026-10-13: as tall as the page asks for once an account is open. Order 1s's Enrichment group
    # took it past the fixed 4,200 pixels (4,242), and a page shorter than its own size hint squeezes
    # its buttons - which this test then read as two button heights.
    p.resize(900, max(4200, p.sizeHint().height()))
    wait_until(lambda: False, 0.1)
    buttons = [b for b in p.findChildren(QPushButton) if not is_exempt(b)]
    assert len(buttons) >= 16
    for b in buttons:
        assert b.property("buttonRole") in ROLES, b.text()
        assert not b.icon().isNull() and b.iconSize().height() == ICON_PX, b.text()
        assert b.accessibleName() or b.text()
    assert len({b.height() for b in buttons if b.isVisible()}) == 1
    assert p.add_button.property("buttonRole") == "primary"
    assert p.panel.remove_button.property("buttonRole") == "danger"
    p.hide()


@pytest.mark.slow
def test_an_account_can_be_set_to_mail_only_contacts_only_or_both(world):
    """The owner, 2026-10-11: "edit to mail only or contacts only or both". Saved and reloaded."""
    svc, page = world
    aid = svc.accounts.add("google", "g@x.com", "G", mail_on=True)
    p = page()
    pn = _select(p, aid)
    assert pn.what.currentData() == "both"
    for key, want in (("mail", (True, False)), ("contacts", (False, True)), ("both", (True, True))):
        pn.what.setCurrentIndex(pn.what.findData(key))
        settle(p)
        a = svc.accounts.get(aid)
        assert (a.mail_on, a.contacts_on) == want, key
        q = page()
        assert _select(q, aid).what.currentData() == key, key
        pn = _select(p, aid)


@pytest.mark.slow
def test_contacts_only_after_off_comes_back_two_way_not_silently_off(world):
    svc, page = world
    aid = svc.accounts.add("google", "g@x.com", "G", mail_on=True)
    svc.accounts.update(aid, contacts_on=False, contacts_direction="off")
    p = page()
    pn = _select(p, aid)
    pn.what.setCurrentIndex(pn.what.findData("contacts"))
    settle(p)
    a = svc.accounts.get(aid)
    assert a.contacts_on and a.contacts_direction == "two-way" and not a.mail_on


def test_a_double_click_on_an_account_opens_its_settings(world):
    svc, page = world
    aid = svc.accounts.add("google", "g@x.com", "G", mail_on=True)
    p = page()
    _select(p, aid)
    opened = []
    p.account_list.account_opened.connect(opened.append)
    p.panel.tabs.setCurrentIndex(1)
    item = p.account_list.topLevelItem(0)
    p.account_list.itemActivated.emit(item, 0)
    assert opened == [aid]
    assert p.panel.tabs.currentIndex() == 0


@pytest.mark.slow
def test_master_mirror_and_backup_save_and_reload(world):
    """2026-10-11: the settings that keep contacts in step across the accounts."""
    svc, page = world
    a = svc.accounts.add("carddav", "a@x.com", "Home", options={"server_url": "https://x"})
    svc.accounts.add("google", "b@x.com", "Work")
    p = page()
    p.globals.master.setCurrentIndex(p.globals.master.findData(a))
    p.globals.mirror.setChecked(True)
    p.globals.backup.setChecked(False)
    p.globals.backup_keep.setValue(4)
    settle(p)
    s = svc.settings()
    assert (s.master_account_id, s.mirror_contacts, s.backup_contacts, s.contacts_backup_keep) == (a, True, False, 4)
    q = page()
    assert q.globals.master.currentData() == a and q.globals.mirror.isChecked()
    assert not q.globals.backup.isChecked() and q.globals.backup_keep.value() == 4
    q.globals.master.setCurrentIndex(0)
    settle(q)
    assert svc.settings().master_account_id is None


def test_the_check_button_reports_an_account_and_changes_nothing(world, monkeypatch):
    from app.accounts.service import VerifyReport
    svc, page = world
    aid = svc.accounts.add("carddav", "a@x.com", "Home", options={"server_url": "https://x"})
    monkeypatch.setattr(type(svc), "verify_contacts",
                        lambda self, acct: VerifyReport(acct.id, "Home", remote=3, linked=3), raising=False)
    p = page()
    pn = _select(p, aid)
    pn.verify_button.click()
    wait_until(lambda: "in sync" in pn.notice.text())
    assert "3 at the account" in pn.notice.text() and "in sync" in pn.notice.text()


def test_reconnect_on_an_apple_account_asks_for_the_new_app_password_and_tests_it(world, monkeypatch):
    """The owner (2026-10-12): "apple is disconnected and the reconnect button does not work". An app-password
    account has no browser sign-in, so Reconnect (from the activity row too) asks for the password, then tests."""
    from app.accounts.service import ConnectionReport
    from app.ui.accounts import panel as pm
    svc, page = world
    aid = svc.accounts.add("apple", "me@icloud.com", "Apple", mail_on=True)
    saved = []
    monkeypatch.setattr(pm, "ask_password", lambda parent, who: "abcd-efgh")
    monkeypatch.setattr(svc, "set_app_password", lambda i, pw: saved.append((i, pw)))
    monkeypatch.setattr(svc, "test_connection", lambda acct: ConnectionReport(True, "Connected.", []))
    p = page()
    pn = _select(p, aid)
    pn._row_action(aid, "mail", "reconnect")
    assert wait_until(lambda: saved == [(aid, "abcd-efgh")])
    assert wait_until(lambda: "Connected" in pn.notice.text())
