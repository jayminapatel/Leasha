r"""The Search page's searches in flight: stale ones taken back, the busy line, kept rows.

Layer: L5

Review 2026-10-14, MS-1, MS-7 and MS-8 (and MS-5's grid offer). The decisions
are `presenter.search_feedback`'s; this carries them out, beside the view
rather than in it, because `search_view.py` sits at its 250-line guard.

* **The busy line** is a thin `ShimmerBar` laid over the top edge of the
  results, outside any layout, so showing it moves nothing. It appears
  `BUSY_DELAY_MS` after a search goes out - only while rows are already on
  screen; an empty list has the skeleton (`skeleton.py`) - and goes when the
  latest search's finished answer lands or it fails. Listening to the worker
  itself means a stale answer can never end it, and nothing can leave it on.
* **Kept rows are dimmed** while an answer of nothing stands over them, and the
  status line says so; the full tier's own "nothing" empties the list.
* **Stale searches taken back.** A search not yet started when a newer one
  goes out is taken back from the pool (`QThreadPool.tryTake`) - it would only
  be dropped as stale when it finally ran, after holding a thread.

**Why not a small pool of searches' own** (MS-8 asked to consider one): a search
already running cannot be stopped, and with the reranker on one takes up to
3.3 s (`BUILD_SPEC_V2.md`'s figures, quoted in the review). On a pool of two, a
burst of typing would queue the newest search behind two stale ones still
running - the very wait MS-8 is about. The global pool has a thread per core;
what queued ahead of searches there was the Search grid's thumbnails, and those
have a pool of their own now (`search_photos.decode_pool`).
"""

from __future__ import annotations

import weakref
from typing import Any, Optional

from PySide6.QtCore import QEvent, QObject, QThreadPool, QTimer
from PySide6.QtWidgets import QGraphicsOpacityEffect

from app.ui.presenter.search_feedback import (
    BUSY_DELAY_MS, DIM_OPACITY, EMPTY, GRID_HREF, after_nothing, busy_ends,
    grid_offer, kept_words,
)

__all__ = ["SearchFlight", "BUSY_LINE_PX"]

#: The busy line's height: a hairline that moves, not a second progress bar.
BUSY_LINE_PX = 3

class SearchFlight(QObject):
    """The Search page's searches in flight. A child of the page."""

    def __init__(self, view: Any, *, status: Any, notices: Any) -> None:
        from app.ui.widgets.shimmer_bar import ShimmerBar

        super().__init__(view)
        # Weakly: a child must never keep its page alive (`test_views_are_freed`).
        self._view = weakref.ref(view)
        self._status = status
        self._pool = QThreadPool.globalInstance()
        self._latest = -1
        self._queued: list = []
        self._dimmed = False

        results = view.results
        self.bar = ShimmerBar(results)
        self.bar.setObjectName("searchBusy")
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 0)
        self.bar.setAccessibleName("Searching")
        self.bar.hide()
        results.installEventFilter(self)
        self._place()

        self._busy_timer = QTimer(self)
        self._busy_timer.setSingleShot(True)
        self._busy_timer.setInterval(BUSY_DELAY_MS)
        self._busy_timer.timeout.connect(self._show_busy)
        notices.chosen.connect(self._chosen)

    # -- the pool ---------------------------------------------------------------

    def launch(self, worker: Any) -> Any:
        """Run `worker`, taking back any search of this page's not yet started."""
        from app.ui.workers import run

        self._take_back()
        generation = int(getattr(worker, "generation", 0))
        self._latest = generation
        worker.signals.finished.connect(lambda payload: self._ended(payload[0]))
        worker.signals.failed.connect(lambda _error, g=generation: self._ended(g))
        worker.signals.done.connect(lambda w=worker: self._forget(w))
        self._queued.append(worker)
        run(self._pool, worker)
        self._busy_timer.start()
        return worker

    def _take_back(self) -> None:
        """Every search not yet started is superseded: take it back from the pool."""
        from app.ui.workers import release

        for worker in list(self._queued):
            try:
                if self._pool.tryTake(worker):
                    release(worker)               # it will never say it is done
            except RuntimeError:                  # already finished and deleted
                pass
        self._queued.clear()

    def _forget(self, worker: Any) -> None:
        if worker in self._queued:
            self._queued.remove(worker)

    def idle(self) -> None:
        """The box was emptied: nothing is in flight that matters any more."""
        self._take_back()
        self._latest += 1
        self._hide_busy()
        self.undim()

    # -- the busy line ----------------------------------------------------------

    def busy(self) -> bool:
        return not self.bar.isHidden()

    def _show_busy(self) -> None:
        view = self._view()
        if view is None or not getattr(view, "_shown_anything", False):
            return                                # an empty list has its skeleton
        self._place()
        self.bar.show()
        self.bar.raise_()

    def _hide_busy(self) -> None:
        self._busy_timer.stop()
        self.bar.hide()

    def _ended(self, generation: int) -> None:
        """A search answered (finished, not early) or failed."""
        if busy_ends(generation, self._latest, early=False):
            self._hide_busy()

    def _place(self) -> None:
        parent = self.bar.parentWidget()
        if parent is not None:
            self.bar.setGeometry(0, 0, parent.width(), BUSY_LINE_PX)

    def eventFilter(self, watched: Any, event: Any) -> bool:      # noqa: N802 - Qt's name
        if event.type() == QEvent.Type.Resize and watched is self.bar.parentWidget():
            self._place()
        return False

    # -- an answer of nothing ---------------------------------------------------

    def nothing_found(self, response: Any, summary: str, *, early: bool,
                      shown: bool) -> bool:
        """An answer with no rows. Returns whether rows are still on screen."""
        view = self._view()
        if view is None:
            return shown
        action = after_nothing(shown_anything=shown, early=early,
                               interim=bool(getattr(response, "interim", False)))
        if action == EMPTY:
            self.undim()
            view.results.clear(summary)
            return False
        self._dim(True)
        self._status.say_keeping_details(kept_words(view.input.text()))
        return True

    def found(self) -> None:
        """An answer with rows: whatever was dimmed is this search's now."""
        self.undim()

    def dimmed(self) -> bool:
        return self._dimmed

    def undim(self) -> None:
        self._dim(False)

    def _dim(self, on: bool) -> None:
        view = self._view()
        target: Optional[Any] = getattr(getattr(view, "results", None), "_list", None)
        if target is None or bool(on) == self._dimmed:
            return
        self._dimmed = bool(on)
        if on:
            effect = QGraphicsOpacityEffect(target)
            effect.setOpacity(DIM_OPACITY)
            target.setGraphicsEffect(effect)
        else:
            target.setGraphicsEffect(None)

    # -- the grid offer (MS-5) --------------------------------------------------

    def _grid_box(self) -> Any:
        view = self._view()
        boxes = getattr(getattr(getattr(view, "split", None), "switches", None), "boxes", {})
        return boxes.get("grid") if isinstance(boxes, dict) else None

    def offers(self, response: Any) -> list:
        """The thumbnail grid offered when most rows are pictures - `[]` otherwise."""
        from app.ui.thumbnail_loader import is_image_result

        box = self._grid_box()
        flags = [is_image_result(getattr(r, "ext", "")) for r in getattr(response, "results", ())]
        return grid_offer(flags, grid_on=box is None or box.isChecked())

    def _chosen(self, href: str) -> None:
        if str(href) == GRID_HREF:
            box = self._grid_box()
            if box is not None:
                box.setChecked(True)
