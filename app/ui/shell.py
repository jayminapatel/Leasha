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
from dataclasses import replace
from pathlib import Path
from datetime import datetime
from typing import Any, Optional

from PyQt6.QtCore import QThreadPool, QTimer, pyqtSignal
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
from app.ui.offline_media_view import OfflineMediaView
from app.ui.search_view import SearchView
from app.ui.settings_view import SettingsView
from app.ui.scheduler import IndexScheduler
from app.ui.debug_recorder import recorder_for
from app.ui.theme import detect_scheme, stylesheet
from app.ui.tray import TrayPresence
from app.ui.view_options import load_prefs, save_prefs
from app.ui.window_state import restore_window_state, save_window_state
from app.ui.widgets.no_scroll import protect_all
from app.ui.widgets.scroll import wrap_if_needed
from app.core.run_lock import GUI
# **Worker bodies live in the presenter**, not here: `test_ui_never_blocks`
# reads this file and refuses any store call it cannot prove is inside a
# worker, and it cannot prove that of a module-level function defined here.
from app.ui.presenter import (
    _read_external_run, _scan_and_save, cleared_message, index_bytes, index_counts,
    offline_media_run_summary,
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

    #: Work order 0r item 1c, second clause. The CLIP text-tower embedder's
    #: download-progress reporting reaches `SearchEngine.status_callback`
    #: from a plain `threading.Thread` (`_DownloadProgressWatcher`) or from
    #: `SearchEngine`'s own retrieval-pool worker thread - never the GUI
    #: thread. A `pyqtSignal` is what this codebase already uses to marshal
    #: exactly that safely (`app/ui/workers.py`'s `WorkerSignals`, the same
    #: mechanism `IndexWorker`'s `progress` signal relies on): emitting from
    #: any thread onto a receiver that lives on the GUI thread is queued
    #: automatically, where a direct `self.statusBar().showMessage(...)` call
    #: from that background thread would be an unguarded cross-thread Qt call.
    _clip_download_progress = pyqtSignal(str)

    def __init__(
        self,
        settings: Any,
        store: Any,
        vectors: Any,
        engine: Any,
        *,
        image_vectors: Any = None,
        debug: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        #: Settings changed in this session, by lower-cased key.
        #:
        #: **`Settings` is frozen**, so a control changed in the window cannot
        #: write back to it - the value goes to `.env` and lands here, and
        #: whatever needs it live reads both. Narrow on purpose: only the
        #: search behaviours use it today, because they are the only ones that
        #: must take effect before the next launch.
        self._settings_overrides: dict = {}
        #: True while `resolve_for_run` is out on a worker thread, between one
        #: click on Start and the Pipeline actually being built.
        #:
        #: **Guards against a second dispatch, not just a second click.**
        #: `_start_indexing` is also reached from `F5` and from the scheduler's
        #: `due` signal (line ~756, ~775) - neither goes through the disabled
        #: button, so the button alone cannot stop a second resolve worker
        #: starting while the first is still out. `IndexingView.is_running()`
        #: does not help either: it only becomes true once a `Pipeline` exists,
        #: which is the very thing still being resolved.
        self._resolving_index = False
        self._store = store
        self._vectors = vectors
        self._engine = engine
        #: Work order 0h §1c's follow-up: the same `ImageVectorStore` the
        #: search engine already has (`app.main` opens one and hands it to
        #: both), so a run started from this window writes CLIP vectors the
        #: same way `app.cli index` was already fixed to. `None` is a valid,
        #: H4-shaped input - `_start_indexing` degrades to the pre-existing
        #: behaviour (no image lane) rather than raising, exactly like
        #: `SearchEngine`'s own `image_vectors=`/`clip_text_embedder=`.
        self._image_vectors = image_vectors

        # Work order 0r item 1c, second clause: if the CLIP text-tower
        # embedder's model cache is emptied mid-life (a moved index, a
        # re-staged data directory) and the first image search is what next
        # tries to load it, the download has nowhere to report to on its
        # own - the splash that reports the *startup* download (§1c's first
        # clause, already done) is long closed by then, correctly, and there
        # should be no second splash. This window's own notices bar carries
        # the message instead. Wired here, not in `app/main.py`, because the
        # window - and therefore anywhere to show a message - does not exist
        # yet when `engine` is constructed there.
        self._clip_download_progress.connect(
            lambda message: self.statusBar().showMessage(message, 8_000))
        try:
            engine.status_callback = self._clip_download_progress.emit
        except Exception as exc:               # noqa: BLE001 - H4: the window must still open
            _log.debug("could not wire CLIP download progress to the status "
                       "bar: {}", exc)

        self.setWindowTitle(window_title())
        self.resize(1100, 760)
        # A floor, not the opening size. Without one Qt will happily shrink the
        # window until the tab bar is the only thing left, and a view with no
        # scroll area then has controls that cannot be reached at all.
        self.setMinimumSize(720, 480)
        self.setAcceptDrops(True)

        # §4a-4b: restore the window to its last known state (§4c provides the
        # helper for pop-out windows to use too). The user's last geometry is
        # restored, including whether it was maximised; edge cases are handled:
        # a position that is now off-screen is clamped back on, and a minimised
        # state is never restored (a window that starts invisible looks broken).
        # `_read_state` reads from `index_state`, a generic TEXT-column store
        # shared with every other string setting - a raw geometry blob can
        # never come back as `bytes` from it, so the isinstance(bytes) check
        # this used to gate on was dead code that always evaluated False,
        # silently skipping restoration on every single launch. Base64 is the
        # round-trip: encoded to text on the way in (see closeEvent), decoded
        # back to the real bytes save_window_state produces on the way out.
        saved_geometry_b64 = self._read_state("ui:window_geometry", "")
        if saved_geometry_b64:
            try:
                import base64
                restore_window_state(self, base64.b64decode(saved_geometry_b64))
            except Exception as exc:                 # noqa: BLE001
                _log.warning("could not restore window geometry: {}", exc)

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
        self.search_view.preview.pop_out_requested.connect(self._pin_document)

        # Order 202626270513 §2. Read-only refresh is the view's own - see
        # `OfflineMediaView.refresh` - but Scan/Rescan/Delete run a real
        # `Pipeline` (Scan/Rescan) or a full delete cascade, so they are
        # signals the window turns into a `CallableWorker`, the same split
        # `IndexingView` draws for `_start_indexing`. Left synchronous here,
        # unchanged - deferring it was never part of order 0r item 2b's scope.
        self.offline_media_view = OfflineMediaView(store)
        self.offline_media_view.error.connect(self._show_error)
        self.offline_media_view.scan_requested.connect(self._offline_media_scan)
        self.offline_media_view.rescan_requested.connect(self._offline_media_rescan)
        self.offline_media_view.delete_requested.connect(self._offline_media_delete)

        self.tabs = QTabWidget()
        #: view -> the widget actually sitting in its tab (itself, or a
        #: `QScrollArea` wrapping it). Kept so the deferred tabs can be
        #: inserted at the right position once they exist without losing
        #: track of where the others landed - `QTabWidget.indexOf` on the
        #: exact wrapped widget always answers correctly even after an
        #: insertion has shifted everything after it. See
        #: `_construct_deferred_views`.
        self._tab_wrapped: dict[QWidget, QWidget] = {}
        # (view, title, wrap in a scroll area?)
        #
        # **Only Search and Offline Media are in this loop.** Order 0r item
        # 2b: Files, Indexing, Settings, Mail and Code are all built a beat
        # later by `_construct_deferred_views` and inserted at the positions
        # they would have had here, once they exist, so the tab order
        # nobody has to relearn never changes. Offline Media (order
        # 202626270513) stays here, synchronous, unchanged - deferring it
        # was never part of this item's scope.
        for view, title, scroll in (
            (self.search_view, "Search", False),
            (self.offline_media_view, "Offline Media", False),
        ):
            wrapped = wrap_if_needed(view, scroll=scroll)
            self._tab_wrapped[view] = wrapped
            self._tab_index[view] = self.tabs.addTab(wrapped, title)
        # Refresh a panel when it comes forward rather than on a timer: an
        # index run between visits changes what it should show, and polling a
        # table nobody is looking at is work for nothing. Safe to connect
        # before Mail/Code exist: `_tab_changed` only fires on an actual
        # switch, and there is nothing to switch to yet for either of them -
        # it also guards both references regardless, for the same reason
        # `_focus_mail`/`_focus_code` do.
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
        # The tray object and its install happen here, synchronously, so the
        # icon appears on time; pushing its state into the Settings window
        # box, and the two stored search checkboxes (cloud, rerank), all need
        # `settings_view` - order 0r item 2b now builds that a beat later,
        # see `_construct_deferred_views`.
        self.tray = TrayPresence(self)
        self.tray.minimise_to_tray = self._read_state("ui:tray_minimise", "") == "on"
        self.tray.close_to_tray = self._read_state("ui:tray_close", "") == "on"
        if self.tray.minimise_to_tray or self.tray.close_to_tray:
            if not self.tray.install():
                # Never silently: a preference that does nothing is worse than
                # one that is not offered.
                self.tray.minimise_to_tray = self.tray.close_to_tray = False
                _log.warning("no system tray available; minimising normally")

        #: The popped-out log, or None. Workspace §1c: a **copy**, not a move -
        #: the pane in Settings never leaves, so closing this returns nothing
        #: to re-wire. Declared here, synchronously, because `_apply_theme`
        #: (below, and again from `_construct_deferred_views`) pushes the
        #: palette to whichever of the two exist and is called before either
        #: `_log_window` or `settings_view` need to be built.
        self._log_window: Any = None
        #: Workspace §2. **Multiples are allowed and expected** - comparing
        #: two versions of a drawing falls out for free, and pinning the mail
        #: you are answering while you search for what it mentions is the
        #: whole point. Held so Qt does not collect them the moment the local
        #: name goes out of scope, which is how a window flashes and vanishes.
        self._pinned: list = []
        #: Workspace §3a. The box a global shortcut opens, and the listener
        #: that holds the shortcut. Built lazily - a person who never presses
        #: it never pays for it.
        self._mini: Any = None
        self._hotkey: Any = None

        self.setStatusBar(QStatusBar())
        self._build_shortcuts()

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
        #
        # Order 0r item 2b: Files, Indexing, Settings, Mail and Code are all
        # built here too, on the same next-turn-of-the-loop timing, by
        # `_construct_deferred_views` - the same method Mail and Code alone
        # used to be built by (`_construct_secondary_views`, the 2026-09-07
        # lane-d pass), now carrying the three views that pass audited and
        # left open. Scheduled first so it has run by the time
        # `_start_background_work` does - and unlike the mail/code-only
        # version, `_start_background_work` now genuinely depends on that
        # order: it reaches into `indexing_view` and `settings_view`
        # directly, with no guard of its own, on the assumption that this
        # method has already built them.
        QTimer.singleShot(0, lambda: self._construct_deferred_views(store, settings))
        QTimer.singleShot(0, lambda: self._start_background_work(store, settings))

    def _construct_deferred_views(self, store: Any, settings: Any) -> None:
        """Build Files, Indexing, Settings, Mail and Code, and insert them
        where they belong.

        Order 0r item 2b's audit of `MainWindow.__init__` found the
        constructor doing real, synchronous work for tabs nobody sees the
        instant the window appears - Search is the only one shown at first
        paint, and Offline Media (order 202626270513) is left synchronous,
        unchanged, because deferring it was never part of this item's
        scope. The other five move here: built on the next turn of the
        event loop instead of inside the constructor, via the same
        `QTimer.singleShot(0, ...)` idiom `__init__` already uses for
        `_start_background_work`, a few lines above. This is the same
        deferral a 2026-09-07 session (lane-d) built for Mail and Code
        alone, extended now to the three views that pass audited and left
        for a follow-up: Files, Indexing and Settings.

        **Everything that could reach `self.files_view` / `self.indexing_view`
        / `self.settings_view` / `self.mail_view` / `self.code_view` before
        this callback fires is guarded**, for the gap between `show()`
        returning and this method actually running: `_focus_files`,
        `_focus_indexing`, `_focus_settings`, `_focus_mail`, `_focus_code`
        (Ctrl+P / Ctrl+I / Ctrl+, / Ctrl+M / Ctrl+E), `_start_indexing` (F5,
        a drag-and-drop, the scheduler, a search result's re-index button),
        `_tab_changed` (switching tabs), `_apply_theme` (the debug pane's
        palette), `_offline_media_run_done` (the Files summary refresh),
        `_save_code_types` and `closeEvent`. A rapid keypress, drop or close
        in that gap does nothing, rather than raising `AttributeError` on an
        attribute that does not exist yet.
        """
        # **Guarded as a whole**, matching `_start_background_work`'s own
        # reasoning a few lines below. A real, live gap: `test_closing_
        # before_deferred_views_exist_does_not_crash` proved that an
        # immediate `close()` with no event-loop turn at all can leave this
        # `QTimer.singleShot(0, ...)` callback still pending when a caller
        # then closes the store (this file's own `store.close()` in a
        # test's `finally`, or a real close racing ahead of the very first
        # event-loop turn) - the callback still fires later, against a
        # store that is no longer open. A window that never gets its
        # secondary tabs in that vanishingly rare race is a small problem;
        # an unhandled exception reaching Qt's event loop is a bigger one.
        try:
            # -- Indexing ---------------------------------------------------
            self.indexing_view = IndexingView()
            self.indexing_view.error.connect(self._show_error)
            self.indexing_view.reset_requested.connect(self._reset_index)
            self.indexing_view.start_button.clicked.connect(lambda _checked=False: self._start_indexing())
            self.indexing_view.retry_requested.connect(lambda _code: self._start_indexing())
            self.indexing_view.rescan_archives_requested.connect(self._rescan_archives)
            self.indexing_view.scan_requested.connect(self._scan_corpus)
            self.indexing_view.stop_requested_externally.connect(self._stop_external_run)

            # See `__init__`'s historical comment on why these two timers are
            # *built* here (rather than while other widgets are still being
            # assembled) and *started* only in `_start_background_work`: a
            # worker opening SQLite while the main thread is mid-construction
            # produced an access violation with no Python exception and no
            # window, once. Moving their construction into this deferred pass
            # does not reopen that risk - `_start_background_work` is scheduled
            # after this method (see `__init__`), so both timers already exist
            # by the time it starts them.
            self._watch_timer = QTimer(self)
            self._watch_timer.setInterval(4_000)
            self._watch_timer.timeout.connect(self._poll_external_run)

            self._optimize_timer = QTimer(self)
            self._optimize_timer.setInterval(3_600_000)
            self._optimize_timer.timeout.connect(self._run_idle_optimize)

            self.indexing_view.finished.connect(lambda _stats: self._refresh_status())
            # §5c. A finished run is the only free measurement this application
            # ever gets: it is a benchmark somebody already paid for.
            self.indexing_view.finished.connect(self._learn_from_run)
            self.indexing_view.finished.connect(self._offer_images_pass)
            self.indexing_view.finished.connect(
                lambda _stats: self.indexing_view.refresh_totals(self._store, self._settings)
            )
            # A finished index means new filenames, so the Files summary is stale.
            self.indexing_view.finished.connect(lambda _stats: self.files_view.refresh_summary())
            # New mail too, for the same reason.
            self.indexing_view.finished.connect(lambda _stats: self.mail_view.refresh())

            # -- Settings -----------------------------------------------------
            self.settings_view = SettingsView(settings, store)
            self.settings_view.debug_pane.file_chosen.connect(self._open_path)
            self.settings_view.debug_pane.pop_out.connect(self._pop_out_log)
            self.settings_view.set_roots(self._load_roots(), self._load_root_modes())
            self.settings_view.roots_changed.connect(self._save_roots)
            self.settings_view.root_modes_changed.connect(self._save_root_modes)
            self.settings_view.rescan_archives_requested.connect(self._rescan_archives)
            self.settings_view.code_types_changed.connect(self._save_code_types)
            self.settings_view.code_types.load(*self._load_code_types())
            self.settings_view.pst_backend_changed.connect(self._save_pst_backend)
            self.settings_view.ollama_model_changed.connect(self._ollama_model_changed)
            # `model`/`interpret_on` were `__init__` locals, out of reach from a
            # separate method - recomputed the same way `__init__` computed them
            # the first time, which is safe: nothing can have changed them
            # since, because `settings_view` (the only thing that could) did not
            # exist until the line above.
            self.settings_view.models.load(
                self._read_state("ui:ollama_model", "") or settings.ollama_model,
                int(self._translator.timeout_s), enabled=self._translator.enabled)
            self.settings_view.convert_pst_requested.connect(self._convert_pst)
            # The schedule and the tuning screen both live on the Indexing page
            # now - one place to watch a run and to change how it goes. See §4 of
            # the index-tuning order for why they were separated from Settings.
            self.indexing_view.schedule_box.load_indexing(settings)
            self.indexing_view.schedule_box.schedule_changed.connect(self._schedule_changed)
            self.indexing_view.tuning.load(settings)
            # **Both arrive as registry keys**, which `_limits_changed` wants as
            # `Settings` field names - it upper-cases them for `.env` and applies
            # them to the live object, and that second half is what makes a change
            # reach the *next run in this session* rather than the next launch.
            self.indexing_view.tuning.changed.connect(
                lambda values: self._limits_changed(
                    {key.lower(): value for key, value in values.items()}))
            self.indexing_view.tuning.coverage_changed.connect(self._limits_changed)
            self.indexing_view.tuning.benchmark_requested.connect(self._benchmark_models)
            self.settings_view.theme_changed.connect(self._theme_changed)
            # §1a. **What this surface may do on the person's behalf**, resolved
            # once and pushed to the view - `Settings` belongs to the window, and a
            # view that reaches for one has to be given one in every test.
            self._apply_search_preferences()
            self._apply_hotkey()
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
            self.settings_view.file_types.changes_saved.connect(self._file_types_saved)
            self.settings_view.environment.set_recording_status(
                f"Recording to {self.recorder.path.name}" if self.recorder.enabled
                else "Not recording."
            )
            self._apply_pst_backend(self._store.get_state("ui:pst_backend", "auto") or "auto")

            # Restored here rather than in `__init__`: both controls live on
            # `settings_view`, which this method just built.
            self.settings_view.cloud.setChecked(
                self._read_state("ui:index_cloud", "") == "on")
            stored_rerank = self._read_state("ui:rerank_enabled", "")
            if stored_rerank:
                wanted = stored_rerank == "on"
                self.settings_view.rerank.setChecked(wanted)
                self._set_toolbar_rerank(wanted)

            # The tray object itself was built in `__init__` (so the icon
            # installs on time); pushing its state into the Settings window box
            # and wiring the indexing-finished status line both need
            # `settings_view`/`indexing_view`, which did not exist yet then.
            self.settings_view.window_box.load(
                self.tray.minimise_to_tray, self.tray.close_to_tray,
                theme=self._theme_preference)
            self.settings_view.tray_changed.connect(self._tray_changed)
            self.indexing_view.finished.connect(
                lambda stats: self.tray.set_status(
                    f"{getattr(stats, 'indexed', 0):,} indexed"))

            # -- Files ----------------------------------------------------------
            self.files_view = FilesView(store)
            self.files_view.error.connect(self._show_error)
            self.files_view.search_inside_requested.connect(self._search_inside)
            self.files_view.preview.pop_out_requested.connect(self._pin_document)

            # -- Mail gets its own tab for the reason `mail_view.py` opens with: a
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

            # **§2, and it goes here rather than beside Settings for a reason.**
            # Every tab that has a preview can pin one, and each pane carries its
            # own `body_provider` - §2h's "no special casing" for mail, whose
            # message has no file on disk to open.
            for pane in (self.mail_view.preview, self.code_view.results.preview):
                pane.pop_out_requested.connect(self._pin_document)

            # -- Insert every deferred tab in its original position -------------
            # Anchored off Search rather than Files, because Files itself is one
            # of the tabs being inserted here now - Search is the one tab
            # guaranteed to already exist and to never move (always tab 0).
            # Offline Media (order 202626270513, left synchronous) already sits
            # right after Search; each insertion below pushes it one place
            # further along, ending exactly where it was before this item -
            # right after Code - which is the tab order nobody has to relearn.
            after_search = self._tab_index[self.search_view]

            files_wrapped = wrap_if_needed(self.files_view, scroll=False)
            self.tabs.insertTab(after_search + 1, files_wrapped, "Files")
            self._tab_wrapped[self.files_view] = files_wrapped

            mail_wrapped = wrap_if_needed(self.mail_view, scroll=False)
            self.tabs.insertTab(after_search + 2, mail_wrapped, "Mail")
            self._tab_wrapped[self.mail_view] = mail_wrapped

            code_wrapped = wrap_if_needed(self.code_view, scroll=False)
            self.tabs.insertTab(after_search + 3, code_wrapped, "Code")
            self._tab_wrapped[self.code_view] = code_wrapped

            # Indexing and Settings are always last, so a plain append is
            # correct regardless of how many tabs precede them.
            indexing_wrapped = wrap_if_needed(self.indexing_view, scroll=False)
            self._tab_wrapped[self.indexing_view] = indexing_wrapped
            self.tabs.addTab(indexing_wrapped, "Indexing")

            settings_wrapped = wrap_if_needed(self.settings_view, scroll=True)
            self._tab_wrapped[self.settings_view] = settings_wrapped
            self.tabs.addTab(settings_wrapped, "Settings")

            for view, wrapped in self._tab_wrapped.items():
                self._tab_index[view] = self.tabs.indexOf(wrapped)

            # `protect_all` already ran once in `__init__` for every control that
            # existed by then (Search, Offline Media, the chrome); everything
            # built above did not, so it runs again for exactly what it missed.
            # Safe to call twice - a widget guarded a second time is guarded
            # harmlessly, see `protect`.
            guarded = protect_all(self)
            _log.debug("wheel-guarded {} controls (second pass, Files/Indexing/"
                       "Settings/Mail/Code)", guarded)

            # Recording and the debug pane's palette both reach into views this
            # method just built - `_wire_recorder` wires `indexing_view`,
            # `files_view` and `settings_view` signals directly, and
            # `_apply_theme`'s own guard (see its docstring) means this second
            # call is what actually pushes the palette into
            # `settings_view.debug_pane` for the first time.
            self._wire_recorder()
            self._apply_theme()
        except Exception as exc:                 # noqa: BLE001
            _log.warning("deferred view construction failed: {}", exc)

    def _start_background_work(self, store: Any, settings: Any) -> None:
        """Everything that touches a thread or the store. See `__init__`.

        Guarded as a whole: a window that opens with no file count is a small
        problem, and one that refuses to open is a total one.

        **Depends on `_construct_deferred_views` having already run** -
        `indexing_view` and `settings_view` are used below with no guard of
        their own. `__init__` schedules both with `QTimer.singleShot(0,
        ...)`, that method first, so this is safe; it would not be if the
        two were ever reordered.
        """
        try:
            # The watch for a run another process is doing. Here rather than in
            # `__init__` for the reason this whole method exists.
            self._watch_timer.start()
            self._poll_external_run()
            self._optimize_timer.start()
            self.indexing_view.refresh_totals(store, settings)
            # The two Settings labels that need the store or an import. They
            # used to be filled during `SettingsView.__init__`, which is inside
            # `MainWindow.__init__` - a COUNT(*) and a module import on the UI
            # thread before the first frame.
            self.settings_view.refresh_slow_labels()
            self._refresh_status()
            self._start_scheduler()
            # **Detection shells out to PowerShell**, so it happens here for
            # exactly the reason this method exists. Until it answers, the
            # tuning screen shows the envelope's answers for an unknown
            # machine, which are the cautious ones.
            self.indexing_view.tuning.start_detection(settings.data_path)
            self.indexing_view.tuning.set_last_run(self._last_run_record())
            self._refresh_tuning_status()
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
        self.settings_view.theme_changed.connect(
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
        bind("Ctrl+,", self._focus_settings)
        bind("Ctrl+I", self._focus_indexing)
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
        self.indexing_view.schedule_box.set_schedule_status(self.scheduler.status())

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

        # §1a. **The search behaviours take effect on the very next search**,
        # not at the next launch. For a switch somebody has just turned off,
        # the difference is between a working control and one they conclude is
        # broken - and they would be right to.
        if any(key.startswith("SEARCH_") for key in values):
            for key, value in values.items():
                if key.startswith("SEARCH_"):
                    # `Settings` is frozen, so the live object cannot be
                    # updated - the preferences dictionary is built from these
                    # values instead, which is the same answer by a route that
                    # works. See task #238 for the frozen-Settings question.
                    self._settings_overrides[key.lower()] = value
            self._apply_search_preferences()

        # §3a: the shortcut is re-taken on the spot, because a combination
        # somebody has just typed and cannot try until the next launch is a
        # control they will conclude does not work.
        if any(key.startswith("MINI_SEARCH") for key in values):
            for key, value in values.items():
                if key.startswith("MINI_SEARCH"):
                    self._settings_overrides[key.lower()] = value
            self._apply_hotkey()

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
        # **`Settings` is frozen** (see ~line 128 and the module docstring), so
        # `setattr(self._settings, key, value)` always raised - every time,
        # for every key - and the `except` above caught it at DEBUG, where
        # nobody would ever see it. `.env` was written correctly; the live
        # object never changed, so the "next run in this session" this
        # function's own docstring promises never arrived without a restart.
        # `model_copy(update=...)` is this codebase's actual answer for a
        # frozen `Settings` (see `app.cli`'s `cmd_index`, which does the same
        # for `--rerank-model`): it produces a new instance with these fields
        # changed, and one replacement of the whole batch is what a frozen
        # model allows - there is no field-by-field mutation to fall back to.
        self._settings = self._settings.model_copy(update=values)
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

    # -- the tuning screen's evidence ---------------------------------------

    def _last_run_record(self) -> Optional[dict]:
        r"""What the last run measured, for the tuning footer.

        **Read from what the run itself wrote**, so the footer cannot disagree
        with the run log about what happened. `stats` are stored as a `repr`
        of a plain dict of numbers and strings, which `literal_eval` reads
        without executing anything - a `pickle` here would be a file on disk
        that runs code, for a progress figure.

        Stage timings are not recorded yet; the footer says so rather than
        inventing a split. §6a is where they start being measured.
        """
        raw = self._read_state("last_run_stats", "")
        if not raw:
            return None
        try:
            import ast

            stats = ast.literal_eval(raw)
            if not isinstance(stats, dict):
                return None
        except (ValueError, SyntaxError) as exc:
            _log.debug("the last run's stats could not be read: {}", exc)
            return None

        elapsed = float(stats.get("elapsed_s") or 0.0)
        chunks = float(stats.get("chunks") or 0.0)
        return {
            "stages": stats.get("stages") or {},
            "chunks_per_minute": (chunks / elapsed * 60) if elapsed > 0 else 0,
            # **What the run recorded, not what the settings say now.** The
            # settings are a fallback for records written before §5c existed;
            # reading them for a recent run would describe this moment rather
            # than that one, which is the difference between a measurement and
            # an anecdote.
            "resolved": stats.get("resolved") or {
                "workers": self._settings.index_workers,
                "batch": self._settings.embed_batch,
                "device": self._settings.embed_device,
            },
        }

    def _ocr_mode_for_run(self) -> str:
        r"""Which pass this run is, given *what* to read and *when*.

        `INDEX_OCR_MODE` says what; `INDEX_OCR_PASS` says when. They meet here
        because a run is only ever one pass: choosing to do the images after
        the run means *this* run is the text one, and the images are a second
        run. Neither setting can express that alone, which is why the schedule
        is its own control rather than a fourth value crammed into the mode.
        """
        schedule = str(getattr(self._settings, "index_ocr_pass", "with-run")
                       or "with-run")
        if schedule in ("after-run", "manual"):
            return "text"
        return str(getattr(self._settings, "index_ocr_mode", "both"))

    def _offer_images_pass(self, _stats: Any) -> None:
        """After a text-only run, say the images are still to do.

        **Offered, never started.** A second pass over a scanned corpus is
        hours; launching it because a text run finished - possibly while
        somebody has gone home - is the kind of surprise that gets an
        application uninstalled. `manual` says nothing at all, which is what
        the word means.
        """
        if str(getattr(self._settings, "index_ocr_pass", "")) != "after-run":
            return
        self.statusBar().showMessage(
            "Text is indexed. Images and scans are still to read - press Start "
            "again to do those.", 30_000)

    def _apply_search_preferences(self) -> None:
        """Push the search behaviours to the surfaces that read them.

        Called at start-up and again whenever one is changed, so a switch takes
        effect on the very next search rather than at the next launch - which
        for a behaviour somebody has just switched off is the difference
        between a working control and one they believe is broken.
        """
        try:
            from app.search.policy import preferences

            # What was changed in this session wins over what was loaded at
            # start-up, which is the whole reason the overrides exist.
            self.search_view.set_search_preferences(
                preferences(self._settings, self._settings_overrides))
        except Exception as exc:                 # noqa: BLE001 - never fatal
            _log.debug("the search behaviours could not be applied: {}", exc)

    # -- §3a: search from anywhere --------------------------------------------

    def _apply_hotkey(self) -> None:
        r"""Take, or give back, the global shortcut. **Never raises.**

        Called at start-up and whenever the setting changes. The result is
        pushed back into Settings as a sentence, because a shortcut the
        operating system refused is otherwise indistinguishable from one that
        works - and this is the feature the product is demonstrated with.
        """
        try:
            from app.ui.hotkey import HotkeyListener

            overrides = self._settings_overrides
            wanted = overrides.get(
                "mini_search_enabled",
                getattr(self._settings, "mini_search_enabled", True))
            text = str(overrides.get(
                "mini_search_hotkey",
                getattr(self._settings, "mini_search_hotkey", "")) or "")

            if self._hotkey is None:
                self._hotkey = HotkeyListener()
            self._hotkey.stop()
            taken = (self._hotkey.start(text, self._summon_mini)
                     if wanted else False)
            box = getattr(self.settings_view, "search_behaviour", None)
            if box is not None and hasattr(box, "say_hotkey"):
                box.say_hotkey(text, registered=taken or not wanted)
        except Exception as exc:                 # noqa: BLE001 - see docstring
            _log.debug("could not set the global shortcut: {}", exc)

    def _summon_mini(self) -> None:
        """The shortcut was pressed. **Never raises**: this runs from a native
        event filter, where an exception has nowhere sensible to go."""
        try:
            if self._mini is None:
                from app.ui.widgets.mini_search import MiniSearch

                self._mini = MiniSearch(self._engine)
                self._mini.chosen.connect(self._open_result)
                self._mini.expanded.connect(self._search_from_mini)
            self._mini.summon()
            self._offer_foreground_selection()
        except Exception as exc:                 # noqa: BLE001 - see docstring
            _log.warning("could not open the search box: {}", exc)

    def _offer_foreground_selection(self) -> None:
        r"""§4a: read whatever is selected in the foreground application, off
        a worker, and offer it to the box that is already open.

        **On a worker, never inline.** Reading a selection is a synthetic
        Ctrl+C and a short wait for the clipboard to answer -
        `COPY_TIMEOUT_S` in `app.ui.selection` - and `_summon_mini` opens the
        box before this is even asked for, so a person who never selected
        anything never waits on it either. `MiniSearch.offer_prefill` is
        where "arrived too late" is handled, on the box's own side.

        **Its own switch.** Reading a selection out of whatever application
        somebody was looking at is the more intrusive half of the feature, so
        it is checked separately from `mini_search_enabled` - overrides win
        the same way `_apply_hotkey` already reads them, for a setting just
        changed in this session.
        """
        try:
            overrides = self._settings_overrides
            wanted = overrides.get(
                "mini_search_prefill_selection",
                getattr(self._settings, "mini_search_prefill_selection", True))
            if not wanted:
                return
            from app.ui.selection import read_foreground_selection

            mini = self._mini
            worker = CallableWorker(
                read_foreground_selection, component="ui.mini.selection")
            worker.signals.finished.connect(
                lambda text: mini.offer_prefill(text or ""))
            worker.signals.failed.connect(lambda _error: None)
            run(QThreadPool.globalInstance(), worker)
        except Exception as exc:                 # noqa: BLE001 - see docstring
            _log.debug("could not read the foreground selection: {}", exc)

    def _search_from_mini(self, query: str) -> None:
        """"Show me all of it": bring the window up with this query in it."""
        try:
            self.showNormal()
            self.raise_()
            self.activateWindow()
            self._show(self.search_view)
            self.search_view.input.setText(str(query or ""))
            self.search_view.search_now()
        except Exception as exc:                 # noqa: BLE001 - never fatal
            _log.debug("could not expand the mini search: {}", exc)

    def _refresh_tuning_status(self) -> None:
        """§5d's status line, and the rates Auto-tune resolves against.

        Both read the store, so both happen here rather than in the widget -
        the panel is built inside `MainWindow.__init__`, where nothing may
        touch a database.
        """
        try:
            from app.core.compute_profile import cached_profile
            from app.core.measured import for_profile
            from app.index.autotune import status_line

            profile = cached_profile(self._store, self._settings.data_path)
            self.indexing_view.tuning.set_tuned_status(
                status_line(self._store, profile))
            self.indexing_view.tuning.set_measured(
                {"rates": for_profile(self._store, profile)})
        except Exception as exc:                 # noqa: BLE001 - a status line
            _log.debug("the tuning status could not be refreshed: {}", exc)

    def _learn_from_run(self, stats: Any) -> None:
        r"""§5c: what the run just measured, and what it argues for.

        **In Auto the change applies itself and the notice is past tense.** The
        product rule is explicit: a non-technical person must never be handed a
        decision in order to get the benefit. In Manual it is a proposal, and
        the status bar says so.

        Wrapped whole, because none of this may cost somebody the end of an
        index run that otherwise succeeded.
        """
        try:
            from app.core.compute_profile import cached_profile
            from app.index.autotune import learn

            profile = cached_profile(self._store, self._settings.data_path)
            found = learn(self._store, profile, stats,
                          mode=self.indexing_view.tuning.current_mode(),
                          device=self._settings.embed_device)
            self._refresh_tuning_status()
            self.indexing_view.tuning.set_last_run(self._last_run_record())
            if not found:
                return
            if found.applied:
                self._limits_changed({key.lower(): value
                                      for key, value in found.values.items()})
            self.statusBar().showMessage(found.message, 20_000)
        except Exception as exc:                 # noqa: BLE001
            _log.debug("nothing was learned from this run: {}", exc)

    def _benchmark_models(self) -> None:
        """Time the embedding model on this machine, off the UI thread.

        **The one question the specification sheet cannot answer.** Whether 96
        Iris Xe execution units beat this particular processor on a small embed
        model is not knowable from the numbers on the box, and §0 of the
        index-tuning order says so; this is how somebody finds out.
        """
        from app.core.compute_profile import cached_profile
        from app.core.measured import remember
        from app.index.index_bench import run_index_bench

        # **The whole pipeline, not only the model.** This button used to run
        # `embed_bench`, which answers "how fast is the model here" - the
        # smaller half. A machine whose model is quick and whose disk is slow
        # is bounded by the disk, and a screen holding only the model number
        # will confidently recommend a graphics card to somebody who needs a
        # different drive. `bench-index` on the command line does the same
        # work; this is the same function, so the two cannot disagree.
        devices = ("cpu", "gpu") if self._can_use_gpu() else None

        def measure() -> Any:
            found = run_index_bench(self._settings, devices=devices)
            if not found.error:
                profile = cached_profile(self._store, self._settings.data_path)
                remember(self._store, found.as_measured(profile.fingerprint()))
            return found

        self.statusBar().showMessage(
            "Timing this computer on a fixed workload - about a minute…",
            120_000)
        worker = CallableWorker(measure, component="ui.tuning")
        worker.signals.finished.connect(self._benchmarked)
        worker.signals.failed.connect(self._show_error)
        run(QThreadPool.globalInstance(), worker)

    def _can_use_gpu(self) -> bool:
        """Is there a graphics card worth timing against the processor?

        The one question §0 says the specification sheet cannot answer, so it
        is only worth the extra minute when there is something to compare.
        """
        try:
            from app.core.compute_profile import cached_profile
            from app.index.backends import why_unavailable

            return not why_unavailable(
                cached_profile(self._store, self._settings.data_path))
        except Exception:                        # noqa: BLE001
            return False

    def _benchmarked(self, result: Any) -> None:
        """Report a benchmark in the numbers somebody can act on.

        **Reading, writing and the model, not only the model.** A run whose
        model is quick and whose disk is slow is bounded by the disk, and the
        three side by side are what say which.
        """
        if getattr(result, "error", ""):
            self.statusBar().showMessage(
                f"The benchmark could not run: {result.error}", 15_000)
            return

        rates = dict(getattr(result, "embed_per_second", {}) or {})
        parts = [f"reading {result.extract_per_second:,.0f} files a second",
                 f"writing {result.write_per_second:,.0f} chunks a second"]
        parts += [f"meaning {rate:,.0f} a second on the "
                  f"{'graphics card' if device == 'gpu' else 'processor'}"
                  for device, rate in rates.items()]
        # Notes carry the things that make a number untrustworthy - a model
        # that would not load, a graphics card that declined. Saying the
        # number without them is how a figure nobody should act on gets quoted
        # for a year.
        said = "; ".join(parts) + ("  " + " ".join(result.notes)
                                   if result.notes else "")
        self.statusBar().showMessage(f"This computer: {said}", 40_000)
        self._refresh_tuning_status()

    def _focus_files(self) -> None:
        """Ctrl+P, the shortcut every editor uses for "go to file"."""
        files_view = getattr(self, "files_view", None)
        if files_view is None:
            # Order 0r item 2b: Files is built a beat after the window
            # appears - see `_construct_deferred_views`. Pressed inside
            # that gap, which needs unlucky timing; doing nothing is
            # correct here, not a bug to chase.
            return
        self._show(files_view)
        files_view.focus()

    def _focus_indexing(self) -> None:
        """Ctrl+I. Indexing has nowhere to type, so this only switches tabs."""
        indexing_view = getattr(self, "indexing_view", None)
        if indexing_view is None:
            # See `_focus_files` - same gap, same reason.
            return
        self._show(indexing_view)

    def _focus_settings(self) -> None:
        """Ctrl+,. Settings deliberately does not steal focus - see `_tab_changed`."""
        settings_view = getattr(self, "settings_view", None)
        if settings_view is None:
            # See `_focus_files` - same gap, same reason.
            return
        self._show(settings_view)

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
        mail_view = getattr(self, "mail_view", None)
        if mail_view is None:
            # Order 0r item 2b: Mail is built a beat after the window
            # appears - see `_construct_deferred_views`. Pressed inside
            # that gap, which needs unlucky timing; doing nothing is
            # correct here, not a bug to chase.
            return
        self._show(mail_view)
        mail_view.focus()

    def _focus_code(self) -> None:
        """Ctrl+E. The Code tab, and its search box selected."""
        code_view = getattr(self, "code_view", None)
        if code_view is None:
            # See `_focus_mail` - same gap, same reason.
            return
        self._show(code_view)
        code_view.focus()

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

        # **Workspace §1a: the log's colours are pushed, not read.** The pane
        # paints warnings and errors from theme tokens, and this is the one
        # place that knows which theme is on - so the palette arrives here,
        # every time it changes, and a hardcoded hex never has to exist.
        from app.ui.theme import palette_for

        colours = palette_for(preference, detected=detected)
        # Order 0r item 2b: `settings_view` (and its debug pane) is built a
        # beat after the window appears - see `_construct_deferred_views`,
        # which calls this method again once it exists, so the palette
        # still reaches it, just one tick later than everything else.
        settings_view = getattr(self, "settings_view", None)
        targets = [self._log_window]
        if settings_view is not None:
            targets.append(settings_view.debug_pane)
        for target in targets:
            if target is not None:
                target.set_palette(colours)

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

    def _pin_document(self, row: Any, provider: Any = None) -> None:
        r"""Open this document in a window of its own. Workspace §2.

        **A copy, not a move**: the pane the button was pressed in keeps
        showing what it showed. Never raises - a window that will not open
        must not take the results with it.
        """
        try:
            from app.ui.widgets.preview_window import PreviewWindow

            window = PreviewWindow(row, state=self._log_window_state(),
                                   body_provider=provider)
            window.remember.connect(self._remember_log_window)
            window.open_requested.connect(self._open_path)
            window.reveal_requested.connect(
                lambda path: self._open_path(path, reveal=True))
            window.closed.connect(self._unpin)
            self._pinned.append(window)
            window.show()
            window.raise_()
        except Exception as exc:                 # noqa: BLE001 - see docstring
            _log.warning("could not pin {}: {}", getattr(row, "path", ""), exc)

    def _unpin(self, window: Any) -> None:
        """A pinned window closed. Drop it so it can go."""
        try:
            self._pinned.remove(window)
        except ValueError:
            return

    def _pop_out_log(self) -> None:
        r"""Open the log as its own window, or bring back the one that is open.

        Workspace §1c. **One window, not one per click** - a second identical
        log window is never what anybody meant, and closing the wrong one
        afterwards is a small annoyance the feature does not need.

        Never raises: this is a convenience beside a log, and a window that
        will not open must not take the one you are looking at with it.
        """
        try:
            if self._log_window is None:
                from app.ui.widgets.log_window import LogWindow

                window = LogWindow()
                window.file_chosen.connect(self._open_path)
                window.remember.connect(self._remember_log_window)
                window.closed.connect(self._forget_log_window)
                window.restore(self._log_window_state())
                self._log_window = window
                self._apply_theme()          # paints it in the current theme
            self._log_window.show()
            self._log_window.raise_()
            self._log_window.activateWindow()
        except Exception as exc:             # noqa: BLE001 - see docstring
            _log.warning("could not open the log window: {}", exc)

    def _log_window_state(self) -> dict:
        """What the log window remembered last time. **Never raises.**"""
        try:
            return self._store.all_state()
        except Exception:                    # noqa: BLE001 - a preference
            return {}

    def _remember_log_window(self, values: dict) -> None:
        """Save the window's geometry or its stay-on-top flag, off this thread.

        A move or a resize fires this on every pixel, so it goes to a worker
        rather than fsyncing the database inside a drag.
        """
        store = self._store
        run(QThreadPool.globalInstance(),
            CallableWorker(lambda: store.set_states(dict(values or {})),
                           component="ui.log.window"))

    def _forget_log_window(self) -> None:
        """It was closed. Let it go, so the next pop-out builds a fresh one."""
        self._log_window = None

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
        # `getattr(self, "...", None)` rather than a bare attribute: Order 0r
        # item 2b builds Files, Indexing, Settings, Mail and Code all a beat
        # after the window appears (`_construct_deferred_views`), and
        # `_tab_index.get(None)` is simply `None` - never equal to a real tab
        # index - so this stays correct in the gap before any of them exist,
        # with no exception raised.
        indexing_view = getattr(self, "indexing_view", None)
        files_view = getattr(self, "files_view", None)
        if indexing_view is not None and index == self._tab_index.get(indexing_view):
            indexing_view.refresh_totals(self._store, self._settings)
        elif files_view is not None and index == self._tab_index.get(files_view):
            files_view.refresh_summary()
        elif index == self._tab_index.get(self.offline_media_view):
            self.offline_media_view.refresh()
        elif index == self._tab_index.get(getattr(self, "code_view", None)):
            # On the way in rather than on a timer: repositories change when an
            # index run finds one, which is rare and never while somebody is
            # looking at this tab.
            self.code_view.refresh()

        # By index rather than by widget: a view inside a scroll area is not the
        # tab's widget, which is the same trap `_show` exists to avoid.
        for view in (self.search_view, files_view,
                     getattr(self, "mail_view", None),
                     getattr(self, "code_view", None)):
            if view is not None and self._tab_index.get(view) == index:
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
        """The counts in the status bar, **counted on a worker**.

        `stats()` is three `COUNT(*)`, two of them scans of `chunks`: 93ms on a
        two-million-chunk fixture, so roughly 460ms at ten million. This is
        called when an index run finishes and after a reset - both moments when
        the number has just changed and the table is at its largest - and it
        was doing that arithmetic on the UI thread.
        """
        worker = CallableWorker(index_counts, self._store, component="ui.status")
        worker.signals.finished.connect(self._show_status_counts)
        worker.signals.failed.connect(
            lambda error: _log.debug("status bar not updated: {}", error.message))
        run(QThreadPool.globalInstance(), worker)

    def _show_status_counts(self, counts: Any) -> None:
        """UI thread. `counts` is the sentence the presenter built."""
        if counts:
            self.statusBar().showMessage(str(counts))

    # -- actions ------------------------------------------------------------

    def _open_result(self, row: Any, *, reveal: bool = False) -> None:
        """Open the file, or reveal it, without waiting for Explorer.

        **This was the "too slow" report.** `explorer /select,` takes a few
        hundred milliseconds just to start, and the `exists()` check before it
        is a stat that can block for seconds on a network share or a sleeping
        drive - and both ran on the UI thread, so the window sat frozen through
        a launch that is nearly free once it is off the critical path.

        The click now returns immediately and the error, if any, arrives later.

        **A row on a catalogued Offline Media volume needs resolving
        first** (1b) - `row.path` for one of those is never a real
        filesystem path, it is the letter-free key
        `volume_synthetic_path` builds, and opening it directly would
        report "missing" for a file that is sitting right there once the
        drive is plugged in. `_open_volume_result` does the resolution and
        the open in the same worker.
        """
        if getattr(row, "volume_id", None) is not None:
            self._open_volume_result(row, reveal=reveal)
            return
        self._open_path(row.path, reveal=reveal)

    def _open_volume_result(self, row: Any, *, reveal: bool = False) -> None:
        """Resolve a catalogued-volume row's current real path, then open
        it - one worker, never the interface thread for either half."""
        from app.ui.presenter import resolve_open_path

        def _resolve_and_open() -> Any:
            target = resolve_open_path(self._store, row)
            return open_in_explorer(target, select=reveal)

        worker = CallableWorker(_resolve_and_open, component="ui.open")
        worker.signals.finished.connect(
            lambda error: self._show_error(error) if error is not None else None)
        worker.signals.failed.connect(self._show_error)
        run(QThreadPool.globalInstance(), worker)

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
        # `getattr(..., None)`: Order 0r item 2b builds Indexing a beat
        # after the window appears - `_show(None)` does nothing, and
        # `_start_indexing` guards the same gap itself, immediately below.
        self._show(getattr(self, "indexing_view", None))
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
        #
        # Guarded: Order 0r item 2b builds Code a beat after the window
        # appears, and changing this Settings control in that gap would
        # otherwise raise on an attribute that does not exist yet. Nothing
        # is lost - Code reads this from the store on its own next search
        # regardless of whether it is redrawn immediately here.
        code_view = getattr(self, "code_view", None)
        if code_view is not None:
            code_view.refresh()

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

    def _file_types_saved(self, changes: dict) -> None:
        """A file-type mapping changed - bump the generation, then say so.

        Every other write that bumps the generation happens inside a
        `write()` block already, because it just changed rows the search
        cache is keyed on. This one is different: `FileTypesEditor.save()`
        writes a config file on disk, not a table, and a mapping change
        (a format switched on/off, a converter route added, a size cap
        changed) invalidates cached search results exactly the same way a
        document write does - stale results from before the change must
        not linger. `bump_generation()` is the entry point for exactly
        this: a cache-invalidating event with no natural write() to
        piggy-back on.

        **Off the UI thread**, same reasoning and the same `CallableWorker`
        shape as `_run_idle_optimize` just below: `bump_generation()` opens
        a real `write()` transaction, and this method runs on a signal
        straight from the settings page, so "cheap" here is still a stutter
        the person clicking Save would feel. The status message is not
        conditioned on the bump succeeding - it reports that the mapping
        itself saved, which already happened by the time this signal fires;
        a failed bump only means the *next* search, not this save, might
        briefly serve a stale cache, and `bump_generation()` already logs
        its own failures.
        """
        worker = CallableWorker(self._store.bump_generation, component="ui.file_types")
        run(QThreadPool.globalInstance(), worker)
        self.statusBar().showMessage(
            f"File types saved - {len(changes)} differ from the defaults. "
            "They apply to the next index run.", 12_000)

    def _show_external_run(self, payload: dict) -> None:
        self.indexing_view.show_external(
            payload.get("record"), locked=bool(payload.get("locked")))
        self._run_link(payload.get("link"))
        if payload.get("front_requested"):
            self._front_self()

    def _front_self(self) -> None:
        r"""Bring this window forward. A second launch asked for it
        (`run_lock.FRONT_STATE_KEY`) after finding the GUI mutex already
        held by this one - reported live as "the box is hard to get to"
        when that used to do nothing at all. Same three calls `_run_link`
        ends on, pulled out because a plain "somebody double-clicked the
        icon again" carries no query to run first.
        """
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _run_idle_optimize(self) -> None:
        r"""§3c: refresh the query planner's statistics, off the UI thread.

        Fires once an hour for as long as the window is open (see
        `_optimize_timer` in `__init__`). Off the UI thread for the same
        reason `_poll_external_run` is: this touches the store, and "cheap"
        on the UI thread is still a stutter nobody can attribute. No signal
        connected to the result - there is nothing to show for a query-planner
        refresh succeeding, and `optimize_query_planner` already logs a
        warning on the way it can fail.
        """
        worker = CallableWorker(self._store.optimize_query_planner,
                                 component="ui.optimize")
        run(QThreadPool.globalInstance(), worker)

    def _run_link(self, request: Any) -> None:
        r"""A `leasha://` link arrived while this window was open. §7a.

        **The second copy did not become a window**, because two of those
        cannot share one index - it left the query behind and exited. This is
        the window picking it up: go to Search, put the words in the box, run
        it, and come to the front. Somebody clicked a link; the answer is a
        page of results, not a second application.

        Never raises. A malformed request costs the link and not the window.
        """
        if request is None:
            return
        try:
            query = str(getattr(request, "query", "") or "").strip()
            if not query:
                return
            scope = str(getattr(request, "scope", "") or "")
            # **`_show`, never `tabs.setCurrentWidget`.** A view inside a
            # scroll area is not the tab's own widget, so that call does
            # nothing at all - no error, no exception, the tab simply does not
            # change. `test_the_window_maps_views_to_tab_indexes` caught this
            # one the day it was written, which is the guard doing its job.
            self._show(self.search_view)
            if scope:
                self.search_view.set_scope(scope)
            self.search_view.input.setText(query)
            self.search_view.search_now()
            # In front of whatever they clicked the link in. `raise_` alone is
            # advisory on Windows; `activateWindow` is the half that actually
            # takes focus, and a search that ran behind another application is
            # a search nobody saw.
            self._front_self()
        except Exception as exc:                 # noqa: BLE001 - see docstring
            _log.warning("could not run a leasha:// link: {}", exc)

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
        r"""Resolve the tuning numbers off-thread, then hand off to `IndexingView`.

        **`resolve_for_run` used to run right here, inline.** On a warm compute-
        profile cache that is imperceptible - but `_profile` falls through to
        `compute_profile.detect()` on a cold or invalidated cache (a first run on
        this machine, a driver or hardware change, a cache write that failed
        last time), and `detect()` shells out to PowerShell for the disk kind and
        the display adapters with 10s and 15s timeouts. Both calls sat on the UI
        thread, at the exact moment somebody clicked Start - non-negotiable #5,
        broken by the one button people click to begin.
        """
        # Order 0r item 2b: Indexing and Settings are built a beat after
        # the window appears (`_construct_deferred_views`). F5, a
        # drag-and-drop, the scheduler firing, or a search result's
        # re-index button can all reach this method, and none of them go
        # through a disabled widget the way the Start button does - so this
        # does nothing in that gap, rather than raising on an attribute
        # that does not exist yet.
        if getattr(self, "indexing_view", None) is None or \
                getattr(self, "settings_view", None) is None:
            return

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

        # A second click, or `F5`, or the scheduler firing while the first
        # resolve is still out on its worker - none of them go through the
        # disabled button (the scheduler and F5 do not touch it at all), and
        # `is_running()` above stays false until the Pipeline this resolve
        # will build actually exists. Without this flag, a burst of clicks
        # during a slow cold-cache detection would queue several resolves and
        # could hand `IndexingView.start` more than one Pipeline.
        if self._resolving_index:
            return
        self._resolving_index = True
        self.indexing_view.start_button.setEnabled(False)
        self.statusBar().showMessage("Checking your hardware…", 30_000)

        # **The same resolution the tuning screen shows.** One function, so a
        # run started from the window and one started from the command line
        # cannot disagree about what `Auto (4)` means. Dispatched to a worker -
        # see the docstring above - with the result handed back to
        # `_index_resolved` by signal, on the GUI thread, exactly as if this
        # had returned in place.
        from app.index.resolve import resolve_for_run

        worker = CallableWorker(resolve_for_run, self._settings, self._store,
                                component="ui.index.resolve")
        worker.signals.finished.connect(
            lambda tuned: self._index_resolved(tuned, chosen, roots, recheck_archives))
        worker.signals.failed.connect(self._index_resolve_failed)
        run(QThreadPool.globalInstance(), worker)

    def _index_resolved(self, tuned: Any, chosen: list[str],
                        roots: Optional[list[str]], recheck_archives: bool) -> None:
        """Build the Pipeline and hand it to `IndexingView`. Back on the GUI thread.

        Everything `_start_indexing` did after calling `resolve_for_run`, moved
        here unchanged - only *when* it runs changed, not what it does.
        """
        from app.index.clip_embedder import ClipImageEmbedder
        from app.index.embedder import Embedder
        from app.index.pipeline import Pipeline, PipelineConfig
        from app.index.walker import WalkConfig

        self._resolving_index = False
        self.statusBar().clearMessage()
        # A second Start click cannot get in *ahead* of this while the resolve
        # was in flight (the flag above stops it), but a run started from
        # elsewhere - the CLI, taking the run lock this window will also wait
        # on - could have begun in the meantime. IndexWorker still surfaces
        # that as a failure if it happens, but there is no reason to build a
        # second Pipeline and throw it away.
        if self.indexing_view.is_running():
            self.indexing_view.start_button.setEnabled(True)
            return

        limits = replace(limits_from_settings(self._settings),
                         workers=tuned.workers)

        # Work order 0h §1c's flagged gap, closed: the only real Pipeline(
        # construction site that had never been given image_embedder=/
        # image_vectors= (app.cli's cmd_index was fixed earlier this
        # session; grep -n "Pipeline(" app/ui/shell.py confirmed this is
        # the window's only one). H4: self._image_vectors is None on a
        # window built without one (an older caller, or a test stub), and
        # ClipImageEmbedder is lazy - nothing loads until the first image
        # is actually embedded, so building it unconditionally here costs
        # nothing on a run that never reaches an image file.
        image_embedder = (
            ClipImageEmbedder.from_settings(self._settings)
            if self._image_vectors is not None else None
        )
        pipeline = Pipeline(
            self._store, self._vectors,
            Embedder.from_settings(self._settings, threads=tuned.onnx_threads),
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
                limits=limits,
                min_free_gb=self._settings.min_free_gb,
                required_free_gb=int(getattr(self._settings, "required_free_gb", 0)),
                ocr_mode=self._ocr_mode_for_run(),
                embed_batch=tuned.embed_batch,
                dedup_chunks=bool(getattr(self._settings, "embed_dedup", True)),
                two_phase=bool(getattr(self._settings, "index_two_phase", True)),
                bulk_fts=str(getattr(self._settings, "index_bulk_fts", "auto")),
                prune_missing=roots is None,     # a folder-scoped run must not prune the rest
                # A folder marked as an archive is walked once and then checked
                # with one `stat` - the largest single saving available on a
                # settled corpus. `recheck_archives` is the "Rescan archived
                # folders now" button, which walks them all in full this once.
                recheck_archives=recheck_archives,
                recheck_days=int(getattr(self._settings, "archive_recheck_days", 30)),
            ),
            image_embedder=image_embedder, image_vectors=self._image_vectors,
        )
        # **The window's run is a writer like any other**, so it names itself
        # on the published record and holds the same lock the CLI takes. The
        # lock itself is acquired by `IndexWorker`, on the worker thread, for
        # exactly as long as the run - taking it here would hold it across the
        # whole life of the window again, which is the bug being fixed.
        pipeline.run_owner = GUI
        self.indexing_view.start(pipeline, total_estimate=self._scan_total(chosen))

    def _index_resolve_failed(self, error: Any) -> None:
        """`resolve_for_run` does not raise by contract - see its own docstring -
        so this is defence in depth, not the expected path. Restores the button
        and surfaces the error exactly as a synchronous failure would have."""
        self._resolving_index = False
        self.statusBar().clearMessage()
        self.indexing_view.start_button.setEnabled(True)
        self._show_error(error)

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

    # -- Offline Media: order 202626270513 -----------------------------------
    #
    # 2a has no progress bar - a status line on the tab itself and a plain
    # `statusBar` sentence when it finishes, the same weight the order gives
    # the whole feature. Scan and Rescan both run a real `Pipeline`, so both
    # take the window's own run lock (`GUI`) exactly as `_start_indexing`
    # does - a Scan started while an ordinary index run is already using the
    # lock waits for it, on the worker thread, never on this one.

    def _offline_media_scan(self, root: str, name: str, description: str) -> None:
        """2b: the first Scan of a chosen folder - catalogues it, then runs a
        `Pipeline` scoped to it. `scan_new_source` does both, off this worker.
        """
        from app.index.offline_media import scan_new_source

        self.offline_media_view.set_busy(f"Scanning {root}\u2026")
        worker = CallableWorker(
            scan_new_source, self._settings, self._store, Path(root),
            name=name, description=(description or None), run_lock_owner=GUI,
            component="ui.offline_media",
        )
        worker.signals.finished.connect(self._offline_media_run_done)
        worker.signals.failed.connect(self._offline_media_run_failed)
        run(QThreadPool.globalInstance(), worker)

    def _offline_media_rescan(self, volume_id: int) -> None:
        """2a's Rescan: 1e's move-repair pass, then a `Pipeline` for what
        actually changed."""
        from app.index.offline_media import rescan_source

        self.offline_media_view.set_busy("Rescanning\u2026")
        worker = CallableWorker(
            rescan_source, self._settings, self._store, volume_id,
            run_lock_owner=GUI, component="ui.offline_media",
        )
        worker.signals.finished.connect(self._offline_media_run_done)
        worker.signals.failed.connect(self._offline_media_run_failed)
        run(QThreadPool.globalInstance(), worker)

    def _offline_media_delete(self, volume_id: int) -> None:
        """2c: the product's one deliberate deletion - the full cascade,
        never the drive itself."""
        from app.index.offline_media import delete_volume

        self.offline_media_view.set_busy("Removing from the index\u2026")
        worker = CallableWorker(
            delete_volume, self._store, self._vectors, volume_id,
            component="ui.offline_media",
        )
        worker.signals.finished.connect(self._offline_media_run_done)
        worker.signals.failed.connect(self._offline_media_run_failed)
        run(QThreadPool.globalInstance(), worker)

    def _offline_media_run_done(self, result: Any) -> None:
        self.offline_media_view.set_busy("")
        self.offline_media_view.refresh()
        # Order 0r item 2b: Files is built a beat after the window appears.
        # Offline Media itself is not deferred, so its Scan/Rescan/Delete
        # buttons are clickable from first paint - a worker finishing that
        # fast, in the same gap, must not raise on an attribute that does
        # not exist yet.
        files_view = getattr(self, "files_view", None)
        if files_view is not None:
            files_view.refresh_summary()
        self.statusBar().showMessage(offline_media_run_summary(result), 20_000)

    def _offline_media_run_failed(self, error: Any) -> None:
        self.offline_media_view.set_busy("")
        self._show_error(error)

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
            # `getattr(..., None)`: see `_reindex_for` - the same gap,
            # guarded the same way.
            self._show(getattr(self, "indexing_view", None))
            self._start_indexing(roots=sorted(set(folders)))
        event.acceptProposedAction()

    #: How long closing waits for background threads to notice and stop. Long
    #: enough for a batch to finish and commit; short enough that nobody reaches
    #: for Task Manager. A worker that ignores it is left to Qt, which is the
    #: same outcome as before - just without the wait.
    SHUTDOWN_GRACE_MS = 4_000

    def changeEvent(self, event: Any) -> None:          # noqa: N802
        """Hide to the tray when minimised; force a repaint when un-minimised.

        **The restore half exists because of a real, reproducible bug**: on
        Windows, going native-minimise (the plain taskbar button - this needs
        no tray setting at all) then restoring left the frame and title bar
        painting correctly while every child widget's content stayed whatever
        it last was - blank on first repro, and *worse* (solid black) after a
        further maximise/restore, because nothing here ever asked Qt to
        actually repaint the tree. `update()` schedules a paint and normally
        that is enough, but the DWM-composited backing store for a window that
        was just minimised is not guaranteed still valid, so a scheduled
        update against stale geometry can be a no-op. Walking every child and
        asking it to redraw is the blunt, reliable fix; it costs one pass over
        the widget tree, once, only on a real restore - not a per-frame cost.
        """
        from PyQt6.QtCore import QEvent

        super().changeEvent(event)
        if event.type() != QEvent.Type.WindowStateChange:
            return
        if self.isMinimized():
            if self.tray.minimise_to_tray and self.tray.installed:
                # Deferred: hiding inside the state-change handler leaves Qt
                # half-way through a transition it has not finished describing.
                QTimer.singleShot(0, self._hide_to_tray)
            return
        # Not minimised any more: either just restored, or some other state
        # change (e.g. maximise) that changeEvent also reports. Both are cheap
        # to repaint and neither should ever be left stale.
        QTimer.singleShot(0, self._repaint_after_state_change)

    def _hide_to_tray(self) -> None:
        self.hide()
        self.tray.notify_hidden()

    def _repaint_after_state_change(self) -> None:
        """Force every child to actually redraw. See `changeEvent`.

        **Measured, not assumed.** A diagnostic build of this method logged
        `isVisible()` at the moment it ran, right after a real restore
        (taskbar click, and separately confirmed via `ShowWindow(SW_RESTORE)`)
        - it printed `False`. Qt's own internal visibility bookkeeping had not
        caught up with the native window, which the OS already reports as
        shown, at the point this deferred callback runs. `update()` (and
        `repaint()`) are no-ops on a widget Qt believes is not visible, so the
        very code meant to fix the blank window was being silently skipped by
        the thing it was calling. `setVisible(True)` forces Qt to reconcile
        its bookkeeping with reality before anything is asked to redraw.
        """
        # Only the top level's own bookkeeping needs correcting - forcing
        # every descendant to setVisible(True) would wrongly reveal anything
        # legitimately hidden (an inactive tab's page, a collapsed panel, a
        # closed popup). Fixing the ancestor is enough for update() to reach
        # everything that is actually supposed to be shown.
        self.setVisible(True)
        self.update()
        for child in self.findChildren(QWidget):
            child.update()

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

        **Perceived-instant close (§3b):** Hide the window immediately so the
        user sees the app gone from the screen at once. Then run the staged
        teardown invisibly, holding the lock until stores are closed.
        """
        # Close-to-tray is a *hide*, so nothing is torn down and the index lock
        # stays held deliberately. Quit from the tray menu clears the flag first,
        # so it falls through to the real shutdown below.
        if self.tray.close_to_tray and self.tray.installed:
            event.ignore()
            self._hide_to_tray()
            return

        # §4a: save window geometry and state for the next launch. This runs
        # before anything else shuts down, while the window is still there to
        # read from. A close-to-tray hides rather than closes, so this only
        # runs on true shutdown.
        #
        # `set_states` stores everything as TEXT (it's shared with every other
        # string setting), so the raw bytes from `save_window_state` are
        # base64-encoded first - storing them any other way round-trips
        # through `str(bytes_value)`, which produces a Python repr string, not
        # the bytes themselves, and silently corrupts the blob. See the
        # matching decode in `__init__`.
        import base64
        geometry_b64 = base64.b64encode(save_window_state(self)).decode("ascii")
        self._store.set_states({"ui:window_geometry": geometry_b64})

        # §3b: Hide the window first (perceived instant close). User sees the
        # app gone from the screen immediately, then the staged teardown runs
        # invisibly while holding the lock.
        self.hide()

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
        # `getattr(..., None)` for Files/Mail/Code: Order 0r item 2b builds
        # all three (plus Indexing and Settings, guarded separately below) a
        # beat after the window appears, and a close arriving before that
        # callback has run (an automated close sent immediately after
        # `show()`, with no event-loop turn in between) must not raise here -
        # skipping a `shutdown()` that has nothing to shut down yet is
        # correct, not a gap, since none of them has started any timer or
        # worker by then.
        for view in (self.search_view, getattr(self, "files_view", None),
                     getattr(self, "mail_view", None),
                     getattr(self, "code_view", None)):
            if view is None:
                continue
            stage(type(view).__name__, view.shutdown)
        # A ceiling changed in the last third of a second is still sitting in a
        # timer. Closing without this loses it - which would be a worse bug than
        # the sluggishness the debounce was added to fix.
        # Same gap, same guard: `indexing_view` is one of the views Order 0r
        # item 2b now defers.
        indexing_view = getattr(self, "indexing_view", None)
        if indexing_view is not None:
            stage("schedule", indexing_view.schedule_box.flush_pending)
            stage("tuning", indexing_view.tuning.flush_pending)
            stage("indexing", indexing_view.stop)
        # **Pre-existing gap, found live by this session's own rapid-close
        # test for item 2b, not introduced by it.** `self.scheduler` is only
        # ever assigned inside `_start_scheduler`, which only ever runs from
        # `_start_background_work` - already deferred via `QTimer.
        # singleShot(0, ...)` in `__init__` long before this item existed.
        # An immediate close (no event-loop turn at all) raises
        # `AttributeError` building this line's own argument, *before*
        # `stage()`'s try/except ever runs - the same "evaluated eagerly,
        # outside the guard" shape as the `mail_view`/`code_view` gaps this
        # item's own changes guard elsewhere in this method. Fixed here,
        # trivially and in the same style, rather than left to make this
        # session's new closeEvent test permanently red for a reason outside
        # item 2b's own scope.
        scheduler = getattr(self, "scheduler", None)
        if scheduler is not None:
            stage("scheduler", scheduler.stop)
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
