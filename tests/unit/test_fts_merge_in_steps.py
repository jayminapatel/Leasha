"""Storage review S6, 2026-10-10: the keyword-index merge lets go of the write lock.

Layer: L1

`optimize_fts` ran one `'optimize'` per index inside one write transaction -
minutes of write lock at ten million passages, with the window's writes
waiting behind it. It now merges in steps (`'merge'`, negative page count),
each in its own short transaction, until a step changes nothing, and leaves
the same single segment `'optimize'` leaves.
"""

from __future__ import annotations

from contextlib import contextmanager

from app.storage import sqlite_store as module
from app.storage.sqlite_store import SqliteStore


def _fill(store, files: int = 60) -> None:
    """One transaction per file, as a run writes: a segment each."""
    for number in range(files):
        file_id = store.upsert_file(path=f"/notes/{number}.txt", size_bytes=1, mtime_ns=1)
        store.replace_chunks(file_id, [
            {"text": f"Barnsley dairy note {number} line {n} " + "pasture " * (n % 7)}
            for n in range(40)])


def _optimize_changes(store) -> int:
    """Rows an `'optimize'` changes now: 1 or 0 when one segment is left."""
    with store.write() as conn:
        before = conn.total_changes
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
        return conn.total_changes - before


def _hits(store, word: str) -> int:
    return int(store.conn.execute(
        "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?", (word,)).fetchone()[0])


def _count_transactions(store, monkeypatch) -> list[int]:
    opened: list[int] = []
    real = SqliteStore.write

    @contextmanager
    def counting(self, **kwargs):
        opened.append(1)
        with real(self, **kwargs) as conn:
            yield conn

    monkeypatch.setattr(SqliteStore, "write", counting)
    return opened


def test_the_merge_ends_where_optimize_would(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "FTS_MERGE_PAGES", 4)     # many steps on a small index
    with SqliteStore(tmp_path / "index.db") as store:
        _fill(store)
        hits = _hits(store, "barnsley")
        opened = _count_transactions(store, monkeypatch)
        assert store.optimize_fts() is True
        # Many short transactions, not one long one: the lock was let go.
        assert len(opened) > 3
        monkeypatch.undo()
        assert _optimize_changes(store) < 2, "segments were left unmerged"
        assert _hits(store, "barnsley") == hits
        assert _hits(store, "pasture") > 0


def test_a_spent_budget_stops_between_steps_and_the_next_call_finishes(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "FTS_MERGE_PAGES", 4)
    with SqliteStore(tmp_path / "index.db") as store:
        _fill(store)
        opened = _count_transactions(store, monkeypatch)
        assert store.optimize_fts(time_budget_s=0) is True
        assert opened == []                               # nothing started
        assert store.optimize_fts() is True
        monkeypatch.undo()
        assert _optimize_changes(store) < 2


def test_an_already_merged_index_costs_one_step_per_table(tmp_path, monkeypatch):
    with SqliteStore(tmp_path / "index.db") as store:
        _fill(store, files=5)
        store.optimize_fts()
        opened = _count_transactions(store, monkeypatch)
        assert store.optimize_fts() is True
        tables = 1 + sum(1 for name in ("files_fts", "messages_fts") if store.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone())
        assert len(opened) == tables


def test_another_write_gets_in_between_steps(tmp_path, monkeypatch):
    """The point of the change: the window's writes are not held for the whole merge."""
    monkeypatch.setattr(module, "FTS_MERGE_PAGES", 4)
    with SqliteStore(tmp_path / "index.db") as store:
        _fill(store)
        real = SqliteStore.write
        interleaved: list[int] = []

        @contextmanager
        def write_then_let_another_in(self, **kwargs):
            with real(self, **kwargs) as conn:
                yield conn
            if not interleaved:
                interleaved.append(1)
                # Between two merge steps: the lock is free for anyone else.
                self.set_state("between", "1")

        monkeypatch.setattr(SqliteStore, "write", write_then_let_another_in)
        assert store.optimize_fts() is True
        monkeypatch.undo()
        assert store.get_state("between") == "1"
