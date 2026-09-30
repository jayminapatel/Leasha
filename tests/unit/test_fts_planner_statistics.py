r"""FTS5 must reach its own rows by key, however large the index is.

Layer: L1. Order 0z (`WORKORDER-robust-indexing-and-status.md`), "writing
measured", 2026-09-30.

Writing mail got slower with every message already in the index. SQLite's own
statement profile showed where: `DELETE FROM chunks_fts_data WHERE id>=? AND
id<=?`, the statement FTS5 runs to remove a piece of its index it has merged
away, took 0.1 ms a call at 2,000 messages, 2.4 ms at 10,000 and 13.9 ms at
20,000. `EXPLAIN QUERY PLAN` said why: `SCAN`. The migrations run `ANALYZE`
on a new, empty database, which records "`chunks_fts_data` has 2 rows", and a
planner told that reads the whole table rather than seek into it.

So these tests ask the database, as `test_query_plans.py` does: the plan of
the statement FTS5 really runs, on the connection that really runs it, and the
number of steps SQLite takes to write a message at two sizes of index.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from app.storage.sqlite_store import SqliteStore

#: The statement FTS5 prepares to remove a segment (`fts5DataDelete`), as
#: SQLite's statement trace shows it.
REMOVE_SEGMENT = "DELETE FROM 'main'.'{}_data' WHERE id>=? AND id<=?"
INDEXES = ("chunks_fts", "messages_fts", "files_fts")
ONE_SEGMENT = (1 << 37, (2 << 37) - 1)


def _plan(conn, index: str) -> str:
    rows = conn.execute("EXPLAIN QUERY PLAN " + REMOVE_SEGMENT.format(index), ONE_SEGMENT)
    return " / ".join(str(tuple(row)[3]) for row in rows)


def _shadow_rows(conn) -> list:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_stat1'").fetchone() is None:
        return []
    return [tuple(row) for row in conn.execute(
        "SELECT tbl, stat FROM sqlite_stat1 WHERE tbl LIKE '%\\_fts\\_%' ESCAPE '\\'")]


def _write(store: SqliteStore, first: int, count: int) -> None:
    """`count` messages, grouped the way the indexer groups them."""
    for start in range(first, first + count, 50):
        with store.batch():
            for n in range(start, min(start + 50, first + count)):
                with store.batch():
                    file_id = store.upsert_file(
                        f"D:\\Mail\\a.pst#{n}", size_bytes=n, mtime_ns=1,
                        status="PARTIAL", source_kind="pst_message",
                        parent_dir="D:\\Mail", ext="pst")
                    store.replace_chunks(file_id, [
                        {"text": f"Subject: budget {n}\n\nquarterly harbour survey number{n} "
                                 f"word{n % 97} word{n % 89} word{n % 83}"}])
                    store.set_message(
                        file_id, subject=f"budget {n}", sender=f"user{n % 40}@acme.com",
                        recipients=f'["team{n % 15}@acme.com"]', sent_at=n)


def _stale(path) -> None:
    """Put back what `ANALYZE` on an empty database leaves behind."""
    raw = sqlite3.connect(path)
    raw.executemany("INSERT INTO sqlite_stat1(tbl, idx, stat) VALUES (?, NULL, '2')",
                    [(f"{index}_data",) for index in INDEXES])
    raw.commit()
    raw.close()


# --- the plan ---------------------------------------------------------------


def test_a_new_database_removes_a_segment_by_key(tmp_path):
    with SqliteStore(tmp_path / "t.db") as store:
        assert _shadow_rows(store.conn) == []
        for index in INDEXES:
            plan = _plan(store.conn, index)
            assert "SEARCH" in plan and "PRIMARY KEY" in plan, f"{index}: {plan}"


def test_the_row_counts_an_empty_database_was_left_with_make_it_read_the_whole_index(tmp_path):
    """The cause, asked of SQLite directly - and the check that this file's
    instrument can see it. If this stops failing the old way, SQLite's planner
    has changed and the fix may no longer be needed: measure before removing it."""
    path = tmp_path / "t.db"
    SqliteStore(path).connect().close()
    _stale(path)
    raw = sqlite3.connect(path)
    try:
        for index in INDEXES:
            assert _plan(raw, index).startswith("SCAN"), (
                "a table recorded as holding two rows is no longer read whole")
    finally:
        raw.close()


def test_an_index_that_has_the_stale_row_counts_loses_them_when_it_is_opened(tmp_path):
    path = tmp_path / "t.db"
    SqliteStore(path).connect().close()
    _stale(path)

    with SqliteStore(path) as store:                   # the first start on this build
        assert _shadow_rows(store.conn) == []
        # The connection that opened the store is the one the command line
        # indexes with: it must not keep what it read before the rows went.
        for index in INDEXES:
            assert "SEARCH" in _plan(store.conn, index), index
        seen: dict = {}

        def elsewhere() -> None:
            seen.update({index: _plan(store.conn, index) for index in INDEXES})

        worker = threading.Thread(target=elsewhere)
        worker.start()
        worker.join(10)
        assert seen and all("SEARCH" in plan for plan in seen.values()), seen
        # And the store still works.
        _write(store, 1, 3)
        assert store.search_bm25("harbour")


def test_refreshing_the_planner_leaves_no_row_counts_for_the_word_index(tmp_path):
    with SqliteStore(tmp_path / "t.db") as store:
        _write(store, 1, 60)
        store.conn.execute("ANALYZE")                  # as a migration or a fixture might
        assert _shadow_rows(store.conn), "ANALYZE recorded nothing, so this proves nothing"
        assert store.optimize_query_planner()
        assert _shadow_rows(store.conn) == []
        # The statistics of the ordinary tables are the planner's to keep.
        assert store.conn.execute(
            "SELECT COUNT(*) FROM sqlite_stat1 WHERE tbl = 'files'").fetchone()[0] > 0


# --- the cost ---------------------------------------------------------------


def _steps_to_write(store: SqliteStore, first: int, count: int) -> int:
    """How many steps SQLite's virtual machine takes to write `count` messages.

    Counted by SQLite's progress handler, in units of 20 steps. It counts the
    statements FTS5 runs on its own tables too, and it does not depend on how
    busy the machine is - which a timing here would."""
    ticks = 0

    def tick() -> int:
        nonlocal ticks
        ticks += 1
        return 0

    store.conn.set_progress_handler(tick, 20)
    try:
        _write(store, first, count)
    finally:
        store.conn.set_progress_handler(None, 0)
    return ticks


def _growth(store: SqliteStore) -> float:
    """Steps to write 200 messages into an index of 2,200, over the same into 200."""
    _write(store, 1, 200)
    small = _steps_to_write(store, 201, 200)
    _write(store, 401, 1800)
    large = _steps_to_write(store, 2201, 200)
    return large / small


@pytest.mark.parametrize("defer", [True, False])
def test_a_message_costs_the_same_to_write_into_a_large_index_as_a_small_one(tmp_path, defer):
    with SqliteStore(tmp_path / "t.db") as store:
        store.defer_fts = defer
        growth = _growth(store)
    # Measured 2026-09-30: 1.02 with the index written once a batch, 1.00 row
    # by row; 1.54 on a database with the stale row counts (the test below).
    assert growth < 1.15, (
        f"writing 200 messages took {growth:.2f} times the steps once the index "
        "held 2,200 messages instead of 200")


def test_with_the_stale_row_counts_it_does_not(tmp_path):
    """The same measurement on a database left as they used to be: the
    instrument sees the fault this file is about."""
    path = tmp_path / "t.db"
    SqliteStore(path).connect().close()
    _stale(path)
    store = SqliteStore(path)
    # Opened without the repair and written row by row through the triggers,
    # as every build before this one opened and wrote it.
    store._forget_fts_statistics = lambda conn: 0          # type: ignore[method-assign]  # noqa: SLF001
    store.defer_fts = False
    store.connect()
    try:
        assert _plan(store.conn, "chunks_fts").startswith("SCAN")
        growth = _growth(store)
    finally:
        store.close()
    # Steps undercount it - what a scan costs is mostly the pages it reads,
    # and the time rose far faster than this - but they are the same on every
    # run, which a timing on a shared machine is not.
    assert growth > 1.3, (
        f"only {growth:.2f} times the steps: the stale row counts no longer "
        "make writing slow down, so this file's other tests prove less than they did")
