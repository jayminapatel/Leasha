"""Closing the store while another thread is mid-query must not crash the process.

Layer: L1

**The bug.** `SqliteStore.close()` closed every thread's connection from the
closing thread. Writers were safe - `close()` waits for the write lock - but a
reader holds no lock, and a connection closed underneath a query that is
running on another thread crashes CPython's `sqlite3` natively: a segmentation
fault (an access violation on Windows), no traceback, nothing to catch. Found
when a full suite run died with the `/` popup's `distinct_value_counts`
worker still inside `fetchall()` while the main thread had closed its store
and moved on to the next test.

**Why a child process.** The failure *is* the process dying, so the reproduction
runs in its own interpreter and the parent only reads its exit code. Before
the fix the stress child died with -11 (SIGSEGV) within its first trials on
every run; after it, it exits 0 with every reader told, in words, that the
store closed.

The in-process tests below pin the parts that are safe to run here: that
`close()` still closes every connection (a handle left open blocks deleting the
index folder on Windows), and that a reader that finds its connection retired
gets the error the workers already treat as "the window is closing".
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from app.core.errors import AppErrorException
from app.storage.sqlite_store import SqliteStore

ROOT = Path(__file__).resolve().parents[2]

#: Wall-clock cap on each child. The fixed child finishes in a few seconds.
CHILD_CAP_S = 180

#: Readers hammering the store while the main thread closes it at a random
#: moment, over and over. Every reader failure must be a Python exception, and
#: the process must survive. Uses `SqliteStore` exactly as the app does.
STRESS_CHILD = textwrap.dedent(r"""
    import faulthandler, random, sys, threading, time
    faulthandler.enable()
    from pathlib import Path
    from app.storage.sqlite_store import SqliteStore

    folder, trials = Path(sys.argv[1]), int(sys.argv[2])
    seed = SqliteStore(folder / "x.db").connect()
    with seed.write() as c:
        c.execute("CREATE TABLE t(a INTEGER, b TEXT)")
        c.execute("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n "
                  "WHERE x < 200000) INSERT INTO t SELECT x, 'v' || (x % 5000) FROM n")
    seed.close()

    SHAPES = [
        "SELECT b, count(*) FROM t GROUP BY b ORDER BY 2 DESC",
        "SELECT a, b FROM t WHERE b LIKE '%1%'",
        "SELECT count(*) FROM t x JOIN t y ON x.a = y.a WHERE x.b LIKE '%9%'",
    ]
    failures = {}
    random.seed(1)
    for _ in range(trials):
        store = SqliteStore(folder / "x.db").connect()
        go = threading.Event()
        def reader():
            go.wait()
            for _ in range(50):
                try:
                    if not store.is_open:
                        return
                    sql = random.choice(SHAPES)
                    if random.random() < 0.5:
                        store.conn.execute(sql).fetchall()
                    else:
                        for _row in store.conn.execute(sql):
                            pass
                except Exception as exc:
                    error = getattr(exc, "error", None)
                    key = f"{type(exc).__name__}: {error.details if error else exc}"
                    failures[key] = failures.get(key, 0) + 1
                    return
        threads = [threading.Thread(target=reader) for _ in range(3)]
        for t in threads:
            t.start()
        go.set()
        time.sleep(random.uniform(0.0, 0.05))
        store.close()
        for t in threads:
            t.join()
    for key, n in sorted(failures.items()):
        print(n, key)
    print("SURVIVED")
