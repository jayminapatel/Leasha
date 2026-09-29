r"""The Indexing page's funnel: one line of counts per status word.

Layer: L5

`Indexed 448,210 · Queued 1,200 · Reading 4 · Skipped 310 · Failed 12 ·
Deferred 45` - the owner chose it as the second place the one-word status
shows (the first is the Status column in every results list). The words are
`app.core.file_state`'s, so a file counted as Deferred here is the file whose
row says Deferred in Search, Files, Mail and Code.

**When it refreshes.** With the page's existing refreshes, and no timer of its
own: `paint_totals` hands it the counts `read_index_summary` already read on
its worker (a tab switch, the end of a run, a reset), and each progress tick
`paint_progress` lets through asks for a fresh read - at most once every
`REFRESH_MIN_S`, and never while the previous read is still out. During a run
that adds the two numbers only the run knows, Reading and Discovered.

**The read is a worker's** (`tasks.status_funnel_counts`), never this
widget's: three indexed statements (`store.status_counts`), but on the thread
that paints, while an index run is writing, even an indexed read can wait on
the write lock.
"""

from __future__ import annotations

import time
from typing import Any, Mapping, Optional

from PyQt6.QtCore import QThreadPool
from PyQt6.QtWidgets import QLabel

from app.core.file_state import FUNNEL_ORDER, explain, funnel_line
from app.ui.tasks import status_funnel_counts
from app.ui.workers import CallableWorker, run

__all__ = ["StatusFunnel", "REFRESH_MIN_S", "OBJECT_NAME"]

OBJECT_NAME = "indexFunnel"

#: The fastest the funnel re-reads the store during a run.
#:
#: **Fixed, not a setting** (non-negotiable 11): five seconds is well under the
#: time it takes somebody to glance at the page and back, and three grouped
#: reads every five seconds is nothing beside the run's own writes. Nobody has
#: a reason to change it. What would: a measurement on a twenty-million-row
#: index showing `store.status_counts` costing more than a few hundred
#: milliseconds - then this goes up, or the statements change.
REFRESH_MIN_S = 5.0


class StatusFunnel(QLabel):
    """One line of counts per status word. Hidden until it has counts."""

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__("", parent)
        self.setObjectName(OBJECT_NAME)
        self.setWordWrap(True)
        self.setAccessibleName("Files by status")
        self.setVisible(False)
        self._busy = False
        self._last_read = 0.0
        self.counts: dict[str, int] = {}

    # -- painting ------------------------------------------------------------

    def show_counts(self, counts: Optional[Mapping[str, int]]) -> None:
        """Paint one set of counts. UI thread, no I/O. `None` changes nothing:
        a failed read is not evidence that every count is zero."""
        if not isinstance(counts, Mapping):
            return
        self.counts = {word: int(n or 0) for word, n in counts.items()}
        self.setText(funnel_line(self.counts))
        # Every word on the line, with its sentence - "what does Deferred
        # mean" is answered where the word is.
        self.setToolTip("\n".join(
            f"{word}: {explain(word)}" for word in FUNNEL_ORDER
            if self.counts.get(word) or word == "Indexed"))
        self.setVisible(True)

    # -- reading -------------------------------------------------------------

    def refresh(self, store: Any, stats: Any = None, *, force: bool = False) -> bool:
        """Ask a worker for fresh counts. Returns whether one was sent.

        Throttled to `REFRESH_MIN_S` unless `force`, and never two at once: a
        read still out when the next tick comes is the answer that tick wanted.
        """
        if store is None or self._busy:
            return False
        now = time.monotonic()
        if not force and now - self._last_read < REFRESH_MIN_S:
            return False
        self._busy = True
        self._last_read = now
        worker = CallableWorker(status_funnel_counts, store, stats,
                                component="ui.index.funnel")
        worker.signals.finished.connect(self.show_counts)
        worker.signals.done.connect(self._read_done)
        run(QThreadPool.globalInstance(), worker)
        return True

    def _read_done(self) -> None:
        self._busy = False

    def tick(self, view: Any, stats: Any) -> bool:
        """A progress tick from `paint_progress`: refresh from the run's store.

        The run's own pipeline holds the store it writes to; its connection is
        per thread (`SqliteStore.conn`), so the worker reads through its own.
        A run in another process has no pipeline here and is left to the
        page's other refreshes.
        """
        worker = getattr(view, "_worker", None)
        store = getattr(getattr(worker, "pipeline", None), "store", None)
        return self.refresh(store, stats)
