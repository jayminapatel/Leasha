"""Review 2026-10-14 CS-1, CS-2, CS-4, CS-10: one handler for the activity rows' buttons, a confirmed Allow that
names what was held, and the two states "Up to date" used to hide.

Layer: L5 presenter (Qt-free), with the two Qt hosts checked against it.

Before: Settings > Accounts showed Allow and Fix these on an account's row and did nothing when they were pressed
(`AccountPanel._row_action` had no branch for either); Allow ran at once wherever it was; a run that wrote all
but a few contacts said "Up to date" with a tick; and a mirror waiting on duplicates said nothing at all.
"""

from __future__ import annotations

import time

import pytest

from app.ui.presenter import account_actions as aa
from app.ui.presenter.account_activity import ACTION_KEYS, actions_for, describe

NOW = time.time()


class Host:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if name in aa.HOST_METHODS:
            return lambda *a: self.calls.append((name, *a))
        raise AttributeError(name)


# ------------------------------------------------------------------------------------------- CS-1: one handler
def test_every_key_the_rows_can_offer_is_handled():
    assert ACTION_KEYS <= aa.handled_keys(), sorted(ACTION_KEYS - aa.handled_keys())


@pytest.mark.parametrize("key, call", [
    ("sync", ("row_sync", 4, "mail")), ("retry", ("row_sync", 4, "mail")),
    ("retry_refused", ("row_sync", 4, "contacts")), ("stop", ("row_stop",)),
    ("pause", ("row_pause", 4, True)), ("resume", ("row_pause", 4, False)),
    ("allow", ("row_allow", 4)), ("fix", ("row_fix", 4)),
    ("test", ("row_account", 4, "test")), ("reconnect", ("row_account", 4, "reconnect")),
    ("settings", ("row_account", 4, "settings")), ("duplicates", ("row_contacts_tool", "duplicates")),
    ("conflicts", ("row_contacts_tool", "conflicts")),
])
def test_each_key_reaches_one_host_method(key, call):
    host = Host()
    assert aa.handle(host, 4, "mail", key) is True
    assert host.calls == [call]


def test_an_unknown_key_is_refused_not_ignored_silently():
    assert aa.handle(Host(), 1, "contacts", "launch") is False


@pytest.mark.parametrize("module, cls", [("app.ui.contacts_tools.sync_dialog", "SyncDialog"),
                                         ("app.ui.accounts.panel", "AccountPanel")])
def test_both_hosts_provide_every_method(module, cls):
    pytest.importorskip("PySide6")
    import importlib
    host = getattr(importlib.import_module(module), cls)
    missing = [m for m in aa.HOST_METHODS if not callable(getattr(host, m, None))]
    assert not missing, f"{cls} lacks {missing}"


# ------------------------------------------------------------------------------------------- CS-2: Allow asks
def test_allow_for_the_delete_brake_names_the_held_contacts_and_says_how_many():
    plan = {"held_deletes": 12, "flagged_deleted": 1,
            "names": {"held_deletes": ["Ann Lee", "Bob Ray", "Cy Dee"], "flagged_deleted": ["Dot Fox"]}}
    q = aa.allow_question("Work", plan, "ERR_SYNC_DELETE_BRAKE", "This sync would delete 12 ... FIX: Check")
    assert q.verb == "Allow and delete 12" and q.danger and q.count == 12
    assert q.text == "Delete 12 contacts from Work?"
    assert "Ann Lee, Bob Ray, Cy Dee and 9 more" in q.informative
    assert "Dot Fox" in q.informative and "Cancel leaves them there" in q.informative


def test_allow_for_the_churn_guard_says_what_changed_and_is_not_a_delete():
    text = ("Work returned 400 of its 500 contacts changed in the same way (phone removed), so nothing was "
            "written to any account. FIX: Look at a few of them")
    q = aa.allow_question("Work", {}, "ERR_SYNC_CHANGES_HELD", text)
    assert q.verb == "Allow and sync" and not q.danger
    assert "phone removed" in q.informative and "FIX" not in q.informative


def test_allow_with_nothing_recorded_still_asks():
    q = aa.allow_question("", None)
    assert q.verb == "Allow" and "this account" in q.text


def test_name_list():
    assert aa.name_list(["A", "B"], 2) == "A, B"
    assert aa.name_list([], 3) == "3 contacts"
    assert aa.name_list(["A"] * 12, 40, shown=10).endswith("and 30 more")


