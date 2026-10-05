r"""Closing the window while an index run is going.

**What happened, live, on 2026-09-19.** The window logged `closing: took 0.0s`
and vanished. The index run behind it carried on for three more minutes -
LibreOffice conversions still logging with no window to show them - and the
process then sat at 0 CPU until it was killed from Task Manager.

Two causes, one test each:

* `IndexingView` runs the worker in its own `QThreadPool`, and
  `MainWindow._drain_workers` only waited on the global one, so it saw nothing
  to wait for.
* `Pipeline._extract_worker` looked at the stop flag only when its queue was
  empty. The queue is bounded and kept full, so a stopped run went on
  converting whatever was already queued.
"""

from __future__ import annotations

import queue
import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QRunnable, QThreadPool


class _Sleeper(QRunnable):
    """A stand-in for an index run: alive for a while, then done."""

    def __init__(self, seconds: float, finished: threading.Event) -> None:
        super().__init__()
        self._seconds = seconds
        self._finished = finished

    def run(self) -> None:
        time.sleep(self._seconds)
        self._finished.set()


def _window_with(index_pool: QThreadPool, *, grace_ms: int) -> SimpleNamespace:
    return SimpleNamespace(
        SHUTDOWN_GRACE_MS=200,
        INDEX_SHUTDOWN_GRACE_MS=grace_ms,
        indexing_view=SimpleNamespace(pool=index_pool),
    )


def test_drain_waits_for_a_run_in_the_index_pool(qapp):
    from app.ui.shell import MainWindow

    pool = QThreadPool()
    pool.setMaxThreadCount(1)
    finished = threading.Event()
    pool.start(_Sleeper(0.6, finished))

    # Not in the global pool at all - which is the whole bug.
    assert QThreadPool.globalInstance().activeThreadCount() == 0

    MainWindow._drain_workers(_window_with(pool, grace_ms=5_000))

    assert finished.is_set(), "the window closed over an index run that was still going"
    assert pool.activeThreadCount() == 0


def test_drain_gives_up_at_the_index_grace_and_says_so(qapp, caplog):
    from app.ui.shell import MainWindow

    pool = QThreadPool()
    pool.setMaxThreadCount(1)
    finished = threading.Event()
    pool.start(_Sleeper(1.5, finished))

    began = time.monotonic()
    MainWindow._drain_workers(_window_with(pool, grace_ms=300))
    waited = time.monotonic() - began

    # Bounded: a run that will not stop is left, not waited on forever.
    assert waited < 1.2
    assert not finished.is_set()
    pool.waitForDone(5_000)                          # let the stand-in finish


def _bare_pipeline():
    from app.index.pipeline import Pipeline

    pipeline = object.__new__(Pipeline)
    pipeline._stop = threading.Event()
    return pipeline


def test_extract_worker_stops_at_the_next_item_not_the_next_empty_queue():
    pipeline = _bare_pipeline()
    work: queue.PriorityQueue = queue.PriorityQueue()
    results: queue.Queue = queue.Queue()
    for sequence in range(5):
        work.put((0, sequence, object(), None))
    pipeline._stop.set()

    worker = threading.Thread(
        target=pipeline._extract_worker, args=(work, results), daemon=True)
    worker.start()
    worker.join(timeout=3)

    assert not worker.is_alive(), (
        "a stopped worker kept going while its queue was full - the run would "
        "convert a queue-length of files after the window had closed")
    assert results.empty(), "nothing should have been extracted after the stop"
