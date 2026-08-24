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

from app.core.branding import window_title
from app.core.logging import logger
from app.index.resources import limits_from_settings
from app.llm.ollama import OllamaClient
from app.search.translate import QueryTranslator
from app.index.schedule import SchedulePolicy
from app.ui.files_view import FilesView
from app.ui.indexing_view import IndexingView
from app.ui.search_view import SearchView
from app.ui.settings_view import SettingsView
from app.ui.scheduler import IndexScheduler
from app.ui.debug_recorder import recorder_for
from app.ui.theme import detect_scheme, stylesheet
from app.ui.widgets.scroll import wrap_if_needed
from app.ui.workers import CallableWorker, open_in_explorer, run

__all__ = ["MainWindow", "DARK_STYLESHEET"]

_log = logger.bind(component="ui.shell")

#: Kept as a name other modules may still import, but the real palettes live in
#: `app/ui/theme.py` and are chosen from the operating system's setting. This
#: was a hardcoded dark sheet, which on a light-mode machine is a bug rather
#: than a style.
DARK_STYLESHEET = stylesheet("dark")


def _err(error: Any) -> dict:
    """An AppError as the few fields a reader of the session file needs."""
    return {
        "code": getattr(error, "code", "?"),
        "component": getattr(error, "component", ""),
        "message": str(getattr(error, "message", error))[:200],
    }


def _stats(stats: Any) -> dict:
    """The numbers from an index run, without the file paths."""
    payload = stats.as_dict() if hasattr(stats, "as_dict") else dict(stats or {})
    keep = ("seen", "indexed", "unchanged", "skipped", "deleted", "chunks",
            "elapsed_s", "pauses", "paused_s", "stopped_early", "skipped_by_code")
    return {key: payload[key] for key in keep if key in payload}