def test_the_sync_log_plan_names_the_held_deletes():
    from types import SimpleNamespace as NS
    from app.ui.accounts.logic import plan_text
    run = NS(plan={"held_deletes": 3, "names": {"held_deletes": ["Ann Lee"]}}, dry_run=False, status="held")
    text = plan_text(run)
    assert "delete 3 more, held for your Allow" in text and "Ann Lee" in text


# --------------------------------------------------------------------------------- CS-4 / CS-10: the two states
def snap(**kw):
    base = {"account_id": 1, "name": "Work", "kind": "contacts", "enabled": True, "live": False, "mode": "interval",
            "next": NOW + 900, "last_success": NOW - 60, "failures": 0,
            "run": {"status": "ok", "added": 0, "changed": 2, "deleted": 0, "error_text": ""}}
    base.update(kw)
    return base


def test_a_run_with_refusals_is_not_up_to_date_with_a_tick():
    a = describe(snap(refused=3, run={"status": "partial", "changed": 2, "error_text": ""}), NOW)
    assert a.state == "partial" and a.word == "Done, with problems"
    assert a.headline == "Up to date - 3 contacts could not be written"
    assert [x.key for x in actions_for(a)] == ["retry_refused", "settings"]
    assert [x.label for x in actions_for(a)][0] == "Retry these"


def test_without_the_refused_count_the_runs_own_number_is_used():
    a = describe(snap(run={"status": "partial", "plan": {"failed_out": 1}, "error_text": ""}), NOW)
    assert a.state == "partial" and a.headline.endswith("1 contact could not be written")


def test_refusals_cleared_means_up_to_date_again():
    a = describe(snap(refused=0), NOW)
    assert a.state == "idle" and a.headline.startswith("Up to date")


def test_the_mirror_waiting_on_duplicates_says_so_with_find_duplicates():
    run = {"status": "ok", "error_text": "", "plan": {"names": {"mirror_waiting": ["3 duplicate groups to merge first"]}}}
    a = describe(snap(refused=0, run=run), NOW)
    assert a.state == "mirror" and "3 duplicate groups are merged" in a.headline
    assert [x.key for x in actions_for(a)] == ["duplicates", "sync", "settings"]
    assert actions_for(a)[0].label == "Find duplicates"


def test_a_clash_waiting_on_you_says_so_and_offers_the_review_not_allow():
    run = {"status": "ok", "error_code": "ERR_SYNC_CONFLICT_HELD",
           "error_text": "2 contact(s) changed in two places and are waiting for your choice. FIX: Open Contacts"}
    a = describe(snap(refused=0, run=run), NOW)
    assert a.state == "clash" and a.word == "Waiting for you"
    assert a.headline == "2 contact(s) changed in two places and are waiting for your choice."
    keys = [x.key for x in actions_for(a)]
    assert keys == ["conflicts", "settings"] and "allow" not in keys
    assert actions_for(a)[0].label == "Changed in two places"


def test_mail_rows_never_take_the_contact_states():
    a = describe(snap(kind="mail", refused=4), NOW)
    assert a.state == "idle"


def test_the_snapshot_carries_the_refused_count_and_the_held_names(tmp_path):
    pytest.importorskip("keyring")
    from tests.unit.accounts_ui_fakes import make_service
    store, svc = make_service(tmp_path)
    try:
        aid = svc.accounts.add("carddav", "a@x.com", "Home")
        acct = svc.accounts.get(aid)
        run = svc.accounts.start_run(acct, "contacts", "sync-now")
        svc.accounts.finish_run(run, "held", held=2, error_code="ERR_SYNC_DELETE_BRAKE",
                                plan={"held_deletes": 2, "names": {"held_deletes": ["Ann Lee", "Bob Ray"]},
                                      "timing": {"pull_s": 1}})
        svc.accounts.record_failures(aid, [{"contact_id": 7, "name": "Cy", "reason": "too long"}], set())
        snaps = [s for s in svc.activity_snapshot() if s["kind"] == "contacts"]
        assert snaps[0]["refused"] == 1
        assert snaps[0]["run"]["plan"] == {"held_deletes": 2, "names": {"held_deletes": ["Ann Lee", "Bob Ray"]}}
        q = aa.allow_question(snaps[0]["name"], snaps[0]["run"]["plan"], snaps[0]["run"]["error_code"])
        assert q.verb == "Allow and delete 2" and "Ann Lee, Bob Ray" in q.informative
    finally:
        store.close()
