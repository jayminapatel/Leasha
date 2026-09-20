"""What the query planner actually does, not what the SQL looks like.

Layer: L1 and L4

Every claim in this file was wrong once. `keyword.py` carried a comment saying
`source_kind` was "already indexed, so this costs nothing" - there was no such
index, and clicking the Mail chip full-scanned `files` on every keystroke. The
comment had been true-looking for months because nobody asked the database.

So these tests ask the database. `EXPLAIN QUERY PLAN` against the **real**
query builder, never a hand-written approximation of it - an approximation was
what first suggested `idx_chunks_first` was being used, and it was not.

A plan test is worth having only because a plan can change underneath working
code: add a column, load different data, and SQLite quietly picks a scan. The
rows still come back correct, just slowly, which is the failure mode no
correctness test can see.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.search.keyword import search
from app.search.query import parse_query
from app.storage.migrations import CURRENT_VERSION, apply_migrations, read_version
from app.storage.sqlite_store import SqliteStore

#: Indexes schema v5 adds. Both are load-bearing and both are measured.
V5_INDEXES = ("idx_files_source_kind", "idx_files_mtime")


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def _add(store, *, path, kind="file", mtime, sender="", subject="", to="[]",
         chunks=3):
    file_id = store.upsert_file(
        path=path, size_bytes=1000, mtime_ns=mtime, source_kind=kind)
    store.replace_chunks(
        file_id, [{"text": f"body of {path} passage {n}"} for n in range(chunks)])
    if sender or subject:
        store.conn.execute(
            "INSERT INTO messages (file_id, subject, sender, recipients, sent_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (file_id, subject, sender, to, mtime // 1_000_000_000))
    return file_id


@pytest.fixture
def populated(store):
    """Enough rows, and enough variety, that a scan is not accidentally cheap."""
    for n in range(400):
        _add(store, path=f"/docs/dir{n % 20}/report{n}.pdf", kind="file",
             mtime=(n * 7919) % 3_000_000)
    for n in range(400):
        # **Deliberately mixed case.** The `LOWER()` removal is only safe
        # because LIKE folds case itself, and a lowercase fixture would agree
        # with the broken version just as readily as the fixed one.
        _add(store, path=f"/mail/msg{n}.msg", kind="pst_message",
             mtime=(n * 104_729) % 3_000_000,
             sender=f"User{n % 40}@Acme.COM", subject=f"Quarterly Report {n}",
             to=f'["Team{n % 15}@Acme.COM"]')
    store.conn.execute("ANALYZE")
    return store


def _plan(store, sql: str, params) -> str:
    rows = store.conn.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return "\n".join(row["detail"] for row in rows)


def _plan_for(store, query: str) -> str:
    """The plan for the SQL `keyword.search` really runs for this query.

    Goes through `_filter_sql` so that changing the builder changes the plan
    under test. Writing the SQL out by hand here would test the test.

    **Two statements since work order 0f §3a's third clause, not one.**
    `_filter_only` now runs a `taken_at_ns IS NULL` query against
    `idx_files_mtime` and a `taken_at_ns IS NOT NULL` query against
    `idx_files_taken_at`, merging the two in Python rather than sorting a
    `COALESCE` expression SQLite cannot index - see `_filter_only`'s own
    docstring and `app.storage.filters.merge_by_date`. Both plans are
    returned, concatenated, so a single `in plan` assertion still reads
    naturally and a regression in either query still fails this test.
    """
    from app.search.keyword import _filter_only, _filter_sql

    parsed = parse_query(query)
    where, params = _filter_sql(parsed)
    captured: list = []

    class Recorder:
        def __init__(self, conn):
            self.conn = _Capture(conn, captured)

    _filter_only(Recorder(store.conn), where, params, 20)
    return "\n".join(_plan(store, sql, sql_params) for sql, sql_params in captured)


class _Capture:
    """Passes SQL through, keeping a copy of every statement run so the test
    can explain each one - `_filter_only` runs two now, not one."""

    def __init__(self, conn, sink):
        self._conn = conn
        self._sink = sink

    def execute(self, sql, params=()):
        self._sink.append((sql, list(params)))
        return self._conn.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._conn, name)


# --- the migration ----------------------------------------------------------

def test_v5_creates_both_indexes(store):
    names = {row[0] for row in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert set(V5_INDEXES) <= names
    assert read_version(store.conn) >= 5


def test_v5_applies_to_a_database_created_before_it(tmp_path):
    """The path that matters: an existing 100GB index, not a fresh one.

    A migration is only ever run once for real, on data somebody cannot afford
    to lose. Creating at v5 and asserting v5 tests nothing about that.
    """
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)

    # Rewind to exactly what the previous build left behind.
    conn.execute("UPDATE schema_version SET version = 4 WHERE id = 1")
    for name in V5_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {name}")
    conn.executemany(
        "INSERT INTO files (path, parent_dir, ext, size_bytes, mtime_ns, status, "
        "source_kind) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(f"/x{n}.pdf", "/", "pdf", 1, n, "INDEXED", "file") for n in range(50)])
    conn.commit()
    assert read_version(conn) == 4

    apply_migrations(conn)

    # `CURRENT_VERSION`, not the literal 5. This asserted `== 5` and broke the
    # moment v6 was added - the claim is "a database from before this migration
    # gets it and keeps its rows", which has nothing to do with the number.
    assert read_version(conn) == CURRENT_VERSION
    names = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert set(V5_INDEXES) <= names
    # Additive: nothing was rewritten, so nothing was lost.
    assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 50
    conn.close()


def test_v5_is_rerunnable(store):
    """`IF NOT EXISTS` throughout, so a half-finished run can be repeated."""
    from app.storage.migrations import _v5_missing_indexes

    _v5_missing_indexes(store.conn)
    _v5_missing_indexes(store.conn)


def test_no_index_exists_that_nothing_uses(store):
    """`idx_chunks_first` was written, measured, and found never to be chosen.

    It is absent on purpose. This test exists so that re-adding it requires
    deleting a test that says why it went - the plan below is the reason, and
    an unused index is a write cost on every document indexed.
    """
    names = {row[0] for row in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "idx_chunks_first" not in names


# --- the plans ---------------------------------------------------------------

def test_filter_only_browse_neither_scans_nor_sorts(populated):
    """P6: `WHERE c.ordinal = 0 ORDER BY f.mtime_ns DESC`.

    Measured at 20,000 files / 120,000 chunks: 6.30ms before, 0.04ms after.
    The temp B-tree is the tell - it means every matching row was materialised
    and sorted before `LIMIT 20` threw all but twenty away.

    **Now two queries, work order 0f section 3a's third clause** - `_filter_only`
    splits on `taken_at_ns IS [NOT] NULL` rather than sort a `COALESCE`
    expression no index can serve (see its own docstring and
    `app.storage.filters.merge_by_date` for the 0.011ms-vs-39.5ms
    measurement). `populated` carries no photographs, so the second query
    matches nothing - but it must still *plan* as an index seek, not a scan,
    which is exactly what would silently regress if the split were ever
    collapsed back into one `ORDER BY COALESCE(...)` statement.

    **2026-09-20: each half walks a bounded window first**, so every statement
    that runs for a common type is a walk of an index in order, never a sort.
    The work done at scale is pinned by the tests below.
    """
    plan = _plan_for(populated, "type:pdf")

    assert "idx_files_mtime" in plan, plan
    assert "idx_files_taken_at" in plan, plan
    assert "USE TEMP B-TREE FOR ORDER BY" not in plan, plan
    for line in plan.splitlines():
        assert not (line.startswith("SCAN files") and "USING" not in line), plan


# --- the filter-only browse at scale, as work done rather than seconds -------
#
# MEASURED 2026-09-20, 200,000 files (50% pdf, 15% txt, 4% xlsx, 1% dwg, the
# rest docx), `_filter_only` limit 100, best of 5, on a machine other work was
# running on (so read the ratios, not the milliseconds):
#
#     type          before (ext index, sort)     after (bounded walk first)
#     pdf                     80 ms (up to 265)      1.4 ms
#     txt                     76 ms                  8 ms
#     xlsx                    29 ms                 39 ms  (falls back: 4% is
#                                                          past the widest window)
#     dwg                     18 ms                 24 ms  (falls back)
#     no matches              0.1 ms                 0.03 ms
#
# Forcing `INDEXED BY idx_files_mtime` instead gives 0.6 ms for pdf but 2.9
# SECONDS for a type with no matches, which is why neither fixed plan is right
# and the query adapts. Counting VM steps below rather than timing, because
# timing is what load changes and steps are what the query does.


@pytest.fixture(scope="module")
def browse_table(tmp_path_factory):
    """30,000 files, half pdf, with unique mtimes and a few photographs.

    Rows go in with SQL directly and the FTS triggers off: this fixture is about
    `files` and the first chunk of each, and 30,000 trigger firings would be the
    slowest thing in the file.
    """
    import random

    path = tmp_path_factory.mktemp("browse") / "index.db"
    with SqliteStore(path) as opened:
        conn = opened.conn
        for name in [row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'")]:
            conn.execute(f"DROP TRIGGER {name}")
        rng = random.Random(7)
        kinds = ["pdf"] * 50 + ["txt"] * 15 + ["xlsx"] * 4 + ["dwg"] * 1 + ["docx"] * 30
        mtimes = rng.sample(range(1_000_000, 900_000_000), 30_000)
        rows = []
        for n, mtime in enumerate(mtimes):
            ext = "rare" if n < 5 else rng.choice(kinds)
            # One file in twenty is a photograph whose shot date is not its mtime.
            shot = rng.randrange(1_000_000, 900_000_000) if n % 20 == 1 else None
            rows.append((f"/d/{n % 300}/f{n}.{ext}", f"/d/{n % 300}", ext, 1000,
                         mtime, "INDEXED", "file", shot))
        conn.executemany(
            "INSERT INTO files (path, parent_dir, ext, size_bytes, mtime_ns, "
            "status, source_kind, taken_at_ns) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.execute("INSERT INTO chunks (file_id, ordinal, text) "
                     "SELECT id, 0, 'first' FROM files")
        conn.execute("INSERT INTO chunks (file_id, ordinal, text) "
                     "SELECT id, 1, 'second' FROM files")
        conn.execute("ANALYZE")
        conn.commit()
        yield opened


def _filter_sql_for(query: str):
    from app.search.keyword import _filter_sql

    return _filter_sql(parse_query(query))


def _legacy_browse(store, query: str, limit: int):
    """What `_filter_only` returned before the window: the two plain statements
    and the merge, with SQLite left to plan them."""
    from app.search import keyword
    from app.storage.filters import merge_by_date

    where, params = _filter_sql_for(query)
    return merge_by_date(
        keyword._plain_filter_only(store, where, params, limit, photos=False),
        keyword._plain_filter_only(store, where, params, limit, photos=True),
        limit=limit, newest_first=True)


def _vm_steps(store, call):
    """Run `call()` counting SQLite virtual-machine instructions - the work the
    query did, which unlike seconds does not depend on what else is running."""
    steps = [0]

    def tick() -> int:
        steps[0] += 1
        return 0

    store.conn.set_progress_handler(tick, 1)
    try:
        result = call()
    finally:
        store.conn.set_progress_handler(None, 0)
    return steps[0], result


@pytest.mark.parametrize("query", [
    "type:pdf", "type:txt", "type:xlsx", "type:dwg", "type:docx", "type:rare",
    "type:nothing", "type:pdf,txt", "type:pdf after:1970-01-10"])
def test_the_bounded_browse_returns_exactly_what_the_plain_one_did(browse_table, query):
    """Speed must not change the answer: same files, same order, photographs
    (which sort by shot date) included."""
    from app.search.keyword import _filter_only

    where, params = _filter_sql_for(query)
    got = _filter_only(browse_table, where, params, 100)
    expected = _legacy_browse(browse_table, query, 100)

    assert [r["file_id"] for r in got] == [r["file_id"] for r in expected], query


def test_a_common_type_browse_visits_a_bounded_number_of_files(browse_table):
    """`type:pdf` is half the files. It used to walk the ext index, fetch every
    pdf's row and sort them all before `LIMIT 100` discarded nearly everything;
    now it reads the newest few hundred files and stops."""
    from app.search.keyword import _filter_only

    where, params = _filter_sql_for("type:pdf")
    new_steps, got = _vm_steps(
        browse_table, lambda: _filter_only(browse_table, where, params, 100))
    old_steps, _ = _vm_steps(
        browse_table, lambda: _legacy_browse(browse_table, "type:pdf", 100))

    assert len(got) == 100
    # Measured 16,800 against 216,000 steps (13x) at 30,000 files; the gap widens
    # with the table, because the new count does not depend on its size.
    assert new_steps * 8 < old_steps, (new_steps, old_steps)


def test_a_type_with_no_matches_browse_is_still_cheap(browse_table):
    """The reason `INDEXED BY idx_files_mtime` is not the fix: walking the
    newest-first index for something that is not there visits every file
    (2.9 s at 200,000). A type nobody has must stay a seek on the ext index."""
    from app.search.keyword import _filter_only

    where, params = _filter_sql_for("type:nothing")
    steps, got = _vm_steps(
        browse_table, lambda: _filter_only(browse_table, where, params, 100))

    assert got == []
    assert steps < 1_000, steps


def test_a_type_with_fewer_files_than_the_limit_lists_them_all(browse_table):
    from app.search.keyword import _filter_only

    where, params = _filter_sql_for("type:rare")
    got = _filter_only(browse_table, where, params, 100)

    assert len(got) == 5
    dates = [int(r["taken_at_ns"] or r["mtime_ns"]) for r in got]
    assert dates == sorted(dates, reverse=True)


def test_a_rare_type_falls_back_and_still_fills_the_limit(browse_table):
    """`dwg` is 1% of the files: 100 of them are not inside any window the walk
    will try, so the plain statement must take over and deliver the full page."""
    from app.search.keyword import _filter_only

    where, params = _filter_sql_for("type:dwg")
    got = _filter_only(browse_table, where, params, 100)

    assert len(got) == 100, len(got)


@pytest.mark.parametrize("scope", ["mail", "documents"])
def test_scope_chip_uses_the_source_kind_index(populated, scope):
    """P3: the comment claimed this index existed. It did not.

    The query is `file_ids_matching`'s, built by the real `_filter_sql`. That
    is the one that matters: the vector side cannot join to `files`, so every
    scoped search runs it to learn which file ids are eligible, and there is no
    `ORDER BY` competing for the index.

    The two scopes get different plans, and both are recorded so that nobody
    later reads the `mail` result as covering both:

    * `mail` is `IN`, so it **seeks** - `SEARCH f USING COVERING INDEX`.
    * `documents` is `NOT IN`, which can never seek, so it **scans**. It scans
      the covering index rather than the table, which is the whole win: the
      rows are narrow and the table is never touched.
    """
    from app.search.keyword import _filter_sql

    where, params = _filter_sql(parse_query("").scoped(scope))
    plan = _plan(populated, f"SELECT id FROM files f WHERE 1=1{where}", params)

    assert "idx_files_source_kind" in plan, plan
    assert "COVERING INDEX" in plan, plan
    if scope == "mail":
        assert plan.startswith("SEARCH"), plan


def test_date_filter_uses_the_mtime_index(populated):
    plan = _plan(
        populated,
        "SELECT id FROM files WHERE mtime_ns >= ? LIMIT 20", (1_000_000,))

    assert "idx_files_mtime" in plan, plan
    assert "SCAN files" not in plan, plan


def test_sender_filter_scans_a_covering_index_not_the_table(populated):
    """P5: a leading `%` can never seek, so the goal is the cheapest scan.

    `sender LIKE '%x%'` cannot use an index to *find* rows - there is no prefix
    to seek to - but `idx_messages_sender` still covers it, so the scan reads
    the index rather than every message row. The subquery is also materialised
    once (`LIST SUBQUERY` / `REUSE LIST SUBQUERY`) rather than re-run per file.
    """
    plan = _plan(
        populated,
        "SELECT id FROM files WHERE id IN "
        "(SELECT file_id FROM messages WHERE sender LIKE ?) LIMIT 20",
        ("%user1@%",))

    assert "COVERING INDEX idx_messages_sender" in plan, plan
    assert "SCAN messages\n" not in plan + "\n", plan


# --- what the LOWER() removal must not change --------------------------------

@pytest.mark.parametrize("query, expected_some", [
    ("from:user1@acme.com", True),
    ("from:USER1@ACME.COM", True),
    ("from:User1@Acme.com", True),
    ("subject:quarterly", True),
    ("subject:QUARTERLY", True),
    ("path:/MAIL/", True),
    ("path:/mail/", True),
    ("to:team1@acme.com", True),
    ("from:nobody@nowhere.test", False),
])
def test_filters_stay_case_insensitive_without_lower(populated, query,
                                                     expected_some):
    """The fixture is mixed-case; the queries are not. Both must still match.

    This is the whole safety argument for removing `LOWER()`. If SQLite ever
    stopped folding case in LIKE, `from:dave` would quietly stop finding
    `Dave@...` - correct-looking, empty, and blamed on the index.
    """
    rows = search(populated, parse_query(query), limit=20)

    assert bool(rows) is expected_some, f"{query} returned {len(rows)} rows"


def test_like_case_insensitivity_is_not_switched_off(store):
    """Pins the assumption itself, so the reason fails before the callers do.

    `PRAGMA case_sensitive_like = ON` is a connection-level switch. Nothing
    sets it today; if anything ever does, this fails with an explanation
    instead of five filters silently returning nothing.
    """
    row = store.conn.execute(
        "SELECT 'ABC' LIKE '%b%' AS folded").fetchone()

    assert row["folded"] == 1, (
        "SQLite LIKE is no longer folding case. Every `from:`, `to:`, "
        "`subject:`, `path:` and `name:` filter in keyword.py depends on it - "
        "they dropped LOWER() because LIKE did the folding. Restore LOWER() "
        "on those predicates, or stop turning case_sensitive_like on.")


def test_removing_lower_did_not_change_which_rows_match(populated):
    """The old and new predicates, side by side, on the same data.

    2.49x faster is only worth having if it is the same answer. Measured at
    60,000 messages: 11.54ms with `LOWER()`, 4.64ms without, 1,650 rows both
    ways.
    """
    old = populated.conn.execute(
        "SELECT file_id FROM messages WHERE LOWER(sender) LIKE ?",
        ("%user1@%",)).fetchall()
    new = populated.conn.execute(
        "SELECT file_id FROM messages WHERE sender LIKE ?",
        ("%user1@%",)).fetchall()

    assert [r[0] for r in old] == [r[0] for r in new]
    assert old, "fixture matched nothing - the comparison proved nothing"
