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

import time
from pathlib import Path
from datetime import datetime
from typing import Any, Optional

from PyQt6.QtCore import QThreadPool, QTimer
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
    QDialog,
    QMainWindow,
    QMessageBox,
    QStatusBar,
    QTabWidget,
    QWidget,
)

from app.core.branding import window_title
from app.core.errors import to_app_error
from app.core.logging import logger
from app.index.resources import limits_from_settings
from app.llm.ollama import OllamaClient
from app.search.translate import TRANSLATE_TIMEOUT_S, QueryTranslator
from app.index.schedule import SchedulePolicy
from app.ui.code_view import CodeView
from app.ui.files_view import FilesView
from app.ui.indexing_view import IndexingView
from app.ui.mail_view import MailView
from app.ui.search_view import SearchView
from app.ui.settings_view import SettingsView
from app.ui.scheduler import IndexScheduler
from app.ui.debug_recorder import recorder_for
from app.ui.theme import detect_scheme, stylesheet
from app.ui.tray import TrayPresence
from app.ui.view_options import load_prefs, save_prefs
from app.ui.widgets.no_scroll import protect_all
from app.ui.widgets.scroll import wrap_if_needed
from app.core.run_lock import GUI
# **Worker bodies live in the presenter**, not here: `test_ui_never_blocks`
# reads this file and refuses any store call it cannot prove is inside a
# worker, and it cannot prove that of a module-level function defined here.
from app.ui.presenter import (
    _read_external_run, _scan_and_save, cleared_message, index_bytes,
)
from app.ui.workers import CallableWorker, open_async, open_in_explorer, run

__all__ = ["MainWindow", "DARK_STYLESHEET"]

