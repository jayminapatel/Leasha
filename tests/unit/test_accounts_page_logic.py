"""Order 1k 10g-10o, 10s, 10m: the decisions behind Settings > Accounts (no Qt).

Layer: L5
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace as NS

from app.accounts.store import Account, AccountSettings
from app.ui.accounts import logic


def test_interval_choices_are_the_spec_list_and_keep_a_saved_odd_value():
    texts = [t for _m, t in logic.interval_items()]
    assert texts == ["5 minutes", "10 minutes", "15 minutes", "30 minutes", "1 hour", "2 hours", "4 hours",
                     "6 hours", "12 hours"]
    assert (45, "45 minutes") in logic.interval_items(45)


def test_pause_choices_and_values():
    now = datetime(2026, 10, 11, 14, 5)
    assert [k for k, _ in logic.PAUSE_CHOICES] == ["hour", "tomorrow", "forever"]
    assert logic.pause_value("hour", now) == (now + timedelta(hours=1)).timestamp()
    assert logic.pause_value("tomorrow", now) == datetime(2026, 10, 12).timestamp()
    assert logic.pause_value("forever", now) == 9e15
    s = AccountSettings(paused_until=9e15)
    assert logic.hold_text(s, now) == "Paused until you turn it back on"


def test_quiet_hours_text_and_off_by_default():
    now = datetime(2026, 10, 11, 23, 0)
    assert logic.hold_text(AccountSettings(), now) is None
    s = AccountSettings(quiet_on=True, quiet_start="22:00", quiet_end="07:00")
    assert logic.hold_text(s, now) == "Quiet hours until 07:00"


def _status(**kw):
    acct = Account(1, "google", "me@x.com", "Work", mail_on=kw.pop("mail_on", False))
    acct.options = kw.pop("options", {})
    base = dict(account=acct, word="Up to date", icon="circle-check", last_sync=None, next_sync=None,
                schedule_text="", hold=None, warning="")
    base.update(kw)
    return NS(**base)


def test_row_view_never_uses_a_provider_logo_and_says_what_to_do():
    row = logic.row_view(_status(word="Needs sign-in", icon="key-round"))
    assert row.glyph in ("mail", "contact", "server") and "Reconnect" in row.note and row.warn
    assert row.last == "Never" and row.next == "Manual"
    row = logic.row_view(_status(hold="Quiet hours until 07:00", next_sync=datetime(2026, 10, 12, 7, 0)))
    assert "Quiet hours until 07:00" in row.note
    imap = Account(2, "imap", "a@b.c", "Mail", server="imap.b.c", mail_on=True)
    assert logic.provider_glyph(imap) == "server"
    assert ("mail", "Mail") in logic.kind_chips(imap)


def test_signin_text_with_and_without_an_expiry():
    a = Account(1, "google", "g@x.com", "G")
    assert "no expiry date is known" in logic.signin_text(a)
    a.options = {"signin_expires_at": 1_800_000_000, "signed_in_at": 1_790_000_000}
    assert "expires" in logic.signin_text(a) and "no expiry" not in logic.signin_text(a)
    a.last_error_code = "ERR_ACCOUNT_SIGNIN_EXPIRED"
    assert "Reconnect" in logic.signin_text(a)
    assert "app password" in logic.signin_text(Account(2, "imap", "a", "b"))


def test_run_rows_and_csv():
    run = NS(id=5, account_id=1, account_name="Home", kind="contacts", trigger="schedule (catch-up)",
             started_at=1_800_000_000, finished_at=1_800_000_075, status="failed", dry_run=False, added=1,
             changed=2, deleted=0, held=0, error_code="ERR_ACCOUNT_UNREACHABLE",
             error_text="Home could not be reached. FIX: Check the connection.", plan=None, undone_at=None)
    row = logic.run_row(run)
    assert row["trigger"] == "Schedule (catch-up)" and row["took"] == "1 min 15 s"
    assert row["error"] == "Home could not be reached." and row["fix"] == "Check the connection."
    csv_text = logic.runs_csv([run])
    assert csv_text.splitlines()[0].startswith("Time,Account,Kind,Started by")
    assert "Check the connection." in csv_text


def test_undo_approve_allow_availability():
    ok = NS(status="ok", dry_run=False, undone_at=None, added=1, changed=0, deleted=0)
    assert logic.can_undo(ok)
    assert not logic.can_undo(NS(**{**ok.__dict__, "undone_at": 5}))
    assert not logic.can_undo(NS(**{**ok.__dict__, "status": "dry-run"}))
    assert logic.can_approve(NS(status="needs-approval")) and not logic.can_approve(NS(status="ok"))
    assert logic.can_allow(NS(status="held"))


# ------------------------------------------------------------------ toasts (10m)
def _out(status="ok", conflicts=0, run_id=1):
    return NS(status=status, account_id=1, account_name="Home", run_id=run_id,
              plan=NS(conflicts=conflicts, held_conflicts=0))


def test_nothing_is_sent_for_an_ordinary_success():
    assert logic.toasts_for(AccountSettings(), [_status()], [_out()], datetime.now(), set()) == []


def test_each_toast_has_its_own_switch():
    now = datetime.now()
    outcomes = [_out("held"), _out(conflicts=2, run_id=2)]
    statuses = [_status(word="Needs sign-in"), _status(warning="Has been failing for more than 24 hours.")]
    statuses[1].account.id = 2
    keys = {t.key.split(":")[0] for t in logic.toasts_for(AccountSettings(), statuses, outcomes, now, set())}
    assert keys == {"brake", "conflict", "signin", "failing"}
    off = AccountSettings(notify_brake=False, notify_conflict=False, notify_signin=False, notify_failed_24h=False)
    assert logic.toasts_for(off, statuses, outcomes, now, set()) == []


def test_a_toast_is_sent_once_until_the_condition_clears():
    now, told = datetime.now(), set()
    st = [_status(word="Needs sign-in")]
    assert len(logic.toasts_for(AccountSettings(), st, [], now, told)) == 1
    assert logic.toasts_for(AccountSettings(), st, [], now, told) == []
    logic.toasts_for(AccountSettings(), [_status()], [], now, told)          # fixed: forgotten
    assert len(logic.toasts_for(AccountSettings(), st, [], now, told)) == 1  # broke again: told again


def test_a_sign_in_expiring_within_two_days_is_told_and_one_further_off_is_not():
    now = datetime(2026, 10, 11, 12, 0)
    soon = _status(options={"signin_expires_at": (now + timedelta(days=1)).timestamp()})
    far = _status(options={"signin_expires_at": (now + timedelta(days=5)).timestamp()})
    assert len(logic.toasts_for(AccountSettings(), [soon], [], now, set())) == 1
    assert logic.toasts_for(AccountSettings(), [far], [], now, set()) == []


def test_guide_path_points_at_the_user_guide():
    p = logic.guide_path()
    assert p is not None and p.name == "USER_GUIDE.html"
