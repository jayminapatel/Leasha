r"""No worker may wait for ever for a database connection.

Layer: L1 (storage)

Order 0m's 2026-09-20 note: pressing Offline Media *Scan* in the launched app
hung, with workers blocked in `SqliteStore._new_connection`. The cause was not
reproduced in-process; what is provable is the shape that lets it happen - the
lock that hands out connections was waited on with no limit, and a locked
database escaped from inside it as a bare `sqlite3.OperationalError` with the
lock still held. Both are now bounded and end in a plain-words error.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from app.core.errors import AppErrorException
from app.storage.sqlite_store import SqliteStore


def test_a_worker_gives_up_on_the_connection_lock_in_plain_words(tmp_path):
    store = SqliteStore(tmp_path / "index.db", timeout=0.4).connect()
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with store._conns_lock:              # a thread stuck opening a connection
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(5)

    outcome: dict = {}

    def worker() -> None:
        started = time.monotonic()
        try:
            store.conn                        # this thread's first use: needs the lock
        except AppErrorException as exc:
            outcome["error"] = exc.error
        outcome["seconds"] = time.monotonic() - started

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    thread.join(10)
    release.set()
    holder.join(5)
    store.close()

    assert not thread.is_alive(), "the worker is still waiting for the lock"
    error = outcome["error"]
    assert error.code == "ERR_DB_BUSY"
    assert "stopped instead of waiting for ever" in error.message
    assert "try again" in error.suggestion.lower()
    assert outcome["seconds"] < 5


def test_a_locked_database_is_reported_and_the_half_open_connection_is_closed(tmp_path, monkeypatch):
    closed: list[bool] = []

    class Locked:
        row_factory = None

        def execute(self, *_a, **_k):
            raise sqlite3.OperationalError("database is locked")

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(sqlite3, "connect", lambda *_a, **_k: Locked())
    store = SqliteStore(tmp_path / "index.db", timeout=0.2)
    with pytest.raises(AppErrorException) as excinfo:
        store.connect()

    assert excinfo.value.error.code == "ERR_DB_BUSY"
    assert "database is locked" in (excinfo.value.error.details or "")
    assert closed == [True]
    # ...and the lock was released: a second attempt is not blocked behind the first.
    with pytest.raises(AppErrorException):
        store.connect()
