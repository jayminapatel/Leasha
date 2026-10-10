"""Schema v36 (storage review S4, 2026-10-10): keyword-only passages are counted from an index.

Layer: L1

`keyword_only_count` (`stats()`, `vector_coverage()`) was a full scan of
`chunks`, text and all - 6.5M rows on the owner's index. A partial index over
`embedded = 2` makes it read only those rows. The plan is pinned so a reworded
query that the planner can no longer match to the index fails here.
"""

from __future__ import annotations

import sqlite3

from app.storage.migrations import CURRENT_VERSION, _write_version
from app.storage.sqlite_store import SqliteStore


def _plan(store, sql: str) -> str:
    return " ".join(str(row[3]) for row in store.conn.execute("EXPLAIN QUERY PLAN " + sql))


def _fill(store) -> list[int]:
    ids: list[int] = []
    for number in range(6):
        file_id = store.upsert_file(path=f"/sheet{number}.xlsx", size_bytes=1, mtime_ns=1)
        ids += store.replace_chunks(file_id, [{"text": f"row {n} of sheet {number}"}
                                              for n in range(5)])
    return ids


def test_the_count_reads_the_partial_index_not_the_table(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        _fill(store)
        plan = _plan(store, f"SELECT COUNT(*) AS n FROM chunks WHERE embedded = {store.KEYWORD_ONLY}")
        assert "idx_chunks_keyword_only" in plan, plan


def test_the_count_is_right_through_marking_and_resetting(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        ids = _fill(store)
        assert store.keyword_only_count() == 0
        store.mark_keyword_only(ids[:7])
        assert store.keyword_only_count() == 7
        assert store.vector_coverage(0)["chunks_keyword_only"] == 7
        assert store.stats()["chunks_keyword_only"] == 7
        assert store.reset_keyword_only() == 7
        assert store.keyword_only_count() == 0


def test_an_index_made_by_the_previous_build_gains_it(tmp_path):
    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        ids = _fill(store)
        store.mark_keyword_only(ids[:3])
    with sqlite3.connect(db) as conn:                   # the way v35 left it
        conn.execute("DROP INDEX idx_chunks_keyword_only")
        _write_version(conn, 35)
    with SqliteStore(db) as store:
        assert store.schema_version == CURRENT_VERSION
        assert store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'idx_chunks_keyword_only'").fetchone()
        assert store.keyword_only_count() == 3
