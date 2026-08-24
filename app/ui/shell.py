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
from datetime import datetime
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
from app.index.resources import limits_from_settings
from app.index.schedule import SchedulePolicy
from app.ui.files_view import FilesView
from app.ui.graph_view import GraphView
from app.ui.indexing_view import IndexingView
from app.ui.search_view import SearchView
from app.ui.settings_view import SettingsView
from app.ui.scheduler import IndexScheduler
from app.ui.theme import detect_scheme, stylesheet
from app.ui.workers import CallableWorker, open_in_explorer, run

__all__ = ["MainWindow", "DARK_STYLESHEET"]

_log = logger.bind(component="ui.shell")

#: Kept as a name other modules may still import, but the real palettes live in
#: `app/ui/theme.py` and are chosen from the operating system's setting. This
#: was a hardcoded dark sheet, which on a light-mode machine is a bug rather
#: than a style.
DARK_STYLESHEET = stylesheet("dark")


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
        self.indexing_view.finished.connect(
            lambda _stats: self.indexing_view.refresh_totals(self._store)
        )
        # A finished index means new filenames, so the Files summary is stale.
        self.indexing_view.finished.connect(lambda _stats: self.files_view.refresh_summary())

        self.settings_view = SettingsView(settings, store)
        self.settings_view.set_roots(self._load_roots())
        self.settings_view.roots_changed.connect(self._save_roots)
        self.settings_view.pst_backend_changed.connect(self._save_pst_backend)
        self.settings_view.convert_pst_requested.connect(self._convert_pst)
        self.settings_view.indexing.load_indexing(settings)
        self.settings_view.indexing.schedule_changed.connect(self._schedule_changed)
        self.settings_view.indexing.limits_changed.connect(self._limits_changed)
        self.settings_view.indexing.theme_changed.connect(self._theme_changed)
        self._apply_pst_backend(self._store.get_state("ui:pst_backend", "auto") or "auto")

        self.files_view = FilesView(store)
        self.files_view.error.connect(self._show_error)

        self.graph_view = GraphView(store, settings)
        self.graph_view.error.connect(self._show_error)
        self.graph_view.search_requested.connect(self._search_for)

        self.tabs = QTabWidget()
        self.tabs.addTab(self.search_view, "Search")
        self.tabs.addTab(self.files_view, "Files")
        self.tabs.addTab(self.indexing_view, "Indexing")
        self.tabs.addTab(self.graph_view, "Graph")
        self.tabs.addTab(self.settings_view, "Settings")
        # Reload the graph panel when it comes forward rather than on a timer:
        # an index run between visits changes what it should show, and polling
        # a table nobody is looking at is work for nothing.
        self.tabs.currentChanged.connect(self._tab_changed)
        self.setCentralWidget(self.tabs)

        self.setStatusBar(QStatusBar())
        self.indexing_view.refresh_totals(store)
        self._refresh_status()
        self._build_shortcuts()
        self._start_scheduler()

        self._apply_theme()
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
        bind("Ctrl+G", lambda: self.tabs.setCurrentWidget(self.graph_view))
        bind("Ctrl+P", self._focus_files)
        bind("Esc", self._clear_search)
        # QAction.triggered emits `checked: bool`, so the slot must tolerate a
        # positional argument. Binding the method directly raises TypeError the
        # first time anyone presses F5 - which nothing would catch until then.
        bind("F5", lambda _checked=False: self._start_indexing())

    # -- the schedule -------------------------------------------------------

    def _start_scheduler(self) -> None:
        """Wire the clock to the indexer.

        `is_running` is the guard that matters. The single-instance lock stops a
        second *process* touching the database; nothing stops this application
        starting a scheduled run on top of one already in progress, and that is
        the mistake a timer makes at 02:00 with nobody watching.
        """
        self.scheduler = IndexScheduler(
            SchedulePolicy.from_settings(self._settings),
            is_running=lambda: self.indexing_view.is_running(),
            load_last_run=self._load_last_index_time,
            save_last_run=self._save_last_index_time,
            parent=self,
        )
        self.scheduler.due.connect(lambda: self._start_indexing())
        self.scheduler.state_changed.connect(
            lambda text: self.statusBar().showMessage(f"Indexing: {text}", 8_000)
        )
        self.indexing_view.finished.connect(lambda _stats: self.scheduler.notify_finished())
        self.scheduler.start()

    def _schedule_changed(self, policy: Any) -> None:
        """Apply a schedule change immediately, and persist it.

        Persisted in `index_state` rather than rewritten into `.env`: the app
        must never edit a file the user maintains by hand, and a settings panel
        that silently rewrites configuration is how hand-written comments and
        overrides disappear. `.env` remains the default; this is the override.
        """
        self._store.set_state("ui:index_schedule", policy.mode)
        self._store.set_state("ui:index_interval_hours", str(policy.interval_hours))
        self._store.set_state(
            "ui:index_daily_at", f"{policy.daily_at[0]:02d}:{policy.daily_at[1]:02d}"
        )
        self.scheduler.set_policy(policy)
        self.settings_view.indexing.set_schedule_status(self.scheduler.status())

    def _limits_changed(self, values: dict) -> None:
        """Persist the resource ceilings. They take effect on the next run.

        Not on the run in flight: changing the worker count mid-run would mean
        stopping and restarting threads that are holding files open, and the
        gain is a few minutes on a job measured in hours.
        """
        for key, value in values.items():
            self._store.set_state(f"ui:{key}", str(value))
        self.statusBar().showMessage("Saved. Applies to the next index run.", 5_000)

    def _load_last_index_time(self) -> Optional[datetime]:
        raw = self._store.get_state("index:last_run")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            # A corrupt timestamp must not stop the app opening. Treating it as
            # "never ran" schedules one run, which is the safe direction.
            return None

    def _save_last_index_time(self, when: datetime) -> None:
        self._store.set_state("index:last_run", when.isoformat(timespec="seconds"))

    def _focus_files(self) -> None:
        """Ctrl+P, the shortcut every editor uses for "go to file"."""
        self.tabs.setCurrentWidget(self.files_view)
        self.files_view.focus()

    def _theme_changed(self, preference: str) -> None:
        self._store.set_state("ui:theme", preference)
        self._apply_theme()

    def _apply_theme(self) -> None:
        """Follow the operating system unless told otherwise.

        Hardcoding dark was not a style choice, it was a bug: on a machine set
        to light mode the application looked like it belonged to a different
        operating system, and it is harder to read on a bright screen, not
        easier.
        """
        from PyQt6.QtGui import QGuiApplication

        preference = self._store.get_state("ui:theme", "system") or "system"
        detected = detect_scheme(QGuiApplication.instance())
        self.setStyleSheet(stylesheet(preference, detected=detected))

        # Qt 6.5+ emits this when the system switch is flipped, so the window
        # follows without a restart.
        try:
            QGuiApplication.instance().styleHints().colorSchemeChanged.connect(
                lambda _scheme: self._apply_theme()
            )
        except Exception:                    # noqa: BLE001 - older Qt, or no hints
            pass

    def _tab_changed(self, index: int) -> None:
        widget = self.tabs.widget(index)
        if widget is self.graph_view:
            self.graph_view.refresh()
        elif widget is self.indexing_view:
            self.indexing_view.refresh_totals(self._store)
        elif widget is self.files_view:
            self.files_view.refresh_summary()
            self.files_view.focus()

    def _search_for(self, term: str) -> None:
        """Jump from a graph node to the results for it.

        Quoted, because entity names contain spaces far more often than search
        terms do - "Acme Water Ltd" unquoted is three separate terms and finds
        the wrong thing.
        """
        if not term:
            return
        self.tabs.setCurrentWidget(self.search_view)
        self.search_view.input.setText(f'"{term}"')
        self.search_view.search_now()
        self.search_view.focus()

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
        run(QThreadPool.globalInstance(), worker)

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

    def _save_pst_backend(self, backend: str) -> None:
        try:
            self._store.set_state("ui:pst_backend", backend)
        except Exception as exc:                 # noqa: BLE001
            _log.warning("PST backend choice not saved: {}", exc)
        self._apply_pst_backend(backend)

    def _apply_pst_backend(self, backend: str) -> None:
        from app.extract.base import extractor_for

        extractor = extractor_for(Path("x.pst"))
        if extractor is not None:
            extractor.backend = backend

    def _convert_pst(self, archive: str, destination: str) -> None:
        """Export an archive to .eml, off the UI thread.

        Long-running and worth doing once: afterwards the mail is ordinary files
        that need neither Outlook nor libpff, and the folder can simply be added
        as an index root.
        """
        from app.extract import pst_libpff

        target = Path(destination) / Path(archive).stem
        self.statusBar().showMessage(f"Converting {Path(archive).name}…")

        worker = CallableWorker(
            pst_libpff.export_to_eml, Path(archive), target, component="ui.convert",
        )
        worker.signals.finished.connect(
            lambda count: self._conversion_done(count, target)
        )
        worker.signals.failed.connect(self._show_error)
        run(QThreadPool.globalInstance(), worker)

    def _conversion_done(self, count: int, target: Path) -> None:
        self.statusBar().showMessage(f"Wrote {count:,} messages to {target}", 15_000)
        roots = self.settings_view.current_roots()
        if str(target) not in roots:
            # Offer it as an index root immediately - converting and then having
            # to remember to add the folder is a step nobody should have to take.
            self.settings_view.roots.addItem(str(target))
            self._save_roots(self.settings_view.current_roots())

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
                # Memory, CPU, battery and disk ceilings, from .env. Without
                # these an index run competes with whatever the person is
                # actually doing, and gets switched off for good.
                limits=limits_from_settings(self._settings),
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