""")

#: One long query, closed under it. Measures that `close()` interrupts the
#: query rather than waiting for it to run to the end.
LONG_QUERY_CHILD = textwrap.dedent(r"""
    import faulthandler, sys, threading, time
    faulthandler.enable()
    from pathlib import Path
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(Path(sys.argv[1]) / "x.db").connect()
    started, outcome = threading.Event(), []
    def reader():
        try:
            conn = store.conn
            started.set()
            conn.execute("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c "
                         "WHERE x < 50000000) SELECT count(*) FROM c").fetchall()
            outcome.append("finished")
        except Exception as exc:
            outcome.append(str(exc.error.details) if hasattr(exc, "error") else repr(exc))
    t = threading.Thread(target=reader)
    t.start()
    started.wait()
    time.sleep(0.3)
    t0 = time.perf_counter()
    store.close()
    took = time.perf_counter() - t0
    t.join()
    print("CLOSE_TOOK", round(took, 3))
    print("READER", outcome[0])
""")


def _run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", script, *args],
        cwd=str(ROOT), capture_output=True, text=True, timeout=CHILD_CAP_S)


def test_closing_under_running_readers_does_not_crash_the_process(tmp_path):
    proc = _run(STRESS_CHILD, str(tmp_path), "60")
    assert proc.returncode == 0, (
        f"the child died with exit code {proc.returncode} - a native crash when "
        f"close() ran under a reader.\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr[-4000:]}")
    assert "SURVIVED" in proc.stdout
    # Every reader that lost the race was told so in the words the workers
    # recognise as a window close - never a bare sqlite3 error.
    for line in proc.stdout.splitlines()[:-1]:
        assert "was closed while a worker was using it" in line, proc.stdout


def test_close_interrupts_a_long_query_instead_of_waiting_for_it(tmp_path):
    proc = _run(LONG_QUERY_CHILD, str(tmp_path))
    assert proc.returncode == 0, proc.stderr[-4000:]
    took = float(next(line.split()[1] for line in proc.stdout.splitlines()
                      if line.startswith("CLOSE_TOOK")))
    reader = next(line for line in proc.stdout.splitlines() if line.startswith("READER"))
    assert "was closed while a worker was using it" in reader
    # The query counts to fifty million - seconds of work. Interrupted, close()
    # returns in milliseconds; the cap is generous for a slow CI machine.
    assert took < 2.0, proc.stdout


# -- in process: nothing here can crash, it only checks bookkeeping ----------

def _open_on_threads(store: SqliteStore, n: int, *, stay_alive: threading.Event):
    """Open a connection on each of `n` threads that then stay idle but alive,
    the way a QThreadPool thread waits for its next task."""
    opened = threading.Barrier(n + 1)

    def body():
        store.conn.execute("SELECT 1").fetchall()
        opened.wait()
        stay_alive.wait()

    threads = [threading.Thread(target=body) for _ in range(n)]
    for t in threads:
        t.start()
    opened.wait()
    return threads


def test_close_still_closes_every_threads_connection(tmp_path):
    store = SqliteStore(tmp_path / "x.db").connect()
    release = threading.Event()
    threads = _open_on_threads(store, 3, stay_alive=release)
    try:
        conns = list(store._open)
        assert len(conns) == 4                    # the main thread's and three
        store.close()
        assert store._open == []
        for conn in conns:
            # A closed sqlite3 connection refuses even this.
            with pytest.raises(sqlite3.ProgrammingError):
                conn.total_changes
    finally:
        release.set()
        for t in threads:
            t.join()


def test_a_reader_whose_connection_was_retired_gets_the_shutdown_error(tmp_path):
    store = SqliteStore(tmp_path / "x.db").connect()
    cursor = store.conn.execute(
        "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 10) "
        "SELECT x FROM c")
    assert next(cursor)["x"] == 1
    store.close()
    with pytest.raises(AppErrorException) as caught:
        next(cursor)
    assert "was closed while a worker was using it" in caught.value.error.details


def test_the_store_opens_again_after_close(tmp_path):
    store = SqliteStore(tmp_path / "x.db").connect()
    store.close()
    store.connect()
    try:
        assert store.conn.execute("SELECT 1").fetchone()[0] == 1
        start = time.perf_counter()
        store.close()
        assert time.perf_counter() - start < 1.0   # nothing busy, nothing to wait for
    finally:
        store.close()
