r"""When the hits cluster in time. Workspace §3d.

Layer: L5 presenter — Qt-free, so the bucketing and the wording are testable
without a strip to draw them on.

**The question it answers is one a list cannot.** Fifty results, and forty of
them are from one fortnight in 2019 — that is the answer, and reading fifty
dates off a list is the only way to see it today. A band over the results
makes it a glance.

**It composes with the filters that already exist.** Clicking a period gives
back an `after:`/`before:` pair, which goes into the box like anything typed
there. §3d is explicit: *no new query semantics.* The band is a way of writing
a filter, not a second kind of filtering — and that is also what makes it
undoable, because what it produces is visible in the search box.

**The scale is chosen from the span, not fixed.** A corpus spanning fifteen
years bucketed by month is a hundred and eighty bars nobody can hit; one
spanning a fortnight bucketed by year is a single bar saying nothing.

**Moved here unchanged from `docs/_superseded/timeline.py`.** `bands()` reads
only `row.mtime_ns`, which `ResultRow` already carries field-for-field, so
nothing needed adapting. The Qt half - the strip itself, and the off switch
§6 requires of it - is new: `widgets/timeline_strip.py`. No Qt draft existed
to check it against.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, NamedTuple, Optional

__all__ = ["Band", "SCALES", "scale_for", "bands", "label_for", "NANOS"]

#: Nanoseconds to seconds. `mtime_ns` is what rows carry.
NANOS = 1_000_000_000

#: The scales, coarsest first, with roughly how long a bucket lasts.
SCALES: tuple[str, ...] = ("year", "quarter", "month", "week", "day")

#: How many bands are worth drawing.
#:
#: **Twelve, because they are click targets.** A strip is a few pixels tall
#: and the whole width of a results list; past a dozen the bars are narrower
#: than a fingertip and the feature becomes a decoration that occasionally
#: filters the wrong period.
MAX_BANDS = 12


class Band(NamedTuple):
    """One period on the strip: what it covers, how many, what to type."""

    label: str
    count: int
    after: str
    before: str

    @property
    def filter_text(self) -> str:
        """The period as the filters the parser already accepts.

        `after:2019-01-01 before:2019-12-31` - two operators that have worked
        since Layer 4, which is the whole of §3d's "no new query semantics".
        """
        return f"after:{self.after} before:{self.before}"


def _to_date(value: Any) -> Optional[date]:
    """`mtime_ns` as a date. None for the zero this codebase uses for unknown.

    **Never raises.** Rows come from four views and a store, and one bad
    timestamp must cost that row's bar rather than the strip.
    """
    try:
        seconds = int(value) / NANOS
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc).date()
    except (OverflowError, OSError, ValueError):
        return None


def scale_for(span_days: int) -> str:
    r"""Which bucket size suits a span of this many days.

    Chosen so the strip lands near - never over - `MAX_BANDS` bars.

    >>> scale_for(4000), scale_for(400), scale_for(60), scale_for(3)
    ('year', 'quarter', 'week', 'day')
    """
    days = max(0, int(span_days))
    if days > 366 * 3:
        return "year"
    if days > 366:
        return "quarter"
    if days > 120:
        return "month"
    if days > 14:
        return "week"
    return "day"


def _bucket(when: date, scale: str) -> tuple:
    """`(start, end)` of the bucket `when` falls in, for this scale."""
    if scale == "year":
        return date(when.year, 1, 1), date(when.year, 12, 31)
    if scale == "quarter":
        first = 3 * ((when.month - 1) // 3) + 1
        start = date(when.year, first, 1)
        end_month = first + 2
        end = (date(when.year + 1, 1, 1) if end_month == 12
               else date(when.year, end_month + 1, 1)) - timedelta(days=1)
        return start, end
    if scale == "month":
        start = date(when.year, when.month, 1)
        end = ((date(when.year + 1, 1, 1) if when.month == 12
                else date(when.year, when.month + 1, 1)) - timedelta(days=1))
        return start, end
    if scale == "week":
        start = when - timedelta(days=when.weekday())
        return start, start + timedelta(days=6)
    return when, when


def label_for(start: date, end: date, scale: str) -> str:
    r"""What a bar says, in the shortest form that is unambiguous.

    **The bar is a few characters wide.** `2019` for a year, `Q3 2019` for a
    quarter, `Mar 2019` for a month, a date for anything shorter - and never
    a range, because two dates do not fit and the tooltip carries the detail.
    """
    if scale == "year":
        return f"{start.year}"
    if scale == "quarter":
        return f"Q{(start.month - 1) // 3 + 1} {start.year}"
    if scale == "month":
        return f"{start:%b} {start.year}"
    if scale == "week":
        return f"{start.day} {start:%b}"
    return f"{start.day} {start:%b} {start.year}"


def bands(rows: Iterable[Any], *, max_bands: int = MAX_BANDS) -> tuple:
    r"""The strip: one `Band` per period that has anything in it.

    Oldest first, which is the direction time runs and the direction every
    other timeline anybody has seen runs in.

    **Empty when there is nothing to say**, and that is a decision: fewer than
    two periods means the band would be one full-width bar, which conveys
    nothing and costs a strip of screen that the results could have had.
    """
    dates = [found for found in
             (_to_date(getattr(row, "mtime_ns", 0)) for row in rows or ())
             if found is not None]
    if len(dates) < 2:
        return ()

    span = (max(dates) - min(dates)).days
    scale = scale_for(span)

    counted: dict = {}
    for when in dates:
        key = _bucket(when, scale)
        counted[key] = counted.get(key, 0) + 1

    if len(counted) < 2:
        return ()

    ordered = sorted(counted.items())
    if len(ordered) > max(2, int(max_bands)):
        # **Trimmed to the busiest, then put back in time order.** Dropping
        # the tail would hide the recent end on a corpus that starts in 1998,
        # and the periods worth clicking are the ones with something in them.
        ordered = sorted(sorted(ordered, key=lambda pair: -pair[1])[:max_bands])

    return tuple(
        Band(label_for(start, end, scale), count,
             start.isoformat(), end.isoformat())
        for (start, end), count in ordered
    )
