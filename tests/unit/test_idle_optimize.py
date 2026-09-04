r"""§3c: `PRAGMA optimize` runs on an idle timer, not on close.

Layer: L1/L5

**A self-inflicted regression, found live.** An earlier change deleted the
`PRAGMA optimize` call from `SqliteStore.close()` with a comment saying it
belonged on an idle timer instead - and never added anything to call it from
anywhere, silently losing the optimize entirely rather than relocating it.
`optimize_query_planner()` is the missing half of that move; `MainWindow`
schedules it hourly while the window is open (see `shell.py`'s
`_optimize_timer` / `_run_idle_optimize`).
"""

from __future__ import annotations

from app.storage.sqlite_store import SqliteStore


def test_optimize_query_planner_runs_pragma_optimize(tmp_path):
    """Returns True and does not raise on an open, writable store."""
    with SqliteStore(tmp_path / "index.db") as store:
        assert store.optimize_query_planner() is True


def test_optimize_query_planner_never_raises_on_a_closed_store(tmp_path):
    """A closed store must cost a stale query plan, never a crash.

    Mirrors `optimize_fts`'s own contract: `write()` on a closed store raises
    `AppErrorException`, and a caller that fires this from a timer must not
    see that escape - it should log a warning and return False instead.
    """
    store = SqliteStore(tmp_path / "index.db")
    store.connect()
    store.close()

    assert store.optimize_query_planner() is False


def test_close_no_longer_runs_pragma_optimize_itself():
    """§3c: `close()` must not call PRAGMA optimize - that is the whole point
    of moving it off the exit path, so a relaunch never waits on it.

    `sqlite3.Connection` is a C-extension type; its methods can't be
    monkeypatched to spy on calls (`execute` is read-only), so this checks
    the actual source of `close()` rather than instrumenting a live
    connection - a direct regression check for the exact mistake that
    prompted this file: PRAGMA optimize was deleted from here and never
    relocated anywhere.
    """
    import inspect

    source = inspect.getsource(SqliteStore.close)
    assert 'execute("PRAGMA optimize")' not in source, \
        "PRAGMA optimize must run from the idle timer, not from close()"
