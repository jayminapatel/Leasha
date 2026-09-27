r"""A page switch never waits for the indexer's write transaction (bug 3a).

Layer: L5

**What was wrong.** `MainWindow._remember_page` saved `ui:page` with
`store.set_state` on the UI thread. `set_state` goes through `write()`, which
takes `SqliteStore._write_lock` - one process-wide `RLock` with no timeout, and
the same lock the indexer holds for every `store.batch()`. So while an index
ran, every click on the rail waited for whatever batch was in flight, and the
whole window froze with it. `test_ui_never_blocks.py` had waved `set_state`
through as "keyed state", which is why nothing caught it: a keyed write is
cheap *to run*, but not to *wait for*.

**How it is proved here.** A thread takes the write lock and sits on it - an
indexer batch, as far as the store can tell - and the rail is switched. The
switch must return well inside a frame budget, and the page must still be on
disk once the lock is let go. Against the unfixed window the switch sits for
the whole hold (`HOLD_S`) instead.
"""

from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("PyQt6")

pytestmark = [pytest.mark.qt, pytest.mark.gui]

#: How long the pretend indexer holds the lock. Long enough that a switch which
#: waits for it cannot pass by accident; short enough that the unfixed window
#: fails in seconds rather than hanging the run.
HOLD_S = 2.0

#: The switch must come back inside this. A frame is ~16ms; 100ms is the point
#: at which a click starts to feel ignored, and far below `HOLD_S`.
PROMPT_S = 0.1


def _hold_write_lock(store, held: threading.Event, release: threading.Event) -> threading.Thread:
    """Take the store's write lock on another thread, the way `batch()` does."""
    def indexer() -> None:
        with store._write_lock:
            held.set()
            release.wait(HOLD_S)

    thread = threading.Thread(target=indexer, name="pretend-indexer", daemon=True)
    thread.start()
    assert held.wait(5), "the pretend indexer never took the write lock"
    return thread


def _wait_for(app, predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_switching_page_during_an_index_batch_returns_at_once(gui_mainwindow):
    app, window, store, _engine = gui_mainwindow
    rail = window.rail
    target = next(i for i in range(rail.count())
                  if i != rail.currentIndex() and rail.tabText(i))
    title = rail.tabText(target)
    assert store.get_state("ui:page") != title, "the target page is already saved"

    held, release = threading.Event(), threading.Event()
    thread = _hold_write_lock(store, held, release)
    try:
        began = time.monotonic()
        rail.setCurrentIndex(target)
        elapsed = time.monotonic() - began
    finally:
        release.set()
        thread.join(5)

    assert elapsed < PROMPT_S, (
        f"switching page took {elapsed * 1000:.0f}ms while the indexer held the "
        "write lock - the UI thread is waiting on a store write again")
    assert _wait_for(app, lambda: store.get_state("ui:page") == title), (
        "the page switch returned promptly but ui:page was never saved")


def test_queued_writes_land_in_the_order_they_were_made(gui_mainwindow):
    """One writer thread, so the last choice is the one that sticks.

    A pool with several threads could run an older write after a newer one
    and quietly restore the page somebody had just left.
    """
    from app.ui.state_writes import pool, save_state

    _app, _window, store, _engine = gui_mainwindow
    assert pool().maxThreadCount() == 1

    held, release = threading.Event(), threading.Event()
    thread = _hold_write_lock(store, held, release)
    try:
        for n in range(40):
            save_state(store, "test:ordered", str(n))
    finally:
        release.set()
        thread.join(5)
    assert pool().waitForDone(5000)
    assert store.get_state("test:ordered") == "39"


def test_a_failed_queued_write_is_logged_not_swallowed(gui_mainwindow):
    """Non-negotiable #2: the worker says what went wrong, and `on_failed`
    hears about it, so a caller that told somebody "saved" can say otherwise."""
    from app.ui.state_writes import pool, save_states

    app, _window, _store, _engine = gui_mainwindow

    class _Locked:
        def set_states(self, _values):
            raise RuntimeError("database is locked")

    failures: list = []
    save_states(_Locked(), {"test:k": "v"}, on_failed=failures.append)
    assert pool().waitForDone(5000)
    assert _wait_for(app, lambda: bool(failures)), "on_failed was never called"
    assert "locked" in str(getattr(failures[0], "details", failures[0]))


def test_closing_drains_queued_writes_before_the_store_goes(gui_mainwindow):
    """`_drain_workers` is the close path's wait. A write still queued behind an
    index batch when the window closes must land, not be abandoned."""
    from app.ui.state_writes import save_state

    _app, window, store, _engine = gui_mainwindow
    held, release = threading.Event(), threading.Event()
    thread = _hold_write_lock(store, held, release)
    save_state(store, "test:at_close", "kept")
    threading.Timer(0.3, release.set).start()
    try:
        window._drain_workers()
    finally:
        release.set()
        thread.join(5)
    assert store.get_state("test:at_close") == "kept"
