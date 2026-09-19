"""Indexing progress, and the panel that says what could not be read.

Layer: L5

The skipped-files panel is not an error log. On 100GB it is the answer to
"what is missing from my search results, and why?" — which is a question no
search box can answer and most search tools never even acknowledge. Four thousand
scanned PDFs is one row saying "4,000 files hold text as images", with the fix
beside it, not four thousand lines nobody reads.

Progress is reported by **file count, never by bytes.** Measured on a real
corpus, a 40MB slide deck yields fewer chunks than a 30KB Word document — file
size is off as a predictor of work by three orders of magnitude, so a byte-based
bar sits frozen on one deck and then races through a thousand documents.

**Pages-reorg order, §2: three shelves behind the same sidebar Settings
uses**, not one page with a schedule box and the whole Index Tuning screen
glued on beneath a skips panel that was never meant to share a page with
either. **§2b, the layout fix.** The owner reported the page as broken; the
cause, read rather than guessed: `shell.py` wraps Settings in a scroll area
(`wrap_if_needed(view, scroll=True)`) because it is "six group boxes
stacked vertically", but wraps this page with `scroll=False`, on the
assumption — true when it was written — that a page "built around a table,
list or splitter... already scrolls its own contents". That stopped being
true the moment the tuning order folded the whole Index Tuning screen
(a mode switch, a machine card, and four more group boxes) in underneath
the skips panel with nothing to scroll it: the page's required height
outgrew the window with no scrollbar anywhere to reach the rest of it.
`shell.py` is outside this thread's file scope, so the fix has to hold
without changing that line: each of the three shelves below carries its
*own* `widgets/scroll.scrollable` wrap internally, the same mechanism
Settings gets externally, so Schedule and Tuning scroll on their own
regardless of what `shell.py` decides for the page as a whole.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, QThreadPool, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.logging import logger
from app.ui.presenter import (
    finished_text,
    index_summary,
    progress_for,
    progress_text,
    read_index_summary,
    when_text,
)
from app.ui.indexing_settings import IndexingSettings
from app.ui.widgets.archived_roots import ArchivedRoots
from app.ui.widgets.category_nav import CategoryNav
from app.ui.widgets.external_run import paint_external
from app.ui.widgets.index_controls import build_controls
from app.ui.widgets.index_stats import IndexStats
from app.ui.widgets.scroll import scrollable
from app.ui.widgets.skips_panel import SkipsPanel
from app.ui.widgets.tuning_box import TuningBox
from app.ui.workers import CallableWorker, IndexWorker, run

__all__ = ["IndexingView"]

_log = logger.bind(component="ui.indexing")

#: The three shelves, in display order.
CATEGORY_STATUS = "Status"
CATEGORY_SCHEDULE = "Schedule"
CATEGORY_TUNING = "Tuning"




class IndexingView(QWidget):
    """Start, watch, pause and resume an index run; review what was skipped."""

    #: Asked for, not performed here. The view has no business deleting an
    #: index; the window owns the stores and does it, after confirming.
    reset_requested = pyqtSignal()

    finished = pyqtSignal(object)        # IndexStats
    error = pyqtSignal(object)
    #: UI Redesign (202626160950 §2d/§2g): the rail's pill paints from this -
    #: (state, indexed, value, total, paused, stopped_early, error) as plain
    #: data, emitted on the UI thread from the same slots that paint this page.
    progressed = pyqtSignal(str, int, int, int, bool, bool, str)
    #: The document count the totals worker came back with, for the pill.
    totals_shown = pyqtSignal(int)
    retry_requested = pyqtSignal(str)    # an error code to retry
    #: "Rescan these folders now" on the archived-folders panel. One full walk,
    #: not a change of policy - the modes stay as they are.
    rescan_archives_requested = pyqtSignal()
    #: Count the corpus before indexing it, so the bar has a real denominator.
    scan_requested = pyqtSignal()
    #: Stop a run belonging to **another process**. The window owns the store,
    #: so it writes the flag; this view only knows that it was asked for.
    stop_requested_externally = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # **Its own pool, with one thread.**
        #
        # This used `QThreadPool.globalInstance()`, which every search, filename
        # lookup, mail filter and environment check also uses. That pool has
        # roughly one thread per core, and an index run holds a slot for *hours*
        # - so on a four-core machine a quarter of the interactive capacity is
        # gone for the duration, and a burst of typing can queue behind it.
        #
        # The work was always on a worker; it was competing with the work that
        # has somebody waiting on it. A dedicated pool means indexing can never
        # starve a keystroke, which is what "runs in the background" has to mean
        # if it is to mean anything.
        #
        # One thread because the pipeline manages its own file workers
        # internally, governed by the memory and CPU ceilings in Settings.
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._worker: Optional[IndexWorker] = None
        self._refreshing = False
        #: True between clicking Stop and the run ending. Stopping can take a
        #: while on a large file, and a dead button with no explanation reads
        #: as a click that was ignored.
        self._stopping = False
        self._next_run_text = ""
        self._total_estimate = 0
        #: The published record of a run **another process** is doing, or None.
        #: Set by `show_external`, which the window calls on a poll. Keeping it
        #: here rather than re-reading the store is what lets `is_running` and
        #: `stop` answer without touching a database on the UI thread.
        self._external: Optional[dict] = None

        self.headline = QLabel("Nothing indexed yet.")
        self.headline.setObjectName("indexHeadline")

        # What is in the index right now, independent of any run. The page
        # previously showed nothing at all until an index was started, so
        # opening it answered none of "is there an index, how big, how old" -
        # which is the whole reason somebody opens it.
        # A grid, not a sentence. The page previously carried one line -
        # documents and chunks - and showed *nothing at all* when the store read
        # failed, because the handler was `except: return`. A blank page is the
        # worst answer to "is my index working": it looks identical to an empty
        # index, a broken one, and a bug.
        self.totals = QLabel("")
        self.totals.setObjectName("indexTotals")
        self.totals.setVisible(False)

        self.stats_box = IndexStats()

        self.bar = QProgressBar()
        self.bar.setTextVisible(True)
        self.bar.setRange(0, 1)
        self.bar.setValue(0)

        self.detail = QLabel("")
        self.detail.setObjectName("indexDetail")

        # Things that are not failures but are worth knowing before a run that
        # takes days - "40GB free on the index drive" at minute one rather than
        # at hour sixty. Hidden until there is one, so it costs no space.
        self.notices = QLabel("")
        self.notices.setObjectName("indexNotices")
        self.notices.setWordWrap(True)
        self.notices.setVisible(False)

        # Four buttons and the sentences that say what each will do - see
        # `widgets.index_controls`. Out of the view because this file is at the
        # 250-line guard, and because "what happens when I press this" is copy
        # rather than layout.
        (self.start_button, self.scan_button, self.stop_button,
         self.reset_button, controls) = build_controls(
            on_stop=self.stop,
            on_scan=lambda: self.scan_requested.emit(),
            on_reset=lambda: self.reset_requested.emit())

        # Both panels are their own widgets: this view had reached the 250-line
        # guard, and the guard is right - a view that keeps growing is a view
        # where logic starts to live.
        self.skips = SkipsPanel(self.retry_requested)
        # *"Skip cheaply, but never silently."* A folder deliberately not walked
        # must say so, with its count and the date of its last full pass, or it
        # is indistinguishable from one that was never indexed.
        self.archives = ArchivedRoots()
        self.archives.rescan_requested.connect(self.rescan_archives_requested)

        # **Its own shelf now, not glued beneath the skips panel.** Tuning is
        # watched, not configured once: somebody changes a ceiling because of
        # what the bar in front of them is doing, and a screen that makes
        # them go and find another tab to do it is a screen they use once.
        # The controls themselves are widgets, because this file is at
        # the 250-line guard - see `widgets/tuning_box.py`.
        self.schedule_box = IndexingSettings()
        self.tuning = TuningBox()

        # --- §2a: three shelves, one sidebar (see the module docstring for
        # §2b, the layout fix this split is).
        #
        # **Status keeps its old, unwrapped shape.** `self.skips` already
        # scrolls its own contents (`stretch=1`, exactly as before) and was
        # never the reported fault - wrapping it in a second scroll area
        # would only reintroduce the two-scrollbars problem `widgets/scroll.py`
        # warns about. Schedule and Tuning are the two shelves that pushed the
        # old single page past its height with nothing to scroll it, so they
        # are the two that get `scrollable()` - the same mechanism `shell.py`
        # already gives Settings, applied here internally because `shell.py`
        # itself is outside this thread's file scope.
        status_page = QWidget()
        status_layout = QVBoxLayout(status_page)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(8)
        status_layout.addWidget(self.headline)
        status_layout.addWidget(self.totals)
        status_layout.addWidget(self.stats_box)
        status_layout.addWidget(self.bar)
        status_layout.addWidget(self.detail)
        status_layout.addWidget(self.notices)
        status_layout.addLayout(controls)
        status_layout.addWidget(self.archives)
        status_layout.addWidget(self.skips, stretch=1)

        schedule_page = QWidget()
        schedule_layout = QVBoxLayout(schedule_page)
        schedule_layout.setContentsMargins(0, 0, 0, 0)
        schedule_layout.addWidget(self.schedule_box)
        schedule_layout.addStretch(1)

        tuning_page = QWidget()
        tuning_layout = QVBoxLayout(tuning_page)
        tuning_layout.setContentsMargins(0, 0, 0, 0)
        tuning_layout.addWidget(self.tuning)
        tuning_layout.addStretch(1)

        self._nav = CategoryNav()
        self._nav.add_category(CATEGORY_STATUS, status_page)
        self._nav.add_category(CATEGORY_SCHEDULE, scrollable(schedule_page))
        self._nav.add_category(CATEGORY_TUNING, scrollable(tuning_page))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._nav)

    # -- running ------------------------------------------------------------

    def is_running(self) -> bool:
        r"""Is an index run in flight **anywhere on this machine**?

        The scheduler asks before starting one. Two runs writing into the same
        SQLite file is exactly the corruption the lock prevents between
        processes, and nothing prevented it inside one.

        This used to be `self._worker is not None`, which stopped being the
        whole answer the moment the run lock was split from the window lock: an
        `app.cli index` may be under way with nothing in this process knowing.
        The scheduler firing into that would have produced an error rather than
        a corruption - the lock still holds - but an error on a timer, every
        hour, for a reason the person never sees, is its own kind of broken.
        """
        return self._worker is not None or bool(self._external)

    def refresh_totals(self, store: Any, settings: Any = None) -> None:
        """Read the index summary in a worker and paint it.

        **Never leaves the panel blank.** The previous version was
        `except: return`, so a locked or missing database produced an empty page
        with nothing to explain it - and an empty page is indistinguishable from
        an empty index. Every outcome now produces rows, including the failure.
        """
        if self._refreshing:
            return
        self._refreshing = True

        worker = CallableWorker(
            read_index_summary, store, settings, component="ui.index.totals"
        )
        worker.signals.finished.connect(self._show_totals)
        worker.signals.done.connect(self._totals_done)
        run(QThreadPool.globalInstance(), worker)

    def _totals_done(self) -> None:
        self._refreshing = False

    def _show_totals(self, payload: dict) -> None:
        """Paint the summary. UI thread, no I/O."""
        rows = index_summary(
            payload.get("stats"),
            payload.get("vectors"),
            data_path=payload.get("data_path", ""),
            disk_bytes=payload.get("disk_bytes"),
            last_run=when_text(payload.get("last_run") or ""),
            next_run=self._next_run_text,
            error=payload.get("error", ""),
        )
        self.stats_box.show_rows(rows)
        stats = payload.get("stats") or {}
        try:
            self.totals_shown.emit(int(stats.get("files_total", 0) or 0))
        except (AttributeError, TypeError, ValueError):
            pass

    @property
    def pool(self) -> QThreadPool:
        """The pool an index run lives in, so a closing window can wait on it.

        It is not the global pool - see `__init__` - which is exactly why
        `MainWindow._drain_workers` could not see the run and let the window
        close over one that was still going.
        """
        return self._pool

    def set_next_run(self, text: str) -> None:
        self._next_run_text = text

    def start(self, pipeline: Any, *, total_estimate: int = 0) -> None:
        if self._worker is not None:
            return
        self._total_estimate = total_estimate
        self._stopping = False
        # A determinate bar with no total is a barber pole that spins forever,
        # which reads as "stuck" - and the caller never had a total to give,
        # because the walker discovers files as it goes. So it starts at zero
        # and grows its own denominator from `seen` on the first progress tick.
        self.bar.setRange(0, max(1, total_estimate))
        self.bar.setValue(0)
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.headline.setText("Indexing…")

        worker = IndexWorker(pipeline)
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(self._on_finished)
        worker.signals.failed.connect(self._on_failed)
        worker.signals.done.connect(self._on_done)
        self._worker = worker
        run(self._pool, worker)

    def stop(self) -> None:
        """Ask the run to end after the file it is on.

        Both buttons are disabled while it winds down, which is correct - there
        is nothing useful to click - but it left the panel looking frozen with
        no explanation. `_stopping` makes the progress ticks say what is going
        on instead, and `_on_done` clears it.
        """
        if self._worker is not None:
            self._stopping = True
            self._worker.stop()
        elif self._external:
            # **A run this window did not start.** The two processes share
            # nothing but the database, so the request goes there and the runner
            # picks it up at its next checkpoint. A request rather than a kill:
            # terminating it would leave the vector store mid-write, which is
            # the one thing the lock exists to prevent.
            self._stopping = True
            self.stop_requested_externally.emit()
        else:
            return
        self.headline.setText("Stopping after the current file…")
        self.detail.setText("Everything indexed so far is kept.")
        self.stop_button.setEnabled(False)

    def show_external(self, record: Any, *, locked: bool) -> None:
        """Draw a run this window did not start. See `widgets.external_run`.

        One line here because the view is at its length guard and because the
        decisions - is this record live, whose run is it, what does the bar read
        - are all testable without Qt and belong beside the presenter rules they
        use rather than in a widget tree.
        """
        paint_external(self, record, locked=locked)

    def _on_progress(self, stats: Any) -> None:
        # See `presenter.progress_for` for both bugs this has had: the numerator
        # once left out `indexed`, so a fresh corpus sat near zero for hours;
        # then the denominator was `seen`, which a bounded queue keeps close to
        # the numerator, so it read 100% within seconds of starting.
        # `(0, 0)` is Qt's indeterminate range - a moving barber pole - and it
        # is what `progress_for` returns while the size of the job is genuinely
        # unknown. No branch is needed: `setValue` on an indeterminate bar is
        # ignored, so the same two lines serve both cases.
        value, total = progress_for(stats, total_estimate=self._total_estimate)
        self.bar.setRange(0, total)
        self.bar.setValue(value)
        self.progressed.emit("running", int(getattr(stats, "indexed", 0) or 0),
                             int(value), int(total),
                             bool(getattr(stats, "paused", False)), False, "")

        headline, detail = progress_text(
            stats, total_estimate=self._total_estimate, stopping=self._stopping
        )
        self.headline.setText(headline)
        self.detail.setText(detail)
        self.skips.show_skips(stats.skipped_by_code)
        self.archives.show_roots(getattr(stats, "skipped_roots", ()))
        self.show_notices(getattr(stats, "notices", ()))

    def _on_finished(self, stats: Any) -> None:
        # **Only a run that reached the end is full.** `pipeline.run` returns
        # normally after a Stop and after the governor aborts, so this painted a
        # complete green bar over a run stopped at 3% - next to a headline
        # saying it had been stopped, so the panel contradicted itself. The same
        # fault was found and fixed in `_on_failed` and not here.
        finished_whole = not self._stopping and not getattr(stats, "stopped_early", None)
        self.bar.setRange(0, 1)
        self.bar.setValue(1 if finished_whole else 0)
        self.progressed.emit("finished", int(getattr(stats, "indexed", 0) or 0),
                             1, 1, False, not finished_whole, "")
        headline, detail = finished_text(stats)
        self.headline.setText(headline)
        self.detail.setText(detail)
        self.skips.show_skips(stats.skipped_by_code)
        self.archives.show_roots(getattr(stats, "skipped_roots", ()))
        self.show_notices(getattr(stats, "notices", ()))
        self.finished.emit(stats)

    def _on_failed(self, error: Any) -> None:
        # Reset the bar. A run that failed at 40% left the bar at 40%, which
        # invites the reading that it is still going - and the buttons come
        # back a moment later, so the panel contradicts itself.
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.headline.setText(error.message)
        self.detail.setText(error.suggestion)
        self.progressed.emit("failed", 0, 0, 1, False, False,
                             str(getattr(error, "message", "") or ""))
        self.error.emit(error)

    def _on_done(self) -> None:
        self._worker = None
        self._stopping = False
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    # -- the panels below the bar -------------------------------------------

    def show_skips(self, summary: dict[str, int]) -> None:
        """Kept as a method because the window and the tests both call it."""
        self.skips.show_skips(summary)

    def show_notices(self, notices: Any) -> None:
        """Draw the run's notices, or hide the label when there are none."""
        lines = [str(line) for line in (notices or ()) if str(line).strip()]
        self.notices.setVisible(bool(lines))
        self.notices.setText("\n".join(lines))
