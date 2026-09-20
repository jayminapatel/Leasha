r"""Order 202626270602 (0n) section 4c - the Life Timeline at 200,000 rows.

Layer: L4

"a month with 4,000 photos paginates/clusters ... scrolling stays worker-fed"
is only true if a page costs what a page should. This builds the synthetic
index (`tests/fixtures/timeline_scale.py`) and measures it. **Synthetic, and
said so: this is not the owner's real index at `D:\Leasha\Data`, which cannot be
reached from here** - the row count and the shape (15 years of files, 14%
photographs, 12% on three catalogued drives, 10% mail dated by `sent_at`, one
month with 4,000 extra photographs) are plausible for the stated corpus, and the
exact ratios are assumptions.

**Two measures, because the machine is noisy.** Wall time is reported as the
minimum of several runs and its spread (2026-09-20, this project's machine,
Windows 11, 12 logical CPUs, CPython 3.12, SQLite 3.49.1, *with other engineers'
test suites running on it*):

    first page of a 4,000-photograph month     43-59 ms   (min 43)   87,100 VM steps
    first page of a quiet month                36-42 ms              24,300 steps
    first page of a 15-year range              44-58 ms              22,200 steps
    photographs only, dense month              75-136 ms             32,200 steps
    ONE PAGE FIFTEEN YEARS IN, before the fix  139-221 ms         2,095,300 steps
    the same page, after the fix               (see below)           95,500 steps
    the count for the year/month picker        1.06-1.40 s         8,888,300 steps
    walking all 5,587 entries of the dense month, 28 pages   1.08-1.31 s   3,053,800 steps

(A second run, with the machine heavily loaded, took the same queries 3-10x
longer - the picker's count 13 s, the dense month's first page 65-129 ms - and
**cost the same VM steps to the digit**, which is the point of measuring them.)

**VM steps are the noise-free number** (`sqlite3` progress handler, one tick per
100 instructions): they do not change with what else the machine is doing, so
the assertions below are on steps, with wall-time floors ~10x the loaded
measurement as a second net.

**The deep page was the bug this measurement found, and `EXPLAIN QUERY PLAN`
could not see it.** Its plan was a `SEARCH ... USING INDEX idx_files_mtime` on
every branch - correct - but the cursor's "strictly after" test is a row-value
comparison SQLite cannot seek on, so the search began at the *period's* start
and walked the index forward, filtering, until it reached the cursor: 2.1
million steps to serve page 400. The fix makes the cursor's own date a lower bound
too (`app/reports/timeline.py::_file_bounds`), so the index seeks straight to it.

**The count for the picker is a scan, on purpose** (`NOT INDEXED` - see
`timeline_overview`): walking `idx_files_mtime` and fetching each row took 3.1 s
here, the plain scan 0.25 s. It is 1 s under load, on a worker, cached until
something is indexed; it is not on any scrolling path. **Not measured: 20 million
rows** (the order's own upper aim for `files`) - a scan is linear, so expect
seconds-to-a-minute there, and that is the number that would justify a
maintained per-month count table if it ever mattered.
"""

from __future__ import annotations

import datetime
import statistics
import time

import pytest

from app.reports.timeline import (
    PAGE_SIZE, Cursor, Period, date_of_file, refresh_overview, timeline_overview, timeline_page,
)
from tests.fixtures.timeline_scale import DENSE_MONTH, build_timeline_scale_store

FILES = 200_000

#: VM steps, the noise-free measure. Each ceiling is ~4x the measured figure above.
FIRST_PAGE_STEPS = 400_000
#: **The regression this file exists for.** A deep page must cost what a first page
#: costs, not what the walk to it costs. Measured after the fix: see the run log
#: in the module docstring; before it, 2,095,300.
DEEP_STEPS = 400_000
OVERVIEW_STEPS = 30_000_000

#: Wall-time floors, milliseconds.
#: **Deliberately loose**: a loaded run measured the picker's count at 13 s where a
#: quiet one measured 1 s. Wall time only catches a change of order of magnitude;
#: the step ceilings above are the tight guard.
FLOOR_MS = {"page": 5_000, "deep page": 10_000, "overview": 90_000, "walk": 60_000}


def steps_of(store, work) -> int:
    """SQLite virtual-machine steps `work()` costs on this thread's connection."""
    ticks = [0]

    def tick() -> int:
        ticks[0] += 1
        return 0

    store.conn.set_progress_handler(tick, 100)
    try:
        work()
    finally:
        store.conn.set_progress_handler(None, 0)
    return ticks[0] * 100


def timed(work, runs: int = 3) -> tuple[float, float]:
    """`(minimum, spread)` in milliseconds over `runs` runs - the minimum is the
    honest number on a noisy machine, the spread says how noisy."""
    took = []
    for _ in range(runs):
        started = time.perf_counter()
        work()
        took.append((time.perf_counter() - started) * 1000)
    return min(took), max(took) - min(took)


@pytest.fixture(scope="module")
def scale(tmp_path_factory):
    store, facts = build_timeline_scale_store(tmp_path_factory.mktemp("timeline_scale") / "i.db", FILES)
    yield store, facts
    store.close()