class MainWindow(QMainWindow):
    """Search, indexing and settings in one window."""

    def __init__(
        self,
        settings: Any,
        store: Any,
        vectors: Any,
        engine: Any,
        *,
        debug: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._store = store
        self._vectors = vectors
        self._engine = engine

        self.setWindowTitle(window_title())
        self.resize(1100, 760)
        # A floor, not the opening size. Without one Qt will happily shrink the
        # window until the tab bar is the only thing left, and a view with no
        # scroll area then has controls that cannot be reached at all.
        self.setMinimumSize(720, 480)
        self.setAcceptDrops(True)

        #: view -> tab index. A wrapped view is not the widget in the tab, so
        #: `tabs.widget(i) is self.settings_view` is False and
        #: `setCurrentWidget(self.settings_view)` silently does nothing. Both
        #: fail quietly, which is how a scroll area breaks navigation without
        #: anybody noticing. This map is the single answer to "which tab is that".
        self._tab_index: dict[QWidget, int] = {}

        # Read once here rather than on every repaint: `_apply_theme` runs on
        # each system colour change, and a database read on the UI thread is
        # exactly what this session's freeze turned out to be.
        self._theme_preference = self._read_state("ui:theme", "system")
        self._theme_hooked = False

        # Off unless explicitly asked for, by `--debug` or the switch in
        # Settings. See `debug_recorder.py`: it records the shape of what
        # happened, never the content, so a session file is safe to send.
        self.recorder = recorder_for(
            settings.log_path,
            enabled=debug or self._read_state("ui:debug_recording", "") == "on",
            context={"roots": len(self._load_roots())},
        )

        # Optional, and never on the retrieval path: `Interpret` spends a
        # second on a model to build a query, which then goes into the box for
        # the person to read and edit. Plain Enter never touches it.
        translator = QueryTranslator(
            OllamaClient(settings.ollama_url, settings.ollama_model)
        )
        self.search_view = SearchView(engine, translator)
        self.search_view.result_opened.connect(self._open_result)
        self.search_view.reveal_requested.connect(lambda row: self._open_result(row, reveal=True))
        self.search_view.reindex_requested.connect(self._reindex_for)
        self.search_view.error.connect(self._show_error)

        self.indexing_view = IndexingView()
        self.indexing_view.error.connect(self._show_error)
        self.indexing_view.reset_requested.connect(self._reset_index)
        self.indexing_view.start_button.clicked.connect(lambda _checked=False: self._start_indexing())
        self.indexing_view.retry_requested.connect(lambda _code: self._start_indexing())
        # Connected once, here. Connecting inside _start_indexing would add a
        # slot per run, so the tenth index would refresh the status bar ten times.
        self.indexing_view.finished.connect(lambda _stats: self._refresh_status())
        self.indexing_view.finished.connect(
            lambda _stats: self.indexing_view.refresh_totals(self._store, self._settings)
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
        self.settings_view.environment.recording.setChecked(self.recorder.enabled)
        self.settings_view.debug_recording_toggled.connect(self._debug_recording_toggled)
        self.settings_view.environment.set_recording_status(
            f"Recording to {self.recorder.path.name}" if self.recorder.enabled
            else "Not recording."
        )
        self._apply_pst_backend(self._store.get_state("ui:pst_backend", "auto") or "auto")

        self.files_view = FilesView(store)
        self.files_view.error.connect(self._show_error)
        self.files_view.search_inside_requested.connect(self._search_inside)

        self.tabs = QTabWidget()
        # (view, title, wrap in a scroll area?)
        #
        # Only stacked forms are wrapped. Search, Files and Indexing are
        # each built around a table, list or splitter that already fills the
        # window and scrolls its own contents - nesting a second scroll area
        # around one of those makes the two fight over the wheel, and the outer
        # one usually wins, which feels broken and is very hard to report.
        #
        # Settings is the opposite: six group boxes stacked vertically, growing
        # every time an option is added. It had no scrollbar at all, so the
        # bottom of it was simply unreachable on a short window.
        for view, title, scroll in (
            (self.search_view, "Search", False),
            (self.files_view, "Files", False),
            (self.indexing_view, "Indexing", False),
            (self.settings_view, "Settings", True),
        ):
            self._tab_index[view] = self.tabs.addTab(
                wrap_if_needed(view, scroll=scroll), title
            )
        # Refresh a panel when it comes forward rather than on a timer: an
        # index run between visits changes what it should show, and polling a
        # table nobody is looking at is work for nothing.
        self.tabs.currentChanged.connect(self._tab_changed)
        self.setCentralWidget(self.tabs)

        self.setStatusBar(QStatusBar())
        self.indexing_view.refresh_totals(store, settings)
        self._refresh_status()
        self._build_shortcuts()
        self._start_scheduler()
        self._wire_recorder()

        self._apply_theme()
        self.search_view.focus()
        self._warm_models()

    # -- the debug recorder --------------------------------------------------

    def _debug_recording_toggled(self, on: bool) -> None:
        """Remember the choice; it takes effect at the next start.

        Deliberately not applied to the running window. Turning recording on
        mid-session would produce a file that begins in the middle of whatever
        went wrong, missing the startup context that makes the rest readable -
        and the whole point of the feature is a file somebody else can follow
        from the top.
        """
        self._store.set_state("ui:debug_recording", "on" if on else "off")
        if on and not self.recorder.enabled:
            self.settings_view.environment.set_recording_status(
                "Recording starts the next time you open the app. "
                "The file goes in logs\\sessions\\."
            )
        elif not on and self.recorder.enabled:
            self.settings_view.environment.set_recording_status(
                f"Still recording to {self.recorder.path.name} until you close the app."
            )

    def _wire_recorder(self) -> None:
        """Attach the recorder to signals that already exist.

        Deliberately in one place rather than a `recorder.event(...)` line
        scattered through every view. Two reasons: the views stay thin, which is
        the rule the whole layer is built on and which a size test enforces; and
        when recording is off this costs a handful of `NullRecorder` calls
        instead of forty branches that each have to be written correctly.

        Every widget that matters already emits a signal, because that is how it
        talks to this window - so there is nothing to add to them, only to
        listen to.
        """
        if not self.recorder.enabled:
            return

        record = self.recorder.event
        _log.info("recording this session to {}", self.recorder.path)
        self.statusBar().showMessage(
            f"Recording this session to {self.recorder.path.name}", 10_000
        )

        self.tabs.currentChanged.connect(
            lambda i: record("tab", name=self.tabs.tabText(i))
        )

        self.search_view.error.connect(lambda e: record("error", where="search", **_err(e)))
        self.indexing_view.error.connect(lambda e: record("error", where="index", **_err(e)))
        self.files_view.error.connect(lambda e: record("error", where="files", **_err(e)))

        self.indexing_view.start_button.clicked.connect(
            lambda _c=False: record("click", what="start_indexing")
        )
        self.indexing_view.finished.connect(
            lambda stats: record("index_finished", **_stats(stats))
        )
        self.settings_view.environment.run_doctor_button.clicked.connect(
            lambda _c=False: record("click", what="run_doctor")
        )
        self.settings_view.roots_changed.connect(
            lambda roots: record("roots_changed", count=len(roots))
        )
        self.settings_view.indexing.theme_changed.connect(
            lambda pref: record("theme_changed", preference=pref)
        )

        # Searches are recorded by *shape* only - length and result count, never
        # the query. A file full of somebody's actual searches is a liability,
        # and a recorder nobody dares send is a recorder that does nothing.
        self.search_view.searched.connect(
            lambda info: record("search", **info)
        )
        self.search_view.interpreted.connect(
            lambda tr: record(
                "interpreted", changed=tr.changed, from_cache=tr.from_cache,
                elapsed_s=round(tr.elapsed_s, 2), note=tr.note,
                # The queries themselves are content and are never recorded;
                # their shape is what makes a session file diagnosable.
                raw_len=len(tr.raw), query_len=len(tr.query),
            )
        )

    # -- shortcuts ----------------------------------------------------------

    def _build_shortcuts(self) -> None:
        def bind(sequence: str, slot: Any) -> None:
            action = QAction(self)
            action.setShortcut(QKeySequence(sequence))
            action.triggered.connect(slot)
            self.addAction(action)

        bind("Ctrl+K", self._focus_search)
        bind("Ctrl+F", self._focus_search)
        bind("Ctrl+,", lambda: self._show(self.settings_view))
        bind("Ctrl+I", lambda: self._show(self.indexing_view))
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

    def _read_state(self, key: str, default: str = "") -> str:
        """One small key, defaulted rather than raised.

        Every `get_state` call in the window went through its own try/except, or
        through none at all - and the ones with none turn a locked database into
        a window that will not open.
        """
        try:
            return self._store.get_state(key, default) or default
        except Exception as exc:                 # noqa: BLE001
            _log.warning("could not read {}: {}", key, exc)
            return default

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
        self._show(self.files_view)
        self.files_view.focus()

    def _theme_changed(self, preference: str) -> None:
        self._theme_preference = preference
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

        preference = self._theme_preference
        detected = detect_scheme(QGuiApplication.instance())
        self.setStyleSheet(stylesheet(preference, detected=detected))

        # Qt 6.5+ emits this when the system switch is flipped, so the window
        # follows without a restart.
        #
        # **Connected exactly once.** This used to be connected here, inside the
        # function it calls back into - so every theme change added another
        # connection, and one flick of the system switch then re-ran the handler
        # once per change the person had ever made, each re-entering and
        # connecting again. Signal connections are not idempotent, and Qt gives
        # no warning: it looks fine until the machine changes theme, and then the
        # window locks up rebuilding its stylesheet exponentially.
        if not self._theme_hooked:
            self._theme_hooked = True
            try:
                QGuiApplication.instance().styleHints().colorSchemeChanged.connect(
                    lambda _scheme: self._apply_theme()
                )
            except Exception:                # noqa: BLE001 - older Qt, or no hints
                pass

    def _show(self, view: QWidget) -> None:
        """Bring a view's tab forward, wrapped or not.

        Always use this instead of `tabs.setCurrentWidget(view)`, which does
        nothing at all for a view inside a scroll area - no error, no exception,
        the tab simply does not change.
        """
        index = self._tab_index.get(view)
        if index is not None:
            self.tabs.setCurrentIndex(index)

    def _tab_changed(self, index: int) -> None:
        if index == self._tab_index.get(self.indexing_view):
            self.indexing_view.refresh_totals(self._store, self._settings)
        elif index == self._tab_index.get(self.files_view):
            self.files_view.refresh_summary()
            self.files_view.focus()


    def _focus_search(self) -> None:
        self._show(self.search_view)
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
        self._show(self.indexing_view)
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

        # Checked *before* anything is built. `IndexingView.start` already
        # refuses a second run, but it refused silently and only after this
        # method had constructed a Pipeline and an Embedder - which loads the
        # ONNX model - purely to throw them away. Six starts in seven seconds
        # appeared in the log from ordinary clicking, and each one paid that
        # cost. Saying so is also better than appearing to ignore the button.
        if self.indexing_view.is_running():
            self._show(self.indexing_view)
            self.statusBar().showMessage("An index run is already in progress.", 5_000)
            return

        chosen = roots or self.settings_view.current_roots()
        if not chosen:
            self._show(self.settings_view)
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

    def _search_inside(self, path: str) -> None:
        """Found it by name; now find what is in it.

        Uses `path:` with the file's own name, so the search is scoped to that
        one document. Without this the filename browser is a dead end - you can
        see a file and do nothing with it but open it.
        """
        from pathlib import Path as _Path

        name = _Path(path).name
        if not name:
            return
        self._show(self.search_view)
        self.search_view.input.setText(f'path:"{name}" ')
        self.search_view.input.setFocus()
        self.statusBar().showMessage(
            f"Searching inside {name} - type what you are looking for.", 8_000)

    def _reset_index(self) -> None:
        """Delete everything indexed, after asking, and never the documents.

        **The confirmation says what is and is not at risk**, because "reset"
        is a word people have learned to fear from applications that mean
        something else by it. Nothing here touches a single document: the index
        is derived from them and is rebuilt by pointing the indexer at the same
        folders again. The only real cost is the time to do that.
        """
        if self.indexing_view.is_running():
            self.statusBar().showMessage(
                "Stop the index run before resetting.", 6_000)
            return

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Reset the index?")
        box.setText("Delete everything that has been indexed and start over?")
        box.setInformativeText(
            "Your documents and emails are NOT touched - the index is built from "
            "them and can always be rebuilt.\n\n"
            "What it costs is the time to index again, and your saved folders, "
            "schedule and settings are kept."
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Reset
        )
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Reset:
            return

        self.recorder.event("click", what="reset_index")
        self.statusBar().showMessage("Clearing the index…")

        def clear() -> int:
            removed = self._store.clear_index()
            self._vectors.drop()
            return removed

        worker = CallableWorker(clear, component="ui.reset")
        worker.signals.finished.connect(self._index_cleared)
        worker.signals.failed.connect(self._show_error)
        run(QThreadPool.globalInstance(), worker)

    def _index_cleared(self, removed: int) -> None:
        self.statusBar().showMessage(
            f"Index cleared - {removed:,} documents removed. "
            "Press Start indexing to rebuild.", 15_000)
        self.indexing_view.refresh_totals(self._store, self._settings)
        self.files_view.refresh_summary()
        self._refresh_status()

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
            self._show(self.indexing_view)
            self._start_indexing(roots=sorted(set(folders)))
        event.acceptProposedAction()

    #: How long closing waits for background threads to notice and stop. Long
    #: enough for a batch to finish and commit; short enough that nobody reaches
    #: for Task Manager. A worker that ignores it is left to Qt, which is the
    #: same outcome as before - just without the wait.
    SHUTDOWN_GRACE_MS = 4_000

    def closeEvent(self, event: Any) -> None:           # noqa: N802
        """Ask every background job to stop, then wait briefly before closing.

        **This was three nested tracebacks on exit.** The window closed while a
        background job was 200 seconds into waiting on a dead Ollama. Closing
        tore down the QApplication, sip deleted the worker's `WorkerSignals`,
        and the thread - still running, knowing nothing about any of it -
        finished and emitted into a deleted C++ object.

        The stores are worse than the signals. `SqliteStore.__exit__` runs on the
        way out of `main()`, so a worker still holding a cursor finds the
        database closed underneath it. Asking first, and waiting, turns a
        shutdown race into an ordinary stop.
        """
        self.recorder.event("closing")
        self.indexing_view.stop()
        try:
            self.scheduler.stop()
        except Exception:                                # noqa: BLE001
            pass

        self._drain_workers()
        self.recorder.close()

        try:
            self._engine.close()
        except Exception:                                # noqa: BLE001
            pass
        super().closeEvent(event)

    def _drain_workers(self) -> None:
        """Wait for the thread pool, keeping the UI alive while it empties.

        `waitForDone` alone would block the UI thread, so a slow worker would
        freeze the window during the one operation nobody will wait out - and
        we would be back to Task Manager. Pumping events while waiting keeps
        the window painting until the threads are actually finished.
        """
        from PyQt6.QtCore import QDeadlineTimer, QEventLoop, QThreadPool
        from PyQt6.QtWidgets import QApplication

        pool = QThreadPool.globalInstance()
        deadline = QDeadlineTimer(self.SHUTDOWN_GRACE_MS)
        while pool.activeThreadCount() and not deadline.hasExpired():
            pool.waitForDone(50)
            QApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)

        remaining = pool.activeThreadCount()
        if remaining:
            _log.warning(
                "closing with {} background thread(s) still running; "
                "they will be abandoned", remaining,
            )