#: Where the results pane's text size and spacing live.
RESULTS_PREFS_KEY = "ui:results"

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
        #
        # The model and its budget come from `index_state` when they are there,
        # falling back to `.env`. Same rule as every other setting in this
        # window: the application never edits a file the user maintains by
        # hand, so a choice made in Settings is stored beside the index.
        model = self._read_state("ui:ollama_model", "") or settings.ollama_model
        budget = self._read_state("ui:ollama_timeout_s", "")
        # **Off unless switched on**, and switched on only by an explicit tick
        # or by choosing a model. Most machines have no Ollama, and the promise
        # of this application is that it is entirely local - so nothing may
        # contact another process unless somebody asked for it.
        # New installs start off. **But somebody who already chose a model was
        # already using this**, and a new default must never silently take away
        # something that was working - so an existing `ui:ollama_model` counts
        # as consent until they say otherwise.
        stored = self._read_state("ui:ollama_enabled", "")
        interpret_on = (
            stored == "on" if stored
            else bool(self._read_state("ui:ollama_model", ""))
        )
        self._ollama = OllamaClient(settings.ollama_url, model)
        translator = QueryTranslator(
            self._ollama,
            timeout_s=float(budget) if budget.isdigit() else TRANSLATE_TIMEOUT_S,
            enabled=interpret_on,
        )
        self._translator = translator
        # **Warmed at startup only when it was already switched on**, which is
        # somebody having asked for the feature in a previous session. A new
        # install loads nothing and contacts nothing - see `_warm_translator`
        # for why the first press otherwise pays 8.2s against a 5s budget.
        translator.just_enabled = interpret_on
        self.search_view = SearchView(engine, translator)
        self.search_view.result_opened.connect(self._open_result)
        self.search_view.reveal_requested.connect(lambda row: self._open_result(row, reveal=True))
        self.search_view.reindex_requested.connect(self._reindex_for)
        self.search_view.error.connect(self._show_error)
        # Restored from last time, then saved whenever it changes. Three keys in
        # one transaction - see `set_states`.
        # Applied at startup too, not only when the setting changes - otherwise
        # the button is visible for the first session after being turned off.
        self.search_view.set_interpret_enabled(interpret_on)
        self.search_view.set_view_preferences(load_prefs(store, RESULTS_PREFS_KEY))
        self.search_view.view_preferences_changed.connect(
            lambda prefs: save_prefs(self._store, RESULTS_PREFS_KEY, prefs))

        self.indexing_view = IndexingView()
        self.indexing_view.error.connect(self._show_error)
        self.indexing_view.reset_requested.connect(self._reset_index)
        self.indexing_view.start_button.clicked.connect(lambda _checked=False: self._start_indexing())
        self.indexing_view.retry_requested.connect(lambda _code: self._start_indexing())
        self.indexing_view.rescan_archives_requested.connect(self._rescan_archives)
        self.indexing_view.scan_requested.connect(self._scan_corpus)
        self.indexing_view.stop_requested_externally.connect(self._stop_external_run)

        # **The window watches for a run it did not start.** `app.cli index` is
        # a separate process since the run lock was split from the window lock,
        # so an index can be under way with nothing in here knowing - which used
        # to mean a bar at zero and a Start button that produced a lock error.
        #
        # A poll rather than a notification, because the two processes share
        # only a SQLite file and there is nothing to notify through. Every four
        # seconds: a checkpoint is at most two seconds apart, and a bar that
        # updates twice per checkpoint is smooth enough for something measured
        # in hours. The read is one row and it is skipped entirely while this
        # window is running its own index.
        # **Built here, started in `_start_background_work`.** Starting it here
        # broke the rule stated forty lines below - *"nothing runs on a
        # background thread until construction is over"* - which exists because
        # a worker opening SQLite while the main thread is inside
        # `_apply_theme` produced an access violation with no Python exception
        # and no window. `_poll_external_run` starts exactly such a worker, and
        # calling it from `__init__` put this application back into the same
        # race it had already been debugged out of once.
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(4_000)
        self._watch_timer.timeout.connect(self._poll_external_run)
        # Connected once, here. Connecting inside _start_indexing would add a
        # slot per run, so the tenth index would refresh the status bar ten times.
        self.indexing_view.finished.connect(lambda _stats: self._refresh_status())
        self.indexing_view.finished.connect(
            lambda _stats: self.indexing_view.refresh_totals(self._store, self._settings)
        )
        # A finished index means new filenames, so the Files summary is stale.
        self.indexing_view.finished.connect(lambda _stats: self.files_view.refresh_summary())
        # New mail too, for the same reason.
        self.indexing_view.finished.connect(lambda _stats: self.mail_view.refresh())

        self.settings_view = SettingsView(settings, store)
        self.settings_view.set_roots(self._load_roots(), self._load_root_modes())
        self.settings_view.roots_changed.connect(self._save_roots)
        self.settings_view.root_modes_changed.connect(self._save_root_modes)
        self.settings_view.rescan_archives_requested.connect(self._rescan_archives)
        self.settings_view.code_types_changed.connect(self._save_code_types)
        self.settings_view.code_types.load(*self._load_code_types())
        self.settings_view.pst_backend_changed.connect(self._save_pst_backend)
        self.settings_view.ollama_model_changed.connect(self._ollama_model_changed)
        self.settings_view.models.load(
            model, int(translator.timeout_s), enabled=interpret_on)
        self.settings_view.convert_pst_requested.connect(self._convert_pst)
        self.settings_view.indexing.load_indexing(settings)
        self.settings_view.indexing.schedule_changed.connect(self._schedule_changed)
        self.settings_view.indexing.limits_changed.connect(self._limits_changed)
        self.settings_view.indexing.theme_changed.connect(self._theme_changed)
        self.settings_view.environment.recording.setChecked(self.recorder.enabled)
        self.settings_view.debug_recording_toggled.connect(self._debug_recording_toggled)
        # **Both of these were emitted into nothing.** The rerank switch looked
        # like it worked and changed no behaviour at all; the cloud switch was
        # read live when a run started, so it worked for that run and silently
        # reset to off at the next launch - which reads as the setting being
        # ignored, and is the harder of the two to notice.
        self.settings_view.rerank_toggled.connect(self._rerank_toggled)
        # The toolbar box is the one every search reads, so it reports here too.
        toolbar_rerank = getattr(self.search_view, "rerank_toggle", None)
        if toolbar_rerank is not None:
            toolbar_rerank.toggled.connect(self._rerank_toggled)
        self.settings_view.cloud_toggled.connect(self._cloud_toggled)
        self.settings_view.settings_changed.connect(self._settings_changed)
        self.settings_view.move_index_requested.connect(self._change_index_location)
        self.settings_view.rebuild_vectors_requested.connect(self._change_meaning_model)
        self.settings_view.error.connect(self._show_error)
        self.settings_view.file_types.changes_saved.connect(
            lambda changes: self.statusBar().showMessage(
                f"File types saved - {len(changes)} differ from the defaults. "
                "They apply to the next index run.", 12_000)
        )
        self.settings_view.environment.set_recording_status(
            f"Recording to {self.recorder.path.name}" if self.recorder.enabled
            else "Not recording."
        )
        self._apply_pst_backend(self._store.get_state("ui:pst_backend", "auto") or "auto")

        self.files_view = FilesView(store)
        self.files_view.error.connect(self._show_error)
        self.files_view.search_inside_requested.connect(self._search_inside)

        # Mail gets its own tab for the reason `mail_view.py` opens with: a
        # mailbox is scanned in columns and read newest first, and relevance
        # ranking answers a question nobody asked of it.
        self.mail_view = MailView(store)
        self.mail_view.error.connect(self._show_error)
        self.mail_view.search_inside_requested.connect(self._search_inside)

        # Repositories are a browser, not a second search - see code_view.py.
        self.code_view = CodeView(store)
        self.code_view.error.connect(self._show_error)
        self.code_view.search_repo_requested.connect(self._search_repo)
        self.code_view.open_requested.connect(self._open_path)
        self.code_view.reveal_requested.connect(
            lambda path: self._open_path(path, reveal=True))
        self.code_view.indexing_requested.connect(
            lambda: self._show(self.indexing_view))

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
            (self.mail_view, "Mail", False),
            # After Mail and before Indexing: the four "find something" tabs
            # stay together and the two "manage the app" tabs stay at the end.
            (self.code_view, "Code", False),
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

        # **Every scroll-sensitive control in the window, in one call.**
        # Qt lets the wheel change a combo box or spin box that does not have
        # focus, so scrolling a settings page silently alters the memory
        # ceiling, the worker count and the schedule on the way past. Doing
        # this per page would mean the one somebody forgets is the one that
        # matters; doing it here means a new page gets it for free.
        guarded = protect_all(self)
        _log.debug("wheel-guarded {} controls", guarded)

        # **Opt-in, and off until asked for.** An application that vanishes
        # from the taskbar when you did not ask it to is alarming: you close a
        # window, it disappears, and there is no obvious way back.
        # Restore the two switches that persist as window state. Set before the
        # signals are live would be simpler, but these are connected in the
        # block above - so the stored value is written back through the same
        # handler, which is harmless and keeps one path rather than two.
        self.settings_view.cloud.setChecked(
            self._read_state("ui:index_cloud", "") == "on")
        # **Both controls, from one stored value.** The Settings checkbox was
        # initialised here and the toolbar one was hard-coded True and never
        # saved, so the two disagreed from the first launch after anybody
        # changed it - and the toolbar is the one every search actually reads.
        stored_rerank = self._read_state("ui:rerank_enabled", "")
        if stored_rerank:
            wanted = stored_rerank == "on"
            self.settings_view.rerank.setChecked(wanted)
            self._set_toolbar_rerank(wanted)

        self.tray = TrayPresence(self)
        self.tray.minimise_to_tray = self._read_state("ui:tray_minimise", "") == "on"
        self.tray.close_to_tray = self._read_state("ui:tray_close", "") == "on"
        self.settings_view.window_box.load(
            self.tray.minimise_to_tray, self.tray.close_to_tray)
        self.settings_view.tray_changed.connect(self._tray_changed)
        if self.tray.minimise_to_tray or self.tray.close_to_tray:
            if not self.tray.install():
                # Never silently: a preference that does nothing is worse than
                # one that is not offered.
                self.tray.minimise_to_tray = self.tray.close_to_tray = False
                _log.warning("no system tray available; minimising normally")
        self.indexing_view.finished.connect(
            lambda stats: self.tray.set_status(
                f"{getattr(stats, 'indexed', 0):,} indexed"))

        self.setStatusBar(QStatusBar())
        self._build_shortcuts()
        self._wire_recorder()

        self._apply_theme()
        self.search_view.focus()

        # **Nothing runs on a background thread until construction is over.**
        #
        # `refresh_totals`, `_warm_translator` and `_warm_models` each start a
        # worker, and they used to start here - part way through `__init__`,
        # with `_apply_theme()` still to come. That put a thread opening SQLite
        # connections and reading the schema alongside a main thread applying a
        # stylesheet, which re-polishes every widget in the tree.
        #
        # The result was an access violation during `MainWindow.__init__`: no
        # Python exception, no traceback, no window. The faulthandler dump named
        # `_apply_theme` on the main thread and `read_index_summary ->
        # SqliteStore.stats -> _new_connection` on another, which is the whole
        # story - two threads inside a half-built window.
        #
        # A zero-delay timer starts them on the next turn of the event loop,
        # when the widget tree is complete and Qt is idle. Nothing about the
        # window waits for any of them, so the only visible difference is that
        # the file counts appear a frame later.
        #
        # **`QTimer` is imported at the top of this module, not here.** It used
        # to be imported on this line, and that was harmless right up until the
        # watch timer above needed it too: a function-local `import` makes the
        # name local for the *entire* function, so a use earlier in `__init__`
        # raised `UnboundLocalError` and the window would not open at all.
        # Python binds by function, not by line.
        QTimer.singleShot(0, lambda: self._start_background_work(store, settings))

    def _start_background_work(self, store: Any, settings: Any) -> None:
        """Everything that touches a thread or the store. See `__init__`.

        Guarded as a whole: a window that opens with no file count is a small
        problem, and one that refuses to open is a total one.
        """
        try:
            # The watch for a run another process is doing. Here rather than in
            # `__init__` for the reason this whole method exists.
            self._watch_timer.start()
            self._poll_external_run()
            self.indexing_view.refresh_totals(store, settings)
            # The two Settings labels that need the store or an import. They
            # used to be filled during `SettingsView.__init__`, which is inside
            # `MainWindow.__init__` - a COUNT(*) and a module import on the UI
            # thread before the first frame.
            self.settings_view.refresh_slow_labels()
            self._refresh_status()
            self._start_scheduler()
            self._warm_translator()
            self._warm_models()
        except Exception as exc:                 # noqa: BLE001
            _log.warning("background start-up work failed: {}", exc)

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
        bind("Ctrl+Shift+P", self._toggle_preview)
        bind("Ctrl+M", self._focus_mail)
        # **The Code tab was the only find tab without one.** Search, Files and
        # Mail all have a key that jumps to their box; Code did not, so the one
        # tab whose users are most likely to be keyboard-driven was the one that
        # needed a mouse. Ctrl+E for "code", since Ctrl+C is taken by copy.
        bind("Ctrl+E", self._focus_code)
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
        # **"Next run" was permanently blank.** `set_next_run` existed, said what
        # it was for, and nothing ever called it - so the one line answering "is
        # this thing going to run on its own, and when" showed nothing at all,
        # on a page whose whole job is to answer that.
        self.scheduler.state_changed.connect(self.indexing_view.set_next_run)
        self.indexing_view.finished.connect(lambda _stats: self.scheduler.notify_finished())
        self.scheduler.start()
        self.indexing_view.set_next_run(self.scheduler.status())

    def _schedule_changed(self, policy: Any) -> None:
        """Apply a schedule change immediately, and persist it.

        Persisted in `index_state` rather than rewritten into `.env`: the app
        must never edit a file the user maintains by hand, and a settings panel
        that silently rewrites configuration is how hand-written comments and
        overrides disappear. `.env` remains the default; this is the override.
        """
        self._store.set_states({
            "ui:index_schedule": policy.mode,
            "ui:index_interval_hours": str(policy.interval_hours),
            "ui:index_daily_at": f"{policy.daily_at[0]:02d}:{policy.daily_at[1]:02d}",
        })
        self.scheduler.set_policy(policy)
        self.settings_view.indexing.set_schedule_status(self.scheduler.status())

    def _rerank_toggled(self, enabled: bool) -> None:
        """Apply the rerank switch now, and remember it.

        Live where it can be: the engine holds the reranker, and a quality
        setting that needs a restart to take effect is one people conclude does
        nothing. Persisted alongside, so the next launch agrees with the box.
        """
        reranker = getattr(self._engine, "reranker", None)
        if reranker is not None:
            try:
                reranker.enabled = bool(enabled)
            except Exception as exc:             # noqa: BLE001 - never fatal
                _log.warning("could not apply the rerank setting live: {}", exc)
        # **Whichever control was used, the other follows.** Signals are blocked
        # on the way in, or setting one would emit back into this handler and
        # the two would bounce off each other.
        self._set_toolbar_rerank(bool(enabled))
        settings_box = getattr(self.settings_view, "rerank", None)
        if settings_box is not None and settings_box.isChecked() != bool(enabled):
            settings_box.blockSignals(True)
            settings_box.setChecked(bool(enabled))
            settings_box.blockSignals(False)
        self._store.set_state("ui:rerank_enabled", "on" if enabled else "off")

    def _set_toolbar_rerank(self, enabled: bool) -> None:
        """Show `enabled` on the search bar's box without re-emitting."""
        toggle = getattr(self.search_view, "rerank_toggle", None)
        if toggle is None or toggle.isChecked() == enabled:
            return
        toggle.blockSignals(True)
        toggle.setChecked(enabled)
        toggle.blockSignals(False)

    def _change_index_location(self) -> None:
        """Ask what to do about the index location, then record the decision.

        **Nothing is moved from here, and nothing is moved while the app is
        running.** The stores are open; copying a database out from underneath
        an open connection is how a half-copied index becomes the only index.
        So the decision is written down and applied by the installer path on the
        next start, which is the one moment nothing is holding the files.

        `.env` is written by `env_writer`, never by hand - that is the rule the
        settings work established, and this is the setting most able to do harm.
        """
        from app.ui.widgets.index_flows import ADOPT, FRESH, IndexLocationDialog

        if self.indexing_view.is_running():
            self.statusBar().showMessage(
                "An index run is in progress. Stop it before moving the index.",
                8_000)
            return

        dialog = IndexLocationDialog(Path(self._settings.data_path), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        choice = dialog.choice()

        # **`.env` is NOT written here, and that is a correction.** It used to
        # be written immediately while the files stayed put - so the next start
        # opened an empty folder and an intact index became unreferenced. The
        # write and the move are one operation, performed together at startup by
        # `app.core.index_move`, before any store opens. Until then nothing has
        # changed and the application keeps working exactly as it did.
        try:
            from app.core.index_move import plan_move, write_pending

            plan_move(Path(self._settings.data_path), choice.destination, choice.action)
            write_pending(Path(self._settings.project_path), choice.action, choice.destination)
        except Exception as exc:                 # noqa: BLE001
            self._show_error(to_app_error(exc, "ui.settings"))
            return

        self.settings_view.data_path.setText(str(choice.destination))

        if choice.action == ADOPT:
            what = "will use the index already there"
        elif choice.action == FRESH:
            what = "will start a new, empty index there"
        else:
            what = "will move the index there, which can take a while"
        self.statusBar().showMessage(
            f"Saved: the app {what} when you restart it. Nothing has moved yet, "
            "and this index keeps working until then.", 12_000)

    def _change_meaning_model(self) -> None:
        """Confirm the cost of changing the embedding model, then record it."""
        from app.ui.widgets.index_flows import RebuildVectorsDialog

        if self.indexing_view.is_running():
            self.statusBar().showMessage(
                "An index run is in progress. Stop it before changing the model.",
                8_000)
            return

        current_dim = int(getattr(self._settings, "embed_dim", 384) or 384)
        dialog = RebuildVectorsDialog(
            str(getattr(self._settings, "embed_model", "")),
            self._chunk_count(),
            self,
            current_dim=current_dim,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        try:
            from app.core.env_writer import apply_values

            # **Both keys, in one write.** Writing `EMBED_MODEL` alone left
            # `EMBED_DIM` describing the previous model, which is not a
            # settings inconsistency but a broken index: the store refuses
            # vectors of the wrong width, and the refusal arrives on the first
            # batch after the new model has been downloaded, naming a setting
            # the person never edited. `env_writer` writes the file atomically,
            # so the two cannot land apart.
            apply_values(Path(self._settings.env_file), {
                "EMBED_MODEL": dialog.chosen_model(),
                "EMBED_DIM": str(dialog.chosen_dim()),
            })
        except Exception as exc:                 # noqa: BLE001
            self._show_error(to_app_error(exc, "ui.settings"))
            return

        self._store.set_state("index:rebuild_vectors", "pending")
        if dialog.chosen_dim() != current_dim:
            self.statusBar().showMessage(
                f"Saved - {dialog.chosen_model()} at {dialog.chosen_dim()} "
                "dimensions. The vector store is rebuilt from empty on the next "
                "index run, so meaning-based search returns nothing until it "
                "finishes. Keyword search is unaffected.", 20_000)
        else:
            self.statusBar().showMessage(
                "Saved. Restart, then run an index to re-embed everything - search "
                "keeps working on the old vectors until it finishes.", 12_000)

    def _chunk_count(self) -> int:
        """How many chunks would have to be re-embedded. Never raises.

        A count over `chunks` is one indexed aggregate and runs once, in
        response to a deliberate click, to put a real number in front of a
        decision that costs hours. Zero if it cannot be read - the dialog then
        says "a few minutes", which is the honest thing to say when the size is
        unknown rather than a number that was guessed.
        """
        try:
            return int(self._store.stats().get("chunks_total", 0))
        except Exception as exc:                 # noqa: BLE001
            _log.debug("could not count chunks: {}", exc)
            return 0

    def _settings_changed(self, values: dict) -> None:
        """Write `.env` settings a panel has changed, and apply what applies now.

        **The application writes `.env`; the user never does.** That is the rule
        the settings work established, and the reason `env_writer` exists - it
        preserves comments and keys this build has never heard of, so a newer
        installer's settings survive an older window saving one number.

        Debounced upstream, so this runs once when somebody stops adjusting a
        control rather than once per notch.
        """
        if not values:
            return
        try:
            from app.core.env_writer import apply_values

            apply_values(Path(self._settings.env_file), values)
        except Exception as exc:                 # noqa: BLE001
            self._show_error(to_app_error(exc, "ui.settings"))
            return

        # Reranking is the one that can take effect without a restart, and the
        # one people most want to see change - the rest are read when the thing
        # that uses them next starts.
        reranker = getattr(self._engine, "reranker", None)
        if reranker is not None:
            for key, attribute in (("RERANK_TOP_N", "top_n"),
                                   ("RERANK_WINDOW_CHARS", "window_chars")):
                if key in values:
                    try:
                        setattr(reranker, attribute, int(values[key]))
                    except Exception as exc:     # noqa: BLE001 - never fatal
                        _log.debug("could not apply {} live: {}", key, exc)

        if "RERANK_MODEL" in values:
            self.statusBar().showMessage(
                "Saved. The rerank model is loaded at startup, so it changes "
                "the next time the app opens.", 8_000)

    def _tray_changed(self, minimise: bool, close: bool) -> None:
        """Apply and persist the tray preferences.

        Installs the icon the moment either is switched on, and says so if the
        desktop has no tray - a preference that silently does nothing is worse
        than one that is not offered, and this one was previously both.
        """
        self.tray.minimise_to_tray = bool(minimise)
        self.tray.close_to_tray = bool(close)
        self._store.set_states({
            "ui:tray_minimise": "on" if minimise else "off",
            "ui:tray_close": "on" if close else "off",
        })

        if (minimise or close) and not self.tray.installed and not self.tray.install():
            self.tray.minimise_to_tray = self.tray.close_to_tray = False
            self.settings_view.minimise_to_tray.setChecked(False)
            self.settings_view.close_to_tray.setChecked(False)
            self.statusBar().showMessage(
                "This desktop has no notification area, so the window will "
                "minimise normally.", 8_000)

    def _cloud_toggled(self, enabled: bool) -> None:
        """Remember whether to index cloud-only files.

        Read live when a run starts, so it always worked *for that run* - and
        reset to off at every launch, which looks exactly like a setting being
        ignored. In `index_state` rather than `.env`: it is a decision about how
        this window starts a run, and the walker takes it as a parameter.
        """
        self._store.set_state("ui:index_cloud", "on" if enabled else "off")

    def _limits_changed(self, values: dict) -> None:
        r"""Persist the resource ceilings. They take effect on the next run.

        Not on the run in flight: changing the worker count mid-run would mean
        stopping and restarting threads that are holding files open, and the
        gain is a few minutes on a job measured in hours.

        **These were written to the wrong place, and so they did nothing.**
        Every ceiling here landed in `index_state` under `ui:index_memory_mb`
        and friends - and nothing anywhere read those keys. `limits_from_
        settings` reads `Settings`, which is built from `.env`, so the memory
        ceiling, the worker count, the CPU cap and the free-space floor were all
        adjustable, saved, reported as saved, and inert. Six controls with real
        consequences, none of which had any.

        It matters more at a terabyte than it did at 100GB: raising the memory
        ceiling is the difference between a run that pauses constantly and one
        that does not, and somebody who raised it and saw no change would
        reasonably conclude the governor is broken rather than that the setting
        never arrived.

        So it goes through `.env` like every other setting - non-negotiable 11 -
        and the in-memory `Settings` is updated too, so the *next run in this
        session* uses it rather than requiring a restart.
        """
        if not values:
            return
        # `current_limits` keys are `Settings` field names, and the `.env` key
        # is the same name upper-cased - which is not a coincidence, it is how
        # `config.load_settings` reads them. Asserted by `test_settings_registry`.
        self._settings_changed({key.upper(): value for key, value in values.items()})
        for key, value in values.items():
            try:
                setattr(self._settings, key, value)
            except Exception as exc:                 # noqa: BLE001 - never fatal
                _log.debug("could not apply {} to the live settings: {}", key, exc)
        self.statusBar().showMessage("Saved. Applies to the next index run.", 5_000)

    def _ollama_model_changed(self, enabled: bool, model: str, timeout_s: int) -> None:
        """Apply a model choice immediately, and persist it.

        **Live, not on restart.** The client and the translator are mutated in
        place rather than rebuilt, so the choice takes effect on the very next
        press of Interpret - which matters because the natural next thing to do
        after choosing a model is to try it.

        The health cache is cleared: it was answered about the *old* model, and
        a stale "yes" would let a generate call proceed against a model that is
        not installed, failing several seconds later for no visible reason.
        """
        self._translator.reconfigure(
            model=model or None, timeout_s=float(timeout_s), enabled=enabled)
        self._store.set_states({
            "ui:ollama_enabled": "on" if enabled else "off",
            "ui:ollama_model": model,
            "ui:ollama_timeout_s": str(int(timeout_s)),
        })
        # The button appears and disappears with the setting, rather than
        # sitting there greyed out - an Interpret button that cannot interpret
        # is a permanent question with no answer on screen.
        self.search_view.set_interpret_enabled(enabled)
        self._warm_translator()
        self.statusBar().showMessage(
            f"Interpret will use {model}, with up to {timeout_s}s." if enabled
            else "Query interpretation is off. Search is unaffected.", 8_000)

    def _warm_translator(self) -> None:
        """Load the model, once, at the moment somebody asks for the feature.

        **Not at startup.** Interpretation is optional and off by default, and
        loading a model into VRAM for somebody who never presses the button is
        a cost they did not ask for. But the *first* press then pays the load -
        8.2s measured here, against a five-second budget - and reports a
        timeout, which reads as a broken model rather than a cold one. Ollama
        also drops the model again after five minutes of quiet by default; the
        client now says thirty.

        **On a worker**, because it is a network round trip and this runs
        inside the handler that saves a setting. Nothing waits for the result:
        a warm that fails costs a slow first press, which is where we started.
        """
        translator = getattr(self, "_translator", None)
        if translator is None or not getattr(translator, "just_enabled", False):
            return
        translator.just_enabled = False

        worker = CallableWorker(translator.warm, component="ui.translate")
        worker.signals.failed.connect(lambda _error: None)
        run(QThreadPool.globalInstance(), worker)

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

    def _toggle_preview(self) -> None:
        """Ctrl+Shift+P, on whichever list is in front.

        **Deliberately not one setting for the whole window.** Each list keeps
        its own preferences under its own key, and somebody who wants the pane
        on Code has said nothing about wanting it on Mail. The key is the same
        everywhere, which is the part that has to be consistent.

        Guarded: not every page has a list - Settings and Indexing do not - and
        a shortcut that raises on the wrong tab is worse than one that does
        nothing there.
        """
        button = getattr(self._current_view(), "view_button", None)
        toggle = getattr(button, "toggle_preview", None)
        if toggle is not None:
            toggle()

    def _focus_mail(self) -> None:
        """Ctrl+M. Mail is a browser, so this lands in its filter box."""
        self._show(self.mail_view)
        self.mail_view.focus()

    def _focus_code(self) -> None:
        """Ctrl+E. The Code tab, and its search box selected."""
        self._show(self.code_view)
        self.code_view.focus()

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

    def _current_view(self) -> Any:
        """The view whose tab is in front, or None.

        **By index, never `tabs.currentWidget()`.** A view inside a scroll area
        is not the tab's widget - the scroll area is - which is the same trap
        `_show` exists to avoid, and it returns the wrong object silently.
        """
        index = self.tabs.currentIndex()
        for view, at in self._tab_index.items():
            if at == index:
                return view
        return None

    def _tab_changed(self, index: int) -> None:
        """Refresh what the tab shows, then put the cursor where typing goes.

        **Every tab, not one.** Files focused its filter and Search and Mail did
        not, so switching to the tab whose entire purpose is a text box left the
        person reaching for the mouse to click into it. The rule is the same one
        `protect_all` follows: do it once for every view rather than per page,
        so the one somebody forgets is not the one that matters - and a tab
        added later gets it without anybody remembering.

        Asked for by capability rather than by name: a view that has nowhere to
        type has no `focus`, and Settings deliberately does not steal it.
        """
        if index == self._tab_index.get(self.indexing_view):
            self.indexing_view.refresh_totals(self._store, self._settings)
        elif index == self._tab_index.get(self.files_view):
            self.files_view.refresh_summary()
        elif index == self._tab_index.get(self.code_view):
            # On the way in rather than on a timer: repositories change when an
            # index run finds one, which is rare and never while somebody is
            # looking at this tab.
            self.code_view.refresh()

        # By index rather than by widget: a view inside a scroll area is not the
        # tab's widget, which is the same trap `_show` exists to avoid.
        for view in (self.search_view, self.files_view, self.mail_view,
                     self.code_view):
            if self._tab_index.get(view) == index:
                view.focus()
                break


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
        """Open the file, or reveal it, without waiting for Explorer.

        **This was the "too slow" report.** `explorer /select,` takes a few
        hundred milliseconds just to start, and the `exists()` check before it
        is a stat that can block for seconds on a network share or a sleeping
        drive - and both ran on the UI thread, so the window sat frozen through
        a launch that is nearly free once it is off the critical path.

        The click now returns immediately and the error, if any, arrives later.
        """
        self._open_path(row.path, reveal=reveal)

    def _open_path(self, path: str, *, reveal: bool = False) -> None:
        """The same, for a caller that has a path rather than a result row.

        The Code tree hands up a path: its rows are repositories and files, not
        search results, and giving it a fake row to satisfy an attribute lookup
        would be the wrong way round.
        """
        # The shared helper - see `workers.open_async`. This was the correct
        # version and `files_view` had its own, blocking, copy; one function now,
        # so a third caller cannot get it wrong.
        open_async(path, reveal=reveal, on_error=self._show_error)

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

    def _load_code_types(self) -> tuple:
        """Which file types the Code tab lists. See `app/core/code_types.py`."""
        from app.core.code_types import choice_from

        return choice_from(self._store)

    def _save_code_types(self, preset: str, groups: list) -> None:
        from app.core.code_types import STATE_KEY, dump_choice

        try:
            self._store.set_state(STATE_KEY, dump_choice(preset, groups))
        except Exception as exc:                 # noqa: BLE001
            _log.warning("code file types not saved: {}", exc)
            self.statusBar().showMessage(
                "That Code file-type choice was not saved.", 8_000)
            return
        # The Code tab reads this per search, so it takes effect on the next
        # keystroke - but it is already on screen, so redraw it now.
        self.code_view.refresh()

    def _load_root_modes(self) -> dict:
        """Which folders the owner has declared static. See `index/archives.py`."""
        from app.index.archives import MODE_STATE_KEY, load_modes

        try:
            return load_modes(self._store.get_state(MODE_STATE_KEY, "") or "")
        except Exception as exc:                     # noqa: BLE001
            _log.debug("index root modes not read: {}", exc)
            return {}

    def _save_root_modes(self, modes: dict) -> None:
        from app.index.archives import MODE_STATE_KEY, dump_modes

        try:
            self._store.set_state(MODE_STATE_KEY, dump_modes(modes))
        except Exception as exc:                     # noqa: BLE001
            # **Said out loud.** A mode that silently failed to save looks like
            # it worked until the next run walks 1.5TB anyway, and by then
            # nobody connects the two.
            _log.warning("index root modes not saved: {}", exc)
            self.statusBar().showMessage(
                "That folder's Live/Archive setting was not saved.", 8_000)

    def _rescan_archives(self) -> None:
        """One full walk of every archival folder, now. Not a policy change."""
        self._start_indexing(recheck_archives=True)

    # -- a run belonging to another process ---------------------------------

    def _poll_external_run(self) -> None:
        r"""Is something else indexing, and how far has it got?

        **Two questions, and only one of them is authoritative.** `is_indexing`
        asks the mutex, which the operating system releases when a process dies;
        `active_run` reads the description that process last wrote. A record
        without a lock is a crash, not a run, and must never refuse Start.

        Off the UI thread, because both touch the store and this runs on a timer
        for as long as the window is open. Cheap - one mutex probe and one row -
        but "cheap" on the UI thread is how a window develops a stutter nobody
        can attribute.
        """
        if self.indexing_view.is_running() and self.indexing_view._worker is not None:
            return                       # our own run; the live signal is better

        worker = CallableWorker(_read_external_run, self._store,
                                component="ui.index.watch")
        worker.signals.finished.connect(self._show_external_run)
        run(QThreadPool.globalInstance(), worker)

    def _show_external_run(self, payload: dict) -> None:
        self.indexing_view.show_external(
            payload.get("record"), locked=bool(payload.get("locked")))

    def _stop_external_run(self) -> None:
        """Ask the other process to stop. A request, not a kill.

        Terminating it would leave the vector store mid-write, which is the one
        thing the run lock exists to prevent - so this writes the flag and the
        runner honours it at its next checkpoint, keeping everything read so far.
        """
        from app.core.run_lock import request_stop

        try:
            request_stop(self._store)
        except Exception as exc:                 # noqa: BLE001
            _log.warning("could not ask the other run to stop: {}", exc)

    def _scan_corpus(self) -> None:
        r"""Count the corpus so the progress bar has a real denominator.

        Answers the complaint behind the Scan button. `app.cli scan` was the
        only thing that had ever written a total, and nothing in the window
        could run one - so a GUI-started index always had `total_estimate == 0`
        and the bar was a busy indicator for its entire length. Correct by its
        own rules, and indistinguishable from broken.

        **Reads no file contents**, so it takes no run lock: it walks folders,
        adds up sizes, and opens only the tail of a sampled archive and a
        sampled PDF. Two of these at once would waste effort and nothing worse.
        """
        chosen = self.settings_view.current_roots()
        if not chosen:
            self._show(self.settings_view)
            self.statusBar().showMessage(
                "Add at least one folder to index in Settings.", 8_000)
            return

        self.indexing_view.scan_button.setEnabled(False)
        self.statusBar().showMessage("Counting files… the bar will show a real "
                                     "percentage once this finishes.", 0)

        worker = CallableWorker(_scan_and_save, self._store, chosen,
                                component="ui.index.scan")
        worker.signals.finished.connect(self._scan_finished)
        worker.signals.failed.connect(self._show_error)
        worker.signals.done.connect(
            lambda: self.indexing_view.scan_button.setEnabled(True))
        run(QThreadPool.globalInstance(), worker)

    def _scan_finished(self, payload: dict) -> None:
        files = int(payload.get("files", 0) or 0)
        self.statusBar().showMessage(
            f"{files:,} files to index. The progress bar can show a percentage "
            f"now.", 10_000)

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
            self.settings_view.add_root(str(target))

    def _save_roots(self, roots: list[str]) -> None:
        try:
            # The key `app.cli index` reads when it is given no folders, so
            # the command line and the window index the same thing. Named
            # rather than spelled out twice - see `cli.ROOTS_STATE_KEY`.
            from app.cli import ROOTS_STATE_KEY

            self._store.set_state(ROOTS_STATE_KEY, "|".join(roots))
        except Exception as exc:                 # noqa: BLE001
            _log.warning("index roots not saved: {}", exc)

    def _start_indexing(self, *, roots: Optional[list[str]] = None,
                        recheck_archives: bool = False) -> None:
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
                    name_only=bool(getattr(
                        self._settings, "index_name_only", True)),
                ),
                # Memory, CPU, battery and disk ceilings, from .env. Without
                # these an index run competes with whatever the person is
                # actually doing, and gets switched off for good.
                limits=limits_from_settings(self._settings),
                min_free_gb=self._settings.min_free_gb,
                required_free_gb=int(getattr(self._settings, "required_free_gb", 0)),
                ocr_mode=str(getattr(self._settings, "index_ocr_mode", "both")),
                prune_missing=roots is None,     # a folder-scoped run must not prune the rest
                # A folder marked as an archive is walked once and then checked
                # with one `stat` - the largest single saving available on a
                # settled corpus. `recheck_archives` is the "Rescan archived
                # folders now" button, which walks them all in full this once.
                recheck_archives=recheck_archives,
                recheck_days=int(getattr(self._settings, "archive_recheck_days", 30)),
            ),
        )
        # **The window's run is a writer like any other**, so it names itself
        # on the published record and holds the same lock the CLI takes. The
        # lock itself is acquired by `IndexWorker`, on the worker thread, for
        # exactly as long as the run - taking it here would hold it across the
        # whole life of the window again, which is the bug being fixed.
        pipeline.run_owner = GUI
        self.indexing_view.start(pipeline, total_estimate=self._scan_total(chosen))

    def _scan_total(self, roots: list[str]) -> int:
        """How many files `app.cli scan` counted, if it counted these folders.

        **Without it a week-long run has no percentage at all.** The bar grows
        its own denominator from what the walker has found so far, which is
        honest but reads as 97% within the first minute - the work queue is
        bounded, so `seen` is never far ahead of `done`. A scan is the only
        thing that knows the real total, and this is where it gets used.

        Zero for no scan or a scan of different folders, which `progress_for`
        already reads as "no estimate".
        """
        from app.index.scan import SCAN_STATE_KEY, saved_total

        try:
            return saved_total(self._store.get_state(SCAN_STATE_KEY, "") or "", roots)
        except Exception as exc:                     # noqa: BLE001 - a bar, not a run
            _log.debug("no scan total available: {}", exc)
            return 0

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

    def _search_repo(self, name: str) -> None:
        """Found the repository; now find what is in it.

        **The filter stays visible and editable.** It is put in the box rather
        than applied as hidden state, for the same reason an interpreted query
        is shown: invisible narrowing makes search unpredictable, and somebody
        who can see `repo:"leasha"` can delete it or type another. That is the
        feature, not a leak.

        The name is quoted because repository folders contain spaces more often
        than anybody would like, and an unquoted value ends at the first one -
        matching a different repository, or none, with nothing on screen to
        explain why.
        """
        if not name:
            return
        self._show(self.search_view)
        self.search_view.set_scope("code")
        self.search_view.input.setText(f'repo:"{name}" ')
        self.search_view.input.setFocus()
        self.statusBar().showMessage(
            f"Searching {name} - type what you are looking for.", 8_000)

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

        def clear() -> dict:
            # **Measured, because "it did nothing" was the report.** The size on
            # the Indexing page is the whole of DATA_PATH, which includes the
            # 130MB model cache and deliberately survives a reset - so on a
            # small index the number barely moves and there is nothing saying
            # why. Weighing the two things a reset actually removes, before and
            # after, turns that into a sentence.
            before = index_bytes(self._store, self._settings)
            removed = self._store.clear_index()
            self._vectors.drop()
            return {"removed": removed,
                    "freed": max(0, before - index_bytes(self._store, self._settings))}

        worker = CallableWorker(clear, component="ui.reset")
        worker.signals.finished.connect(self._index_cleared)
        worker.signals.failed.connect(self._show_error)
        run(QThreadPool.globalInstance(), worker)

    def _index_cleared(self, outcome: Any) -> None:
        self.statusBar().showMessage(cleared_message(outcome), 20_000)
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

    def changeEvent(self, event: Any) -> None:          # noqa: N802
        """Hide to the tray when minimised, if that was asked for."""
        from PyQt6.QtCore import QEvent

        super().changeEvent(event)
        if (event.type() == QEvent.Type.WindowStateChange
                and self.isMinimized() and self.tray.minimise_to_tray
                and self.tray.installed):
            # Deferred: hiding inside the state-change handler leaves Qt
            # half-way through a transition it has not finished describing.
            QTimer.singleShot(0, self._hide_to_tray)

    def _hide_to_tray(self) -> None:
        self.hide()
        self.tray.notify_hidden()

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
        # Close-to-tray is a *hide*, so nothing is torn down and the index lock
        # stays held deliberately. Quit from the tray menu clears the flag first,
        # so it falls through to the real shutdown below.
        if self.tray.close_to_tray and self.tray.installed:
            event.ignore()
            self._hide_to_tray()
            return

        self.recorder.event("closing")
        # **Every stage is timed, and the total is logged.** The window was
        # reported as "lingering" after close, and the run log could not confirm
        # or deny it: there was no line between "entering the event loop" and
        # "the event loop returned", so a shutdown that took twenty seconds and
        # one that took two looked identical. The single-instance lock is held
        # for all of that time - correctly, the stores are still open - so a
        # slow close is what a relaunch runs into. Timing each stage means the
        # next slow one names itself instead of being guessed at.
        began = time.monotonic()
        _log.info("closing: stopping timers and background work")
        stages: list[tuple[str, float]] = []

        def stage(name: str, action: Any) -> None:
            started = time.monotonic()
            try:
                action()
            except Exception as exc:                     # noqa: BLE001
                _log.warning("closing: {} failed, continuing: {}", name, exc)
            stages.append((name, time.monotonic() - started))

        # **Stop new work before tearing anything down.** Twelve threads were
        # still running at close, and the debounce timers kept firing into an
        # engine and a store that were being shut. Cancelling first turns a race
        # into an ordinary stop - the same reasoning as asking the index run to
        # stop rather than closing over it.
        for view in (self.search_view, self.files_view, self.mail_view,
                     self.code_view):
            stage(type(view).__name__, view.shutdown)
        # A ceiling changed in the last third of a second is still sitting in a
        # timer. Closing without this loses it - which would be a worse bug than
        # the sluggishness the debounce was added to fix.
        stage("settings", self.settings_view.indexing.flush_pending)
        stage("indexing", self.indexing_view.stop)
        stage("scheduler", self.scheduler.stop)
        stage("workers", self._drain_workers)
        stage("recorder", self.recorder.close)
        stage("engine", self._engine.close)

        slow = ", ".join(f"{name} {seconds:.1f}s"
                         for name, seconds in stages if seconds >= 0.2)
        _log.info("closing: took {:.1f}s{}", time.monotonic() - began,
                  f" ({slow})" if slow else "")
        super().closeEvent(event)

    def _drain_workers(self) -> None:
        """Wait for the thread pool, keeping the UI alive while it empties.

        `waitForDone` alone would block the UI thread, so a slow worker would
        freeze the window during the one operation nobody will wait out - and
        we would be back to Task Manager. Pumping events while waiting keeps
        the window painting until the threads are actually finished.
        """
        from PyQt6.QtCore import QDeadlineTimer, QEventLoop
        from PyQt6.QtWidgets import QApplication

        pool = QThreadPool.globalInstance()
        deadline = QDeadlineTimer(self.SHUTDOWN_GRACE_MS)
        while pool.activeThreadCount() and not deadline.hasExpired():
            pool.waitForDone(50)
            # **User input excluded.** Pumping *all* events here re-enters the
            # loop while the window is closing, so a keystroke or a click landing
            # in that window starts a fresh search against a store that is about
            # to be shut - the very race `shutdown()` was just called to end.
            # Paint and timer events are what keep the window alive while the
            # pool empties; input is not, and there is nothing useful left to do
            # with it.
            QApplication.processEvents(
                QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 10)

        remaining = pool.activeThreadCount()
        if remaining:
            _log.warning(
                "closing with {} background thread(s) still running; "
                "they will be abandoned", remaining,
            )
