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

from PyQt6.QtCore import QThreadPool, pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from app.ui.widgets.report_export_dialog import SourceSelectionDialog
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
)


class ReportsView(QWidget):
    """A list of reports; pick one, read it, export it."""

    error = pyqtSignal(object)

    def __init__(self, store: Any = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._sources: list = []
        self._generated_at: Optional[int] = None
        #: The Space Report, rendered on the worker with the rest of the
        #: snapshot - it touches the store, so never on this thread.
        self._space_document: str = ""

        intro = QLabel(
            "What Leasha has catalogued, read out as a document rather "
            "than searched. Nothing here changes anything on your drives."
        )
        intro.setWordWrap(True)

        self.list = QListWidget()
        self.list.setAccessibleName("Available reports")
        for key, title, description in REPORTS:
            item = QListWidgetItem(title)
            item.setData(1, key)
            item.setToolTip(description)
            self.list.addItem(item)
        self.list.currentRowChanged.connect(self._show_selected)

        self.timestamp = QLabel("")
        self.timestamp.setObjectName("resultsSummary")

        self.body = QTextBrowser()
        self.body.setAccessibleName("Report contents")
        self.body.setOpenExternalLinks(False)

        self.export = QPushButton("Export…")
        self.export.setToolTip(
            "Choose which sources to include, then save this report as a "
            "PDF you can print or hand to someone else.")
        self.export.clicked.connect(self._start_export)
        self.export.setEnabled(False)

        right = QVBoxLayout()
        right.addWidget(self.timestamp)
        right.addWidget(self.body, 1)
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
        worker = CallableWorker(_report_snapshot, self._store, component="ui.reports")
        worker.signals.finished.connect(self._loaded)
        worker.signals.failed.connect(self.error.emit)
        run(QThreadPool.globalInstance(), worker)

    def _loaded(self, snapshot: Any) -> None:
        self._sources, self._generated_at, self._space_document = snapshot
        self.export.setEnabled(bool(self._sources) or bool(self._space_document))
        self._show_selected(self.list.currentRow())

    def _show_selected(self, row: int) -> None:
        from app.reports.inheritance import data_timestamp_sentence, render_inheritance_document

        self.timestamp.setText(data_timestamp_sentence(self._generated_at))
        if row < 0:
            self.body.setMarkdown("Nothing indexed yet.")
            return
        key = self.list.item(row).data(1)
        if key == "inheritance":
            if not self._sources:
                self.body.setMarkdown("Nothing indexed yet.")
            else:
                self.body.setMarkdown(
                    render_inheritance_document(self._sources, generated_at=self._generated_at))
        elif key == "space":
            self.body.setMarkdown(self._space_document or "Nothing indexed yet.")

    # -- export -----------------------------------------------------------

    def _selected_key(self) -> str:
        row = self.list.currentRow()
        return str(self.list.item(row).data(1)) if row >= 0 else ""

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


def _report_snapshot(store: Any) -> tuple:
    """The read-only half of a refresh, off the worker thread - the same
    split `_offline_media_snapshot` already draws."""
    from app.reports.inheritance import catalogue_sources, report_generated_at
    from app.reports.space import (
        find_duplicate_groups, find_source_uniqueness, render_space_document,
        total_reclaimable_bytes,
    )

    roots = _local_roots(store)
    sources = catalogue_sources(store, roots=roots)
    generated_at = report_generated_at(store)
    # The Space Report, exactly as `app.cli report space` builds it.
    space = render_space_document(
        find_duplicate_groups(store), find_source_uniqueness(store),
        total_reclaimable=total_reclaimable_bytes(store), generated_at=generated_at)
    return sources, generated_at, space


def _local_roots(store: Any) -> list:
    try:
        raw = store.get_state("ui:roots", "") or ""
    except Exception:                            # noqa: BLE001 - a report, not a search
        return []
    return [part.strip() for part in raw.split("|") if part.strip()]


def _write_pdf(document: str, path: str) -> None:
    """PDF via the print machinery, on a worker - `QTextDocument`/`QPrinter`
    are the same route `preview_window.py`'s own Print already uses.
    Never on the UI thread: a long report over a large catalogue lays out
    every page before anything is written.
    """
    from PyQt6.QtGui import QTextDocument
    from PyQt6.QtPrintSupport import QPrinter

    doc = QTextDocument()
    doc.setMarkdown(document)
    printer = QPrinter(QPrinter.PrinterMode.HighResolution)
    printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
    printer.setOutputFileName(path)
    doc.print(printer)
