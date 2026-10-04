r"""The Mail tab's list walks the date index and stops at its limit.

Layer: L1

Measured 2026-10-04 with `tools/fts_scale_bench.py` on 35,000 messages: the
newest 500 took 583 ms, because `ORDER BY m.sent_at IS NULL, m.sent_at DESC`
cannot be read from `idx_messages_sent` - every message was read and sorted to
keep 500. The order is unchanged: dated messages by date, ties by `file_id`,
undated ones last, in both directions.
"""

from __future__ import annotations

import pytest

from app.storage.sqlite_store import SqliteStore


@pytest.fixture()
def mailbox(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        with store.write() as conn:
            for file_id in range(1, 61):
                conn.execute(
                    "INSERT INTO files (id, path, parent_dir, ext, size_bytes, mtime_ns, "
                    "status, source_kind) VALUES (?, ?, 'D:\\Mail', 'msg', 1, 1, 'INDEXED', "
                    "'pst_message')", (file_id, rf"D:\Mail\a.pst::{file_id}"))
                # Every seventh undated, and dates repeat so ties need file_id.
                sent = None if file_id % 7 == 0 else 1_700_000_000 + (file_id % 11) * 60
                conn.execute(
                    "INSERT INTO messages (file_id, subject, sender, sent_at) "
                    "VALUES (?, ?, 'a@example.com', ?)", (file_id, f"s{file_id}", sent))
        yield store


def expected(store, newest: bool) -> list[int]:
    rows = store.conn.execute("SELECT file_id, sent_at FROM messages").fetchall()
    sign = -1 if newest else 1
    return [r[0] for r in sorted(
        rows, key=lambda r: (r[1] is None, sign * (r[1] or 0), sign * r[0]))]


@pytest.mark.parametrize("sort", ["", "oldest"])
@pytest.mark.parametrize("limit", [5, 50, 55, 200])
def test_the_order_is_the_order_it_always_was(mailbox, sort, limit):
    got = [r["file_id"] for r in mailbox.browse_messages(sort=sort, limit=limit)]
    assert got == expected(mailbox, newest=sort != "oldest")[:limit]


def test_the_newest_page_walks_the_index_rather_than_sorting(mailbox):
    seen: list[str] = []
    mailbox.conn.set_trace_callback(seen.append)
    try:
        mailbox.browse_messages(limit=10)
    finally:
        mailbox.conn.set_trace_callback(None)
    selects = [s for s in seen if s.lstrip().upper().startswith("SELECT")]
    assert selects
    plan = " ".join(
        str(row[-1]) for row in mailbox.conn.execute("EXPLAIN QUERY PLAN " + selects[0]))
    assert "idx_messages_sent" in plan
    assert "TEMP B-TREE" not in plan, plan
