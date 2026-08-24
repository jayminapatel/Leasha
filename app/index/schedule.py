"""When the next index run is due.

Layer: L3

**All of this is a pure function of two timestamps and a policy**, which is the
only reason it can be trusted. Scheduling bugs are the ones that never reproduce:
"it ran twice on the day the clocks changed", "it stopped running in November",
"it fires every second on a machine that has been asleep". None of those can be
found by running the app and waiting, so none of the logic here waits for
anything - `due_at` takes `last_run` and `now` and returns an answer.

The Qt timer that calls it lives in `app/ui/scheduler.py` and does nothing except
ask this module the time and start a run. That split is the same seam used for
COM and Qt everywhere else in this project.

**Four policies, and the awkward one is `daily`.**

| policy | next run |
|---|---|
| `manual` | never - only when asked |
| `startup` | once, shortly after the app opens |
| `interval` | `last_run + N hours` |
| `daily` | the next occurrence of a wall-clock time |

`daily` is the only one that uses wall-clock time, and wall-clock time is not
monotonic: it jumps at a DST boundary, when the machine wakes from sleep, and
when somebody corrects the clock. Everything here is therefore written to be
**idempotent about the past** - a due time that has already passed means "run
now", never "run once for every occurrence you missed". A laptop opened after
a fortnight away must index once, not fourteen times.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

__all__ = [
    "SchedulePolicy",
    "due_at",
    "is_due",
    "describe",
    "STARTUP_DELAY_S",
    "MIN_GAP_S",
]

#: How long after launch a `startup` run begins. Not zero: the first seconds
#: after opening are when the person is trying to search, the models are warming
#: and the window is painting. Starting a 100GB walk into that is the worst
#: possible first impression.
STARTUP_DELAY_S = 120

#: No automatic run may start within this long of the previous one finishing,
#: whatever the policy says. The backstop against a misconfigured interval, a
#: clock jump, or a bug here turning into a machine that indexes continuously.
MIN_GAP_S = 300


@dataclass(frozen=True, slots=True)
class SchedulePolicy:
    """What the user asked for. Built from `Settings`; validated at load."""

    mode: str = "manual"                 # manual | startup | interval | daily
    interval_hours: int = 6
    daily_at: tuple[int, int] = (2, 0)   # (hour, minute), local time

    @classmethod
    def from_settings(cls, settings: object) -> "SchedulePolicy":
        from app.core.config import parse_daily_at   # noqa: PLC0415 - avoids a cycle

        return cls(
            mode=str(getattr(settings, "index_schedule", "manual")),
            interval_hours=int(getattr(settings, "index_interval_hours", 6)),
            daily_at=parse_daily_at(str(getattr(settings, "index_daily_at", "02:00"))) or (2, 0),
        )


def due_at(
    policy: SchedulePolicy,
    *,
    last_run: Optional[datetime],
    now: datetime,
    started_at: Optional[datetime] = None,
) -> Optional[datetime]:
    """When the next automatic run is due, or None if there is not one.

    `started_at` is when the application opened, and only `startup` uses it.

    A returned time in the past means **due now**, and exactly once. That is the
    whole defence against a machine that was asleep for a fortnight waking up
    and deciding it owes fourteen runs.
    """
    if policy.mode == "manual":
        return None

    if policy.mode == "startup":
        if last_run is not None:
            return None                  # a startup run happens once per launch
        base = started_at or now
        return base + timedelta(seconds=STARTUP_DELAY_S)

    if policy.mode == "interval":
        hours = max(1, policy.interval_hours)
        if last_run is None:
            # Never run before: due after the startup delay rather than the
            # instant the app opens, for the same reason `startup` waits.
            return (started_at or now) + timedelta(seconds=STARTUP_DELAY_S)
        return last_run + timedelta(hours=hours)

    if policy.mode == "daily":
        hour, minute = policy.daily_at
        today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if last_run is None:
            # Never run: the next occurrence, today if it has not passed.
            return today if today > now else today + timedelta(days=1)
        if last_run >= today:
            # Already ran at or after today's slot - the next one is tomorrow.
            return today + timedelta(days=1)
        # Today's slot has not been served yet. If it has already passed, this
        # is in the past, which correctly means "run now, once".
        return today

    return None


def is_due(
    policy: SchedulePolicy,
    *,
    last_run: Optional[datetime],
    now: datetime,
    started_at: Optional[datetime] = None,
    last_finished: Optional[datetime] = None,
) -> bool:
    """Should a run start right now?

    `last_finished` enforces `MIN_GAP_S` regardless of policy. That backstop
    exists because every other guard here is arithmetic on a clock somebody else
    controls, and the failure mode without it - a machine that indexes without
    stopping - is the one that gets the application uninstalled.
    """
    when = due_at(policy, last_run=last_run, now=now, started_at=started_at)
    if when is None or when > now:
        return False
    if last_finished is not None and (now - last_finished).total_seconds() < MIN_GAP_S:
        return False
    return True


def describe(policy: SchedulePolicy, *, last_run: Optional[datetime], now: datetime) -> str:
    """One line for the settings panel and the status bar.

    Says what will happen and when, in words. "interval: 6" tells somebody
    nothing they can act on; "Every 6 hours - next around 14:30" does.
    """
    if policy.mode == "manual":
        return "Only when you ask."

    when = due_at(policy, last_run=last_run, now=now)
    if policy.mode == "startup":
        return (
            "Once, shortly after the app opens."
            if last_run is None
            else "Once per launch - already done this session."
        )

    if when is None:
        return "Not scheduled."

    if when <= now:
        due = "due now"
    elif when.date() == now.date():
        due = f"next at {when:%H:%M}"
    elif when.date() == (now + timedelta(days=1)).date():
        due = f"next tomorrow at {when:%H:%M}"
    else:
        due = f"next {when:%a %d %b at %H:%M}"

    if policy.mode == "interval":
        hours = max(1, policy.interval_hours)
        unit = "hour" if hours == 1 else "hours"
        return f"Every {hours} {unit} - {due}."
    return f"Daily at {policy.daily_at[0]:02d}:{policy.daily_at[1]:02d} - {due}."
