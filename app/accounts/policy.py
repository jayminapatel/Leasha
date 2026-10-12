"""When the next account sync is due.

Layer: L7

Order 1k, items 10a, 10e, 10i and 10h; decisions D11 and D16. This sits beside
`app/index/schedule.py` and is written the same way, for the same reason:
**everything here is a pure function of timestamps and a policy**, because
scheduling bugs are the ones that never reproduce ("it ran twice the day the
clocks changed", "it syncs every second on a laptop that slept"). Nothing here
waits for anything, reads a clock, or imports Qt. The Qt timer that calls it
lives in `app/ui/` and does nothing but ask this module and start work.

**Why not reuse `SchedulePolicy`.** It counts in hours and its floor
(`MIN_GAP_S` = 300) is meant for index runs that take minutes to hours. A mail
or contacts check every 5 minutes is the shortest the person can choose, and a
check is a couple of seconds of network. So this has its own minutes-based
policy and its own, smaller, floor.

All datetimes are **naive local time**, as in `schedule.py`: the policy says
"daily at 02:00" and the person means the wall clock on the wall.

**Idempotent about the past.** A due time that has already passed means "sync
now, once" - never once per occurrence missed. A laptop asleep for a fortnight
syncs once (D11.4). A start after Leasha was closed follows D16 (`catch_up_due`).

**Two known simplifications, same as the index schedule.** Interval arithmetic
is wall-clock, so an hourly schedule is 1 wall-clock hour across a DST change,
which may be 0 or 2 real hours once a year; and a `daily` slot that does not
exist that night (02:30 when the clocks go forward) is served the first moment
the wall clock is past it. Neither skips or doubles a sync, which is what
matters.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Optional

from app.accounts.http import backoff_minutes

__all__ = [
    "MODES", "INTERVAL_CHOICES_MIN", "DEFAULT_INTERVAL_MIN", "MIN_GAP_S", "CATCH_UP_DELAY_S",
    "FAILING_WARNING_AFTER", "SyncPolicy", "due_at", "is_due", "describe", "catch_up_due",
    "in_quiet_hours", "quiet_hours_end", "paused", "pause_until", "hold_reason",
    "AccountSyncState", "record_success", "record_failure", "next_retry_at", "next_attempt_at",
    "failing_for_24h", "parse_hhmm",
]

MODES = ("manual", "interval", "daily")

#: The choices the Settings page offers (10g): 5, 10, 15, 30 minutes, then 1, 2,
#: 4, 6, 12 hours. Any whole number of minutes >= 1 is *accepted* (the CLI and a
#: saved value may hold one), the list is only what the control shows.
INTERVAL_CHOICES_MIN = (5, 10, 15, 30, 60, 120, 240, 360, 720)

#: The interval a policy starts with, and the one it takes when the person
#: switches from Manual to a timed schedule (D11.3).
DEFAULT_INTERVAL_MIN = 60

#: No automatic sync may start within this long of the previous attempt,
#: whatever the policy says. 60 s rather than the index's 300 s because a
#: mail/contacts check is seconds of network, not a long walk, and 5 minutes is
#: the shortest schedule on offer. It is still a backstop and not a tuning
#: knob: every other guard is arithmetic on a clock somebody else controls, and
#: a bug that syncs in a tight loop would hit a provider's rate limit and get
#: the person's account throttled. Evidence that would change it: a provider
#: documenting a lower per-user limit than one request a minute.
MIN_GAP_S = 60

#: D16: a catch-up sync starts this long after the window opens, not at once -
#: the first seconds are when the person is looking at the window, not waiting
#: for a download.
CATCH_UP_DELAY_S = 60

#: 10e: an account failing this long shows a warning on its row and in the tray.
FAILING_WARNING_AFTER = timedelta(hours=24)


def parse_hhmm(text: object) -> Optional[tuple[int, int]]:
    """'14:05' -> (14, 5); None when it is not a valid 24-hour time."""
    try:
        h, m = str(text).strip().split(":")
        hour, minute = int(h), int(m)
    except (ValueError, AttributeError):
        return None
    return (hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None


@dataclass(frozen=True, slots=True)
class SyncPolicy:
    """What the person asked for, for one kind (mail or contacts). Default: manual."""

    mode: str = "manual"                       # manual | interval | daily
    interval_minutes: int = DEFAULT_INTERVAL_MIN
    daily_at: tuple[int, int] = (2, 0)         # (hour, minute), local time

    @classmethod
    def from_values(cls, mode: object = "manual", interval_minutes: object = DEFAULT_INTERVAL_MIN,
                    daily_at: object = "02:00") -> "SyncPolicy":
        """Build from saved values, repairing anything invalid rather than raising:
        a bad saved value must degrade to the safe default (manual), never stop the app."""
        m = str(mode).strip().lower()
        if m not in MODES:
            m = "manual"
        try:
            n = int(interval_minutes)
        except (TypeError, ValueError):
            n = DEFAULT_INTERVAL_MIN
        return cls(m, max(1, n), parse_hhmm(daily_at) or (2, 0))

    def switched_to(self, mode: str) -> "SyncPolicy":
        """The policy after the person picks a mode. Going from Manual to a timed
        schedule starts at 1 hour (D11.3) whatever the stored interval was."""
        if mode not in MODES:
            return self
        if self.mode == "manual" and mode == "interval":
            return replace(self, mode=mode, interval_minutes=DEFAULT_INTERVAL_MIN)
        return replace(self, mode=mode)

    @property
    def interval(self) -> timedelta:
        return timedelta(minutes=max(1, self.interval_minutes))


def due_at(policy: SyncPolicy, last_sync: Optional[datetime], now: datetime) -> Optional[datetime]:
    """When the next scheduled sync is due, or None for Manual.

    A returned time in the past means **due now, once**. A `last_sync` in the
    future (the clock was moved back) is treated as `now`: otherwise a laptop
    whose clock was corrected by a day would wait an extra day.
    """
    if policy.mode == "manual":
        return None
    if last_sync is not None and last_sync > now:
        last_sync = now

    if policy.mode == "interval":
        if last_sync is None:
            return now                          # never synced: due now
        return last_sync + policy.interval

    if policy.mode == "daily":
        hour, minute = policy.daily_at
        today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if last_sync is None:
            return today if today > now else now
        # The latest slot that has come: today's, or - before today's time - yesterday's. Comparing with today's
        # alone missed every day skipped while the app was closed, whenever it opened before the slot's time
        # (found 2026-10-13 at 00:39: a sync three days old was not due until 02:00).
        slot = today if today <= now else today - timedelta(days=1)
        if last_sync >= slot:
            return slot + timedelta(days=1)     # the latest slot is already served
        return slot
    return None


def is_due(policy: SyncPolicy, last_sync: Optional[datetime], now: datetime, *,
           last_attempt: Optional[datetime] = None) -> bool:
    """Should a scheduled sync start right now?

    `last_attempt` enforces `MIN_GAP_S` whatever the policy says. It is the last
    *attempt*, not the last success: a failing account must not be retried
    in a tight loop (the backoff of 10e sits on top of this).
    """
    when = due_at(policy, last_sync, now)
    if when is None or when > now:
        return False
    if last_attempt is not None and 0 <= (now - last_attempt).total_seconds() < MIN_GAP_S:
        return False
    return True


def _unit(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _every(minutes: int) -> str:
    minutes = max(1, minutes)
    if minutes < 60:
        return _unit(minutes, "minute")
    hours, rest = divmod(minutes, 60)
    return _unit(hours, "hour") if not rest else f"{_unit(hours, 'hour')} {_unit(rest, 'minute')}"


def _when(when: datetime, now: datetime) -> str:
    if when <= now:
        return "due now"
    if when.date() == now.date():
        return f"next at {when:%H:%M}"
    if when.date() == (now + timedelta(days=1)).date():
        return f"next tomorrow at {when:%H:%M}"
    return f"next {when:%a %d %b at %H:%M}"


def describe(policy: SyncPolicy, last_sync: Optional[datetime], now: datetime) -> str:
    """One line for the account row: what will happen and when, in words."""
    if policy.mode == "manual":
        return "Only when you ask"
    when = due_at(policy, last_sync, now)
    if when is None:
        return "Not scheduled"
    due = _when(when, now)
    if policy.mode == "interval":
        return f"Every {_every(policy.interval_minutes)} - {due}"
    return f"Daily at {policy.daily_at[0]:02d}:{policy.daily_at[1]:02d} - {due}"


def catch_up_due(policy: SyncPolicy, last_sync: Optional[datetime], now: datetime,
                 window_opened_at: datetime) -> Optional[datetime]:
    """D16: when the one catch-up sync should run after Leasha opens, or None.

    If the last sync is older than the schedule's interval (for `daily`: a
    slot has passed since the last sync), return one minute after the window
    opened. One sync, never one per missed time - the same rule as the index
    schedule. Manual never catches up.
    """
    when = due_at(policy, last_sync, now)
    if when is None or when > now:
        return None
    return window_opened_at + timedelta(seconds=CATCH_UP_DELAY_S)


# --------------------------------------------------------------------------
# Quiet hours (10i) and pause (10h)
# --------------------------------------------------------------------------

def in_quiet_hours(now: datetime, start: Optional[tuple[int, int]], end: Optional[tuple[int, int]]) -> bool:
    """Is `now` inside the quiet span? Start inclusive, end exclusive.

    A span that crosses midnight (22:00 -> 07:00) is two pieces: from the start
    to midnight, and from midnight to the end. `start == end`, or either
    missing, means quiet hours are off - not "all day", which nobody means.
    """
    if start is None or end is None or tuple(start) == tuple(end):
        return False
    t = (now.hour, now.minute)
    s, e = tuple(start), tuple(end)
    return s <= t < e if s < e else (t >= s or t < e)


def quiet_hours_end(now: datetime, start: Optional[tuple[int, int]],
                    end: Optional[tuple[int, int]]) -> Optional[datetime]:
    """When the quiet span that contains `now` ends, or None if not in one."""
    if not in_quiet_hours(now, start, end):
        return None
    out = now.replace(hour=end[0], minute=end[1], second=0, microsecond=0)
    return out if out > now else out + timedelta(days=1)


def paused(now: datetime, until: Optional[datetime]) -> bool:
    """Is sync paused? `until` None means not paused; `datetime.max` means until turned back on."""
    return until is not None and now < until


def pause_until(now: datetime, choice: str) -> Optional[datetime]:
    """The pause choices of 10h: 'hour', 'tomorrow' (the next midnight), 'forever' (until turned on)."""
    if choice == "hour":
        return now + timedelta(hours=1)
    if choice == "tomorrow":
        return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if choice == "forever":
        return datetime.max
    return None


def hold_reason(now: datetime, *, quiet_start: Optional[tuple[int, int]] = None,
                quiet_end: Optional[tuple[int, int]] = None,
                paused_until: Optional[datetime] = None) -> Optional[str]:
    """Why a *scheduled* sync is being held back right now, in words for the row (10i), else None.

    Sync now ignores this; only the schedule asks.
    """
    if paused(now, paused_until):
        if paused_until == datetime.max:
            return "Paused until you turn it back on"
        return f"Paused until {paused_until:%H:%M}" if paused_until.date() == now.date() \
            else f"Paused until {paused_until:%a %d %b %H:%M}"
    end = quiet_hours_end(now, quiet_start, quiet_end)
    if end is not None:
        return f"Quiet hours until {end:%H:%M}"
    return None


# --------------------------------------------------------------------------
# Failure, backoff and Retry-After (10e)
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AccountSyncState:
    """What the scheduler remembers per account and kind. Immutable: every
    change returns a new one, which keeps the logic here pure."""

    last_success: Optional[datetime] = None
    last_attempt: Optional[datetime] = None
    failures: int = 0                          # consecutive; 0 after a success
    first_failure_at: Optional[datetime] = None
    retry_after_until: Optional[datetime] = None   # a provider's Retry-After, as a time


def record_success(state: AccountSyncState, now: datetime) -> AccountSyncState:
    """A success resets the backoff and the failing clock."""
    return AccountSyncState(last_success=now, last_attempt=now)


def record_failure(state: AccountSyncState, now: datetime,
                   retry_after_s: Optional[float] = None) -> AccountSyncState:
    until = now + timedelta(seconds=retry_after_s) if retry_after_s is not None and retry_after_s >= 0 else None
    return AccountSyncState(
        last_success=state.last_success, last_attempt=now,
        failures=state.failures + 1,
        first_failure_at=state.first_failure_at or now,
        retry_after_until=until,
    )


def next_retry_at(state: AccountSyncState, interval_minutes: int) -> Optional[datetime]:
    """The earliest a failed account may be tried again, or None if it is not failing.

    1, 2, 4, 8 ... minutes after the last attempt, capped at the schedule's
    interval; a provider's `Retry-After` always wins - even when it is shorter
    than our backoff and even when it is longer than the cap, because it is the
    only party that knows its own limit.
    """
    if state.failures <= 0 or state.last_attempt is None:
        return None
    if state.retry_after_until is not None:
        return state.retry_after_until
    return state.last_attempt + timedelta(minutes=backoff_minutes(state.failures, interval_minutes))


def next_attempt_at(policy: SyncPolicy, state: AccountSyncState, now: datetime) -> Optional[datetime]:
    """When the scheduler should next start this account: the schedule, delayed by any backoff.

    None for Manual. A failing account is due at the later of its schedule and
    its retry time, so a failure never makes a sync *earlier*, and never
    sooner than `MIN_GAP_S` after the last attempt.
    """
    base = due_at(policy, state.last_success, now)
    if base is None:
        return None
    candidates = [base]
    retry = next_retry_at(state, policy.interval_minutes if policy.mode == "interval" else 24 * 60)
    if retry is not None:
        candidates.append(retry)
    if state.last_attempt is not None and state.last_attempt <= now:
        candidates.append(state.last_attempt + timedelta(seconds=MIN_GAP_S))
    return max(candidates)


def failing_for_24h(first_failure_at: Optional[datetime], now: datetime) -> bool:
    """Has the account been failing for 24 hours or more? (The warning on its row and in the tray.)"""
    return first_failure_at is not None and (now - first_failure_at) >= FAILING_WARNING_AFTER
