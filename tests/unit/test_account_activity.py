"""What each account is doing: the words, the snapshot behind them and the table that shows them.

The owner (2026-10-12): contacts and mail sync progress and status for each account is a must - "currently it is not
clear what the account is doing". One word, "Waiting", covered five different states; each now has its own sentence.
"""

from __future__ import annotations

import time
from datetime import datetime

import pytest

from app.ui.presenter.account_activity import STATE_ICONS, STATE_WORDS, ahead_words, describe, when_words
from app.ui.widgets.icons import ICON_NAMES

NOW = datetime(2026, 10, 12, 8, 14).timestamp()


def snap(**kw):
    base = {"account_id": 1, "name": "MS Main", "kind": "mail", "enabled": True, "live": False, "hold": None,
            "signin_expired": False, "mode": "interval", "next": NOW + 2700, "last_success": NOW - 600,
            "failures": 0, "run": {"status": "ok", "added": 0, "changed": 4, "deleted": 0, "error_text": ""}}
    base.update(kw)
    return base


def test_every_state_has_a_word_and_a_real_icon():
    assert set(STATE_WORDS) == set(STATE_ICONS)
    assert all(icon in ICON_NAMES for icon in STATE_ICONS.values())


def test_up_to_date_says_when_it_last_ran_what_it_did_and_when_it_runs_next():
    a = describe(snap(kind="contacts"), NOW)
    assert a.state == "idle" and "4 changed" in a.headline
    assert "Last run 10 min ago" in a.detail and "next in 45 min" in a.detail


def test_mail_waiting_for_another_accounts_mail_says_so():
    a = describe(snap(next=NOW - 5, queued_behind="GMail Main's mail"), NOW)
    assert a.state == "queued" and "waiting for GMail Main's mail to finish" in a.headline
    assert "one account at a time" in a.detail


def test_due_now_with_nothing_ahead_is_not_called_waiting():
    a = describe(snap(next=NOW - 5), NOW)
    assert a.state == "queued" and a.headline == "Due now - it starts within a minute"


def test_a_mail_fetch_shows_how_far_how_fast_and_how_long_is_left():
    a = describe(snap(live=True, mail={"fetched": 12300, "total": 80000, "rate_per_s": 4.2, "eta_s": 17000,
                                       "state": "fetching"}, messages=12300, meaning_waiting=50000), NOW)
    assert a.state == "running" and a.fraction == pytest.approx(0.15375)
    assert "12,300 of about 80,000 (15%)" in a.headline and "4 a second" in a.headline and "hours left" in a.headline
    assert "12,300 messages searchable" in a.detail
    assert "50,000" not in a.detail                         # the meaning backlog is said once, under the table


def test_a_mail_fetch_with_no_total_is_a_moving_bar_not_a_made_up_number():
    a = describe(snap(live=True, mail={"fetched": 700, "total": None}), NOW)
    assert a.busy and a.fraction is None and "700 messages so far" in a.headline
    assert describe(snap(live=True, mail={"fetched": 0, "total": None}), NOW).headline == "Checking for new mail"


def test_a_contact_sync_shows_the_contact_it_is_writing():
    a = describe(snap(kind="contacts", live=True,
                      live_progress={"phase": "writing", "done": 120, "total": 1436, "current": "Waqas Khan"}), NOW)
    assert a.fraction == pytest.approx(120 / 1436) and "Writing 120 of 1,436 contacts" in a.headline
    assert "Waqas Khan" in a.detail
    assert describe(snap(kind="contacts", live=True, live_progress={"phase": "reading", "done": 300}),
                    NOW).headline == "Reading the account - 300 contacts so far"


def test_failed_says_what_the_account_said_and_when_it_tries_again():
    a = describe(snap(run={"status": "failed", "error_text": "MS Main refused the request (HTTP 500)."},
                      failures=3), NOW)
    assert a.state == "failed" and "refused the request (HTTP 500)" in a.headline and "trying again" in a.detail


def test_sign_in_stopped_manual_paused_and_off_each_have_their_own_sentence():
    assert describe(snap(signin_expired=True), NOW).state == "signin"
    stopped = describe(snap(run={"status": "interrupted"}), NOW)
    assert stopped.state == "stopped" and "app closed" in stopped.headline
    assert describe(snap(mode="manual"), NOW).state == "manual"
    held = describe(snap(hold="Quiet hours until 07:00"), NOW)
    assert held.state == "paused" and "Quiet hours until 07:00" in held.headline
    by_you = describe(snap(paused_by_person=True), NOW)
    assert by_you.state == "paused" and "You paused" in by_you.headline
    assert describe(snap(enabled=False), NOW).state == "off"


