"""Closing the window must actually end the process.

Layer: L4

Reported: *"when you close the gui it does not exit it is stuck i need to press
ctrl c"*.

`SearchEngine` runs its two retrievers on a `ThreadPoolExecutor`. Those worker
threads are **non-daemon**, and `concurrent.futures` installs an `atexit` hook
that joins every one of them at interpreter shutdown. `shutdown(wait=False)`
returns immediately and then Python blocks on that join anyway - after Qt has
closed the window, with nothing left on screen to explain the wait.

So the window vanishes, the process stays, and the only way out is Ctrl+C in a
console the person may not even have open.

`cancel_futures=True` drops what is queued, which is exactly the work worth
abandoning: nobody is waiting for a result in a window that has closed.
"""

from __future__ import annotations

import threading
import time

from app.search.engine import SearchEngine


def _engine():
    """An engine with only the pool wired up - close() touches nothing else."""
    from concurrent.futures import ThreadPoolExecutor

    engine = SearchEngine.__new__(SearchEngine)
    engine._closed = False
    engine._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="search")
    return engine


def test_queued_work_is_cancelled_rather_than_waited_for():
    """**The fix.** Without `cancel_futures`, these run to completion and the
    interpreter's atexit join waits for every one."""
    engine = _engine()
    release = threading.Event()
    ran = []

    def slow(n):
        release.wait(5)
        ran.append(n)

    # Two fill the pool; the rest queue behind them.
    futures = [engine._pool.submit(slow, n) for n in range(8)]

    engine.close()
    cancelled = sum(1 for f in futures if f.cancelled())
    release.set()

    # The two occupying the pool were already running and cannot be recalled;
    # everything behind them is exactly the work worth abandoning.
    assert cancelled >= len(futures) - 2, (
        f"only {cancelled} of {len(futures)} queued searches were dropped - "
        f"the rest run to completion and the atexit join waits for them")
    assert len(ran) <= 2, "cancelled work ran anyway"


def test_close_returns_promptly_even_with_work_in_flight():
    """It must not become a blocking wait in the handler that closes the window.

    The grace period belongs in `shell._drain_workers`, where the UI is kept
    painting; a blocking close here freezes the window during the one operation
    nobody will wait out.
    """
    engine = _engine()
    release = threading.Event()
    engine._pool.submit(release.wait, 5)

    started = time.perf_counter()
    engine.close()
    elapsed = time.perf_counter() - started
    release.set()

    assert elapsed < 1.0, f"close() blocked for {elapsed:.1f}s"


def test_closed_is_true_afterwards():
    """`closed` is what stops a search in flight from submitting to a dead
    pool and reporting `RuntimeError` to the owner as "a bug"."""
    engine = _engine()
    assert engine.closed is False

    engine.close()

    assert engine.closed is True


def test_closing_twice_is_harmless():
    """`closeEvent` can run more than once - close to tray, then quit."""
    engine = _engine()
    engine.close()
    engine.close()


def test_the_shutdown_guard_exists_in_the_search_path():
    """`closed` is checked before submitting, so a search in flight when the
    window closes reports ERR_SHUTTING_DOWN rather than `RuntimeError: cannot
    schedule new futures after shutdown` - which the worker boundary dutifully
    reported to the owner as "a bug", three times. Shutting down is not a bug.
    """
    import inspect

    source = inspect.getsource(SearchEngine.search)

    assert "ERR_SHUTTING_DOWN" in source
