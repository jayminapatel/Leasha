r"""The Reports page: read-only surfaces over the existing index.

Layer: L5, driving L4 (`app/reports/`)

Order 202626270602 (0n) section 1. A sibling of Indexing/Settings, not one
of the four search tabs - reports are outputs, not searches. **This view
decides nothing about what a report says** - `app/reports/inheritance.py`
builds the document; this shows it and asks for an Export location. Room
for future reports: `REPORTS` is a small registry, not two hard-coded
screens, so a second report is one more entry rather than a second view.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QThreadPool, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

# The PDF writer, kept under the name it had while it lived in this file
# (2026-10-02: moved to keep this view under its line limit).
from app.ui.report_pdf import write_pdf as _write_pdf
from app.ui.widgets.report_export_dialog import SourceSelectionDialog
from app.ui.widgets.report_list import ReportList
from app.ui.widgets.space_table import SpaceTables
from app.ui.widgets.timeline_host import REPORT_KEY, attach_timeline, show_timeline_only
from app.ui.workers import CallableWorker, run

__all__ = ["ReportsView", "REPORTS"]

#: (key, title, one-line plain-words description). Room for future reports:
#: adding one is a new row here plus a case in `_render`, never a second view.
REPORTS: tuple[tuple[str, str, str], ...] = (
    ("inheritance", "Digital Inheritance",
     "A map of every source Leasha knows about - names, locations and "
     "what's in each - for someone who isn't you."),
    # Order 0n section 3, wired by WORKORDER-space-report-and-idle-tune-ui-wiring
    # (2026-09-16). The content is `app.reports.space` - the same words
    # `leasha report space` prints.
    ("space", "The Space Report",
     "Which files exist in more than one place, how much room the copies "
     "take, and what exists nowhere else - so you know what is safe to "
     "clear and what is not."),
    # Order 0n section 4: a place to wander rather than a document. Its own
    # view (`timeline_view.py`) sits in the same pane; nothing here decides
    # what it shows.
    ("timeline", "Browse your timeline",
     "Everything from a month or year - photos, files and mail together, "
     "wherever they are kept now - in the order it happened."),
)


class ReportsView(QWidget):
    """A list of reports; pick one, read it, export it."""

    error = Signal(object)
    #: A timeline entry was opened / shown in its folder - it carries `path`,
    #: `volume_id` and `relative_path`, everything the shell's own opener reads.
    opened = Signal(object)
    reveal_requested = Signal(object)

    def __init__(self, store: Any = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._sources: list = []
        self._generated_at: Optional[int] = None
        #: Section 3c's cache: `_report_snapshot` skips the expensive Space
        #: Report queries entirely when the store's own data timestamp has
        #: not moved since this value - the same signal `1b` already shows
        #: the user, so no separate "am I stale" marker to invent or forget
        #: to update.
        self._space_cached_at: Optional[int] = None
        #: The Space Report, rendered on the worker with the rest of the
        #: snapshot - it touches the store, so never on this thread.
        self._space_document: str = ""
        #: 2026-10-02: one load at a time. `_loading` is True from `refresh`
        #: starting a worker until that worker is done; `_refresh_again` is a
        #: refresh that was asked for meanwhile, run once the first has landed.
        self._loading = False
        self._refresh_again = False

        intro = QLabel(
            "What Leasha has catalogued, read out as a document rather "
            "than searched. Nothing here changes anything on your drives."
        )
        intro.setWordWrap(True)

        self.list = ReportList(REPORTS)        # 2026-10-04: with icons, like the category lists
        self.list.currentRowChanged.connect(self._show_selected)

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
        self.export.setToolTip(
            "Choose which sources to include, then save this report as a "
            "PDF you can print or hand to someone else.")
        self.export.clicked.connect(self._start_export)
        self.export.setEnabled(False)

        right = QVBoxLayout()
        right.addWidget(self.timestamp)
        right.addWidget(self.progress_label)
        right.addWidget(self.body, 1)
        right.addWidget(self.space_table, 1)
        self.timeline = attach_timeline(self, store, right)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.export)
        right.addLayout(buttons)
        right_widget = QWidget()
        right_widget.setLayout(right)

        split = QSplitter()
        split.addWidget(self.list)
        split.addWidget(right_widget)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 3)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(split, 1)

        if self.list.count():
            self.list.setCurrentRow(0)

    # -- filling it in --------------------------------------------------

    def refresh(self) -> None:
        """Re-read the catalogue, off a worker - every tab switch, the
        same "refresh on forward, never on a timer" rule the other pages
        already follow."""
        if self._store is None:
            return
        self.timeline.refresh()
        # **One load at a time** (2026-10-02). Each load carries the "data as
        # of" stamp it was started with, and that stamp only moves when a load
        # lands - so a second load started before the first had finished ran
        # every Space Report query again, and its answer, equal to the first
        # but a new object, rebuilt the table: the sort and any opened row
        # gone, under the person's pointer. Leaving Reports and coming straight
        # back was enough. A refresh asked for while one is running now waits
        # for it and then runs with the stamp that load left, which costs one
        # cheap query when nothing has moved.
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
        self._loading = False
        if self._refresh_again:
            self._refresh_again = False
            self.refresh()

    def _on_progress(self, stage: Any) -> None:
        self.progress_label.setText(str(stage))

    def _loaded(self, snapshot: Any) -> None:
        self.progress_label.setText("")
        if snapshot is None:
            # Section 3c: the store's data timestamp had not moved since
            # the last load, so the worker skipped the Space Report's own
            # queries entirely - what is already on screen is still current.
            return
        self._sources, self._generated_at, self._space_document = snapshot
        self._space_cached_at = self._generated_at
        self.export.setEnabled(bool(self._sources) or bool(self._space_document))
        self._show_selected(self.list.currentRow())

    def _show_selected(self, row: int) -> None:
        from app.reports.inheritance import data_timestamp_sentence, render_inheritance_document

        self.timestamp.setText(data_timestamp_sentence(self._generated_at))
        tabled = False
        if show_timeline_only(self, row >= 0 and self.list.item(row).data(REPORT_KEY) == "timeline"):
            return
        if row < 0:
            self.body.setMarkdown("Nothing indexed yet.")
            return
        key = self.list.item(row).data(REPORT_KEY)
        if key == "inheritance":
            if not self._sources:
                self.body.setMarkdown("Nothing indexed yet.")
            else:
                self.body.setMarkdown(
                    render_inheritance_document(self._sources, generated_at=self._generated_at))
        elif key == "space":
            findings = getattr(self._space_document, "findings", None)
            if findings is not None:
                self.space_table.set_findings(
                    findings, getattr(self._space_document, "shaped", None))
            else:
                self.body.setMarkdown(self._space_document or "Nothing indexed yet.")
            tabled = findings is not None
        self.body.setVisible(not tabled)
        self.space_table.setVisible(tabled)

    # -- export -----------------------------------------------------------

    def _selected_key(self) -> str:
        row = self.list.currentRow()
        return str(self.list.item(row).data(REPORT_KEY)) if row >= 0 else ""

    def _start_export(self) -> None:
        if self._selected_key() == "space":
            # No source picker: the Space Report is about the whole corpus.
            if not self._space_document:
                return
            path, _filter = QFileDialog.getSaveFileName(
                self, "Export report", "space-report.pdf", "PDF files (*.pdf)")
            if path:
                worker = CallableWorker(_write_pdf, self._space_document, path,
                                        component="ui.reports.export")
                worker.signals.failed.connect(self.error.emit)
                run(QThreadPool.globalInstance(), worker)
            return
        if not self._sources:
            return
        dialog = SourceSelectionDialog(self._sources, self)
        if dialog.exec() != SourceSelectionDialog.DialogCode.Accepted:
            return
        excluded = dialog.excluded_names()
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export report", "report.pdf", "PDF files (*.pdf)")
        if not path:
            return
        self._export_to(path, excluded)

    def _export_to(self, path: str, excluded: set) -> None:
        from app.reports.inheritance import render_inheritance_document
        from dataclasses import replace

        sources = [replace(s, include=s.name not in excluded) for s in self._sources]
        document = render_inheritance_document(sources, generated_at=self._generated_at)
        worker = CallableWorker(_write_pdf, document, path, component="ui.reports.export")
        worker.signals.failed.connect(self.error.emit)
        run(QThreadPool.globalInstance(), worker)


def _report_snapshot(
    store: Any, last_known_generated_at: Optional[int] = None,
    on_progress: Any = None,
) -> Optional[tuple]:
    """The read-only half of a refresh, off the worker thread - the same
    split `_offline_media_snapshot` already draws.

    Section 3c, performance: `report_generated_at` (`MAX(files.indexed_at)`)
    is the same "data as of" timestamp `1b` already shows the user, and it
    only moves when an index run actually adds or touches a file - exactly
    "the next index run" the item asks to cache until. Checking it first,
    cheaply, and returning `None` when it has not moved skips the expensive
    duplicate/uniqueness queries on every tab switch that changes nothing.
    """
    from app.reports.inheritance import catalogue_sources, report_generated_at
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

    stage("Reading sources...")
    roots = _local_roots(store)
    sources = catalogue_sources(store, roots=roots)
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
    return sources, generated_at, shape(space)


def _local_roots(store: Any) -> list:
    try:
        raw = store.get_state("ui:roots", "") or ""
    except Exception:                            # noqa: BLE001 - a report, not a search
        return []
    return [part.strip() for part in raw.split("|") if part.strip()]
