r"""When the results cluster in time. Workspace §3d.

Layer: L5 presenter — Qt-free (see `app/ui/timeline.py`), moved here
unchanged from `docs/_superseded/timeline.py`: `bands()` reads only
`row.mtime_ns`, which `ResultRow` already carries.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from app.ui.timeline import NANOS, bands, label_for, scale_for


def _ns(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * NANOS)


def row(when: date):
    return SimpleNamespace(mtime_ns=_ns(when))


def test_scale_for_picks_the_coarsest_scale_that_still_fits():
    assert scale_for(4000) == "year"
    assert scale_for(400) == "quarter"
    assert scale_for(150) == "month"
    assert scale_for(60) == "week"
    assert scale_for(3) == "day"


def test_label_for_each_scale():
    start = date(2019, 7, 15)
    assert label_for(start, start, "year") == "2019"
    assert label_for(start, start, "quarter") == "Q3 2019"
    assert label_for(start, start, "month") == "Jul 2019"
    assert label_for(start, start, "week") == "15 Jul"
    assert label_for(start, start, "day") == "15 Jul 2019"


def test_bands_of_nothing_or_one_date_is_empty():
    """§3d: a single bar conveys nothing, so it is not drawn at all."""
    assert bands([]) == ()
    assert bands([row(date(2020, 1, 1))]) == ()


def test_bands_of_a_wide_spread_are_ordered_oldest_first():
    rows = [row(date(2019, 1, 5)), row(date(2022, 6, 1)), row(date(2019, 1, 6))]
    found = bands(rows)
    assert len(found) >= 2
    assert found[0].after < found[-1].after


def test_bands_count_hits_per_bucket():
    rows = [row(date(2020, 1, 1)), row(date(2020, 1, 2)), row(date(2023, 1, 1))]
    found = bands(rows)
    counts = {band.label: band.count for band in found}
    assert sum(counts.values()) == 3


def test_a_clicked_bands_filter_text_uses_the_existing_operators():
    rows = [row(date(2019, 1, 1)), row(date(2019, 6, 1))]
    found = bands(rows)
    assert found  # a span within one year still yields something
    for band in found:
        assert band.filter_text == f"after:{band.after} before:{band.before}"


def test_bands_never_raises_on_a_bad_timestamp():
    rows = [SimpleNamespace(mtime_ns="not a number"), row(date(2020, 1, 1)),
           row(date(2021, 1, 1))]
    assert bands(rows) is not None


def test_bands_ignores_the_zero_unknown_timestamp():
    rows = [SimpleNamespace(mtime_ns=0), row(date(2020, 1, 1)), row(date(2021, 1, 1))]
    found = bands(rows)
    assert sum(band.count for band in found) == 2