def test_times_are_in_words():
    assert when_words(None, NOW) == "never" and when_words(NOW - 30, NOW) == "just now"
    assert when_words(NOW - 7200, NOW) == "today 06:14" and when_words(NOW - 86400, NOW).startswith("yesterday")
    assert ahead_words(NOW + 30, NOW) == "within a minute" and ahead_words(NOW - 1, NOW) == "due now"


# ----------------------------------------------------------------------- the snapshot, on a real service
def test_the_snapshot_covers_every_account_and_kind_and_marks_a_dead_run_stopped(tmp_path):
    from tests.unit.accounts_ui_fakes import make_service

    store, svc = make_service(tmp_path)
    try:
        a = svc.accounts.add("google", "a@x.com", "Home", mail_on=True)
        b = svc.accounts.add("microsoft", "pending-1", "Work", mail_on=True)
        run = svc.accounts.start_run(svc.accounts.get(b), "mail", "schedule")          # left "running" by a dead app
        snaps = svc.activity_snapshot(datetime.fromtimestamp(NOW))
        assert {(s["account_id"], s["kind"]) for s in snaps} == {(a, "contacts"), (a, "mail"), (b, "contacts"), (b, "mail")}
        mail_b = next(s for s in snaps if s["account_id"] == b and s["kind"] == "mail")
        assert mail_b["run"]["status"] == "interrupted" and mail_b["live"] is False
        assert describe(mail_b, NOW).state == "stopped"
        # now Home's mail really is running: Work's mail is queued behind it, by name
        svc._running.add((a, "mail"))
        snaps = svc.activity_snapshot(datetime.fromtimestamp(NOW))
        mail_b = next(s for s in snaps if s["account_id"] == b and s["kind"] == "mail")
        assert mail_b["queued_behind"] == "Home's mail"
        assert next(s for s in snaps if s["account_id"] == a and s["kind"] == "mail")["live"] is True
        assert run
    finally:
        store.close()


# --------------------------------------------------------------------------------------------- the table
def test_the_table_shows_a_row_for_each_enabled_kind_with_its_bar_and_words(qtbot):
    from app.ui.widgets.activity_table import ActivityTable

    table = ActivityTable()
    qtbot.addWidget(table)
    snaps = [snap(kind="contacts"),
             snap(live=True, mail={"fetched": 500, "total": 1000, "rate_per_s": 2, "eta_s": 250, "state": "fetching"}),
             snap(account_id=2, name="Off Account", kind="mail", enabled=False)]
    shown = table.set_snapshots(snaps, NOW)
    assert table.rowCount() == 2 and len(shown) == 2                       # an unused kind is not a row
    assert table.item(0, 2).text() == "Up to date" and table.item(1, 2).text() == "Running"
    bar = table.cellWidget(1, 3)
    assert (bar.minimum(), bar.maximum(), bar.value()) == (0, 1000, 500)
    assert "500 of about 1,000 (50%)" in table.item(1, 4).text()
    one = ActivityTable(single_account=True)
    qtbot.addWidget(one)
    assert one.isColumnHidden(0)
    assert len(one.set_snapshots(snaps, NOW, only=2, hide_off=False)) == 1


def test_the_one_line_for_everything_says_how_many_in_each_state_and_the_backlog_once():
    from app.ui.presenter.account_activity import account_line, overall_line

    snaps = [snap(kind="contacts"), snap(live=True, mail={"fetched": 5, "total": 10}, meaning_waiting=5_400_000),
             snap(account_id=2, name="W", kind="contacts", run={"status": "failed", "error_text": "no"}, failures=1)]
    acts = [describe(x, NOW) for x in snaps]
    line = overall_line(acts, snaps)
    assert line.startswith("1 running, 1 failed, 1 up to date") and line.count("5,400,000") == 1
    assert account_line(acts[:2]) == "Contacts: up to date - Mail: running 50%"
    assert overall_line([], []) == "No account is set up to sync contacts or mail."


