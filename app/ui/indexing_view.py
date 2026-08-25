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
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, QThreadPool, pyqtSignal
from PyQt6.QtWidgets import (
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core.logging import logger
from app.ui.presenter import (
    format_count,
    group_skips,
    finished_text,
    index_summary,
    progress_for,
    progress_text,
    read_index_summary,
    when_text,
)
from app.ui.widgets.index_stats import IndexStats
from app.ui.workers import CallableWorker, IndexWorker, run

__all__ = ["IndexingView"]

_log = logger.bind(component="ui.indexing")




class IndexingView(QWidget):
    """Start, watch, pause and resume an index run; review what was skipped."""

    #: Asked for, not performed here. The view has no business deleting an
    #: index; the window owns the stores and does it, after confirming.
    reset_requested = pyqtSignal()

    finished = pyqtSignal(object)        # IndexStats
    error = pyqtSignal(object)
    retry_requested = pyqtSignal(str)    # an error code to retry

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool.globalInstance()
        self._worker: Optional[IndexWorker] = None
        self._refreshing = False
        #: True between clicking Stop and the run ending. Stopping can take a
        #: while on a large file, and a dead button with no explanation reads
        #: as a click that was ignored.
        self._stopping = False
        self._next_run_text = ""
        self._total_estimate = 0

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

        self.start_button = QPushButton("Start indexing")
        # **"Stop", not "Pause".** It said Pause and there is no resume: the
        # run ends, and the next Start begins a new one. It is a cheap end -
        # everything already indexed is kept and nothing is redone - but a
        # button that promises to pause and then stops is a button people stop
        # trusting. The automatic pausing in the status line is a different
        # thing entirely: that is the resource governor, and it does resume.
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.setToolTip(
            "Stops after the current file. Everything already indexed is kept, "
            "and starting again picks up where this left off rather than redoing it."
        )
        self.stop_button.clicked.connect(self.stop)

        # Destructive, so it is placed away from Start and asks before acting.
        self.reset_button = QPushButton("Reset index…")
        self.reset_button.setToolTip(
            "Delete everything indexed and start over.\n\n"
            "Your documents are never touched - the index is built from them and "
            "can always be rebuilt. What it costs is the time to index again."
        )
        self.reset_button.clicked.connect(lambda _c=False: self.reset_requested.emit())

        controls = QHBoxLayout()
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        controls.addStretch(1)
        controls.addWidget(self.reset_button)

        self._skips_box = QGroupBox("Skipped files")
        self._skips_layout = QVBoxLayout(self._skips_box)
        self._skips_box.setVisible(False)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._skips_box)

        # `scroll` takes the stretch, and the skipped box inside it is hidden
        # until there is something to show. Maximised, that left an enormous
        # empty panel with the controls squashed at the top - so it only claims
        # space once it has content, and a spacer absorbs the rest.
        self._scroll = scroll
        scroll.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.addWidget(self.headline)
        layout.addWidget(self.totals)
        layout.addWidget(self.stats_box)
        layout.addWidget(self.bar)
        layout.addWidget(self.detail)
        layout.addLayout(controls)
        layout.addWidget(scroll, stretch=1)
        layout.addStretch(1)

    # -- running ------------------------------------------------------------

    def is_running(self) -> bool:
        """Is an index run in flight?

        The scheduler asks before starting one. Two runs writing into the same
        SQLite file is exactly the corruption the single-instance lock prevents
        between processes, and nothing prevented it inside one.
        """
        return self._worker is not None

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
            self.headline.setText("Stopping after the current file…")
            self.detail.setText("Everything indexed so far is kept.")
            self.stop_button.setEnabled(False)

    def _on_progress(self, stats: Any) -> None:
        # See `presenter.progress_for`: the numerator used to leave out
        # `indexed`, so a first index of a fresh corpus sat near zero for hours
        # while the log showed thousands of files done.
        value, total = progress_for(stats, total_estimate=self._total_estimate)
        self.bar.setRange(0, total)
        self.bar.setValue(value)

        headline, detail = progress_text(
            stats, total_estimate=self._total_estimate, stopping=self._stopping
        )
        self.headline.setText(headline)
        self.detail.setText(detail)
        self.show_skips(stats.skipped_by_code)

    def _on_finished(self, stats: Any) -> None:
        self.bar.setRange(0, 1)
        self.bar.setValue(1)
        headline, detail = finished_text(stats)
        self.headline.setText(headline)
        self.detail.setText(detail)
        self.show_skips(stats.skipped_by_code)
        self.finished.emit(stats)

    def _on_failed(self, error: Any) -> None:
        # Reset the bar. A run that failed at 40% left the bar at 40%, which
        # invites the reading that it is still going - and the buttons come
        # back a moment later, so the panel contradicts itself.
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.headline.setText(error.message)
        self.detail.setText(error.suggestion)
        self.error.emit(error)

    def _on_done(self) -> None:
        self._worker = None
        self._stopping = False
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    # -- the skipped panel --------------------------------------------------

    def show_skips(self, summary: dict[str, int]) -> None:
        while self._skips_layout.count():
            item = self._skips_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        groups = group_skips(summary or {})
        self._skips_box.setVisible(bool(groups))
        # The scroll area only claims layout space when it has something in it.
        # Left permanently visible it swallowed the whole window when maximised.
        self._scroll.setVisible(bool(groups))
        if not groups:
            return

        total = sum(group.count for group in groups)
        self._skips_box.setTitle(f"{format_count(total)} files skipped — review")

        for group in groups:
            self._skips_layout.addWidget(_SkipRow(group, self.retry_requested))
        self._skips_layout.addStretch(1)


class _SkipRow(QWidget):
    """One reason, its count, its fix, and a retry button only when one helps."""

    def __init__(self, group: Any, retry_signal: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        heading = QLabel(f"{format_count(group.count)} × {group.message}")
        heading.setWordWrap(True)
        heading.setObjectName("skipHeading")

        fix = QLabel(group.suggestion)
        fix.setWordWrap(True)
        fix.setObjectName("skipFix")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(heading)
        layout.addWidget(fix)

        if group.examples:
            examples = QLabel("e.g. " + ", ".join(group.examples))
            examples.setWordWrap(True)
            examples.setObjectName("skipExamples")
            layout.addWidget(examples)

        if group.retryable:
            # Only when it can help. A retry button on 4,000 scanned PDFs would
            # do nothing at all, which is worse than not offering one.
            button = QPushButton("Retry these")
            button.setMaximumWidth(140)
            button.clicked.connect(lambda: retry_signal.emit(group.code))
            layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)
