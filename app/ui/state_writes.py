r"""Keyed UI state saved off the UI thread, one write at a time, in order.

Layer: L5

**Why this exists (bug 3a).** Every remembered UI choice - the last page, the
theme, a column width, the tray switches - was saved with `store.set_state` on
the UI thread, on the reasoning that one keyed upsert into a table of a few
dozen rows is cheap. Running it *is* cheap. Waiting to run it is not:
`set_state` goes through `SqliteStore.write()`, which takes `_write_lock`, the
one process-wide lock the indexer also holds for every `store.batch()`. So while
an index ran, clicking a page on the rail waited for the indexer's current
transaction, and the whole window froze with it - non-negotiable #5 broken by a
call that looked free.

**What this does instead.** Each write becomes a `CallableWorker` on a pool of
its own with **exactly one thread**. One thread is the point, not a
limitation: two quick page switches must land in the order they were made, or
an older write finishing last quietly restores the page somebody just left. The
global pool runs tasks on whichever thread is free, and gives no such promise.

**Fire and forget, never silent.** The caller gets on with its click; a write
that fails is logged by `CallableWorker` with its `AppError` (non-negotiable
#2), and `on_failed` lets a caller that told somebody "saved" say otherwise.

**Reads are not routed here, deliberately.** `get_state`/`all_state` run on the
reading thread's own connection, and under WAL a reader sees the last committed
snapshot without waiting for the writer (`SqliteStore.__init__` explains the
per-thread connections). The one thing a caller gives up is read-your-own-
write: a `get_state` straight after `save_state` may see the previous value.
A caller that needs the new value on screen hands its follow-up to `on_saved`.

**At shutdown** `MainWindow._drain_workers` waits for `pool()` with a bounded
grace, after the index run has been asked to stop, so the last page and the
last setting are on disk before the store closes.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.core.logging import logger
from app.ui.workers import CallableWorker, run

__all__ = ["pool", "save_state", "save_states", "settle_before_run", "start"]

_log = logger.bind(component="ui.state_writes")

#: Created on first use rather than at import, so importing a module that
#: saves state never needs Qt to be up - and `view_options`' decision half
#: stays importable without a display.
_POOL: Any = None


def pool() -> Any:
    """The one-thread pool every queued state write runs on."""
    global _POOL
    if _POOL is None:
        from PySide6.QtCore import QThreadPool

        _POOL = QThreadPool()
        # **One**, so writes stay in the order they were made. See the module
        # docstring; this is the line the ordering test pins.
        _POOL.setMaxThreadCount(1)
    return _POOL


def start(worker: CallableWorker, *, owner: Any = None,
          on_saved: Callable[[], Any] | None = None,
          on_failed: Callable[[Any], Any] | None = None) -> CallableWorker:
    """Queue an already-built worker behind every earlier state write.

    For a caller whose write is more than one `set_states` - `view_options`
    hands its own `save_prefs` here, so the test that knows worker bodies by
    their `CallableWorker(fn, ...)` call can see that `save_prefs` is one.

    `owner`, when given, is the QObject whose lifetime the callbacks depend on:
    they are routed through `later.when_done`, so a write that lands after the
    settings page has gone does not call into a deleted widget.
    """
    if on_saved is not None or on_failed is not None:
        finished = (lambda _result: on_saved()) if on_saved is not None else None
        from PySide6.QtCore import QObject

        # `isinstance`: a controller test hands in a `SimpleNamespace` window as
        # the owner, which `when_done` cannot parent to. The callbacks then
        # connect directly, as they do with no owner at all (2026-10-08).
        if owner is not None and isinstance(owner, QObject):
            from app.ui.later import when_done

            when_done(owner, worker, finished=finished, failed=on_failed)
        else:
            if finished is not None:
                worker.signals.finished.connect(finished)
            if on_failed is not None:
                worker.signals.failed.connect(on_failed)
    return run(pool(), worker)


def save_states(store: Any, values: dict, *, component: str = "ui.state",
                owner: Any = None, on_saved: Callable[[], Any] | None = None,
                on_failed: Callable[[Any], Any] | None = None) -> CallableWorker | None:
    """`store.set_states(values)`, off the UI thread. One transaction, as before.

    `values` is copied here, on the caller's thread, so a dict the caller goes
    on to change cannot alter what is written. No store - a test's bare stand-
    in, a pane built before the window has one - is a no-op, as it always was.
    """
    if store is None or not values:
        return None
    worker = CallableWorker(store.set_states, dict(values), component=component)
    return start(worker, owner=owner, on_saved=on_saved, on_failed=on_failed)


def save_state(store: Any, key: str, value: str, *, component: str = "ui.state",
               owner: Any = None, on_saved: Callable[[], Any] | None = None,
               on_failed: Callable[[Any], Any] | None = None) -> CallableWorker | None:
    """`store.set_state(key, value)`, off the UI thread and in order."""
    if store is None:
        return None
    worker = CallableWorker(store.set_state, key, value, component=component)
    return start(worker, owner=owner, on_saved=on_saved, on_failed=on_failed)


def settle_before_run(timeout_ms: int) -> bool:
    """Wait, bounded, for every queued write. **Only ever off the UI thread.**

    Its one caller is `IndexWorker.run`, on the index run's own thread: a
    setting changed a moment before Start must be on disk before the run reads
    it (the archive modes and cloud-content folders are read by the run
    itself). On the UI thread this would be the very freeze this module
    exists to remove - `test_ui_never_blocks` allows the wait here and pins
    the caller. Returns False when the ceiling was reached; the run then
    reads whatever is committed, as it always did.
    """
    return bool(pool().waitForDone(timeout_ms))
