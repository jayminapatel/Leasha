r"""`batch()` waits for the write lock as `write()` does, and says so when it cannot.

Storage review S3, 2026-10-10. `batch()` opened its transaction with a bare `BEGIN IMMEDIATE`, so a second
process holding the write lock past `busy_timeout` made it raise a bare
`sqlite3.OperationalError: database is locked` - reported in the window as "an
unexpected error, this is a bug". `write()` already went through
`_begin_write`: one more wait, then `ERR_DB_BUSY` in plain words.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.core.errors import AppErrorException
from app.storage.sqlite_store import SqliteStore


def _hold_write_lock(path) -> sqlite3.Connection:
    other = sqlite3.connect(str(path), isolation_level=None, timeout=0)
    other.execute("BEGIN IMMEDIATE")
    return other


def test_a_batch_refused_the_write_lock_says_err_db_busy(tmp_path):
    store = SqliteStore(tmp_path / "index.db", timeout=0.2).connect()
    other = _hold_write_lock(store.db_path)
    try:
        with pytest.raises(AppErrorException) as excinfo:
            with store.batch():
                pytest.fail("the batch body ran without the write lock")
        assert excinfo.value.error.code == "ERR_DB_BUSY"
        # Nothing of the refused batch is left on this thread: the next one runs.
        assert getattr(store._local, "batch_depth", 0) == 0
        assert not store.conn.in_transaction
    finally:
        other.execute("ROLLBACK")
        other.close()
    with store.batch():
        store.set_state("after", "1")
    assert store.get_state("after") == "1"
    store.close()


def test_a_batch_waits_once_more_and_succeeds_when_the_lock_comes_free(tmp_path, monkeypatch):
    """The first wait is lost; the second one finds the lock free - the same
    two-attempt path `write()` takes."""
    store = SqliteStore(tmp_path / "index.db", timeout=0.2).connect()
    other = _hold_write_lock(store.db_path)
    calls: list[int] = []
    real = SqliteStore._begin_write

    def begin(self, conn):
        calls.append(1)
        return real(self, conn)

    # Let the holder go as soon as the first refusal has been logged.
    from app.storage import sqlite_store as module

    def release_then_warn(*args, **kwargs):
        if other.in_transaction:
            other.execute("ROLLBACK")

    monkeypatch.setattr(SqliteStore, "_begin_write", begin)
    monkeypatch.setattr(module._log, "warning", release_then_warn)
    try:
        with store.batch():
            store.set_state("k", "v")
    finally:
        other.close()
    assert calls == [1]
    assert store.get_state("k") == "v"
    store.close()