def test_a_failure_puts_what_to_do_in_the_details_not_the_headline():
    a = describe(snap(run={"status": "failed", "error_text": "MS Main refused the request (HTTP 500). FIX: Press Test connection."},
                      failures=2), NOW)
    assert a.headline == "Failed: MS Main refused the request (HTTP 500)." and "What to do: Press Test connection." in a.detail


# ------------------------------------------------------------------------------------------------ the buttons
def _keys(**kw):
    from app.ui.presenter.account_activity import actions_for

    return [a.key for a in actions_for(describe(snap(**kw), NOW))]


def test_the_buttons_a_row_offers_follow_its_state():
    assert _keys() == ["sync", "settings"]                                         # up to date
    assert _keys(live=True, mail={"fetched": 5, "total": 10}) == ["stop", "pause", "settings"]
    assert _keys(kind="contacts", live=True) == ["stop", "settings"]
    assert _keys(paused_by_person=True) == ["resume", "settings"]
    assert _keys(hold="Quiet hours until 07:00") == ["sync", "settings"]           # Sync now ignores quiet hours
    assert _keys(run={"status": "failed", "error_text": "no"}, failures=1) == ["retry", "test", "settings"]
    assert _keys(signin_expired=True) == ["reconnect", "settings"]
    assert _keys(enabled=False) == ["settings"]


def test_a_service_pauses_and_resumes_mail_through_the_syncer_and_says_so_when_there_is_none(tmp_path):
    from tests.unit.accounts_ui_fakes import make_service

    store, svc = make_service(tmp_path)
    try:
        assert svc.pause_mail(1) is False and svc.resume_mail(1) is False        # no mail syncer attached
        seen = []
        svc.mail_sync = SimpleNamespaceSyncer(seen)
        assert svc.pause_mail(7) and svc.resume_mail(7) and seen == [("pause", 7), ("resume", 7)]
    finally:
        store.close()


class SimpleNamespaceSyncer:
    def __init__(self, seen):
        self.seen = seen

    def pause(self, account_id):
        self.seen.append(("pause", account_id))

    def resume(self, account_id):
        self.seen.append(("resume", account_id))


def test_the_live_contact_sync_events_reach_the_snapshot_for_an_account_panel_that_was_not_listening(tmp_path):
    from tests.unit.accounts_ui_fakes import make_service

    store, svc = make_service(tmp_path)
    try:
        a = svc.accounts.add("google", "a@x.com", "Home")
        track = svc._track(None)
        svc._running.add((a, "contacts"))
        track({"account_id": a, "phase": "writing", "done": 120, "total": 1436, "current": "Waqas Khan"})
        snap_c = next(s for s in svc.activity_snapshot(datetime.fromtimestamp(NOW))
                      if s["account_id"] == a and s["kind"] == "contacts")
        assert snap_c["live_progress"]["done"] == 120
        act = describe(dict(snap_c, now=time.time() + 30), time.time())
        assert "Writing 120 of 1,436 contacts" in act.headline and "Waqas Khan" in act.detail
        track({"account_id": a, "phase": "done"})                                 # the run ended: nothing is kept
        svc._running.discard((a, "contacts"))
        assert "live_progress" not in next(s for s in svc.activity_snapshot(datetime.fromtimestamp(NOW))
                                           if s["account_id"] == a and s["kind"] == "contacts")
    finally:
        store.close()


def test_the_table_follows_the_theme_icons_in_theme_tokens_and_buttons_rebuilt_when_the_ink_changes(qtbot, monkeypatch):
    from app.ui.widgets import activity_table as at
    from app.ui.widgets.activity_table import ActivityTable

    assert all(token in {"danger", "warning", "accent_text"} for token in at._TONE.values())   # tokens, not colours
    table = ActivityTable()
    qtbot.addWidget(table)
    table.set_snapshots([snap(kind="contacts")], NOW)
    first = table.cellWidget(0, 6)
    table.set_snapshots([snap(kind="contacts")], NOW)
    assert table.cellWidget(0, 6) is first                         # same state, same theme: the buttons stay put
    dark = dict(at.theme_colours(), text_dim="#123456")
    monkeypatch.setattr(at, "theme_colours", lambda: dark)
    table.set_snapshots([snap(kind="contacts")], NOW)
    assert table.cellWidget(0, 6) is not first                     # a new theme: repainted in its ink
