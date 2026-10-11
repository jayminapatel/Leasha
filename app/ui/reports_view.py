r"""The Space Report pane: the one report there is, inside the Tidy page.

Layer: L5, driving L4 (`app/reports/`)

Order 202626270602 (0n) section 1 made a Reports page of a list of reports.
Order 1q (2026-10-11) took Digital Inheritance away at the owner's word and put
what was left - the Space Report - in the Tidy page's list (`tidy_view.py`), so
this is a pane now, not a page. **It decides nothing about what the report
says** - `app/reports/space.py` builds the document; this shows it as a table
and asks for an Export location.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QThreadPool, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

# The PDF writer, kept under the name it had while it lived in this file
# (2026-10-02: moved to keep this view under its line limit).
from app.ui.report_pdf import write_pdf as _write_pdf
from app.ui.widgets.space_table import SpaceTables
from app.ui.workers import CallableWorker, run

__all__ = ["ReportsView"]


class ReportsView(QWidget):
    """The Space Report: read it, sort it, export it."""

    error = Signal(object)

    def __init__(self, store: Any = None, parent: Optional[QWidget] = None) -> None:
        """Build the report pane. UI thread; `refresh` reads on a worker."""
        super().__init__(parent)
        self._store = store
        self._generated_at: Optional[int] = None
        #: Section 3c's cache: `_report_snapshot` skips the expensive Space
        #: Report queries entirely when the store's own data timestamp has
        #: not moved since this value - the same signal `1b` already shows
        #: the user, so no separate "am I stale" marker to invent or forget
        #: to update.
        self._space_cached_at: Optional[int] = None
        #: The Space Report, rendered on the worker - it touches the store,
        #: so never on this thread.
        self._space_document: str = ""
        #: 2026-10-02: one load at a time. `_loading` is True from `refresh`
        #: starting a worker until that worker is done; `_refresh_again` is a
        #: refresh that was asked for meanwhile, run once the first has landed.
        self._loading = False
        self._refresh_again = False

        self.timestamp = QLabel("")
        self.timestamp.setObjectName("resultsSummary")

        #: Order 0n section 3c: a worker stage, not a spinner - cleared the
        #: instant the worker finishes or is skipped as unchanged.
        self.progress_label = QLabel("")
        self.progress_label.setObjectName("resultsSummary")

        self.body = QTextBrowser()
        self.body.setAccessibleName("Report contents")
        self.body.setOpenExternalLinks(False)
        #: Order 0n section 3a: the Space Report on screen is a sortable table
        #: whose rows open to show their copies. Export still writes the
        #: document - both are made from the same findings (`SpaceDocument`).
        self.space_table = SpaceTables()
        self.space_table.hide()

        self.export = QPushButton("Export…")
        # 1q: the source picker went with Digital Inheritance; this says what
        # the button does now.
        self.export.setToolTip("Save the Space Report as a PDF you can print or keep.")
        self.export.clicked.connect(self._start_export)
        self.export.setEnabled(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.timestamp)
        layout.addWidget(self.progress_label)
        layout.addWidget(self.body, 1)
        layout.addWidget(self.space_table, 1)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.export)
        layout.addLayout(buttons)

    # -- filling it in --------------------------------------------------

    def refresh(self) -> None:
        """Re-read the report, off a worker - every time it comes forward,
        the same "refresh on forward, never on a timer" rule the other pages
        follow."""
        if self._store is None:
            return
        # **One load at a time** (2026-10-02). A second load started before
        # the first had finished ran every Space Report query again, and its
        # answer, equal to the first but a new object, rebuilt the table: the
        # sort and any opened row gone, under the person's pointer. A refresh
        # asked for while one is running waits for it and then runs with the
        # stamp that load left, which costs one cheap query when nothing moved.
        if self._loading:
            self._refresh_again = True
            return
        self._loading = True
        worker = CallableWorker(
            _report_snapshot, self._store, self._space_cached_at,
            component="ui.reports", report_progress=True)
        worker.signals.progress.connect(self._on_progress)
        worker.signals.finished.connect(self._loaded)
        worker.signals.failed.connect(self.error.emit)
        # `done` comes after `finished` or `failed`, and on its own for a load
        # abandoned at shutdown - so the page can never be left "loading".
        worker.signals.done.connect(self._load_done)
        run(QThreadPool.globalInstance(), worker)

    def _load_done(self) -> None:
        """UI thread: the load's worker is done; run a refresh asked for meanwhile."""
        self._loading = False
        if self._refresh_again:
            self._refresh_again = False
            self.refresh()

    def _on_progress(self, stage: Any) -> None:
        self.progress_label.setText(str(stage))

    def _loaded(self, snapshot: Any) -> None:
        """UI thread: keep the snapshot (None means unchanged) and redraw the report."""
        self.progress_label.setText("")
        if snapshot is None:
            # Section 3c: the store's data timestamp had not moved since
            # the last load, so the worker skipped the Space Report's own
            # queries entirely - what is already on screen is still current.
            return
        self._generated_at, self._space_document = snapshot
        self._space_cached_at = self._generated_at
        self.export.setEnabled(bool(self._space_document))
        self._show()

    def _show(self) -> None:
        """Draw the report from the snapshot in hand - no store read."""
        from app.reports.common import data_timestamp_sentence

        self.timestamp.setText(data_timestamp_sentence(self._generated_at))
        findings = getattr(self._space_document, "findings", None)
        if findings is not None:
            self.space_table.set_findings(findings, getattr(self._space_document, "shaped", None))
        else:
            self.body.setMarkdown(self._space_document or "Nothing indexed yet.")
        self.body.setVisible(findings is None)
        self.space_table.setVisible(findings is not None)

    # -- export -----------------------------------------------------------

    def _start_export(self) -> None:
        """Export: ask for a path; the PDF is written on a worker."""
        if not self._space_document:
            return
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export report", "space-report.pdf", "PDF files (*.pdf)")
        if path:
            worker = CallableWorker(_write_pdf, self._space_document, path,
                                    component="ui.reports.export")
            worker.signals.failed.connect(self.error.emit)
            run(QThreadPool.globalInstance(), worker)


