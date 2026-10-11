"""How the rest of the window reaches the Life Timeline.

Layer: L5

Order 202626270602 (0n) section 4b, moved by order 1i (2026-10-11). The
timeline is a rail page of its own, **Browse**, and every door ends at
`TimelineView.browse_*` on that page:

* **The rail's "Browse"** (and Go > Browse) needs no wiring: it is the page.
* **A result's right-click menu, "See everything from this month"** - the row
  carries a modified time, which for a photograph is the day it was copied and
  for a message is its container's, so the *truthful* date is asked of the index
  on a worker (`date_of_file`) and the timeline opens at that month.
* **The timeline strip above the results** - a right-click on a period there
  opens the timeline on exactly that period. (A left click keeps doing what it
  always did: add the period to the search as `after:`/`before:`.)

And the other way: an item opened from the timeline goes through the window's
own opener (`MainWindow._open_result`), which already resolves a file on a
catalogued drive to its current mount point and says so when the drive is not
plugged in.

**The window still owns the views.** Everything is read through `self._w`.
`test_ui_never_blocks` scans this module like the rest of `app/ui`: the one read
of the index here is a `CallableWorker`.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QThreadPool

from app.reports.timeline import date_of_file
from app.ui.workers import CallableWorker, run

__all__ = ["TimelineController", "NO_DATE"]

#: Said (as a toast) when a file has no date the timeline trusts.
NO_DATE = "That one has no date Leasha can trust, so it is not on the timeline."


class TimelineController(QObject):
    """Connects the window's pages to the timeline. Holds no state of its own."""

    def __init__(self, window: Any) -> None:
        """Wire the Browse page, the results menu and the strip to the timeline."""
        super().__init__(window)
        self._w = window
        timeline = window.timeline_view
        timeline.opened.connect(window._open_result)
        timeline.reveal_requested.connect(lambda row: window._open_result(row, reveal=True))
        window.search_view.results.period_requested.connect(self.browse_period)
        strip = getattr(window.search_view.split, "timeline", None)
        if strip is not None:
            strip.browse_requested.connect(self.browse_range)

    def browse_period(self, row: Any) -> None:
        """Open the timeline at the month this result is really from."""
        file_id = int(getattr(row, "file_id", row) or 0)
        worker = CallableWorker(date_of_file, self._w._store, file_id, component="ui.timeline")
        worker.signals.finished.connect(self._open_month)
        worker.signals.failed.connect(self._w._show_error)
        run(QThreadPool.globalInstance(), worker)

    def _open_month(self, when_ns: Any) -> None:
        """UI thread: the worker found the date (or none); open the timeline there."""
        if when_ns is None:
            self._w.notify(NO_DATE)
            return
        self._w._show(self._w.timeline_view)
        self._w.timeline_view.browse_month_of(int(when_ns))

    def browse_range(self, after: str, before: str) -> None:
        """Open the timeline on the period a strip band stands for."""
        self._w._show(self._w.timeline_view)
        self._w.timeline_view.browse_range(after, before)
