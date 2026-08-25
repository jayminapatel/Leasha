"""One SQLite connection per thread, and why sharing one was wrong.

Layer: L1

The shared connection was filed as a scalability finding. It was a
**correctness** one. Two threads on a single connection share its transaction
state, so a search running while indexing read *inside* the indexing
transaction - and when that transaction rolled back, the search had already
returned a result for a document that never entered the index.

`test_a_reader_never_sees_another_threads_open_transaction` is that bug. It
failed before the change with `/DIRTY.pdf` in the results.

The rest guard the things per-thread connections newly make possible to get
wrong: a worker thread that never called `connect()`, a connection nobody
closes, and pragmas set on one connection but not the next.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from app.core.errors import AppErrorException
from app.storage.sqlite_store import SqliteStore

TIMEOUT = 10.0


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def _in_thread(fn):
    """Run `fn` on its own thread, returning its value or re-raising."""
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:      # re-raised on the calling thread
            box["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(TIMEOUT)
    assert not thread.is_alive(), "thread did not finish - probably blocked on the db"
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _paths(store):
    return [row[0] for row in store.conn.execute("SELECT path FROM files ORDER BY path")]


def _insert(conn, path):
    conn.execute(
        "INSERT INTO files (path, parent_dir, ext, size_bytes, mtime_ns, status, "
        "source_kind) VALUES (?, '/', 'pdf', 1, 1, 'INDEXED', 'file')", (path,))


# --- the bug ----------------------------------------------------------------

def test_a_reader_never_sees_another_threads_open_transaction(store):
    """The reason this change exists.

    On one shared connection this returned `/DIRTY.pdf` - a row inside a
    transaction that then rolled back. In the app that is a search result the
    user can click for a document that was never indexed.
    """
    store.upsert_file(path="/committed.pdf", size_bytes=1, mtime_ns=1,
                      source_kind="file")

    writing = threading.Event()
    finish = threading.Event()
    seen: dict = {}

    def writer():
        try:
            with store.write() as conn:
                _insert(conn, "/DIRTY.pdf")
                writing.set()
                finish.wait(TIMEOUT)
                raise RuntimeError("roll this back")
        except RuntimeError:
            pass

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    assert writing.wait(TIMEOUT), "writer never opened its transaction"

    # The reader runs on its own thread, mid-write, exactly as a search does.
    seen["during"] = _in_thread(lambda: _paths(store))
    finish.set()
    thread.join(TIMEOUT)

    assert seen["during"] == ["/committed.pdf"], (
        "a reader saw another thread's uncommitted rows: " + repr(seen["during"]))
    assert _paths(store) == ["/committed.pdf"]


def test_a_reader_is_not_blocked_by_a_write_in_flight(store):
    """WAL, and the reason per-thread connections are cheap rather than a trade.

    If this hangs, readers are queueing behind the writer and every search
    during indexing pays for it.
    """
    writing = threading.Event()
    finish = threading.Event()

    def writer():
        with store.write() as conn:
            _insert(conn, "/slow.pdf")
            writing.set()
            finish.wait(TIMEOUT)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    assert writing.wait(TIMEOUT)

    # Must return while the write is still open, not after it commits.
    assert _in_thread(lambda: _paths(store)) == []

    finish.set()
    thread.join(TIMEOUT)
    assert _paths(store) == ["/slow.pdf"]


def test_committed_work_is_visible_to_other_threads(store):
    """The other half: isolation must not become staleness.

    A snapshot held open too long would make indexing progress invisible to
    the UI, which looks exactly like indexing being broken.
    """
    store.upsert_file(path="/first.pdf", size_bytes=1, mtime_ns=1, source_kind="file")
    assert _in_thread(lambda: _paths(store)) == ["/first.pdf"]

    _in_thread(lambda: store.upsert_file(
        path="/second.pdf", size_bytes=1, mtime_ns=2, source_kind="file"))

    assert _paths(store) == ["/first.pdf", "/second.pdf"]


# --- what per-thread connections newly make possible to get wrong ------------

def test_each_thread_gets_its_own_connection(store):
    main = store.conn
    other = _in_thread(lambda: store.conn)

    assert other is not main
    assert isinstance(other, sqlite3.Connection)


def test_the_same_thread_gets_the_same_connection_back(store):
    assert store.conn is store.conn

    def twice():
        return store.conn is store.conn

    assert _in_thread(twice) is True


def test_a_worker_thread_never_had_to_call_connect(store):
    """Qt creates threads this code never sees, so opening must be lazy.

    Requiring `connect()` per thread would mean every worker either remembers
    to call it or silently shares the main thread's connection - which is the
    bug at the top of this file.
    """
    assert _in_thread(lambda: store.stats()) is not None


def test_every_connection_gets_the_pragmas_not_just_the_first(store):
    """`foreign_keys` is per-connection, and the cascade deletes depend on it.

    A worker with it off would leave chunks behind when their file went, and
    nothing would say so until a search returned text from a deleted document.
    """
    def pragmas():
        conn = store.conn
        return (conn.execute("PRAGMA foreign_keys").fetchone()[0],
                conn.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                conn.execute("PRAGMA busy_timeout").fetchone()[0])

    assert pragmas() == _in_thread(pragmas)
    assert _in_thread(pragmas)[0] == 1, "foreign_keys off on a worker connection"
    assert _in_thread(pragmas)[1] == "wal"
    assert _in_thread(pragmas)[2] > 0, "busy_timeout unset - a busy db would fail, not wait"


def test_cascade_delete_still_works_from_a_worker_thread(store):
    """The consequence of the pragma above, rather than the pragma itself."""
    def index_then_delete():
        file_id = store.upsert_file(path="/doomed.pdf", size_bytes=1, mtime_ns=1,
                                    source_kind="file")
        store.replace_chunks(file_id, [{"text": "one"}, {"text": "two"}])
        assert store.conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE file_id = ?", (file_id,)).fetchone()[0] == 2
        store.delete_file(file_id)
        return file_id

    file_id = _in_thread(index_then_delete)

    assert store.conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE file_id = ?", (file_id,)).fetchone()[0] == 0


def test_close_closes_every_threads_connection_not_just_its_own(tmp_path):
    """`threading.local` cannot be enumerated, so they are tracked separately.

    A connection left open holds a file handle and its share of the WAL. On
    Windows that is also what stops the index directory from being deleted or
    moved afterwards.
    """
    store = SqliteStore(tmp_path / "index.db").connect()
    worker_conn = _in_thread(lambda: store.conn)

    store.close()

    with pytest.raises(sqlite3.ProgrammingError):
        worker_conn.execute("SELECT 1")


def test_using_a_closed_store_says_so_rather_than_reopening(tmp_path):
    """Silently opening a fresh connection after `close()` would resurrect it.

    A worker outliving the window would then keep the database alive, and the
    file handle with it.
    """
    store = SqliteStore(tmp_path / "index.db").connect()
    store.close()

    assert store.is_open is False
    with pytest.raises(AppErrorException):
        _ = store.conn
    with pytest.raises(AppErrorException):
        _in_thread(lambda: store.conn)


def test_a_store_can_be_reopened_after_closing(tmp_path):
    """`connect()` after `close()` is a fresh start, not a corpse.

    The settings panel changes the index location by closing and reopening.
    """
    path = tmp_path / "index.db"
    store = SqliteStore(path).connect()
    store.upsert_file(path="/kept.pdf", size_bytes=1, mtime_ns=1, source_kind="file")
    store.close()

    store.connect()
    try:
        assert store.is_open is True
        assert _paths(store) == ["/kept.pdf"]
        assert _in_thread(lambda: _paths(store)) == ["/kept.pdf"]
    finally:
        store.close()


def test_concurrent_writers_serialise_rather_than_collide(store):
    """Every write lands. WAL allows one writer, and the lock queues the rest."""
    threads = [
        threading.Thread(
            target=lambda n=n: store.upsert_file(
                path=f"/w{n:02d}.pdf", size_bytes=1, mtime_ns=n,
                source_kind="file"),
            daemon=True)
        for n in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(TIMEOUT)
        assert not thread.is_alive(), "a writer never finished - deadlock or lock timeout"

    # Zero-padded: `/w10.pdf` sorts before `/w2.pdf` as a string, and the first
    # version of this test failed on its own naming rather than on the code.
    assert _paths(store) == [f"/w{n:02d}.pdf" for n in range(12)]


def test_migrations_run_once_however_many_threads_connect(tmp_path):
    """Under the lock, so workers cannot race the schema into existence twice."""
    store = SqliteStore(tmp_path / "index.db").connect()
    try:
        for _ in range(4):
            _in_thread(lambda: store.conn)

        rows = store.conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        assert rows == 1
        assert store.schema_version >= 5
    finally:
        store.close()
