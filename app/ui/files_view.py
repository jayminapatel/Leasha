"""Find a file by its name. The other kind of search.

Layer: L4 (UI), driving L1

**Why this is a tab and not a filter.** `chunks_fts` indexes what documents
*say*. Until schema v4 nothing indexed what they were *called*, so a file named
`Invoice 2024.pdf` whose contents never used those words could not be found at
all - which is how most people look for most files most of the time.

That is a different question, answered from a different index, and it deserves
its own place rather than being a mode of the search box. Everything about it is
different: no embedding, no reranking, no snippets, results on every keystroke,
and a result row that shows size and age rather than a passage of text.

**Substring, not word-prefix.** The index uses FTS5's trigram tokeniser, so
"voice" finds "Invoice". People type the middle of a name and expect a hit; a
word tokeniser cannot do that and the feature feels broken.

**Messages are deliberately absent.** A PST message's "path" is a synthetic key
nobody typed and nobody would recognise, and mail would outnumber documents ten
to one here. Mail is searched from the search tab.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.ui.presenter import file_rows
from app.ui.workers import CallableWorker, open_in_explorer, run

__all__ = ["FilesView", "NAME_DEBOUNCE_MS"]

#: Much shorter than the search box's debounce. There is no model to load and
#: no vectors to probe - one FTS5 lookup over a table of filenames - so the only
#: reason to wait at all is to avoid a query per keystroke on a fast typist.
NAME_DEBOUNCE_MS = 80


class FilesView(QWidget):
    """A filename browser: type, get files, double-click to open."""

    error = pyqtSignal(object)

    def __init__(self, store: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._generation = 0

        self.input = QLineEdit()
        self.input.setPlaceholderText(
            "Part of a file name — 'voice' finds 'Invoice 2024.pdf'"
        )
        self.input.setClearButtonEnabled(True)
        self.input.textChanged.connect(self._on_typed)

        self.summary = QLabel("")
        self.summary.setObjectName("resultsSummary")

        self.results = QTableWidget(0, 5)
        self.results.setHorizontalHeaderLabels(["Name", "Size", "Modified", "Type", "Folder"])
        self.results.verticalHeader().hide()
        self.results.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.results.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.results.setSortingEnabled(False)      # ranked by match quality, not by column
        header = self.results.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.results.itemDoubleClicked.connect(self._open_selected)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(NAME_DEBOUNCE_MS)
        self._timer.timeout.connect(self._run)

        top = QHBoxLayout()
        top.addWidget(self.input, stretch=1)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.summary)
        layout.addWidget(self.results, 1)

        self.refresh_summary()

    def focus(self) -> None:
        self.input.setFocus()
        self.input.selectAll()

    def refresh_summary(self) -> None:
        try:
            total = self._store.count_named_files()
        except Exception:                          # noqa: BLE001 - a label is not worth crashing over
            return
        self.summary.setText(
            f"{total:,} file names indexed."
            if total
            else "No file names indexed yet — run an index first."
        )

    # -- searching -----------------------------------------------------------

    def _on_typed(self, _text: str) -> None:
        self._timer.start()

    def _run(self) -> None:
        text = self.input.text().strip()
        if len(text) < 2:
            self.results.setRowCount(0)
            self.refresh_summary()
            return

        # Generation-tagged, like the main search box: a lookup that lands after
        # newer typing must be dropped rather than overwrite fresher results.
        # These are fast enough that it rarely happens and cheap enough to be
        # certain about.
        self._generation += 1
        generation = self._generation

        worker = CallableWorker(
            self._store.search_files_by_name, text, limit=200, component="ui.files"
        )
        worker.signals.finished.connect(
            lambda rows, g=generation: self._show(rows, g, text)
        )
        worker.signals.failed.connect(self.error.emit)
        run(QThreadPool.globalInstance(), worker)

    def _show(self, rows: Any, generation: int, text: str) -> None:
        if generation != self._generation:
            return                                  # a newer query has been sent

        display = file_rows(rows)
        self.results.setRowCount(len(display))
        for index, row in enumerate(display):
            name = QTableWidgetItem(row.name)
            name.setData(Qt.ItemDataRole.UserRole, row.file_id)
            if row.note:
                name.setToolTip(row.note)
            for column, value in enumerate(
                (name, row.size, row.modified, row.kind, row.folder)
            ):
                item = value if isinstance(value, QTableWidgetItem) else QTableWidgetItem(value)
                if column in (1, 2):
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                self.results.setItem(index, column, item)

        self.summary.setText(
            f"{len(display):,} file name{'s' if len(display) != 1 else ''} contain '{text}'"
            if display
            else f"No file name contains '{text}'."
        )

    # -- opening -------------------------------------------------------------

    def _open_selected(self) -> None:
        items = self.results.selectedItems()
        if not items:
            return
        cell = self.results.item(items[0].row(), 0)
        if cell is None:
            return
        file_id = cell.data(Qt.ItemDataRole.UserRole)
        try:
            record = self._store.get_file_by_id(int(file_id))
        except Exception:                          # noqa: BLE001
            record = None
        if record is None:
            return
        # Reveal in Explorer rather than launching: a filename search is usually
        # the first half of "and then do something with it in the folder".
        found = open_in_explorer(record.path, select=True)
        if found is not None:
            self.error.emit(found)
