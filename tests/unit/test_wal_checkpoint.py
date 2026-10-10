"""Storage review S5, 2026-10-10: the write-ahead log is bounded and checkpointed.

Layer: L1

No `journal_size_limit` and no checkpoint but SQLite's passive auto-checkpoint:
with the window's readers open beside a run, `knowledge.db-wal` could grow for
the whole run and stay at its high-water mark for the life of the window.
`checkpoint_wal` tries `TRUNCATE`, settles for `PASSIVE` when a reader is in
the way, and never raises.
"""

from __future__ import annotations

import os
import sqlite3

from app.storage import sqlite_store as module
from app.storage.sqlite_store import SqliteStore


def _wal_bytes(store) -> int:
    try:
        return os.path.getsize(str(store.db_path) + "-wal")
    except OSError:
        return 0


def _write_a_lot(store, rows: int = 3000) -> None:
    with store.write() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS ballast (x BLOB)")
        conn.executemany("INSERT INTO ballast VALUES (?)", [(b"x" * 1000,)] * rows)


def test_every_connection_carries_the_journal_size_limit(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        limit = store.conn.execute("PRAGMA journal_size_limit").fetchone()[0]
        assert limit == module.WAL_SIZE_LIMIT_BYTES


def test_a_free_log_is_emptied(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        _write_a_lot(store)
        assert _wal_bytes(store) > 1_000_000
        busy, log, done = store.checkpoint_wal()
        assert busy == 0 and log == done == 0          # TRUNCATE: log reset to nothing
        assert _wal_bytes(store) == 0
        # The store's own busy timeout is put back afterwards.
        assert store.conn.execute("PRAGMA busy_timeout").fetchone()[0] == int(store._timeout * 1000)


def test_a_reader_in_the_way_gets_passive_not_an_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "CHECKPOINT_WAIT_S", 0.1)
    with SqliteStore(tmp_path / "index.db") as store:
        _write_a_lot(store, 10)
        reader = sqlite3.connect(str(store.db_path), isolation_level=None)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM ballast").fetchone()   # a held snapshot
        try:
            _write_a_lot(store)
            result = store.checkpoint_wal()
            assert result is not None
            busy, log, done = result
            assert busy == 0 and log > 0               # PASSIVE ran
            assert done < log                          # and the reader kept the rest
        finally:
            reader.execute("COMMIT")
            reader.close()
        assert store.checkpoint_wal()[0] == 0
        assert _wal_bytes(store) == 0


def test_the_log_is_cut_back_to_the_limit_when_it_starts_over(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "WAL_SIZE_LIMIT_BYTES", 64 * 1024)
    monkeypatch.setattr(module, "CHECKPOINT_WAIT_S", 0.1)
    with SqliteStore(tmp_path / "index.db") as store:
        reader = sqlite3.connect(str(store.db_path), isolation_level=None)
        _write_a_lot(store, 10)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM ballast").fetchone()
        _write_a_lot(store)
        reader.execute("COMMIT")
        reader.close()
        big = _wal_bytes(store)
        assert big > 1_000_000
        # A PASSIVE that copies everything, then the next write restarts the log.
        store.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        store.set_state("after", "1")
        assert _wal_bytes(store) <= 64 * 1024


def test_inside_a_batch_it_declines_without_touching_the_batch(tmp_path):
    with SqliteStore(tmp_path / "index.db") as store:
        with store.batch():
            store.set_state("inside", "1")
            assert store.checkpoint_wal() is None
            assert store.conn.in_transaction
        assert store.get_state("inside") == "1"


def test_a_closed_store_answers_none(tmp_path):
    store = SqliteStore(tmp_path / "index.db").connect()
    store.close()
    assert store.checkpoint_wal() is None
