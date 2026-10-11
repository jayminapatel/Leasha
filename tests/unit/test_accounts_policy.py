"""Order 1k, items 10a, 10e, 10h, 10i; decisions D11, D16.

Layer: L7

Pure functions of timestamps, so every test is instant. Datetimes are naive
local time built from real UTC instants through `zoneinfo` (Europe/London) the
way `app/index/schedule.py` is tested, so the DST nights are real ones.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.accounts.policy import (
    MIN_GAP_S, AccountSyncState, SyncPolicy, catch_up_due, describe, due_at, failing_for_24h,
    hold_reason, in_quiet_hours, is_due, next_attempt_at, next_retry_at, parse_hhmm, pause_until,
    paused, quiet_hours_end, record_failure, record_success,
)

LONDON = ZoneInfo("Europe/London")


def local(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi)


def utc_to_local(*args):
    return datetime(*args, tzinfo=timezone.utc).astimezone(LONDON).replace(tzinfo=None)


MANUAL = SyncPolicy()
HOURLY = SyncPolicy("interval", 60)
DAILY = SyncPolicy("daily", daily_at=(2, 30))


# ---- the policy itself ------------------------------------------------------

def test_default_is_manual_and_switching_to_timed_starts_at_one_hour():
    p = SyncPolicy()
    assert p.mode == "manual" and p.interval_minutes == 60
    assert p.switched_to("interval").interval_minutes == 60
    assert SyncPolicy("manual", 15).switched_to("interval").interval_minutes == 60
    assert SyncPolicy("interval", 15).switched_to("daily").interval_minutes == 15   # a later change keeps it
    assert p.switched_to("nonsense") == p


def test_bad_saved_values_degrade_to_the_safe_default():
    assert SyncPolicy.from_values("weekly", "x", "25:99") == SyncPolicy("manual", 60, (2, 0))
    assert SyncPolicy.from_values("interval", 0).interval_minutes == 1       # any int >= 1 minute accepted
    assert SyncPolicy.from_values("interval", 7).interval_minutes == 7
    assert SyncPolicy.from_values("daily", 60, "14:05").daily_at == (14, 5)
    assert parse_hhmm("7:5") == (7, 5) and parse_hhmm("24:00") is None and parse_hhmm(None) is None


def test_manual_is_never_due():
    now = local(2026, 10, 11, 12)
    assert due_at(MANUAL, None, now) is None and due_at(MANUAL, local(2020, 1, 1), now) is None
    assert not is_due(MANUAL, None, now) and not is_due(MANUAL, local(2020, 1, 1), now)
    assert catch_up_due(MANUAL, None, now, now) is None


# ---- intervals --------------------------------------------------------------

def test_interval_due_time_and_not_before():
    last = local(2026, 10, 11, 13, 5)
    assert due_at(HOURLY, last, last) == local(2026, 10, 11, 14, 5)
    assert not is_due(HOURLY, last, local(2026, 10, 11, 14, 4))
    assert is_due(HOURLY, last, local(2026, 10, 11, 14, 5))
    five = SyncPolicy("interval", 5)
    assert due_at(five, last, last) == last + timedelta(minutes=5)
    assert due_at(HOURLY, None, last) == last                    # never synced: due now


def test_a_fortnight_asleep_is_due_once_not_fourteen_times():
    last = local(2026, 10, 1, 9, 0)
    wake = local(2026, 10, 15, 9, 0)
    assert is_due(HOURLY, last, wake)
    # Simulate the scheduler ticking each minute after waking: it syncs, records
    # the sync, and is then not due again until an interval later.
    syncs = 0
    t, state_last = wake, last
    while t < wake + timedelta(minutes=59):
        if is_due(HOURLY, state_last, t):
            syncs += 1
            state_last = t
        t += timedelta(minutes=1)
    assert syncs == 1
    assert due_at(HOURLY, state_last, t) == wake + timedelta(hours=1)


def test_daily_asleep_for_a_fortnight_is_due_once():
    last = local(2026, 10, 1, 2, 30)
    wake = local(2026, 10, 15, 9, 0)
    assert is_due(DAILY, last, wake)
    after = wake
    assert not is_due(DAILY, after, wake + timedelta(hours=5))   # synced at 09:00: today's slot is served
    assert due_at(DAILY, after, wake) == local(2026, 10, 16, 2, 30)


def test_clock_moved_back_does_not_wait_the_extra_time():
    last = local(2026, 10, 11, 13, 0)
    now = local(2026, 10, 10, 13, 0)                              # corrected back by a day
    assert due_at(HOURLY, last, now) == now + timedelta(hours=1)  # not 25 hours away
    assert not is_due(HOURLY, last, now)
    daily_last = local(2026, 10, 11, 3, 0)
    assert due_at(DAILY, daily_last, local(2026, 10, 10, 12, 0)) == local(2026, 10, 11, 2, 30)


def test_sixty_second_floor_holds_whatever_the_policy_says():
    one_minute = SyncPolicy("interval", 1)
    assert MIN_GAP_S == 60
    last = local(2026, 10, 11, 12, 0)
    assert is_due(one_minute, last, last + timedelta(minutes=1))
    # A failing account whose last success is ancient is "due" every tick; the
    # floor keeps the attempts at least a minute apart.
    ancient = local(2020, 1, 1)
    attempt = local(2026, 10, 11, 12, 0)
    assert not is_due(HOURLY, ancient, attempt + timedelta(seconds=59), last_attempt=attempt)
    assert is_due(HOURLY, ancient, attempt + timedelta(seconds=60), last_attempt=attempt)


# ---- daylight saving (Europe/London, 2026) ----------------------------------

def _count_daily_syncs(policy, start_utc, hours, step_min=5):
    """Tick a simulated scheduler through real UTC time; return the local times it synced."""
    t = datetime(*start_utc, tzinfo=timezone.utc)
    end = t + timedelta(hours=hours)
    last = None
    out = []
    while t < end:
        now = t.astimezone(LONDON).replace(tzinfo=None)
        if is_due(policy, last, now):
            out.append(now)
            last = now
        t += timedelta(minutes=step_min)
    return out


def test_daily_slot_that_does_not_exist_on_spring_forward_is_served_once():
    # 2026-03-29: 01:00 GMT jumps to 02:00 BST, so 02:30 never happens on the wall clock.
    p = SyncPolicy("daily", daily_at=(1, 30))
    # Start the evening before, last sync already done at the 28th's slot.
    syncs = _count_daily_syncs(p, (2026, 3, 28, 12, 0), hours=48)
    days = [s.date().isoformat() for s in syncs]
    assert days.count("2026-03-29") == 1 and days.count("2026-03-30") == 1
    assert days.count("2026-03-28") <= 1


def test_daily_slot_repeated_on_fall_back_is_served_once():
    # 2026-10-25: 02:00 BST falls back to 01:00 GMT, so 01:30 happens twice.
    p = SyncPolicy("daily", daily_at=(1, 30))
    syncs = _count_daily_syncs(p, (2026, 10, 24, 12, 0), hours=52)
    days = [s.date().isoformat() for s in syncs]
    assert days.count("2026-10-25") == 1, syncs
    assert days.count("2026-10-26") == 1


def test_hourly_across_the_clock_changes_never_doubles_or_stops():
    for start in ((2026, 3, 28, 20, 0), (2026, 10, 24, 20, 0)):
        t = datetime(*start, tzinfo=timezone.utc)
        end = t + timedelta(hours=30)
        last, syncs = None, []
        while t < end:
            now = t.astimezone(LONDON).replace(tzinfo=None)
            if is_due(HOURLY, last, now):
                syncs.append(now)
                last = now
            t += timedelta(minutes=5)
        # 30 real hours: between 28 and 31 syncs (wall-clock hours; see policy docstring), none within the floor.
        assert 28 <= len(syncs) <= 31, (start, len(syncs))
        assert all((b - a).total_seconds() >= 3600 or b < a for a, b in zip(syncs, syncs[1:]))


def test_dst_instants_are_the_real_ones():
    assert utc_to_local(2026, 3, 29, 0, 59) == datetime(2026, 3, 29, 0, 59)
    assert utc_to_local(2026, 3, 29, 1, 0) == datetime(2026, 3, 29, 2, 0)    # the hour that vanished


# ---- D16 catch-up ----------------------------------------------------------

def test_catch_up_is_one_minute_after_the_window_opens_when_the_last_sync_is_older_than_the_interval():
    opened = local(2026, 10, 11, 9, 0)
    last = local(2026, 10, 10, 18, 0)                        # 15 hours ago, hourly schedule
    assert catch_up_due(HOURLY, last, opened, opened) == local(2026, 10, 11, 9, 1)


def test_no_catch_up_when_the_last_sync_is_recent_or_never_for_manual():
    opened = local(2026, 10, 11, 9, 0)
    assert catch_up_due(HOURLY, local(2026, 10, 11, 8, 30), opened, opened) is None
    assert catch_up_due(MANUAL, None, opened, opened) is None


def test_catch_up_for_a_daily_schedule_on_a_laptop_closed_at_night():
    opened = local(2026, 10, 11, 9, 0)
    assert catch_up_due(DAILY, local(2026, 10, 10, 2, 30), opened, opened) == local(2026, 10, 11, 9, 1)
    assert catch_up_due(DAILY, local(2026, 10, 11, 2, 31), opened, opened) is None   # today's already served


def test_catch_up_is_a_single_time_however_many_were_missed():
    opened = local(2026, 10, 15, 9, 0)
    only = catch_up_due(SyncPolicy("interval", 5), local(2026, 10, 1), opened, opened)
    assert only == local(2026, 10, 15, 9, 1)                  # one value, not one per missed time
    assert catch_up_due(SyncPolicy("interval", 5), None, opened, opened) == local(2026, 10, 15, 9, 1)


# ---- describe ----------------------------------------------------------------

def test_describe():
    now = local(2026, 10, 11, 13, 10)
    assert describe(MANUAL, None, now) == "Only when you ask"
    assert describe(HOURLY, local(2026, 10, 11, 13, 5), now) == "Every 1 hour - next at 14:05"
    assert describe(SyncPolicy("interval", 5), local(2026, 10, 11, 13, 5), now) == "Every 5 minutes - due now"
    assert describe(SyncPolicy("interval", 240), local(2026, 10, 11, 13, 5), now) == "Every 4 hours - next at 17:05"
    assert describe(SyncPolicy("interval", 90), local(2026, 10, 11, 13, 5), now) == "Every 1 hour 30 minutes - next at 14:35"
    assert describe(HOURLY, local(2026, 10, 11, 9, 0), now) == "Every 1 hour - due now"
    assert describe(DAILY, local(2026, 10, 11, 2, 30), now) == "Daily at 02:30 - next tomorrow at 02:30"
    assert describe(HOURLY, local(2026, 10, 11, 23, 30), local(2026, 10, 11, 23, 40)) == "Every 1 hour - next tomorrow at 00:30"


# ---- quiet hours and pause ---------------------------------------------------

def test_quiet_hours_overnight_and_same_day():
    night = ((22, 0), (7, 0))
    for h, m, want in [(21, 59, False), (22, 0, True), (23, 59, True), (0, 0, True), (3, 30, True),
                       (6, 59, True), (7, 0, False), (12, 0, False)]:
        assert in_quiet_hours(local(2026, 10, 11, h, m), *night) is want, (h, m)
    day = ((9, 0), (17, 0))
    assert in_quiet_hours(local(2026, 10, 11, 9, 0), *day) and not in_quiet_hours(local(2026, 10, 11, 17, 0), *day)
    assert not in_quiet_hours(local(2026, 10, 11, 3, 0), *day)


def test_quiet_hours_off_when_unset_or_equal():
    assert not in_quiet_hours(local(2026, 10, 11, 3), None, None)
    assert not in_quiet_hours(local(2026, 10, 11, 3), (22, 0), None)
    assert not in_quiet_hours(local(2026, 10, 11, 3), (7, 0), (7, 0))


def test_quiet_hours_end_and_hold_reason_say_what_is_holding_the_sync():
    night = {"quiet_start": (22, 0), "quiet_end": (7, 0)}
    assert quiet_hours_end(local(2026, 10, 11, 23, 0), (22, 0), (7, 0)) == local(2026, 10, 12, 7, 0)
    assert quiet_hours_end(local(2026, 10, 11, 3, 0), (22, 0), (7, 0)) == local(2026, 10, 11, 7, 0)
    assert quiet_hours_end(local(2026, 10, 11, 12, 0), (22, 0), (7, 0)) is None
    assert hold_reason(local(2026, 10, 11, 23, 0), **night) == "Quiet hours until 07:00"
    assert hold_reason(local(2026, 10, 11, 12, 0), **night) is None


def test_pause():
    now = local(2026, 10, 11, 12, 0)
    assert not paused(now, None)
    assert paused(now, now + timedelta(hours=1)) and not paused(now + timedelta(hours=1), now + timedelta(hours=1))
    assert pause_until(now, "hour") == local(2026, 10, 11, 13, 0)
    assert pause_until(now, "tomorrow") == local(2026, 10, 12, 0, 0)
    assert paused(now, pause_until(now, "forever")) and pause_until(now, "x") is None
    assert hold_reason(now, paused_until=now + timedelta(hours=1)) == "Paused until 13:00"
    assert hold_reason(now, paused_until=pause_until(now, "forever")) == "Paused until you turn it back on"


# ---- failure, backoff, Retry-After (10e) ------------------------------------

def test_backoff_after_failures_is_1_2_4_8_capped_at_the_interval_and_reset_by_success():
    s = AccountSyncState(last_success=local(2026, 10, 11, 8, 0))
    now = local(2026, 10, 11, 9, 0)
    waits = []
    for _ in range(8):
        s = record_failure(s, now)
        waits.append((next_retry_at(s, 30) - now).total_seconds() / 60)
        now = next_retry_at(s, 30)                                   # the retry happens exactly when allowed
    assert waits == [1, 2, 4, 8, 16, 30, 30, 30]
    s = record_success(s, now)
    assert s.failures == 0 and s.first_failure_at is None and next_retry_at(s, 30) is None


def test_retry_after_wins_over_the_backoff_in_both_directions():
    now = local(2026, 10, 11, 9, 0)
    s = record_failure(record_failure(AccountSyncState(), now), now, retry_after_s=600)    # backoff would be 2 min
    assert next_retry_at(s, 60) == now + timedelta(minutes=10)
    s = AccountSyncState()
    for _ in range(6):
        s = record_failure(s, now)
    s = record_failure(s, now, retry_after_s=30)                      # backoff would be 32 min (cap 60)
    assert next_retry_at(s, 60) == now + timedelta(seconds=30)
    s = record_failure(s, now + timedelta(minutes=1))                 # next failure with no header: back to backoff
    assert next_retry_at(s, 60) == now + timedelta(minutes=1) + timedelta(minutes=60)


def test_next_attempt_is_the_later_of_schedule_and_retry_and_never_earlier_than_the_floor():
    now = local(2026, 10, 11, 9, 0)
    ok_at = local(2026, 10, 11, 7, 0)
    s = AccountSyncState(last_success=ok_at)
    assert next_attempt_at(HOURLY, s, now) == local(2026, 10, 11, 8, 0)           # overdue: schedule says earlier
    s = record_failure(s, now)
    assert next_attempt_at(HOURLY, s, now) == now + timedelta(minutes=1)          # backoff pushes it past "overdue"
    s = record_failure(s, now + timedelta(minutes=1), retry_after_s=1800)
    assert next_attempt_at(HOURLY, s, now + timedelta(minutes=1)) == now + timedelta(minutes=31)
    assert next_attempt_at(MANUAL, s, now) is None
    # The other accounts are not slowed: state is per account, so a healthy one is unchanged.
    assert next_attempt_at(HOURLY, AccountSyncState(last_success=ok_at), now) == local(2026, 10, 11, 8, 0)


def test_failing_for_24_hours():
    first = local(2026, 10, 10, 9, 0)
    assert not failing_for_24h(None, first)
    assert not failing_for_24h(first, first + timedelta(hours=23, minutes=59))
    assert failing_for_24h(first, first + timedelta(hours=24))
    s = record_failure(record_failure(AccountSyncState(), first), first + timedelta(hours=5))
    assert s.first_failure_at == first                                # the clock starts at the first failure, not the latest


@pytest.mark.parametrize("failures,interval,expected", [(1, 5, 1), (4, 5, 5), (2, 720, 2)])
def test_state_backoff_uses_the_policy_interval(failures, interval, expected):
    now = local(2026, 10, 11, 9, 0)
    s = AccountSyncState()
    for _ in range(failures):
        s = record_failure(s, now)
    assert next_retry_at(s, interval) == now + timedelta(minutes=expected)
