"""The knowledge graph panel: entities, connections, evidence.

Layer: L6 (UI), built to Layer 5's rules

Thin by design. Everything that decides anything lives in `app/ui/presenter.py`
and `app/graph/render.py`; this arranges widgets and forwards signals, and a
test in the suite fails if it grows past 250 lines.

**Three panes, left to right: what, what with, and where.** An entity table, the
things it connects to, and the passages it actually came from. That last pane is
not decoration - a graph node nobody can trace back to a document is an
assertion, and this is the app that has the evidence to hand.

The picture opens in a browser rather than in a tab here; see
`app/graph/render.py` for why that is a deliberate choice and not a shortcut.
"""

from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Any, Optional

from PyQt6.QtCore import Qt, QThreadPool, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.ui.presenter import entity_rows, graph_headline, graph_phase_line, neighbour_rows
from app.ui.workers import CallableWorker, GraphWorker, run

__all__ = ["GraphView"]


class GraphView(QWidget):
    """Build the graph, browse it, and search from it."""

    search_requested = pyqtSignal(str)   # an entity name to search for
    error = pyqtSignal(object)
    finished = pyqtSignal(object)

    def __init__(self, store: Any, settings: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._settings = settings
        self._worker: Optional[GraphWorker] = None
        self._edges: list[dict] = []
        self._labels: dict[int, str] = {}

        self.headline = QLabel("")
        self.headline.setObjectName("graphHeadline")

        self.build_button = QPushButton("Build graph")
        self.build_button.clicked.connect(lambda _checked=False: self.start(rebuild=False))
        self.rebuild_box = QCheckBox("Start over")
        self.rebuild_box.setToolTip(
            "Discard the stored graph and rebuild it from the index.\n"
            "Always safe: the graph is derived from your documents, never the source."
        )
        self.enrich_box = QCheckBox("Use Ollama for typed entities")
        self.enrich_box.setToolTip(
            "Optional. Adds person / organisation / place types.\n"
            "If Ollama is not running this is skipped and nothing else changes."
        )
        self.open_button = QPushButton("Open interactive view")
        self.open_button.clicked.connect(lambda _checked=False: self._open_html())
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(lambda _checked=False: self._stop())

        self.bar = QProgressBar()
        self.bar.setRange(0, 0)
        self.bar.hide()
        self.status = QLabel("")

        self.entities = QTableWidget(0, 4)
        self.entities.setHorizontalHeaderLabels(["Entity", "Kind", "Documents", "Mentions"])
        self.entities.verticalHeader().hide()
        self.entities.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.entities.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.entities.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.entities.itemSelectionChanged.connect(self._selection_changed)
        self.entities.itemDoubleClicked.connect(self._search_selected)

        self.neighbours = QListWidget()
        self.neighbours.setToolTip("What this entity keeps appearing alongside")
        self.evidence = QListWidget()
        self.evidence.setToolTip("The documents these connections were read from")

        self.search_button = QPushButton("Search for this entity")
        self.search_button.setEnabled(False)
        self.search_button.clicked.connect(lambda _checked=False: self._search_selected())

        self._lay_out()
        self.refresh()

    def _lay_out(self) -> None:
        controls = QHBoxLayout()
        controls.addWidget(self.build_button)
        controls.addWidget(self.rebuild_box)
        controls.addWidget(self.enrich_box)
        controls.addStretch(1)
        controls.addWidget(self.open_button)
        controls.addWidget(self.stop_button)

        right = QVBoxLayout()
        right.addWidget(QLabel("Connected to"))
        right.addWidget(self.neighbours, 1)
        right.addWidget(QLabel("Seen in"))
        right.addWidget(self.evidence, 1)
        right.addWidget(self.search_button)
        panel = QWidget()
        panel.setLayout(right)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.entities)
        split.addWidget(panel)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)

        layout = QVBoxLayout(self)
        layout.addWidget(self.headline)
        layout.addLayout(controls)
        layout.addWidget(self.bar)
        layout.addWidget(self.status)
        layout.addWidget(split, 1)

    # -- data ----------------------------------------------------------------

    def refresh(self) -> None:
        """Reload from the store. Cheap enough to call on every tab switch."""
        try:
            stats = self._store.graph_stats()
            entities = self._store.top_entities(500)
            self._labels = {int(row["id"]): str(row["display"]) for row in entities}
            self._edges = self._store.edges_among(list(self._labels))
        except Exception as exc:                 # noqa: BLE001 - a panel is not worth crashing over
            self.headline.setText(f"Could not read the graph: {exc}")
            return

        self.headline.setText(graph_headline(stats))
        self.open_button.setEnabled(int(stats.get("entities", 0)) > 0)

        rows = entity_rows(entities)
        self.entities.setRowCount(len(rows))
        for index, row in enumerate(rows):
            label = QTableWidgetItem(row.label)
            label.setData(Qt.ItemDataRole.UserRole, row.entity_id)
            if row.typed_by_model:
                label.setToolTip("Type assigned by the local model")
            self.entities.setItem(index, 0, label)
            self.entities.setItem(index, 1, QTableWidgetItem(row.kind))
            self.entities.setItem(index, 2, QTableWidgetItem(row.documents))
            self.entities.setItem(index, 3, QTableWidgetItem(row.mentions))

    def _selected_id(self) -> Optional[int]:
        items = self.entities.selectedItems()
        if not items:
            return None
        first = self.entities.item(items[0].row(), 0)
        return None if first is None else int(first.data(Qt.ItemDataRole.UserRole))

    def _selection_changed(self) -> None:
        from app.graph.render import neighbourhood   # lazy: keeps Qt import time down

        entity_id = self._selected_id()
        self.neighbours.clear()
        self.evidence.clear()
        self.search_button.setEnabled(entity_id is not None)
        if entity_id is None:
            return

        for label, strength, passages in neighbour_rows(
            neighbourhood(entity_id, self._edges, self._labels)
        ):
            self.neighbours.addItem(QListWidgetItem(f"{label}  —  {strength} ({passages})"))

        try:
            for row in self._store.chunks_mentioning(entity_id, 20):
                self.evidence.addItem(QListWidgetItem(str(row["path"])))
        except Exception:                        # noqa: BLE001
            self.evidence.addItem(QListWidgetItem("Could not read the passages."))

    def _search_selected(self) -> None:
        entity_id = self._selected_id()
        if entity_id is not None:
            self.search_requested.emit(self._labels.get(entity_id, ""))

    # -- building ------------------------------------------------------------

    def start(self, *, rebuild: bool) -> None:
        if self._worker is not None:
            return
        self.build_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.bar.show()
        self.status.setText("Starting…")

        self._worker = GraphWorker(
            self._store, self._settings,
            rebuild=rebuild or self.rebuild_box.isChecked(),
            enrich=self.enrich_box.isChecked(),
        )
        self._worker.signals.progress.connect(lambda p: self.status.setText(graph_phase_line(p)))
        self._worker.signals.failed.connect(self.error.emit)
        self._worker.signals.finished.connect(self._done)
        self._worker.signals.done.connect(self._cleanup)
        run(QThreadPool.globalInstance(), self._worker)

    def _stop(self) -> None:
        if self._worker is not None:
            self._worker.stop()
            self.status.setText("Finishing the current batch…")

    def _done(self, result: Any) -> None:
        self.refresh()
        self.finished.emit(result)
        summary = getattr(result, "as_dict", lambda: {})()
        if summary.get("interrupted"):
            self.status.setText("Stopped. Run it again to carry on from here.")
        else:
            self.status.setText("Done.")

    def _cleanup(self) -> None:
        self._worker = None
        self.build_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.bar.hide()

    # -- the picture ---------------------------------------------------------

    def _open_html(self) -> None:
        """Render to a file and hand it to the browser, off the UI thread."""
        out = Path(self._settings.data_path) / "graph.html"

        def build() -> str:
            from app.graph import render

            view = render.select_top(
                self._store.top_entities(render.MAX_RENDER_NODES), self._store.edges_among
            )
            return str(render.render_html(view, out, title="Knowledge graph"))

        worker = CallableWorker(build, component="ui.graph")
        worker.signals.finished.connect(lambda path: webbrowser.open(Path(path).as_uri()))
        worker.signals.failed.connect(self.error.emit)
        run(QThreadPool.globalInstance(), worker)
