r"""The Life Timeline's queries are served by their indexes - pinned.

Layer: L4

**What this guards, and the number behind it.** A page of the timeline is one
small query per date source (`app/reports/timeline.py::branch_sql`), each
meant to *seek* into `idx_files_taken_at`, `idx_files_mtime` or
`idx_messages_sent` and stop after a page. On a 200,000-row index that is
0.6 ms a branch. Left to the planner alone, **on a database that has never
been analysed** (a fresh index, or any copy taken without its write-ahead
file), SQLite instead walks `idx_files_source_kind` - `source_kind = 'file'`
is an equality, so it looks selective, and is true of nearly every row -
fetches every row of the table and sorts them: 72 ms a branch, a hundred
times slower, and growing with the index rather than with the page. The
unary ``+`` on the non-date columns takes those indexes out of the running,
and this test is what notices if somebody "tidies" it away (checked: without
the ``+`` the never-analysed cases below fail).

Both states are checked - never analysed and analysed - because the plan can
differ between them, and an index goes through both.
"""

from __future__ import annotations

import datetime

import pytest

from app.reports import timeline as T
from app.reports.timeline import BRANCH_DATED, BRANCH_FILE, BRANCH_MAIL, Cursor, Period
from tests.fixtures.timeline_scale import build_timeline_scale_store

FILES = 20_000


def plan_of(store, sql: str, params: list) -> list[str]:
    return [row[3] for row in store.conn.execute("EXPLAIN QUERY PLAN " + sql, params)]


def problems_with(plan: list[str], want_index: str) -> list[str]:
    """What is wrong with a plan for a page query, in words - empty when nothing is."""
    found = []
    text = " | ".join(plan)
    if want_index not in text:
        found.append(f"does not seek {want_index}")
    if "SCAN" in text.replace("SCAN v", "").replace("SCAN VOLUMES", ""):
        found.append("scans a table")
    if "TEMP B-TREE FOR ORDER BY" in text:
        found.append("sorts the whole range")
    return found


@pytest.fixture(scope="module", params=["never analysed", "analysed"])
def store(request, tmp_path_factory):
    opened, _facts = build_timeline_scale_store(
        tmp_path_factory.mktemp("plans") / "i.db", FILES, dense=300,
        analyse=request.param == "analysed")
    yield opened
    opened.close()


PERIODS = [Period.month(2015, 6), Period.year(2012), Period.between(None, None),
           Period.between(None, datetime.date(2010, 1, 1))]
KINDS = ["everything", "photos", "videos", "documents", "code"]
CURSORS = [None, Cursor(1_434_000_000_000_000_000, BRANCH_DATED, 500),
           Cursor(1_434_000_000_000_000_000, BRANCH_FILE, 500),
           Cursor(1_434_000_000_000_000_000, BRANCH_MAIL, 500)]


@pytest.mark.parametrize("period", PERIODS, ids=["month", "year", "everything", "open start"])
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("cursor", CURSORS, ids=["first page", "after dated", "after file", "after mail"])
@pytest.mark.parametrize("branch, index", [(BRANCH_DATED, "idx_files_taken_at"),
                                            (BRANCH_FILE, "idx_files_mtime")])
def test_a_file_branch_seeks_its_date_index_and_needs_no_sort(store, branch, index, cursor, kind, period):
    sql, params = T.branch_sql(branch, period, cursor, kind, 201)
    assert problems_with(plan_of(store, sql, params), index) == []


@pytest.mark.parametrize("period", PERIODS, ids=["month", "year", "everything", "open start"])
@pytest.mark.parametrize("cursor", CURSORS, ids=["first page", "after dated", "after file", "after mail"])
def test_the_mail_branch_seeks_the_sent_date_and_needs_no_sort(store, cursor, period):
    sql, params = T.branch_sql(BRANCH_MAIL, period, cursor, "everything", 201)
    assert problems_with(plan_of(store, sql, params), "idx_messages_sent") == []


def test_the_overview_is_one_asked_for_table_scan_not_a_walk_of_the_date_index(store):
    r"""Measured: the index walk 3.1 s, the scan 0.25 s at 200,000 rows - see
    `timeline_overview`. `NOT INDEXED` keeps it that way whatever the statistics."""
    from pathlib import Path

    source = Path(T.__file__).read_text(encoding="utf-8")
    assert "FROM files AS f NOT INDEXED" in source


def test_the_checker_itself_would_notice_a_bad_plan(store):
    """A guard that has never rejected anything has not been shown to work."""
    bad = ("SELECT f.id FROM files f WHERE COALESCE(f.taken_at_ns, f.mtime_ns) >= ? "
           "ORDER BY COALESCE(f.taken_at_ns, f.mtime_ns), f.id LIMIT 201")
    found = problems_with(plan_of(store, bad, [0]), "idx_files_mtime")
    assert "scans a table" in found or "sorts the whole range" in found


def steps_of(store, work) -> int:
    """SQLite virtual-machine steps `work()` costs - the noise-free measure."""
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


def test_a_deep_page_seeks_to_its_cursor_instead_of_walking_to_it(store):
    r"""**The bug the plans above could not see.** A page fifteen years into a
    200,000-row index cost 2,095,300 VM steps against 22,000 for the first page,
    and every branch's plan was still "SEARCH ... USING INDEX" - the index was used,
    from the wrong end: the cursor's "strictly after" test is a row-value
    comparison SQLite cannot seek on, so the search began at the period's start and
    filtered its way forward to the cursor. The cursor's own date is now a lower bound
    as well (`_file_bounds`, `_mail_bounds`). Here at 20,000 rows, checked both ways
    round: the fix passes, and the old bounds fail it."""
    from app.reports.timeline import timeline_page

    period = Period.between(None, None)
    deep = Cursor(int(datetime.datetime(2018, 1, 1).timestamp()) * 10**9, BRANCH_FILE, 1)
    first = steps_of(store, lambda: timeline_page(store, period, connected={}))
    seeking = steps_of(store, lambda: timeline_page(store, period, cursor=deep, connected={}))
    assert seeking < 4 * first + 20_000, (first, seeking)


def test_the_deep_page_guard_would_notice_the_old_bounds(store, monkeypatch):
    """Put the bug back - the file branches' lower bound ignoring the cursor - and
    the guard above must fail. A guard never shown to fail is not shown to work."""
    from app.reports.timeline import timeline_page

    real = T._file_bounds

    def old_bounds(branch, period, cursor):
        where, params = real(branch, period, cursor)
        params[0] = max(period.start_ns, -T._FAR)          # the pre-fix lower bound
        return where, params

    monkeypatch.setattr(T, "_file_bounds", old_bounds)
    period = Period.between(None, None)
    deep = Cursor(int(datetime.datetime(2018, 1, 1).timestamp()) * 10**9, BRANCH_FILE, 1)
    first = steps_of(store, lambda: timeline_page(store, period, connected={}))
    walking = steps_of(store, lambda: timeline_page(store, period, cursor=deep, connected={}))
    assert walking >= 4 * first + 20_000, (first, walking)
