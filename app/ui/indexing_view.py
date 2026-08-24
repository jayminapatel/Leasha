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

from app.ui.presenter import format_count, format_eta, format_when, group_skips
from app.ui.workers import IndexWorker, run

__all__ = ["IndexingView"]


class IndexingView(QWidget):
    """Start, watch, pause and resume an index run; review what was skipped."""

    finished = pyqtSignal(object)        # IndexStats
    error = pyqtSignal(object)
    retry_requested = pyqtSignal(str)    # an error code to retry

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool.globalInstance()
        self._worker: Optional[IndexWorker] = None
        self._total_estimate = 0

        self.headline = QLabel("Nothing indexed yet.")
        self.headline.setObjectName("indexHeadline")

        # What is in the index right now, independent of any run. The page
        # previously showed nothing at all until an index was started, so
        # opening it answered none of "is there an index, how big, how old" -
        # which is the whole reason somebody opens it.
        self.totals = QLabel("")
        self.totals.setObjectName("indexTotals")

        self.bar = QProgressBar()
        self.bar.setTextVisible(True)
        self.bar.setRange(0, 1)
        self.bar.setValue(0)

        self.detail = QLabel("")
        self.detail.setObjectName("indexDetail")

        self.start_button = QPushButton("Start indexing")
        self.stop_button = QPushButton("Pause")
        self.stop_button.setEnabled(False)
        self.stop_button.setToolTip(
            "Stops cleanly. Everything already indexed is kept, and resuming costs nothing."
        )
        self.stop_button.clicked.connect(self.stop)

        controls = QHBoxLayout()
        controls.addWidget(self.start_button)
        controls.addWidget(self.stop_button)
        controls.addStretch(1)

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

    def refresh_totals(self, store: Any) -> None:
        """Show what is already indexed. Cheap enough for every tab switch."""
        try:
            stats = store.stats()
            last = store.get_state("index:last_run")
        except Exception:                    # noqa: BLE001 - a label is not worth crashing over
            return

        documents = int(stats.get("files_total", 0))
        chunks = int(stats.get("chunks_total", 0))
        if not documents:
            self.totals.setText("Nothing indexed yet.")
            return

        when = ""
        if last:
            try:
                from datetime import datetime

                moment = datetime.fromisoformat(last)
                when = f"  ·  last run {format_when(int(moment.timestamp() * 1e9))}"
            except ValueError:
                when = ""
        self.totals.setText(
            f"{documents:,} documents  ·  {chunks:,} searchable chunks{when}"
        )

    def start(self, pipeline: Any, *, total_estimate: int = 0) -> None:
        if self._worker is not None:
            return
        self._total_estimate = total_estimate
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
        if self._worker is not None:
            self._worker.stop()
            self.headline.setText("Finishing the current file…")
            self.stop_button.setEnabled(False)

    def _on_progress(self, stats: Any) -> None:
        done = stats.unchanged + stats.skipped
        # The denominator is what the walker has *found so far*, which grows as
        # it goes. Honest, and it moves - unlike a fixed total nobody can know
        # before the walk finishes, or an indeterminate bar that never resolves.
        total = max(self._total_estimate, stats.seen, done, 1)
        self.bar.setRange(0, total)
        self.bar.setValue(min(done, total))

        remaining = max(0, self._total_estimate - done) if self._total_estimate else 0
        eta = format_eta(remaining, files_per_minute=stats.files_per_minute)

        self.headline.setText(
            f"{format_count(stats.indexed)} documents  ·  "
            f"{format_count(stats.seen)} files seen  ·  "
            f"{format_count(stats.skipped)} skipped"
        )
        current = getattr(stats, "current", "")
        reading = f"  ·  reading {current}" if current else ""
        if current and getattr(stats, "current_item", 0):
            reading += f" [{stats.current_item:,}]"
        self.detail.setText(
            f"{format_count(stats.chunks)} chunks  ·  "
            f"{stats.files_per_minute:,.0f} files/min  ·  {eta}{reading}"
        )
        self.show_skips(stats.skipped_by_code)

    def _on_finished(self, stats: Any) -> None:
        self.bar.setRange(0, 1)
        self.bar.setValue(1)

        if stats.stopped_early is not None:
            self.headline.setText(stats.stopped_early.message)
            self.detail.setText(stats.stopped_early.suggestion)
        else:
            self.headline.setText(
                f"Finished: {format_count(stats.indexed)} indexed, "
                f"{format_count(stats.skipped)} skipped, "
                f"{format_count(stats.deleted)} removed"
            )
            self.detail.setText(
                f"{format_count(stats.chunks)} chunks in {stats.elapsed_s:,.0f}s  ·  "
                f"{stats.files_per_minute:,.0f} files/min, {stats.mb_per_minute:,.1f} MB/min"
            )
        self.show_skips(stats.skipped_by_code)
        self.finished.emit(stats)

    def _on_failed(self, error: Any) -> None:
        self.headline.setText(error.message)
        self.detail.setText(error.suggestion)
        self.error.emit(error)

    def _on_done(self) -> None:
        self._worker = None
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