def _report_snapshot(
    store: Any, last_known_generated_at: Optional[int] = None,
    on_progress: Any = None,
) -> Optional[tuple]:
    """The read-only half of a refresh, off the worker thread.

    Section 3c, performance: `report_generated_at` (`MAX(files.indexed_at)`)
    is the same "data as of" timestamp `1b` already shows the user, and it
    only moves when an index run actually adds or touches a file. Checking it
    first, cheaply, and returning `None` when it has not moved skips the
    expensive duplicate/uniqueness queries on every visit that changes nothing.
    """
    from app.reports.common import report_generated_at
    from app.reports.space import (
        SpaceFindings, document_for, find_duplicate_groups,
        find_near_duplicate_photo_groups, find_source_duplicate_share,
        find_source_uniqueness, hash_coverage, total_reclaimable_bytes,
    )
    from app.ui.presenter.space_rows import shape

    generated_at = report_generated_at(store)
    if last_known_generated_at is not None and generated_at == last_known_generated_at:
        return None

    def stage(text: str) -> None:
        if on_progress is not None:
            on_progress(text)

    stage("Finding duplicates...")
    groups = find_duplicate_groups(store)
    stage("Finding similar photos...")
    near_duplicates = find_near_duplicate_photo_groups(store)
    stage("Working out duplication by source...")
    duplicate_share = find_source_duplicate_share(store)
    stage("Checking what exists nowhere else...")
    uniqueness = find_source_uniqueness(store)
    stage("Writing the report...")
    # The Space Report, exactly as `app.cli report space` builds it - and it
    # carries the findings, so the page can show them as a table.
    space = document_for(SpaceFindings(
        groups=tuple(groups), near_duplicates=tuple(near_duplicates),
        duplicate_share=tuple(duplicate_share), uniqueness=tuple(uniqueness),
        total_reclaimable=total_reclaimable_bytes(store), generated_at=generated_at, **hash_coverage(store)))
    # 2026-10-03: the table's rows are shaped here too, off the window's thread.
    return generated_at, shape(space)
