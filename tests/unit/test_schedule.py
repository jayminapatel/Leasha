"""When the indexer decides to run.

Layer: L3

Scheduling bugs are the ones that never reproduce on demand: "it ran twice the
night the clocks changed", "it stopped running in November", "it indexed
continuously after the laptop woke up". You cannot find those by opening the app
and waiting, which is exactly why none of this logic waits for anything - it is
arithmetic on two timestamps, and every awkward case below is a literal date.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.index.schedule import (
    MIN_GAP_S,
    STARTUP_DELAY_S,
    SchedulePolicy,
    describe,
    due_at,
    is_due,
)

NOW = datetime(2026, 8, 24, 14, 0, 0)          # a Monday afternoon


def at(hour: int, minute: int = 0, day: int = 24) -> datetime:
    return datetime(2026, 8, day, hour, minute, 0)


# -- manual ------------------------------------------------------------------

def test_manual_never_schedules_anything():
    policy = SchedulePolicy(mode="manual")
    assert due_at(policy, last_run=None, now=NOW) is None
    assert not is_due(policy, last_run=None, now=NOW)


def test_manual_stays_manual_however_long_it_has_been():
    policy = SchedulePolicy(mode="manual")
    assert not is_due(policy, last_run=at(0, 0, day=1), now=NOW)


# -- startup -----------------------------------------------------------------

def test_startup_waits_before_starting():
    """The first seconds after launch belong to the person, not the indexer.

    They are trying to search, the models are warming and the window is
    painting. Starting a 100GB walk into that is the worst first impression the
    app could make.
    """
    policy = SchedulePolicy(mode="startup")
    when = due_at(policy, last_run=None, now=NOW, started_at=NOW)
    assert when == NOW + timedelta(seconds=STARTUP_DELAY_S)
    assert not is_due(policy, last_run=None, now=NOW, started_at=NOW)


def test_startup_becomes_due_once_the_delay_has_passed():
    policy = SchedulePolicy(mode="startup")
    later = NOW + timedelta(seconds=STARTUP_DELAY_S + 1)
    assert is_due(policy, last_run=None, now=later, started_at=NOW)


def test_startup_runs_once_per_launch_and_not_again():
    """Otherwise it re-triggers on every timer tick for the rest of the session."""
    policy = SchedulePolicy(mode="startup")
    ran = NOW + timedelta(seconds=STARTUP_DELAY_S + 1)
    assert due_at(policy, last_run=ran, now=ran + timedelta(hours=5), started_at=NOW) is None


# -- interval ----------------------------------------------------------------

def test_interval_counts_from_the_last_run():
    policy = SchedulePolicy(mode="interval", interval_hours=6)
    last = at(8)
    assert due_at(policy, last_run=last, now=NOW) == at(14)


def test_interval_is_not_yet_due_before_the_gap_has_elapsed():
    policy = SchedulePolicy(mode="interval", interval_hours=6)
    assert not is_due(policy, last_run=at(10), now=NOW)


def test_interval_is_due_once_the_gap_has_elapsed():
    policy = SchedulePolicy(mode="interval", interval_hours=6)
    assert is_due(policy, last_run=at(7), now=NOW)


def test_a_never_indexed_machine_still_waits_out_the_startup_delay():
    policy = SchedulePolicy(mode="interval", interval_hours=6)
    assert not is_due(policy, last_run=None, now=NOW, started_at=NOW)
    assert is_due(
        policy, last_run=None,
        now=NOW + timedelta(seconds=STARTUP_DELAY_S + 1), started_at=NOW,
    )


def test_an_interval_of_zero_is_treated_as_one_hour():
    """Belt and braces. Config validation rejects it, but this is the layer that
    would spin if it ever got through."""
    policy = SchedulePolicy(mode="interval", interval_hours=0)
    assert due_at(policy, last_run=at(13), now=NOW) == at(14)


# -- daily, which is where the awkward cases live ----------------------------

def test_daily_schedules_todays_slot_if_it_has_not_passed():
    policy = SchedulePolicy(mode="daily", daily_at=(22, 0))
    assert due_at(policy, last_run=None, now=NOW) == at(22)


def test_daily_schedules_tomorrow_if_todays_slot_has_passed_and_it_ran():
    policy = SchedulePolicy(mode="daily", daily_at=(2, 0))
    assert due_at(policy, last_run=at(2), now=NOW) == at(2, 0, day=25)


def test_daily_is_due_now_if_todays_slot_was_missed():
    """The machine was asleep at 02:00. It should index, not skip the day."""
    policy = SchedulePolicy(mode="daily", daily_at=(2, 0))
    assert is_due(policy, last_run=at(2, 0, day=20), now=NOW)


def test_a_fortnight_asleep_produces_exactly_one_run_not_fourteen():
    """The failure this whole module is written to avoid.

    A due time in the past means "run now", once - never "run once for every
    occurrence you missed". Nothing here accumulates a backlog.
    """
    policy = SchedulePolicy(mode="daily", daily_at=(2, 0))
    long_ago = datetime(2026, 8, 10, 2, 0)

    assert is_due(policy, last_run=long_ago, now=NOW)

    # It ran. The very next check must not want another one.
    just_ran = NOW
    assert not is_due(
        policy, last_run=just_ran, now=NOW + timedelta(seconds=1),
        last_finished=just_ran,
    )


def test_daily_does_not_run_twice_in_one_day():
    policy = SchedulePolicy(mode="daily", daily_at=(2, 0))
    assert not is_due(policy, last_run=at(2), now=at(23), last_finished=at(2, 30))


def test_a_clock_jumped_backwards_does_not_cause_a_second_run():
    """DST, an NTP correction, or a laptop resuming with a stale clock."""
    policy = SchedulePolicy(mode="daily", daily_at=(2, 0))
    ran_at = at(2, 30)
    rewound = at(1, 45)                      # "now" is earlier than the last run
    assert not is_due(policy, last_run=ran_at, now=rewound, last_finished=ran_at)


# -- the backstop ------------------------------------------------------------

def test_nothing_starts_within_the_minimum_gap_of_the_last_finish():
    """The guard that stops a bug here becoming a machine that never idles.

    Every other rule is arithmetic on a clock somebody else controls. This one
    is not, and it is the difference between a scheduling bug and an
    uninstalled application.
    """
    policy = SchedulePolicy(mode="interval", interval_hours=1)
    finished = NOW - timedelta(seconds=MIN_GAP_S - 30)
    assert not is_due(
        policy, last_run=NOW - timedelta(hours=2), now=NOW, last_finished=finished
    )


def test_past_the_minimum_gap_it_may_start():
    policy = SchedulePolicy(mode="interval", interval_hours=1)
    finished = NOW - timedelta(seconds=MIN_GAP_S + 30)
    assert is_due(
        policy, last_run=NOW - timedelta(hours=2), now=NOW, last_finished=finished
    )


# -- what it tells the user --------------------------------------------------

@pytest.mark.parametrize(("policy", "last", "expected"), [
    (SchedulePolicy("manual"), None, "Only when you ask."),
    (SchedulePolicy("interval", interval_hours=6), at(9), "Every 6 hours - next at 15:00."),
    (SchedulePolicy("interval", interval_hours=1), at(13, 30), "Every 1 hour - next at 14:30."),
    (SchedulePolicy("daily", daily_at=(2, 0)), at(2), "Daily at 02:00 - next tomorrow at 02:00."),
])
def test_the_description_says_what_will_happen_and_when(policy, last, expected):
    """"interval: 6" tells nobody anything they can act on."""
    assert describe(policy, last_run=last, now=NOW) == expected


def test_the_description_says_due_now_rather_than_a_past_time():
    """A past timestamp shown as "next at 02:00" reads as broken."""
    policy = SchedulePolicy("daily", daily_at=(2, 0))
    text = describe(policy, last_run=at(2, 0, day=20), now=NOW)
    assert "due now" in text


def test_a_policy_is_built_from_settings_without_a_settings_object():
    """Duck-typed, so the scheduler is testable without loading a real .env."""
    class Fake:
        index_schedule = "daily"
        index_interval_hours = 3
        index_daily_at = "18:30"

    policy = SchedulePolicy.from_settings(Fake())
    assert policy.mode == "daily"
    assert policy.daily_at == (18, 30)


def test_an_unparseable_time_falls_back_rather_than_crashing_the_app():
    """Config validation rejects it first, but a bad stored value must not stop
    the window from opening."""
    class Fake:
        index_schedule = "daily"
        index_interval_hours = 3
        index_daily_at = "not a time"

    assert SchedulePolicy.from_settings(Fake()).daily_at == (2, 0)
