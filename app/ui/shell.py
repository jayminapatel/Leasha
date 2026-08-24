"""The main window.

Layer: L5

Search is the window; indexing and settings are tabs behind it. That ordering is
the whole point of the app — it is a search tool that happens to need an index,
not an indexer with a search box.

**The search bar has focus on launch and `Ctrl+K` returns to it from anywhere.**
Every keyboard path is bound, because the spec requires keyboard-only operation
end to end and because reaching for a mouse to start typing is the difference
between a tool people use and one they open occasionally.

**Models are warmed in the background at startup**, so the first search is not
the one paying the one-to-two second ONNX load.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PyQt6.QtCore import QThreadPool
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QTabWidget,
    QWidget,
)

from app.core.logging import logger
from app.ui.indexing_view import IndexingView
from app.ui.search_view import SearchView
from app.ui.settings_view import SettingsView
from app.ui.workers import CallableWorker, open_in_explorer

__all__ = ["MainWindow", "DARK_STYLESHEET"]

_log = logger.bind(component="ui.shell")

DARK_STYLESHEET = """
QWidget { background: #1e1f22; color: #e6e6e6; font-size: 13px; }
QLineEdit { background: #2b2d31; border: 1px solid #3a3d42; border-radius: 6px; padding: 8px; font-size: 15px; }
QLineEdit:focus { border-color: #5b9dd9; }
QListWidget { background: #232428; border: 1px solid #3a3d42; border-radius: 6px; }
QListWidget::item:selected { background: #33415c; }
QPushButton { background: #2f3136; border: 1px solid #4a4d52; border-radius: 5px; padding: 6px 12px; }
QPushButton:hover { background: #3a3d42; }
QPushButton:disabled { color: #6b6e73; }
QProgressBar { background: #2b2d31; border: 1px solid #3a3d42; border-radius: 5px; text-align: center; }
QProgressBar::chunk { background: #4a7fb5; border-radius: 4px; }
QGroupBox { border: 1px solid #3a3d42; border-radius: 6px; margin-top: 10px; padding-top: 10px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; color: #a9b0b8; }
#resultPath { font-weight: 600; color: #cfe0f0; }
#resultMeta { color: #8b929b; font-size: 11px; }
#resultMissing { color: #d98b5b; font-size: 11px; }
#resultSnippet { color: #d2d5d9; }
#resultSnippet b { color: #ffd479; font-weight: 700; }
#searchStatus, #resultsSummary, #indexDetail { color: #8b929b; font-size: 11px; }
#indexHeadline { font-size: 15px; font-weight: 600; }
#skipHeading { font-weight: 600; }
#skipFix { color: #a9b0b8; }
#skipExamples { color: #7d838b; font-size: 11px; }
"""


class MainWindow(QMainWindow):
    """Search, indexing and settings in one window."""

    def __init__(
        self,
        settings: Any,
        store: Any,
        vectors: Any,
        engine: Any,
        *,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._store = store
        self._vectors = vectors
        self._engine = engine

        self.setWindowTitle("Local Knowledge Graph")
        self.resize(1100, 760)
        self.setAcceptDrops(True)

        self.search_view = SearchView(engine)
        self.search_view.result_opened.connect(self._open_result)
        self.search_view.reveal_requested.connect(lambda row: self._open_result(row, reveal=True))
        self.search_view.reindex_requested.connect(self._reindex_for)
        self.search_view.error.connect(self._show_error)

        self.indexing_view = IndexingView()
        self.indexing_view.error.connect(self._show_error)
        self.indexing_view.start_button.clicked.connect(lambda _checked=False: self._start_indexing())
        self.indexing_view.retry_requested.connect(lambda _code: self._start_indexing())
        # Connected once, here. Connecting inside _start_indexing would add a
        # slot per run, so the tenth index would refresh the status bar ten times.
        self.indexing_view.finished.connect(lambda _stats: self._refresh_status())

        self.settings_view = SettingsView(settings, store)
        self.settings_view.set_roots(self._load_roots())
        self.settings_view.roots_changed.connect(self._save_roots)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.search_view, "Search")
        self.tabs.addTab(self.indexing_view, "Indexing")
        self.tabs.addTab(self.settings_view, "Settings")
        self.setCentralWidget(self.tabs)

        self.setStatusBar(QStatusBar())
        self._refresh_status()
        self._build_shortcuts()

        self.setStyleSheet(DARK_STYLESHEET)
        self.search_view.focus()
        self._warm_models()

    # -- shortcuts ----------------------------------------------------------

    def _build_shortcuts(self) -> None:
        def bind(sequence: str, slot: Any) -> None:
            action = QAction(self)
            action.setShortcut(QKeySequence(sequence))
            action.triggered.connect(slot)
            self.addAction(action)

        bind("Ctrl+K", self._focus_search)
        bind("Ctrl+F", self._focus_search)
        bind("Ctrl+,", lambda: self.tabs.setCurrentWidget(self.settings_view))
        bind("Ctrl+I", lambda: self.tabs.setCurrentWidget(self.indexing_view))
        bind("Esc", self._clear_search)
        # QAction.triggered emits `checked: bool`, so the slot must tolerate a
        # positional argument. Binding the method directly raises TypeError the
        # first time anyone presses F5 - which nothing would catch until then.
        bind("F5", lambda _checked=False: self._start_indexing())

    def _focus_search(self) -> None:
        self.tabs.setCurrentWidget(self.search_view)
        self.search_view.focus()

    def _clear_search(self) -> None:
        self.search_view.input.clear()

    # -- startup ------------------------------------------------------------

    def _warm_models(self) -> None:
        """Load the models off the first search's critical path."""
        worker = CallableWorker(self._engine.warm_up, component="ui.warmup")
        worker.signals.failed.connect(
            lambda error: self.statusBar().showMessage(error.message, 10_000)
        )
        QThreadPool.globalInstance().start(worker)

    def _refresh_status(self) -> None:
        try:
            stats = self._store.stats()
            self.statusBar().showMessage(
                f"{stats['files_total']:,} files  ·  {stats['chunks_total']:,} chunks indexed"
            )
        except Exception as exc:                 # noqa: BLE001 - a status bar is not worth failing over
            _log.debug("status bar not updated: {}", exc)

    # -- actions ------------------------------------------------------------

    def _open_result(self, row: Any, *, reveal: bool = False) -> None:
        error = open_in_explorer(row.path, select=reveal)
        if error is not None:
            self._show_error(error)

    def _reindex_for(self, row: Any) -> None:
        folder = str(Path(row.path).parent)
        self.tabs.setCurrentWidget(self.indexing_view)
        self._start_indexing(roots=[folder])

    def _load_roots(self) -> list[str]:
        """Index roots persist in `index_state`, alongside the index they build.

        Settings that vanish on restart are not settings. They live with the
        index rather than in .env because they describe *this* index, and .env is
        written by the installer and would be overwritten by a repair run.
        """
        try:
            stored = self._store.get_state("ui:roots", "")
        except Exception:                        # noqa: BLE001
            return []
        return [root for root in (stored or "").split("|") if root]

    def _save_roots(self, roots: list[str]) -> None:
        try:
            self._store.set_state("ui:roots", "|".join(roots))
        except Exception as exc:                 # noqa: BLE001
            _log.warning("index roots not saved: {}", exc)

    def _start_indexing(self, *, roots: Optional[list[str]] = None) -> None:
        from app.index.embedder import Embedder
        from app.index.pipeline import Pipeline, PipelineConfig
        from app.index.walker import WalkConfig

        chosen = roots or self.settings_view.current_roots()
        if not chosen:
            self.tabs.setCurrentWidget(self.settings_view)
            self.statusBar().showMessage(
                "Add at least one folder to index in Settings.", 8_000
            )
            return

        pipeline = Pipeline(
            self._store, self._vectors,
            Embedder(self._settings.embed_model, dim=self._settings.embed_dim,
                     cache_dir=str(self._settings.model_cache)),
            PipelineConfig(
                walk=WalkConfig(
                    roots=[Path(root) for root in chosen],
                    include_cloud=self.settings_view.cloud.isChecked(),
                ),
                min_free_gb=self._settings.min_free_gb,
                prune_missing=roots is None,     # a folder-scoped run must not prune the rest
            ),
        )
        self.indexing_view.start(pipeline)

    def _show_error(self, error: Any) -> None:
        """Every error shows what happened, the fix, and a working button."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Something needs attention")
        box.setText(getattr(error, "message", str(error)))
        box.setInformativeText(getattr(error, "suggestion", ""))
        if getattr(error, "details", None):
            box.setDetailedText(error.details)
        box.exec()

    # -- drag and drop ------------------------------------------------------

    def dragEnterEvent(self, event: Any) -> None:       # noqa: N802 - Qt's naming
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: Any) -> None:            # noqa: N802
        """Dropped files and folders index immediately, at top priority."""
        paths = [
            url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()
        ]
        folders = [p if Path(p).is_dir() else str(Path(p).parent) for p in paths]
        if folders:
            self.tabs.setCurrentWidget(self.indexing_view)
            self._start_indexing(roots=sorted(set(folders)))
        event.acceptProposedAction()

    def closeEvent(self, event: Any) -> None:           # noqa: N802
        self.indexing_view.stop()
        try:
            self._engine.close()
        except Exception:                                # noqa: BLE001
            pass
        super().closeEvent(event)
