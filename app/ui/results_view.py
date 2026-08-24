"""The results list.

Layer: L5

Each row shows the path, where in the document the hit is, why it matched, and a
snippet centred on the match with the query terms picked out. All four of those
are computed in `presenter.py`; this module draws them.

**A result whose file has vanished is marked, not hidden.** A path that no longer
exists is a genuine finding - the index is stale for that file - and silently
dropping it would make the result count disagree with the list for reasons
nobody could see.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Sequence

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction, QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from app.ui.presenter import ResultRow, to_rows

__all__ = ["ResultsView"]


class ResultsView(QWidget):
    """A list of search results with open, reveal, copy and add-to-document."""

    opened = pyqtSignal(object)          # ResultRow
    reveal_requested = pyqtSignal(object)
    add_to_document = pyqtSignal(object)
    reindex_requested = pyqtSignal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._rows: list[ResultRow] = []

        self._summary = QLabel("")
        self._summary.setObjectName("resultsSummary")

        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.setWordWrap(True)
        self._list.setUniformItemSizes(False)
        self._list.itemActivated.connect(self._on_activated)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._summary)
        layout.addWidget(self._list, stretch=1)

    # -- populating ---------------------------------------------------------

    def show_results(self, results: Sequence[Any], terms: Sequence[str], summary: str = "") -> None:
        self._rows = to_rows(results, terms)
        self._list.clear()
        self._summary.setText(summary)

        for row in self._rows:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, row)
            widget = _ResultItem(row)
            item.setSizeHint(widget.sizeHint())
            self._list.addItem(item)
            self._list.setItemWidget(item, widget)

    def clear(self, message: str = "") -> None:
        self._rows = []
        self._list.clear()
        self._summary.setText(message)

    def selected_rows(self) -> list[ResultRow]:
        return [
            item.data(Qt.ItemDataRole.UserRole)
            for item in self._list.selectedItems()
        ]

    # -- interaction --------------------------------------------------------

    def _row_for(self, item: Optional[QListWidgetItem]) -> Optional[ResultRow]:
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _on_activated(self, item: QListWidgetItem) -> None:
        row = self._row_for(item)
        if row is not None:
            self.opened.emit(row)

    def _on_context_menu(self, point: Any) -> None:
        row = self._row_for(self._list.itemAt(point))
        if row is None:
            return

        menu = QMenu(self)
        missing = not Path(row.path).exists()

        open_action = QAction("Open", self)
        open_action.setEnabled(not missing)
        open_action.triggered.connect(lambda: self.opened.emit(row))
        menu.addAction(open_action)

        reveal = QAction("Open containing folder", self)
        reveal.setEnabled(not missing)
        reveal.triggered.connect(lambda: self.reveal_requested.emit(row))
        menu.addAction(reveal)

        copy = QAction("Copy path", self)
        copy.triggered.connect(lambda: QGuiApplication.clipboard().setText(row.path))
        menu.addAction(copy)

        menu.addSeparator()
        add = QAction("Add to document", self)
        add.triggered.connect(lambda: self.add_to_document.emit(row))
        menu.addAction(add)

        if missing:
            menu.addSeparator()
            # The offer only appears when it can actually help: the file is gone,
            # so re-indexing is the thing that makes the stale row disappear.
            retry = QAction("File is missing — re-index this folder", self)
            retry.triggered.connect(lambda: self.reindex_requested.emit(row))
            menu.addAction(retry)

        menu.exec(self._list.mapToGlobal(point))


class _ResultItem(QWidget):
    """One row: path, location, why it matched, and a highlighted snippet."""

    def __init__(self, row: ResultRow, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        missing = not Path(row.path).exists()

        heading = QLabel(f"{row.rank}. {row.display_path}")
        heading.setObjectName("resultPath")
        heading.setToolTip(row.path)

        bits = [row.location, row.explain]
        if missing:
            bits.append("file missing")
        meta = QLabel("  ·  ".join(bit for bit in bits if bit))
        meta.setObjectName("resultMissing" if missing else "resultMeta")

        snippet = QLabel(row.snippet.marked())
        snippet.setObjectName("resultSnippet")
        snippet.setWordWrap(True)
        snippet.setTextFormat(Qt.TextFormat.RichText)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)
        layout.addWidget(heading)
        layout.addWidget(meta)
        layout.addWidget(snippet)