DENSE = Period.month(*DENSE_MONTH)
EVERYTHING = Period.between(None, None)
DEEP = Cursor(int(datetime.datetime(2018, 1, 1).timestamp()) * 10**9, 1, 1)


@pytest.mark.slow
def test_the_fixture_has_the_shape_the_measurement_claims(scale) -> None:
    """A floor over a fixture that quietly lost its dense month measures nothing."""
    store, facts = scale
    assert store.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == FILES
    assert facts["mail"] == 20_000 and facts["dense"] == 4_000 and facts["volumes"] == 3
    overview = timeline_overview(store)
    counts = {(y, m): n for y, m, n in overview.months}
    assert counts[DENSE_MONTH] >= 4_000, "the dense month is not dense"
    assert 100 <= len(overview.months) <= 200
    assert overview.by_sent_date == 20_000 and overview.by_camera > 5_000
    assert overview.by_folder_guess > 500 and overview.photos_by_file_date_only > 500


@pytest.mark.slow
def test_a_first_page_costs_a_page_not_a_month(scale) -> None:
    store, _facts = scale
    for label, period in (("the dense month", DENSE), ("a quiet month", Period.month(2008, 1)),
                          ("fifteen years", Period.between(datetime.date(2005, 1, 1),
                                                           datetime.date(2019, 12, 31))),
                          ("everything", EVERYTHING)):
        cost = steps_of(store, lambda: timeline_page(store, period, connected={}))
        assert cost < FIRST_PAGE_STEPS, f"{label}: {cost:,} steps for one page"


@pytest.mark.slow
def test_a_page_fifteen_years_in_costs_what_the_first_page_does(scale) -> None:
    r"""The bug the measurement found: 2,095,300 steps before the fix."""
    store, _facts = scale
    first = steps_of(store, lambda: timeline_page(store, EVERYTHING, connected={}))
    deep = steps_of(store, lambda: timeline_page(store, EVERYTHING, cursor=DEEP, connected={}))
    assert deep < DEEP_STEPS, f"a deep page cost {deep:,} steps (a first page: {first:,})"
    assert deep < 8 * first + 50_000, "paging deeper is costing more the further in it goes"


@pytest.mark.slow
def test_the_whole_dense_month_walks_out_once_each_a_page_at_a_time(scale) -> None:
    store, _facts = scale
    expected = {(y, m): n for y, m, n in timeline_overview(store).months}[DENSE_MONTH]
    seen, cursor, pages = set(), None, 0
    while True:
        page = timeline_page(store, DENSE, cursor=cursor, connected={}, fold=False)
        for entry in page.entries:
            assert entry.file_id not in seen
            seen.add(entry.file_id)
        pages += 1
        cursor = page.cursor
        if cursor is None:
            break
    assert len(seen) == expected, "the picker's count and the pages disagree"
    assert pages == -(-expected // PAGE_SIZE)


@pytest.mark.slow
def test_a_burst_heavy_month_folds_to_far_fewer_rows(scale) -> None:
    store, _facts = scale
    page = timeline_page(store, DENSE, kind="photos", connected={})
    assert page.covered == PAGE_SIZE
    assert len(page.items) < page.covered * 0.6, "burst folding did not fold a month of bursts"


@pytest.mark.slow
def test_the_picker_count_is_one_scan_and_is_cached_until_something_is_indexed(scale) -> None:
    store, _facts = scale
    cost = steps_of(store, lambda: timeline_overview(store))
    assert cost < OVERVIEW_STEPS, f"the overview cost {cost:,} steps"
    first = timeline_overview(store)
    assert refresh_overview(store, "everything", first.generated_at) is None      # nothing new: no recount
    assert refresh_overview(store, "everything", (first.generated_at or 0) - 1) is not None
    assert date_of_file(store, 12345) is not None


@pytest.mark.slow
def test_wall_time_floors_and_the_measurement_report(scale, capsys) -> None:
    store, _facts = scale
    rows = []
    for label, floor_key, work in (
            ("first page, dense month", "page", lambda: timeline_page(store, DENSE, connected={})),
            ("first page, quiet month", "page",
             lambda: timeline_page(store, Period.month(2008, 1), connected={})),
            ("page fifteen years in", "deep page",
             lambda: timeline_page(store, EVERYTHING, cursor=DEEP, connected={})),
            ("the picker's count", "overview", lambda: timeline_overview(store))):
        best, spread = timed(work, 3 if floor_key != "overview" else 2)
        rows.append((label, best, spread, steps_of(store, work)))
        assert best < FLOOR_MS[floor_key], f"{label} took {best:.0f} ms (floor {FLOOR_MS[floor_key]})"
    with capsys.disabled():
        print("\ntimeline at 200,000 rows (min ms, spread ms, VM steps):")
        for label, best, spread, cost in rows:
            print(f"  {label:28s} {best:8.1f} {spread:8.1f} {cost:>12,}")
        print(f"  (median of one page: {statistics.median([timed(lambda: timeline_page(store, DENSE, connected={}), 1)[0] for _ in range(5)]):.1f} ms)")
