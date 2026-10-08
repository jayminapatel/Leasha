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

from PySide6.QtCore import Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QMainWindow,
    QMessageBox,
    QWidget,
)

from app.core.branding import window_title
from app.core.logging import logger
from app.llm.engines import text_model
from app.search.translate import TRANSLATE_TIMEOUT_S, QueryTranslator
from app.ui.later import later
from app.ui.code_view import CodeView
from app.ui.controllers.chat_controller import ChatController
from app.ui.controllers.index_controller import IndexController
from app.ui.controllers.settings_controller import SettingsController
from app.ui.controllers.timeline_controller import TimelineController
from app.ui.files_view import FilesView
from app.ui.indexing_view import IndexingView
from app.ui.mail_view import MailView
from app.ui.photos_view import PhotosView
from app.ui.offline_media_view import OfflineMediaView
from app.ui.reports_view import ReportsView
from app.ui.search_view import SearchView
from app.ui.settings_view import SettingsView
from app.ui.debug_recorder import recorder_for
from app.ui.theme import detect_scheme, stylesheet
from app.ui.tray import TrayPresence
from app.ui.state_writes import pool as state_write_pool, save_state, save_states
from app.ui.view_options import load_prefs, retint_toggles, save_prefs_later
from app.ui.window_state import bring_forward, restore_window_state, save_window_state
from app.ui.widgets.no_scroll import protect_all
from app.ui.widgets.number_field import fit_all as fit_number_fields
from app.ui.widgets.rail import Rail
from app.ui.widgets.restart_note import mark_restart_needed
from app.ui.widgets.search_bar import retint_toolbar
from app.ui.widgets.toast import DEFAULT_TIMEOUT_MS, Toast
from app.ui.widgets.scroll import wrap_if_needed
from app.ui.rail_state import (
    FAILED, FINISHED, IDLE, RUNNING, pill_fraction, pill_text,
)
# **Worker bodies live in the presenter**, not here: `test_ui_never_blocks`
# reads this file and refuses any store call it cannot prove is inside a
# worker, and it cannot prove that of a module-level function defined here.
from app.ui.presenter import index_counts
from app.ui.presenter.opening import Place
from app.ui.workers import CallableWorker, open_row_async, run

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


def _on_battery() -> bool:
    """A confirmed "running unplugged" answer, and only that. `None` - no
    battery, or a platform where the question does not apply - is False, so
    an unreadable state never blocks the idle bench (rule 2)."""
    try:
        import psutil

        state = psutil.sensors_battery()
    except Exception:                            # noqa: BLE001 - advisory
        return False
    return state is not None and not state.power_plugged


#: The keys `_build_shortcuts` binds. A menu action showing one of these
#: must not bind it a second time (§7a).
_BOUND_ELSEWHERE = frozenset({"Ctrl+K", "Ctrl+F", "Ctrl+,", "Ctrl+I", "Ctrl+P",
                              "Ctrl+Shift+P", "Ctrl+M", "Ctrl+E", "Esc", "F5"})



class MainWindow(QMainWindow):
    """Search, indexing and settings in one window."""

    #: Work order 0r item 1c, second clause. The CLIP text-tower embedder's
    #: download-progress reporting reaches `SearchEngine.status_callback`
    #: from a plain `threading.Thread` (`_DownloadProgressWatcher`) or from
    #: `SearchEngine`'s own retrieval-pool worker thread - never the GUI
    #: thread. A `Signal` is what this codebase already uses to marshal
    #: exactly that safely (`app/ui/workers.py`'s `WorkerSignals`, the same
    #: mechanism `IndexWorker`'s `progress` signal relies on): emitting from
    #: any thread onto a receiver that lives on the GUI thread is queued
    #: automatically, where a direct `self.notify(...)` call
    #: from that background thread would be an unguarded cross-thread Qt call.
    _clip_download_progress = Signal(str)

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
        #: `(roots, recheck_archives)` asked for before the Indexing page was
        #: built - see `_start_indexing`. Empty for the whole life of the window
        #: in every case but the first beat after launch.
        self._queued_index_requests: list = []
        self._store = store
        # 2026-10-07: a picture that arrived in mail has no file to decode; the
        # thumbnail decoder reads its bytes through the store's message row.
        from app.ui import thumbnail_loader

        thumbnail_loader.use_store(store)
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
        #: The handlers moved out of this class (work order 202626082352 §7).
        #: **Built before anything below runs**, because `__init__` itself calls
        #: `_load_roots`, `_read_state` consumers and the `_apply_*` methods, and
        #: each of those is a same-named method here that forwards to one of
        #: these. Held as attributes so nothing collects them - a signal
        #: connected to a controller's bound method outlives no reference.
        self.settings_ctl = SettingsController(self)
        self.index_ctl = IndexController(self)
        # Work order 0z F1: "Index files as soon as they are saved". Builds
        # nothing until `_start_background_work` - see `app/ui/folder_watch.py`.
        from app.ui.folder_watch import FolderWatchControl
        self.folder_watch = FolderWatchControl(self)

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
            lambda message: self.notify(message, 8_000))
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
        #
        # **500, not 480**: at 125% scaling the rail needs 454px even with icons
        # alone (`Rail._measure`), and below that every button in it was squeezed
        # short and overlapped by the indexing pill. Removing the floor to let the
        # rail decide is not an option - the pages' own minimums then hold the
        # window at 645, taller than a 1366x768 screen at 125%.
        self.setMinimumSize(720, 500)
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
        #: The index's document count as last painted by the Indexing page's
        #: totals worker; the rail pill shows it when nothing is running
        #: (§2d). `None` until somebody has counted.
        self._last_document_count: Optional[int] = None

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
        # `CHAT_ENGINE` (2026-09-29): the model inside Leasha by default, or
        # Ollama. `text_model` returns an object with Ollama's methods either way.
        self._ollama = text_model(settings, model)
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
        self._lend_open_context()   # 2026-10-04: the one open route, for every page
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
            lambda prefs: save_prefs_later(self._store, RESULTS_PREFS_KEY, prefs))

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

        # §3c: `PRAGMA optimize` refreshes the query planner's statistics.
        # It used to run on every close, which delayed shutdown while
        # holding the single-instance lock - a relaunch would wait on it
        # unnecessarily. Moved here: a coarse, hourly timer while the app is
        # open, which is what SQLite's own guidance recommends anyway
        # (periodic, not per-close). Built here, started in
        # `_start_background_work`, for the same construction-race reason
        # as `_watch_timer` above.
        self._optimize_timer = QTimer(self)
        self._optimize_timer.setInterval(3_600_000)
        self._optimize_timer.timeout.connect(self._run_idle_optimize)
        # Order 0b §5e, wired by WORKORDER-space-report-and-idle-tune-ui-wiring
        # §2 (2026-09-16): the same hourly idle tick decides whether the
        # machine should be timed once, quietly. See `_maybe_run_idle_bench`.
        self._optimize_timer.timeout.connect(self._maybe_run_idle_bench)
        #: True while an idle bench is out on a worker - one at a time.
        self._idle_bench_running = False

        #: The popped-out log, or None. Workspace §1c: a **copy**, not a move -
        #: the pane in Settings never leaves, so closing this returns nothing
        #: to re-wire. Declared here because `_apply_theme` pushes the palette
        #: to whichever of the two exist, and it runs before anybody opens one.
        self._log_window: Any = None
        #: Order 0j section 2: the Photo Tagger window, built on first open.
        self._photo_tagger: Any = None
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
        # §1a. **What this surface may do on the person's behalf**, resolved
        # once and pushed to the view - `Settings` belongs to the window, and a
        # view that reaches for one has to be given one in every test.
        self._apply_search_preferences()
        # The toolbar box is the one every search reads, so it reports here too.
        toolbar_rerank = getattr(self.search_view, "rerank_toggle", None)
        if toolbar_rerank is not None:
            toolbar_rerank.toggled.connect(self._rerank_toggled)
        self._apply_pst_backend(self._store.get_state("ui:pst_backend", "auto") or "auto")

        self.files_view = FilesView(store)
        self.files_view.error.connect(self._show_error)
        self.files_view.search_inside_requested.connect(self._search_inside)
        self.files_view.index_file_requested.connect(self._index_file_now)

        # Order 202626270513 Â§2. Read-only refresh is the view's own - see
        # `OfflineMediaView.refresh` - but Scan/Rescan/Delete run a real
        # `Pipeline` (Scan/Rescan) or a full delete cascade, so they are
        # signals the window turns into a `CallableWorker`, the same split
        # `IndexingView` draws for `_start_indexing`.
        self.offline_media_view = OfflineMediaView(store)
        self.offline_media_view.error.connect(self._show_error)
        self.offline_media_view.scan_requested.connect(self._offline_media_scan)
        self.offline_media_view.rescan_requested.connect(self._offline_media_rescan)
        self.offline_media_view.delete_requested.connect(self._offline_media_delete)

        # Order 202626270602 (0n) §1. Read-only, like every report -
        # `ReportsView.refresh` is the only thing it ever asks the store
        # for, and `_export_to` writes a PDF the person chose the location
        # for, never a user's own file.
        self.reports_view = ReportsView(store)
        self.reports_view.error.connect(self._show_error)
        # Order 0n 4b: the doors into the Life Timeline - see the controller.
        self.timeline_ctl = TimelineController(self)

        # **Order 0r item 2b.** Mail and Code are not what first paint shows
        # (Search is), and building both here was real, measured constructor
        # cost - two more `ResultTable`s, two more preview panes, two more
        # sets of signal wiring - for two tabs nobody sees until they click
        # them. `_construct_secondary_views`, scheduled below with the same
        # `QTimer.singleShot(0, ...)` idiom `_start_background_work` already
        # uses, builds them a beat later instead: on the next turn of the
        # event loop, after `show()` has already painted. Every place in this
        # file that could reach `self.mail_view` / `self.code_view` before
        # that callback fires - `_focus_mail`, `_focus_code`, `_tab_changed`,
        # `_save_code_types`, `closeEvent` - is guarded to do nothing rather
        # than raise, for exactly that gap.
        #
        # This loop only pins what already exists; Mail's and Code's preview
        # panes are pinned inside `_construct_secondary_views` itself,
        # alongside the views, not left here to fail on an attribute that
        # does not exist yet.
        for pane in (self.search_view.preview, self.files_view.preview):
            pane.pop_out_requested.connect(self._pin_document)

        # **UI Redesign (202626160950 §2): the rail, not a tab strip.** `Rail`
        # exposes the `QTabWidget` surface this file already used - addTab,
        # insertTab, currentChanged, currentIndex, indexOf, setCurrentIndex,
        # tabText - so every call site below changed one attribute name.
        # Indexing is not a rail entry; it is the pill at the rail's foot
        # (§2d), fed from `IndexingView.progressed` further down.
        self.rail = Rail()
        #: view -> the widget actually sitting in its tab (itself, or a
        #: `QScrollArea` wrapping it). Kept so Mail and Code can be inserted
        #: at the right position once they exist without losing track of
        #: where Indexing and Settings landed - `indexOf` on the
        #: exact wrapped widget always answers correctly even after an
        #: insertion has shifted everything after it. See
        #: `_construct_secondary_views`.
        self._tab_wrapped: dict[QWidget, QWidget] = {}
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
        #
        # **Mail and Code are not in this loop.** Order 0r item 2b: they are
        # built a beat later by `_construct_secondary_views` and inserted at
        # the positions they would have had here - right after Files - once
        # they exist, so the tab order nobody has to relearn never changes.
        # (view, title, wrap in a scroll area?, rail icon, placement)
        #
        # **Titles are the strings they always were** (§2b). The icon is
        # chosen here, never inferred from the label; "foot" puts Settings
        # at the rail's bottom and "pill" makes Indexing the pill's page.
        for view, title, scroll, icon_name, placement in (
            (self.search_view, "Search", False, "search", ""),
            (self.files_view, "Files", False, "folder", ""),
            (self.offline_media_view, "Offline", False, "hard-drive", ""),
            (self.reports_view, "Reports", False, "chart-column", ""),
        ):
            wrapped = wrap_if_needed(view, scroll=scroll)
            self._tab_wrapped[view] = wrapped
            self._tab_index[view] = self.rail.addTab(
                wrapped, title, icon=icon_name,
                foot=placement == "foot", pill=placement == "pill")
        # Refresh a panel when it comes forward rather than on a timer: an
        # index run between visits changes what it should show, and polling a
        # table nobody is looking at is work for nothing. Safe to connect
        # before Mail/Code exist: `_tab_changed` only fires on an actual
        # switch, and there is nothing to switch to yet for either of them -
        # it also guards both references regardless, for the same reason
        # `_focus_mail`/`_focus_code` do.
        self.rail.currentChanged.connect(self._tab_changed)
        # §2f: the last-open page is remembered under `ui:page`, the same
        # keyed-state pattern as `ui:theme`; read back post-construction in
        # `_start_background_work`, never here (M13).
        self.rail.currentChanged.connect(self._remember_page)
        self.rail.show_pill(pill_text(IDLE), None)
        self.setCentralWidget(self.rail)
        # 2026-10-05: on a Mac, Qt does not open a list's line on Enter (it is
        # Cmd+O there). `enter_key` makes it; on Windows this does nothing.
        from app.ui import enter_key

        enter_key.install()

        # **Every scroll-sensitive control in the window, in one call.**
        # Qt lets the wheel change a combo box or spin box that does not have
        # focus, so scrolling a settings page silently alters the memory
        # ceiling, the worker count and the schedule on the way past. Doing
        # this per page would mean the one somebody forgets is the one that
        # matters; doing it here means a new page gets it for free.
        guarded = protect_all(self)
        _log.debug("wheel-guarded {} controls", guarded)
        # Every number field loses its arrows and gains a back-to-default
        # button (owner, 2026-09-29), here for the same reason as the guard.
        fit_number_fields(self)

        # **Opt-in, and off until asked for.** An application that vanishes
        # from the taskbar when you did not ask it to is alarming: you close a
        # window, it disappears, and there is no obvious way back.
        # Restore the two switches that persist as window state. Set before the
        # signals are live would be simpler, but these are connected in the
        # block above - so the stored value is written back through the same
        # handler, which is harmless and keeps one path rather than two.
        # **Both controls, from one stored value.** The Settings checkbox was
        # initialised here and the toolbar one was hard-coded True and never
        # saved, so the two disagreed from the first launch after anybody
        # changed it - and the toolbar is the one every search actually reads.
        stored_rerank = self._read_state("ui:rerank_enabled", "")
        # 2026-10-04, code review: never touched, the box follows
        # `RERANK_ENABLED` as the engine does (`run.rerank_choice`), no new read.
        from app.search.run import rerank_choice

        # The toolbar's box is the Search page's, so it stays here; the
        # Settings copy of it is set in `_construct_deferred_pages`.
        self._set_toolbar_rerank(rerank_choice(stored_rerank, self._settings))

        self.tray = TrayPresence(self)
        self.tray.minimise_to_tray = self._read_state("ui:tray_minimise", "") == "on"
        self.tray.close_to_tray = self._read_state("ui:tray_close", "") == "on"
        self._motion = self._read_state("ui:motion", "") == "on"
        self._apply_motion()
        if self.tray.minimise_to_tray or self.tray.close_to_tray:
            if not self.tray.install():
                # Never silently: a preference that does nothing is worse than
                # one that is not offered.
                self.tray.minimise_to_tray = self.tray.close_to_tray = False
                _log.warning("no system tray available; minimising normally")

        # **UI Redesign §6 ([FINALISE 1]): no status bar.** Every message
        # that went to the status bar's `showMessage` now goes through `notify`
        # to a toast over the content, verbatim, and is announced (§6c).
        self.toast = Toast(self.rail)
        self._build_shortcuts()
        self._build_menu_bar()
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
        #
        # Order 0r item 2b: Mail and Code are built here too, on the same
        # next-turn-of-the-loop timing, by `_construct_secondary_views`.
        # Scheduled first so it has run by the time `_start_background_work`
        # does, though nothing in either method actually depends on that
        # order today.
        # `later`, not `QTimer.singleShot`: these lambdas have no owner, so a window
        # closed before the next turn of the loop would still have its pages built
        # into it. See `app/ui/later.py` for the measured difference.
        #
        # **Kept, so `app.main` can hold them for the splash hand-off** (0r 2b,
        # 2026-09-29). Measured on the owner's display: these two ran inside
        # the pump that brings the window up, so the splash sat over a finished
        # window for three to four seconds while Settings was built, and the
        # fade would have stalled half-way had it run beside them. Nothing
        # holds them unless asked, so every other caller is unchanged.
        self._deferred_start = [
            later(self, 0, lambda: self._construct_secondary_views(store)),
            later(self, 0, lambda: self._construct_deferred_pages(
                store, settings, model, translator, interpret_on)),
        ]
        self._deferred_held: list[Any] = []

    def hold_deferred_start(self) -> None:
        """Keep the deferred pages from building until `release_deferred_start`.

        For `app.main` only, called straight after construction and before any
        event is pumped: the splash's hold and fade pump events, and the pages
        would otherwise be built inside them. Idempotent; a timer that has
        already fired is left alone.
        """
        for timer in self._deferred_start:
            if timer.isActive():
                timer.stop()
                self._deferred_held.append(timer)

    def release_deferred_start(self) -> None:
        """Let the held pages build on the next turn of the loop. Idempotent."""
        held, self._deferred_held = self._deferred_held, []
        for timer in held:
            timer.start(0)

    def _construct_secondary_views(self, store: Any) -> None:
        """Build Mail and Code, and insert them where they belong.

        Order 0r item 2b's audit of `MainWindow.__init__` found the
        constructor doing real, synchronous work for tabs nobody sees the
        instant the window appears - Search is the only one shown at first
        paint. Mail and Code move here: built on the next turn of the event
        loop instead of inside the constructor, via the same
        `QTimer.singleShot(0, ...)` idiom `__init__` already uses for
        `_start_background_work`, a few lines above.

        **Everything that could reach `self.mail_view` / `self.code_view`
        before this callback fires is guarded**, for the gap between
        `show()` returning and this method actually running:
        `_focus_mail`, `_focus_code` (Ctrl+M / Ctrl+E), `_tab_changed`
        (switching tabs), `_save_code_types` and `closeEvent`. A rapid
        keypress or an immediate close in that gap does nothing, rather than
        raising `AttributeError` on an attribute that does not exist yet.
        """
        # Mail gets its own tab for the reason `mail_view.py` opens with: a
        # mailbox is scanned in columns and read newest first, and relevance
        # ranking answers a question nobody asked of it.
        self.mail_view = MailView(store)
        self.mail_view.error.connect(self._show_error)
        self.mail_view.search_inside_requested.connect(self._search_inside)
        self.mail_view.period_requested.connect(self.timeline_ctl.browse_period)

        # Repositories are a browser, not a second search - see code_view.py.
        self.code_view = CodeView(store)
        self.code_view.error.connect(self._show_error)
        self.code_view.search_repo_requested.connect(self._search_repo)
        self.code_view.open_requested.connect(self._open_path)
        self.code_view.open_at_requested.connect(self._open_code_at)
        self.code_view.reveal_requested.connect(
            lambda path: self._open_path(path, reveal=True))
        self.code_view.indexing_requested.connect(
            lambda: self._show(getattr(self, "indexing_view", None)))

        # **§2, and it goes here rather than beside Settings for a reason.**
        # Every tab that has a preview can pin one, and each pane carries its
        # own `body_provider` - §2h's "no special casing" for mail, whose
        # message has no file on disk to open.
        for pane in (self.mail_view.preview, self.code_view.results.preview):
            pane.pop_out_requested.connect(self._pin_document)

        # Inserted right after Files - the position this pair held in the
        # original single loop - so the tab order nobody has to relearn
        # never changes. `indexOf` on the exact wrapped widget, not a
        # remembered number, is what keeps the refresh below correct
        # regardless of how many tabs an insertion has shifted.
        after_files = self._tab_index[self.files_view]
        # 2026-10-05, the owner: "a chip just for pictures designed to view find
        # and deal with pictures including namings". Photos, right after Files.
        self.photos_view = PhotosView(store)
        self.photos_view.error.connect(self._show_error)
        self.photos_view.open_requested.connect(self._open_path)
        self.photos_view.reveal_requested.connect(self._reveal_path)
        photos_wrapped = wrap_if_needed(self.photos_view, scroll=False)
        self.rail.insertTab(after_files + 1, photos_wrapped, "Photos", icon="image")
        self._tab_wrapped[self.photos_view] = photos_wrapped
        after_files += 1
        mail_wrapped = wrap_if_needed(self.mail_view, scroll=False)
        self.rail.insertTab(after_files + 1, mail_wrapped, "Mail", icon="mail")
        self._tab_wrapped[self.mail_view] = mail_wrapped
        code_wrapped = wrap_if_needed(self.code_view, scroll=False)
        self.rail.insertTab(after_files + 2, code_wrapped, "Code", icon="code")
        self._tab_wrapped[self.code_view] = code_wrapped
        # Chat (order 202626270611): a real tab, after Code. Everything but
        # the page itself - engine, worker, saving - is `ChatController`.
        self.chat_ctl = ChatController(self)
        self.chat_view = self.chat_ctl.build()
        # 2026-10-04: the Sources column's preview pins like every other pane's.
        self.chat_view.preview.pop_out_requested.connect(self._pin_document)
        chat_wrapped = wrap_if_needed(self.chat_view, scroll=False)
        self.rail.insertTab(after_files + 3, chat_wrapped, "Chat", icon="message-square")
        self._tab_wrapped[self.chat_view] = chat_wrapped
        for view, wrapped in self._tab_wrapped.items():
            self._tab_index[view] = self.rail.indexOf(wrapped)

        # `protect_all` already ran once in `__init__` for every control that
        # existed by then; Mail's and Code's controls did not, so it runs
        # again for exactly what it missed. Safe to call twice - a widget
        # guarded a second time is guarded harmlessly, see `protect`.
        guarded = protect_all(self)
        _log.debug("wheel-guarded {} controls (second pass, Mail + Code)", guarded)
        fit_number_fields(self)
        self._apply_motion()                     # §5c: the two new panes too

    def _construct_deferred_pages(self, store: Any, settings: Any, model: str,
                                  translator: Any, interpret_on: bool) -> None:
        """Build Indexing and Settings, wire them, and only then start the work.

        Order 0r item 2b, second pass. These two are the heaviest pages the
        constructor built - Settings alone is six group boxes, a debug pane and
        a file-type editor - and neither is what first paint shows. They are
        built here, on the next turn of the event loop, exactly as Mail and
        Code are by `_construct_secondary_views`. **Files and Search stay
        synchronous**: Search is the first paint, and Mail's and Code's
        insertion position is `self._tab_index[self.files_view]`.

        **The order is guaranteed by construction, not by timer order.**
        `_start_background_work` is called from the end of this method (in a
        `finally`, so it still runs if a page failed to build) rather than
        scheduled beside it, and it guards every touch of these two views with
        `getattr`, so a view that is missing means "skipped", never an
        `AttributeError` swallowed halfway through the start-up work.

        **Everything that reaches these two before they exist is guarded**:
        the Ctrl+, / Ctrl+I shortcuts and menu actions, `_start_indexing`
        (F5, a drop, "Index this folder"), `_tab_changed`, `_apply_theme`
        (which also runs on an operating-system theme change),
        `_rerank_toggled`, `closeEvent` and `_drain_workers`.

        The rail keeps its order - Indexing is the pill at the foot of the
        rail and Settings the foot page - because `Rail.insertTab` places by
        index and Mail/Code's callback refreshes `_tab_index` from
        `_tab_wrapped` whichever of the two callbacks runs first.
        """
        try:
            self.indexing_view = IndexingView()
            self.indexing_view.error.connect(self._show_error)
            self.indexing_view.reset_requested.connect(self._reset_index)
            self.indexing_view.start_button.clicked.connect(lambda _checked=False: self._start_indexing())
            self.indexing_view.retry_requested.connect(lambda _code: self._start_indexing())
            self.indexing_view.rescan_archives_requested.connect(self._rescan_archives)
            # Order 0z F3: "Retry with a longer time limit" on a timed-out type.
            self.indexing_view.timed_out.retryRequested.connect(self._retry_timed_out)
            self.indexing_view.scan_requested.connect(self._scan_corpus)
            self.indexing_view.stop_requested_externally.connect(self._stop_external_run)

            # Connected once, here. Connecting inside _start_indexing would add a
            # slot per run, so the tenth index would refresh the status bar ten times.
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

            self.settings_view = SettingsView(settings, store)
            self.settings_view.debug_pane.file_chosen.connect(self._open_path)
            self.settings_view.debug_pane.pop_out.connect(self._pop_out_log)
            self.settings_view.set_roots(
                self._load_roots(), self._load_root_modes(), self._load_cloud_content_roots(),
                self.settings_ctl._load_first_folders())
            self.settings_view.roots_changed.connect(self._save_roots)
            self.settings_view.root_modes_changed.connect(self._save_root_modes)
            self.settings_view.cloud_content_roots_changed.connect(self._save_cloud_content_roots)
            self.settings_view.first_folders_changed.connect(
                self.settings_ctl._save_first_folders)
            self.settings_view.rescan_archives_requested.connect(self._rescan_archives)
            self.settings_view.index_folder_requested.connect(self._index_folder_now)
            # 2026-10-07, the owner: the list reflects what is in the index.
            self.settings_view.remove_folders_requested.connect(
                self.settings_ctl._remove_folders)
            self.settings_view.remove_leftovers_requested.connect(
                self.settings_ctl._remove_leftovers)
            self.settings_view.roots_changed.connect(self.settings_ctl._check_leftovers)
            self.indexing_view.finished.connect(self.settings_ctl._check_leftovers)
            self.settings_ctl._check_leftovers()
            # 2026-10-07, the owner: each mail archive read its own way, and
            # read again - over the top, or cleared first.
            self.settings_view.mail_archive_choice_changed.connect(
                self.settings_ctl._save_archive_choice)
            self.settings_view.mail_archive_read_again_requested.connect(
                self.settings_ctl._read_archive_again)
            self.settings_view.mail_archive_clear_requested.connect(
                self.settings_ctl._clear_archive)
            self.indexing_view.finished.connect(self.settings_ctl._load_mail_archives)
            self.settings_ctl._load_mail_archives()
            self.settings_view.code_types_changed.connect(self._save_code_types)
            self.settings_view.code_types.load(*self._load_code_types())
            # 2026-10-05: the drop-down shows what was saved. Before the
            # connection below, so showing it is not taken for a new choice.
            self.settings_view.show_pst_backend(
                self._read_state("ui:pst_backend", "auto") or "auto")
            self.settings_view.pst_backend_changed.connect(self._save_pst_backend)
            self.settings_view.ollama_model_changed.connect(self._ollama_model_changed)
            self.settings_view.models.load(
                model, int(translator.timeout_s), enabled=interpret_on)
            self.settings_view.convert_pst_requested.connect(self._convert_pst)
            # The schedule and the tuning screen both live on the Indexing page
            # now - one place to watch a run and to change how it goes. See §4 of
            # the index-tuning order for why they were separated from Settings.
            self.indexing_view.schedule_box.load_indexing(settings)
            self.indexing_view.schedule_box.schedule_changed.connect(self._schedule_changed)
            # 0z F1: the folder watch's switch, and the two things that change
            # which folders it watches.
            self.indexing_view.schedule_box.watch_toggled.connect(self.folder_watch.toggled)
            self.settings_view.roots_changed.connect(self.folder_watch.folders_changed)
            self.settings_view.root_modes_changed.connect(self.folder_watch.folders_changed)
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
            self.settings_view.open_photo_tagger_requested.connect(self._open_photo_tagger)
            self.settings_view.environment.links_toggled.connect(self._links_toggled)
            # **Moved here with the Settings page it reports to**: the sentence saying
            # whether the shortcut was taken is pushed into that page.
            self._apply_hotkey()
            self.settings_view.environment.recording.setChecked(self.recorder.enabled)
            self.settings_view.debug_recording_toggled.connect(self._debug_recording_toggled)
            # **Both of these were emitted into nothing.** The rerank switch looked
            # like it worked and changed no behaviour at all; the cloud switch was
            # read live when a run started, so it worked for that run and silently
            # reset to off at the next launch - which reads as the setting being
            # ignored, and is the harder of the two to notice.
            self.settings_view.rerank_toggled.connect(self._rerank_toggled)
            self.settings_view.cloud_toggled.connect(self._cloud_toggled)
            self.settings_view.settings_changed.connect(self._settings_changed)
            # 2026-10-04: AI programs (MCP). Started, if wanted, with the
            # background work below; stopped in `closeEvent`.
            from app.ui.controllers.mcp_controller import McpController

            self.mcp_ctl = McpController(self)
            # "Clear search history" empties the log the search box's recent
            # searches are read from; without this it kept offering them.
            self.settings_view.history_cleared.connect(
                lambda _removed: self.search_view.saved.forget_recent())
            chat_ctl = getattr(self, "chat_ctl", None)
            if chat_ctl is not None:
                chat_ctl.attach_settings()
            self.settings_view.move_index_requested.connect(self._change_index_location)
            self.settings_view.rebuild_vectors_requested.connect(self._change_meaning_model)
            self.settings_view.error.connect(self._show_error)
            self.settings_view.file_types.changes_saved.connect(self._file_types_saved)
            self.settings_view.environment.set_recording_status(
                f"Recording to {self.recorder.path.name}" if self.recorder.enabled
                else "Not recording."
            )
            # Restore the switches that persist as window state (they were set here
            # in `__init__` before this page was deferred).
            self.settings_view.cloud.setChecked(
                self._read_state("ui:index_cloud", "") == "on")
            # *What gets read* (1 October 2026): the indexing levers this page
            # owns are drawn there, by place, and its sentences follow them.
            from app.ui.widgets.what_gets_read import gather_levers
            gather_levers(self.indexing_view.tuning.coverage, self.settings_view,
                          pst_backend=self._read_state("ui:pst_backend", "auto") or "auto")
            # 2026-10-04, code review: `.env`'s value when never touched.
            from app.search.run import rerank_choice

            self.settings_view.rerank.setChecked(rerank_choice(
                self._read_state("ui:rerank_enabled", ""), self._settings))
            # Read again rather than taken from `self.tray`: `__init__` may since have
            # cleared those flags because the desktop has no notification area, and
            # this box shows what was *chosen*, as it always did.
            self.settings_view.window_box.load(
                self._read_state("ui:tray_minimise", "") == "on",
                self._read_state("ui:tray_close", "") == "on",
                theme=self._theme_preference, motion=self._motion)
            self.settings_view.window_box.motion_changed.connect(self._motion_changed)
            self.settings_view.tray_changed.connect(self._tray_changed)

            # The two rail entries, appended in the order they always had. Indexing
            # has no button - it is the pill's page - and Settings is the foot page.
            for view, title, scroll, icon_name, placement in (
                (self.indexing_view, "Indexing", False, "database", "pill"),
                (self.settings_view, "Settings", True, "settings", "foot"),
            ):
                wrapped = wrap_if_needed(view, scroll=scroll)
                self._tab_wrapped[view] = wrapped
                self._tab_index[view] = self.rail.addTab(
                    wrapped, title, icon=icon_name,
                    foot=placement == "foot", pill=placement == "pill")
            # §2d / §2g: the pill paints from plain data the Indexing page already
            # emits. Nothing here touches the store.
            self.indexing_view.progressed.connect(self._paint_pill)
            self.indexing_view.totals_shown.connect(self._totals_for_pill)
            # 2026-10-05: the tray's line follows the same two signals.
            self.indexing_view.progressed.connect(self._paint_tray)
            self.indexing_view.totals_shown.connect(self._totals_for_tray)

            # Once, for the controls that did not exist when `__init__` ran it.
            guarded = protect_all(self)
            _log.debug("wheel-guarded {} controls (second pass, Indexing + Settings)", guarded)
            # 2026-10-05, the UI review: drop-downs and number fields on these
            # two pages are as wide as what they hold, not as wide as the page.
            from app.ui.widgets.field_width import fit_fields

            for page in (self.indexing_view, self.settings_view):
                fit_fields(page)
            # A group whose only control moved to Indexing is not drawn empty.
            self.settings_view.hide_emptied_boxes()
            fit_number_fields(self)
            # **After the pages are in the window**: `mark_restart_needed` finds
            # controls by `findChild` on the window, and a page that has not been
            # added to the rail yet has no parent to be found under.
            mark_restart_needed(self)
            # The stylesheet is already on the window (set once, in `__init__`);
            # what these pages still need is the palette pushed to the pixmaps
            # and the log pane - see `_push_palette`.
            self._push_palette()
            self._wire_recorder_pages()
        finally:
            self._start_background_work(store, settings)
            self._replay_index_requests()

    def _start_background_work(self, store: Any, settings: Any) -> None:
        """Everything that touches a thread or the store. See `__init__`.

        Guarded as a whole: a window that opens with no file count is a small
        problem, and one that refuses to open is a total one.
        """
        # Called from the end of `_construct_deferred_pages`, which builds the
        # two views everything marked below reaches into. `getattr` rather than
        # a bare attribute for each: if a build failed, or this is ever run
        # first, the step is skipped, and one missing page cannot raise an
        # `AttributeError` that the `except` below turns into "nothing after
        # this line ran" - including the model warm-up.
        indexing_view = getattr(self, "indexing_view", None)
        settings_view = getattr(self, "settings_view", None)
        try:
            # The watch for a run another process is doing. Here rather than in
            # `__init__` for the reason this whole method exists. Not started
            # without the Indexing page: both handlers write to it.
            if indexing_view is not None:
                self._watch_timer.start()
                self._poll_external_run()
                self._optimize_timer.start()
                indexing_view.refresh_totals(store, settings)
            # The two Settings labels that need the store or an import. They
            # used to be filled during `SettingsView.__init__`, which is inside
            # `MainWindow.__init__` - a COUNT(*) and a module import on the UI
            # thread before the first frame.
            if settings_view is not None:
                settings_view.refresh_slow_labels()
            mcp_ctl = getattr(self, "mcp_ctl", None)
            if mcp_ctl is not None:
                mcp_ctl.start_if_wanted()
            # Whether the next Start is the images pass (2026-10-04).
            self.index_ctl.load_images_due()
            # §2f: after construction, like `_restore_last_category` (M13).
            self._restore_last_page()
            self._refresh_status()
            if indexing_view is not None:
                self._start_scheduler()
                # **Detection shells out to PowerShell**, so it happens here for
                # exactly the reason this method exists. Until it answers, the
                # tuning screen shows the envelope's answers for an unknown
                # machine, which are the cautious ones.
                indexing_view.tuning.start_detection(settings.data_path)
                indexing_view.tuning.set_last_run(self._last_run_record())
                self._refresh_tuning_status()
                # 0z F1: starts the folder watch if its switch is on (off by
                # default). A process of its own; nothing here waits for it.
                self.folder_watch.apply()
            self._warm_translator()
            self._warm_models()
            self._start_model_choices()
            if settings_view is not None:
                self._refresh_link_scheme()
        except Exception as exc:                 # noqa: BLE001
            _log.warning("background start-up work failed: {}", exc)

    # -- handlers that live in the controllers -------------------------------
    #
    # `SettingsController` and `IndexController` (app/ui/controllers/) own
    # these; the window keeps a same-named method for each so that signal
    # wiring in `__init__` and every outside caller reach the same behaviour
    # they always did. The bodies, and the reasons for them, are over there.

    # settings: persistence and applying a changed setting

    def _debug_recording_toggled(self, on: bool) -> None:
        self.settings_ctl._debug_recording_toggled(on)

    def _rerank_toggled(self, enabled: bool) -> None:
        self.settings_ctl._rerank_toggled(enabled)

    def _set_toolbar_rerank(self, enabled: bool) -> None:
        self.settings_ctl._set_toolbar_rerank(enabled)

    def _change_index_location(self) -> None:
        self.settings_ctl._change_index_location()

    def _change_meaning_model(self) -> None:
        self.settings_ctl._change_meaning_model()

    def _chunk_count(self) -> int:
        return self.settings_ctl._chunk_count()

    def _settings_changed(self, values: dict) -> None:
        self.settings_ctl._settings_changed(values)

    def _tray_changed(self, minimise: bool, close: bool) -> None:
        self.settings_ctl._tray_changed(minimise, close)

    def _cloud_toggled(self, enabled: bool) -> None:
        self.settings_ctl._cloud_toggled(enabled)

    def _limits_changed(self, values: dict) -> None:
        self.settings_ctl._limits_changed(values)

    def _ollama_model_changed(self, enabled: bool, model: str, timeout_s: int) -> None:
        self.settings_ctl._ollama_model_changed(enabled, model, timeout_s)

    def _apply_search_preferences(self) -> None:
        self.settings_ctl._apply_search_preferences()

    def _apply_hotkey(self) -> None:
        self.settings_ctl._apply_hotkey()

    def _theme_changed(self, preference: str) -> None:
        self.settings_ctl._theme_changed(preference)

    def _refresh_link_scheme(self) -> None:
        self.settings_ctl._refresh_link_scheme()

    def _links_toggled(self, wanted: bool) -> None:
        self.settings_ctl._links_toggled(wanted)

    def _load_roots(self) -> list[str]:
        return self.settings_ctl._load_roots()

    def _load_code_types(self) -> tuple:
        return self.settings_ctl._load_code_types()

    def _save_code_types(self, preset: str, groups: list) -> None:
        self.settings_ctl._save_code_types(preset, groups)

    def _load_root_modes(self) -> dict:
        return self.settings_ctl._load_root_modes()

    def _save_root_modes(self, modes: dict) -> None:
        self.settings_ctl._save_root_modes(modes)

    def _load_cloud_content_roots(self) -> set:
        return self.settings_ctl._load_cloud_content_roots()

    def _save_cloud_content_roots(self, roots: set) -> None:
        self.settings_ctl._save_cloud_content_roots(roots)

    def _file_types_saved(self, changes: dict) -> None:
        self.settings_ctl._file_types_saved(changes)

    def _save_pst_backend(self, backend: str) -> None:
        self.settings_ctl._save_pst_backend(backend)

    def _apply_pst_backend(self, backend: str) -> None:
        self.settings_ctl._apply_pst_backend(backend)

    def _save_roots(self, roots: list[str]) -> None:
        self.settings_ctl._save_roots(roots)

    # the index run: its lifecycle, schedule, tuning and offline-media runs

    def _start_scheduler(self) -> None:
        self.index_ctl._start_scheduler()

    def _schedule_changed(self, policy: Any) -> None:
        self.index_ctl._schedule_changed(policy)

    def _load_last_index_time(self) -> Optional[datetime]:
        return self.index_ctl._load_last_index_time()

    def _save_last_index_time(self, when: datetime) -> None:
        self.index_ctl._save_last_index_time(when)

    def _last_run_record(self) -> Optional[dict]:
        return self.index_ctl._last_run_record()

    def _ocr_mode_for_run(self) -> str:
        return self.index_ctl._ocr_mode_for_run()

    def _offer_images_pass(self, _stats: Any) -> None:
        self.index_ctl._offer_images_pass(_stats)

    def _refresh_tuning_status(self) -> None:
        self.index_ctl._refresh_tuning_status()

    def _learn_from_run(self, stats: Any) -> None:
        self.index_ctl._learn_from_run(stats)

    def _benchmark_models(self) -> None:
        self.index_ctl._benchmark_models()

    def _can_use_gpu(self) -> bool:
        return self.index_ctl._can_use_gpu()

    def _benchmarked(self, result: Any) -> None:
        self.index_ctl._benchmarked(result)

    def _rescan_archives(self) -> None:
        self.index_ctl._rescan_archives()

    def _index_folder_now(self, folder: str) -> None:
        self.index_ctl._index_folder_now(folder)

    def _index_file_now(self, path: str) -> None:
        self.index_ctl._index_file_now(path)

    def _poll_external_run(self) -> None:
        self.index_ctl._poll_external_run()

    def _show_external_run(self, payload: dict) -> None:
        self.index_ctl._show_external_run(payload)

    def _stop_external_run(self) -> None:
        self.index_ctl._stop_external_run()

    def _maybe_run_idle_bench(self) -> None:
        self.index_ctl._maybe_run_idle_bench()

    def _idle_bench_finished(self, result: Any) -> None:
        self.index_ctl._idle_bench_finished(result)

    def _run_idle_optimize(self) -> None:
        self.index_ctl._run_idle_optimize()

    def _scan_corpus(self) -> None:
        self.index_ctl._scan_corpus()

    def _scan_finished(self, payload: dict) -> None:
        self.index_ctl._scan_finished(payload)

    def _scan_total(self, roots: list[str]) -> int:
        return self.index_ctl._scan_total(roots)

    def _convert_pst(self, archive: str, destination: str) -> None:
        self.index_ctl._convert_pst(archive, destination)

    def _conversion_done(self, count: int, target: Path) -> None:
        self.index_ctl._conversion_done(count, target)

    def _start_indexing(self, *, roots: Optional[list[str]] = None,
                        recheck_archives: bool = False) -> None:
        # F5, a drop and "Index this folder" can arrive in the beat before
        # `_construct_deferred_pages` has built the Indexing page; there is
        # nothing to start into yet, so the request is **kept, not dropped**
        # and replayed the moment the page exists (`_replay_index_requests`).
        # It used to be skipped, which lost a folder somebody had just dropped.
        if getattr(self, "indexing_view", None) is None:
            _log.debug("start indexing queued: the Indexing page is not built yet")
            self._queued_index_requests.append((roots, recheck_archives))
            return
        self.index_ctl._start_indexing(roots=roots, recheck_archives=recheck_archives)

    def _replay_index_requests(self) -> None:
        """Start what was asked for before the Indexing page existed. **Once.**

        However many were queued - F5 twice, a drop then F5 - they become one
        run, because `_start_indexing` refuses a second while one is out and
        would drop the rest. `None` roots means "everything configured", which
        already covers any folder named beside it; otherwise the named folders
        are merged. A request that named folders (a drop, "Index this folder")
        also brings the Indexing page forward, as it does when it is not late.
        """
        if getattr(self, "_drops_in_flight", 0):
            return                     # 2026-10-04, code review: a drop's folders land first
        queued, self._queued_index_requests = self._queued_index_requests, []
        if not queued or getattr(self, "indexing_view", None) is None:
            return
        named = [roots for roots, _recheck in queued if roots]
        everything = any(not roots for roots, _recheck in queued)
        if named:
            self._show(self.indexing_view)
        self._start_indexing(
            roots=None if everything else sorted({r for roots in named for r in roots}),
            recheck_archives=any(recheck for _roots, recheck in queued))

    def _index_resolved(self, tuned: Any, chosen: list[str],
                        roots: Optional[list[str]], recheck_archives: bool) -> None:
        self.index_ctl._index_resolved(tuned, chosen, roots, recheck_archives)

    def _index_resolve_failed(self, error: Any) -> None:
        self.index_ctl._index_resolve_failed(error)

    def _retry_timed_out(self, group: str, factor: float) -> None:
        self.index_ctl._retry_timed_out(group, factor)

    def _reset_index(self) -> None:
        self.index_ctl._reset_index()

    def _index_cleared(self, outcome: Any) -> None:
        self.index_ctl._index_cleared(outcome)

    def _offline_media_scan(self, root: str, name: str, description: str) -> None:
        self.index_ctl._offline_media_scan(root, name, description)

    def _offline_media_scan_after_check(self, root: str, name: str, description: str,
                                        suggestion: Any) -> None:
        self.index_ctl._offline_media_scan_after_check(root, name, description, suggestion)

    def _offline_media_scan_confirmed(self, root: str, name: str, description: str,
                                      same_as: Optional[str]) -> None:
        self.index_ctl._offline_media_scan_confirmed(root, name, description, same_as)

    def _offline_media_rescan(self, volume_id: int) -> None:
        self.index_ctl._offline_media_rescan(volume_id)

    def _offline_media_delete(self, volume_id: int) -> None:
        self.index_ctl._offline_media_delete(volume_id)

    def _offline_media_run_done(self, result: Any) -> None:
        self.index_ctl._offline_media_run_done(result)

    def _offline_media_run_failed(self, error: Any) -> None:
        self.index_ctl._offline_media_run_failed(error)

    # -- the debug recorder --------------------------------------------------


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
        self.notify(
            f"Recording this session to {self.recorder.path.name}", 10_000
        )

        self.rail.currentChanged.connect(
            lambda i: record("tab", name=self.rail.tabText(i))
        )

        self.search_view.error.connect(lambda e: record("error", where="search", **_err(e)))
        self.files_view.error.connect(lambda e: record("error", where="files", **_err(e)))

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

    def _wire_recorder_pages(self) -> None:
        """The recorder's listeners on Indexing and Settings, once they exist.

        `_wire_recorder` covers everything built in `__init__`; these two
        pages are built by `_construct_deferred_pages`, which calls this at the
        end. Same recorder, same events, same rule: shapes only.
        """
        if not self.recorder.enabled:
            return

        record = self.recorder.event
        self.indexing_view.error.connect(lambda e: record("error", where="index", **_err(e)))

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

    # -- shortcuts ----------------------------------------------------------

    def _build_shortcuts(self) -> None:
        def bind(sequence: str, slot: Any) -> None:
            action = QAction(self)
            action.setShortcut(QKeySequence(sequence))
            action.triggered.connect(slot)
            self.addAction(action)

        bind("Ctrl+K", self._focus_search)
        bind("Ctrl+F", self._focus_search)
        # `getattr`: Indexing and Settings are built a beat after the window
        # appears (`_construct_deferred_pages`); `_show(None)` does nothing.
        bind("Ctrl+,", lambda: self._show(getattr(self, "settings_view", None)))
        bind("Ctrl+I", lambda: self._show(getattr(self, "indexing_view", None)))
        bind("Ctrl+P", self._focus_files)
        bind("Ctrl+Shift+P", self._toggle_preview)
        bind("Ctrl+M", self._focus_mail)
        # **The Code tab was the only find tab without one.** Search, Files and
        # Mail all have a key that jumps to their box; Code did not, so the one
        # tab whose users are most likely to be keyboard-driven was the one that
        # needed a mouse. Ctrl+E for "code", since Ctrl+C is taken by copy.
        bind("Ctrl+E", self._focus_code)
        # 2026-10-08: Chat's Sources and Preview panel, away and back. Not in the
        # View menu, whose items after Preview pane are the tab's own options.
        bind("Ctrl+B", self._toggle_side_panel)
        bind("Esc", self._clear_search)
        # QAction.triggered emits `checked: bool`, so the slot must tolerate a
        # positional argument. Binding the method directly raises TypeError the
        # first time anyone presses F5 - which nothing would catch until then.
        bind("F5", lambda _checked=False: self._start_indexing())

    def _build_menu_bar(self) -> None:
        r"""§7a: a menu bar built from the shortcuts `_build_shortcuts` binds.

        macOS requires one - Qt moves it into the system bar unaided - and the
        owner chose to show it on Windows too ([FINALISE 2]): one line, and
        it is where people look for "where is the log". Every action here is
        an existing shortcut with its existing effect; the menu text is the
        control's own label or tooltip wording wherever one exists, so nothing
        is phrased two ways. `MenuRole`s file Quit and Preferences under the
        application menu on macOS. Icons per §0.3; tinted in `_apply_theme`.
        """
        from PySide6.QtWidgets import QMenuBar

        bar = QMenuBar(self)
        bar.setNativeMenuBar(True)
        self._menu_actions: list = []

        def add(menu: Any, text: str, slot: Any, keys: str = "", *,
                icon: str = "", role: Any = None, tip: str = "") -> QAction:
            action = QAction(text, self)
            if keys:
                action.setShortcut(QKeySequence(keys))
                if keys in _BOUND_ELSEWHERE:
                    # The QAction in `_build_shortcuts` already owns the key;
                    # this one only *shows* it, or both would fire.
                    action.setShortcutContext(Qt.ShortcutContext.WidgetShortcut)
            if tip:
                action.setStatusTip(tip)
                action.setToolTip(tip)
            if role is not None:
                action.setMenuRole(role)
            action.triggered.connect(lambda _c=False: slot())
            action.icon_name = icon
            menu.addAction(action)
            self._menu_actions.append(action)
            return action

        file = bar.addMenu("&File")
        add(file, "Start indexing", self._start_indexing, "F5", icon="play",
            tip="Start an index run now")
        add(file, "Show the log", self._pop_out_log, icon="file-text",
            tip="Open the log as its own window")
        file.addSeparator()
        add(file, "Settings", lambda: self._show(getattr(self, "settings_view", None)), "Ctrl+,",
            icon="settings", role=QAction.MenuRole.PreferencesRole)
        add(file, "Quit", self.close, "Ctrl+Q", icon="x-circle",
            role=QAction.MenuRole.QuitRole)

        edit = bar.addMenu("&Edit")
        add(edit, "Search", self._focus_search, "Ctrl+K", icon="search",
            tip="Put the cursor in the search box")
        add(edit, "Clear the search", self._clear_search, "Esc", icon="x")
        add(edit, "Save this search…", self._save_current_search, "Ctrl+D", icon="bookmark",
            tip="Give the search in the box a name, so you can run it again")
        add(edit, "Saved searches…", self._manage_saved_searches, icon="list",
            tip="Run, rename or delete the searches you have saved")

        view = bar.addMenu("&View")
        add(view, "Preview pane", self._toggle_preview, "Ctrl+Shift+P",
            icon="panel-right", tip="Show or hide the preview pane beside the results")
        # 2026-10-04, the owner: the tab's View options here too, changing with the tab.
        view.aboutToShow.connect(lambda menu=view: self._fill_view_menu(menu))

        go = bar.addMenu("&Go")
        add(go, "Files", self._focus_files, "Ctrl+P", icon="folder")
        add(go, "Photos", self._show_photos, icon="image",
            tip="See, find and name your photos")
        add(go, "Mail", self._focus_mail, "Ctrl+M", icon="mail")
        add(go, "Code", self._focus_code, "Ctrl+E", icon="code")
        add(go, "Offline", lambda: self._show(self.offline_media_view), icon="hard-drive")
        add(go, "Reports", lambda: self._show(self.reports_view), icon="chart-column")
        add(go, "Indexing", lambda: self._show(getattr(self, "indexing_view", None)),
            "Ctrl+I", icon="database")
        add(go, "People in photos", self._open_photo_tagger, icon="image",
            tip="Name the groups of faces Leasha has found in your photos")

        help_menu = bar.addMenu("&Help")
        add(help_menu, "Keyboard shortcuts", self._show_shortcuts, icon="keyboard",
            tip="Every shortcut, in one list")
        add(help_menu, "About Leasha", self._show_about, icon="info",
            tip="Which build this is, who made it, and what it is built on")
        self.setMenuBar(bar)

    def _fill_view_menu(self, menu: Any) -> None:
        """View, as it opens: Preview pane, then the options of the tab in front.

        2026-10-04, the owner: "the view in each tab should be in the view in
        the menu and should be dynamic i.e. content changes depending on the
        tab you are in". Built by the tab's own View button (`menu_for`), so the
        menu and the tab's icon can never offer different things; a tab with
        no View options (Indexing, Settings, Chat) shows Preview pane alone.
        """
        # 2026-10-07: nine times in two days an item added on the last opening
        # had already been deleted by Qt, `removeAction` raised on it, and -
        # because that happened before the list below was emptied - every later
        # opening raised on the same item until restart. What deletes it first
        # is not known, so this says what it can and carries on: a deleted
        # action has already left the menu, there is nothing to remove.
        from app.ui import qtsip

        old = getattr(self, "_view_menu_built", None)
        tab_now = type(self._current_view()).__name__
        extra, self._view_menu_extra = list(getattr(self, "_view_menu_extra", ())), []
        gone = [index for index, action in enumerate(extra) if qtsip.isdeleted(action)]
        if gone:
            _log.warning(
                "View menu: {} of {} item(s) added last time were already deleted "
                "(positions {}; built for {}, opening on {}; the built menu was {}; "
                "the View menu itself is {})",
                len(gone), len(extra), gone, getattr(self, "_view_menu_tab", "?"), tab_now,
                "none" if old is None else "deleted" if qtsip.isdeleted(old) else "alive",
                "deleted" if qtsip.isdeleted(menu) else "alive")
        for action in extra:
            if not qtsip.isdeleted(action):
                menu.removeAction(action)
        if old is not None and not qtsip.isdeleted(old):
            old.deleteLater()
        self._view_menu_built = None
        self._view_menu_tab = tab_now
        maker = getattr(getattr(self._current_view(), "view_button", None), "menu_for", None)
        if maker is None:
            return
        built = maker(menu)
        if built is None:
            # Once in the same two days: the tab's own menu came back as nothing.
            _log.warning("View menu: {} gave no menu of its own (its View button: {})",
                         tab_now, getattr(self._current_view(), "view_button", None))
            return
        self._view_menu_built = built
        self._view_menu_extra = [menu.addSeparator()]
        # A tab's own menu may offer Preview pane too; the window's item above,
        # with its shortcut, already flips that same preference - once is enough.
        already = {a.text().replace("&", "") for a in menu.actions() if a.text()}
        for action in list(built.actions()):
            if action.text() and action.text().replace("&", "") in already:
                continue
            menu.addAction(action)
            self._view_menu_extra.append(action)

    def _show_shortcuts(self) -> None:
        """Help → Keyboard shortcuts: the bindings, read from the menu itself."""
        lines = []
        for action in getattr(self, "_menu_actions", ()):
            keys = action.shortcut().toString()
            if keys:
                lines.append(f"{keys:<14} {action.text().replace('&', '')}")
        lines.append(f"{'Ctrl+Enter':<14} Interpret")
        lines.append(f"{'Ctrl+F':<14} Search (also Ctrl+K)")
        lines.append(f"{'Ctrl+B':<14} Chat: show or hide the Sources and Preview panel")
        QMessageBox.information(self, "Keyboard shortcuts", "\n".join(lines))

    def _show_about(self) -> None:
        """Help > About Leasha (2026-10-04, the brand assessment)."""
        from app.ui.widgets.about_dialog import AboutDialog

        AboutDialog(self).exec()

    def _tint_menu_icons(self, colours: dict) -> None:
        from app.ui.widgets.icons import icon
        for action in getattr(self, "_menu_actions", ()):
            name = getattr(action, "icon_name", "")
            if name:
                action.setIcon(icon(name, colours.get("text_dim", "#888888")))


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


    # -- §3a: search from anywhere --------------------------------------------


    def _summon_mini(self) -> None:
        """The shortcut was pressed. **Never raises**: this runs from a native
        event filter, where an exception has nowhere sensible to go."""
        try:
            if self._mini is not None and self._mini.isVisible():
                # Pressed again while it is open: it goes (the owner, 2026-10-08).
                self._mini.dismiss()
                return
            if self._mini is None:
                from app.ui.presenter.quick_search import recent_wanted
                from app.ui.widgets.mini_search import MiniSearch

                self._mini = MiniSearch(   # the Settings switches, read per search
                    self._engine, preferences=lambda: getattr(self, "search_preferences", None),
                    offer_recent=lambda: recent_wanted(self._settings, self._settings_overrides))
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

            # **Owned by the palette, not by this window.** Reading the foreground
            # selection waits on another application's clipboard, so this lands late
            # by design; a bare `connect(lambda ...)` has no receiver, and a palette
            # whose C++ half had gone would be prefilled from inside a Qt slot where
            # the RuntimeError has nowhere to go. `when_done` ties the call to the
            # palette's own lifetime - see `app/ui/later.py`.
            from app.ui.later import when_done

            mini = self._mini
            worker = CallableWorker(
                read_foreground_selection, component="ui.mini.selection")
            when_done(mini, worker,
                      finished=lambda text: mini.offer_prefill(text or ""))
            run(QThreadPool.globalInstance(), worker)
        except Exception as exc:                 # noqa: BLE001 - see docstring
            _log.debug("could not read the foreground selection: {}", exc)

    def _search_from_mini(self, query: str) -> None:
        """"Show me all of it": bring the window up with this query in it."""
        try:
            # Keeps a maximised window maximised - see `bring_forward`.
            bring_forward(self)
            self._show(self.search_view)
            self.search_view.input.setText(str(query or ""))
            self.search_view.search_now()
        except Exception as exc:                 # noqa: BLE001 - never fatal
            _log.debug("could not expand the mini search: {}", exc)


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
        # 2026-10-08: Chat has no View button; its own `toggle_preview` shows the
        # Preview page of its side panel, or puts the panel away.
        toggle = toggle or getattr(self._current_view(), "toggle_preview", None)
        if toggle is not None:
            toggle()

    def _toggle_side_panel(self) -> None:
        """Ctrl+B (2026-10-08): put the side panel of the tab in front away, or bring
        it back. Only Chat has one today; elsewhere the key does nothing."""
        toggle = getattr(self._current_view(), "toggle_panel", None)
        if toggle is not None:
            toggle()

    def _focus_mail(self) -> None:
        """Ctrl+M. Mail is a browser, so this lands in its filter box."""
        mail_view = getattr(self, "mail_view", None)
        if mail_view is None:
            # Order 0r item 2b: Mail is built a beat after the window
            # appears - see `_construct_secondary_views`. Pressed inside
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


    def _apply_theme(self) -> None:
        """Follow the operating system unless told otherwise.

        Hardcoding dark was not a style choice, it was a bug: on a machine set
        to light mode the application looked like it belonged to a different
        operating system, and it is harder to read on a bright screen, not
        easier.
        """
        from PySide6.QtGui import QGuiApplication

        preference = self._theme_preference
        detected = detect_scheme(QGuiApplication.instance())
        self.setStyleSheet(stylesheet(preference, detected=detected))
        self._push_palette()

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

    def _push_palette(self) -> None:
        """Everything `_apply_theme` does except set the window's stylesheet.

        **Why this is its own method: `_apply_theme` used to run twice at
        startup** (order 0r's known cost). The first call, in `__init__`, is
        the one first paint needs. The second, from `_construct_deferred_pages`,
        was only there because Mail, Code, Chat, Indexing and Settings did not
        exist yet and their pixmap icons and log pane have to be *told* the
        palette (the stylesheet never reaches a pixmap). Re-setting the same
        stylesheet on the window to do that made Qt re-polish every widget in
        the tree - and by then the tree held Settings, the heaviest page.
        Those late pages now call this alone; the stylesheet is set once, and
        reaches children added later by inheritance.
        """
        from PySide6.QtGui import QGuiApplication

        preference = self._theme_preference
        detected = detect_scheme(QGuiApplication.instance())

        # **Workspace §1a: the log's colours are pushed, not read.** The pane
        # paints warnings and errors from theme tokens, and this is the one
        # place that knows which theme is on - so the palette arrives here,
        # every time it changes, and a hardcoded hex never has to exist.
        from app.ui.theme import palette_for

        colours = palette_for(preference, detected=detected)
        # Indexing and Settings are built a beat after the window appears
        # (`_construct_deferred_pages`, which calls this again once they
        # exist); this also runs on an operating-system theme change.
        indexing_view = getattr(self, "indexing_view", None)
        settings_view = getattr(self, "settings_view", None)
        for target in (getattr(settings_view, "debug_pane", None), self._log_window):
            if target is not None:
                target.set_palette(colours)
        # The rail's and toolbar's icons are pixmaps and never see the sheet
        # (§1c), so the palette is pushed to them the way the log pane's is.
        self.rail.retint(colours)
        retint_toolbar(self.search_view, colours)
        # 2026-10-04: the Preview toggle on Files, Mail, Code and Chat too.
        for view in (self.files_view, getattr(self, "mail_view", None),
                     getattr(self, "code_view", None), getattr(self, "chat_view", None)):
            if view is not None:
                retint_toggles(view, colours)
        # 2026-10-05: the Photos tab's View icon follows the theme too.
        photos = getattr(self, "photos_view", None)
        if photos is not None:
            photos.view_button.retint(colours)
        self._tint_menu_icons(colours)
        for pane in self._preview_panes():
            pane.retint(colours)
        # §0.3: the Indexing page's five controls carry icons - now drawn by
        # the button system with the rest (`_style_buttons`, below), so Start
        # takes the primary's ink rather than every icon the same grey.
        for view in (self.files_view, getattr(self, "mail_view", None),
                     getattr(self, "code_view", None),
                     indexing_view, settings_view, self.reports_view):
            for target in (view, getattr(view, "_nav", None), getattr(view, "list", None)):
                retint = getattr(target, "retint", None)
                if callable(retint):
                    retint(colours)
        self._style_buttons(colours)

    def _style_buttons(self, colours: dict) -> None:
        """The button system (`widgets/buttons.py`, owner 2026-09-27).

        Every action button on the pages built so far gets its icon, its kind
        and its natural width - buttons already done are skipped, so running
        this again when the late pages arrive only touches the new ones - and
        then every button icon in every open window is redrawn in this
        palette, because icons are pictures and never see the stylesheet.
        """
        from app.ui.widgets.buttons import retint_all, style_all

        # The whole window: every page, the preview pane, the find and notice
        # bars. Pop-outs and dialogs style themselves as they are built.
        style_all(self, only_new=True)
        retint_all(colours)

    def _pin_document(self, row: Any, provider: Any = None) -> None:
        r"""Open this document in a window of its own. Workspace §2.

        **A copy, not a move**: the pane the button was pressed in keeps
        showing what it showed. Never raises - a window that will not open
        must not take the results with it. Built as the lightbox is
        (`preview_window.pop_out`); its Open takes the row (2026-10-04).
        """
        try:
            from app.ui.widgets.preview_window import pop_out

            window = pop_out(row, store=self._store, state=self._log_window_state(),
                             body_provider=provider, on_error=self._show_error,
                             remember=self._remember_log_window, closed=self._unpin)
            self._pinned.append(window)
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


    def _open_photo_tagger(self) -> None:
        r"""Open the window for naming the people in photos, or bring it back.

        Order 0j section 2. `PhotoTaggerPage` was finished and tested and
        nothing ever created it. **Built here, on first use**, so a person who
        never turns face recognition on never pays for it. One window, like
        the log's. Never raises: a convenience must not take the window it was
        opened from with it.
        """
        try:
            if self._photo_tagger is None:
                from app.ui.widgets.photo_tagger_window import PhotoTaggerWindow

                window = PhotoTaggerWindow(self._store, self)
                window.opened.connect(self._open_path)
                self._photo_tagger = window
                self._apply_theme()
            self._photo_tagger.show()
            self._photo_tagger.raise_()
            self._photo_tagger.activateWindow()
        except Exception as exc:             # noqa: BLE001 - see docstring
            _log.warning("could not open the Photo Tagger: {}", exc)

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
        # The ordered state writer rather than the global pool: a drag queues
        # dozens of these, and on a pool with several threads an earlier
        # geometry could land after the last one (bug 3a, `state_writes`).
        save_states(self._store, dict(values or {}), component="ui.log.window")

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
            self.rail.setCurrentIndex(index)

    def _current_view(self) -> Any:
        """The view whose tab is in front, or None.

        **By index, never `tabs.currentWidget()`.** A view inside a scroll area
        is not the tab's widget - the scroll area is - which is the same trap
        `_show` exists to avoid, and it returns the wrong object silently.
        """
        index = self.rail.currentIndex()
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
        # `getattr(self, "code_view"/"mail_view", None)` rather than a bare
        # attribute: Order 0r item 2b builds both a beat after the window
        # appears (`_construct_secondary_views`), and `_tab_index.get(None)`
        # is simply `None` - never equal to a real tab index - so this stays
        # correct in the gap before either exists, with no exception raised.
        indexing_view = getattr(self, "indexing_view", None)
        if indexing_view is not None and index == self._tab_index.get(indexing_view):
            indexing_view.refresh_totals(self._store, self._settings)
        elif index == self._tab_index.get(self.files_view):
            self.files_view.refresh_summary()
        elif index == self._tab_index.get(self.offline_media_view):
            self.offline_media_view.refresh()
        elif index == self._tab_index.get(self.reports_view):
            self.reports_view.refresh()
        elif index == self._tab_index.get(getattr(self, "photos_view", None)):
            # 2026-10-05: on the way in - a run, or a name given, changes it.
            self.photos_view.refresh()
        elif index == self._tab_index.get(getattr(self, "code_view", None)):
            # On the way in rather than on a timer: repositories change when an
            # index run finds one, which is rare and never while somebody is
            # looking at this tab.
            self.code_view.refresh()

        # Not while somebody is arrowing down the rail: taking the focus into
        # the page after the first Down left every other page unreachable by
        # the arrow keys (found by test_ui_redesign_scenarios.py).
        if self.rail.column.hasFocus():
            return

        # By index rather than by widget: a view inside a scroll area is not the
        # tab's widget, which is the same trap `_show` exists to avoid.
        for view in (self.search_view, self.files_view,
                     getattr(self, "mail_view", None),
                     getattr(self, "code_view", None)):
            if view is not None and self._tab_index.get(view) == index:
                view.focus()
                break


    def notify(self, text: str, timeout_ms: Optional[int] = None, *,
               level: str = "info") -> None:
        """§6b: what the status bar's `showMessage(text, ms)` used to do.

        Same positional shape, so the 39 call sites changed one name. A
        message with no timeout showed until replaced; here it shows for the
        default and is queued rather than overwritten.
        """
        self.toast.show_message(text, level, timeout_ms or DEFAULT_TIMEOUT_MS)

    def _paint_pill(self, state: str, indexed: int, value: int, total: int,
                    paused: bool, stopped_early: bool, error: str) -> None:
        """§2d: the rail's indexing pill, from plain data. UI thread, no I/O."""
        kind = {"running": RUNNING, "finished": FINISHED,
                "failed": FAILED}.get(state, IDLE)
        documents = self._last_document_count if kind in (FINISHED, IDLE) else None
        self.rail.show_pill(
            pill_text(kind, indexed=indexed, documents=documents, paused=paused,
                      stopped_early=stopped_early, error=error),
            pill_fraction(value, total) if kind == RUNNING else None)

    def _motion_changed(self, on: bool) -> None:
        """§5c: one keyed upsert, then every preview pane hears about it."""
        self._motion = bool(on)
        save_state(self._store, "ui:motion", "on" if on else "off",
                   component="ui.motion")
        self._apply_motion()

    def _preview_panes(self) -> list:
        panes = []
        for view in (self.search_view, self.files_view,
                     getattr(self, "mail_view", None),
                     getattr(self, "code_view", None),
                     getattr(self, "chat_view", None)):     # 2026-10-04
            pane = getattr(view, "preview", None) or getattr(
                getattr(view, "results", None), "preview", None)
            if pane is not None:
                panes.append(pane)
        return panes

    def _apply_motion(self) -> None:
        for pane in self._preview_panes():
            pane.motion = self._motion

    def _paint_tray(self, state: str, _indexed: int, value: int, total: int,
                    paused: bool, stopped_early: bool, _error: str) -> None:
        """The tray menu's live line, from the pill's data. UI thread, no I/O."""
        import datetime as _dt

        from app.ui.presenter.tray_words import tray_status

        if state == "finished":
            self._tray_finished_at = _dt.datetime.now()
        self._tray_state = (state, value, total, paused, stopped_early)
        self.tray.set_status(tray_status(
            state, value=value, total=total, paused=paused, stopped_early=stopped_early,
            documents=getattr(self, "_last_document_count", None),
            finished_at=getattr(self, "_tray_finished_at", None)))

    def _totals_for_tray(self, _documents: int) -> None:
        """A new count: the idle or finished line says it."""
        state, value, total, paused, stopped_early = getattr(
            self, "_tray_state", ("idle", 0, 0, False, False))
        if state != "running":
            self._paint_tray(state, 0, value, total, paused, stopped_early, "")

    def show_indexing_page(self) -> None:
        """The tray status line's click."""
        self._show(getattr(self, "indexing_view", None))

    def _totals_for_pill(self, documents: int) -> None:
        """The Indexing page counted; the pill shows the figure when idle."""
        self._last_document_count = int(documents)
        self.search_view.home.set_count(int(documents))     # §3a greeting
        if not self.indexing_view.is_running():
            self.rail.show_pill(pill_text(IDLE, documents=documents), None)

    def _remember_page(self, index: int) -> None:
        """§2f: one keyed upsert, the same path `ui:theme` takes."""
        # **Queued, never waited for (bug 3a).** A synchronous `set_state` here
        # took the store's write lock on the UI thread - the lock the indexer
        # holds for every batch - so during a run each click on the rail froze
        # the window until the indexer's transaction committed.
        # `test_page_switch_never_waits.py` holds that lock and switches page.
        title = self.rail.tabText(index)
        if title:
            save_state(self._store, "ui:page", title, component="ui.page")

    def _restore_last_page(self) -> None:
        """§2f: read post-construction (M13), and only if the page exists."""
        wanted = self._read_state("ui:page", "")
        for index in range(self.rail.count()):
            if wanted and self.rail.tabText(index) == wanted:
                self.rail.setCurrentIndex(index)
                return

    def _focus_search(self) -> None:
        self._show(self.search_view)
        self.search_view.focus()

    def _save_current_search(self) -> None:
        """Edit > Save this search. Adoptions 3b: only ever because somebody asked."""
        from app.ui.widgets.saved_dialogs import ask_to_save

        view = self.search_view
        ask_to_save(self, view.saved, view.input.text(), view.current_scope())

    def _manage_saved_searches(self) -> None:
        """Edit > Saved searches: the list, with run, rename and delete."""
        from app.ui.widgets.saved_dialogs import SavedSearchesDialog

        dialog = SavedSearchesDialog(self.search_view.saved, self)
        dialog.run_requested.connect(self._run_saved_search)
        self._saved_dialog = dialog
        dialog.show()

    def _run_saved_search(self, token: str) -> None:
        self._show(self.search_view)
        self.search_view.input.setText(token)
        self.search_view.search_now()

    def _clear_search(self) -> None:
        """Escape. **An open find bar closes first**; only a second press empties
        the box, so dismissing a little bar never throws away the search that
        produced the document being read."""
        for pane in self._preview_panes():
            find = getattr(pane, "find", None)
            if find is not None and find.isVisible():
                find.dismissed.emit()
                return
        self.search_view.input.clear()

    # -- startup ------------------------------------------------------------

    def _start_model_choices(self) -> None:
        """2026-10-04: the Chat tab's model is loaded ahead of the first question, a
        beat after start-up (`chat_controller.preload`), and Interpret's model menu on
        the Search page is filled - now if Interpret is on, else when `⋯` is opened."""
        from app.ui.controllers import chat_controller
        from app.ui.controllers.interpret_controller import InterpretModels

        menu = getattr(self.search_view, "interpret_models", None)
        if menu is not None and getattr(self, "interpret_ctl", None) is None:
            self.interpret_ctl = InterpretModels(self, menu, self._translator)
            self.search_view.more_menu.aboutToShow.connect(self.interpret_ctl.list_if_needed)
        if not chat_controller.BACKGROUND_MODELS:
            return
        if getattr(self._translator, "enabled", False) and menu is not None:
            self.interpret_ctl.list_if_needed()
        # 2026-10-04, the owner (2.2a): the chat model is no longer loaded a
        # beat after start-up (`chat_ctl.preload`). Loading it holds Python's
        # lock for the whole load - 5.5 s measured for the 1.66 GB model - so
        # the window froze while it opened (14.3 s at 22:08, with the
        # reranker and the meaning model). It loads when Chat is first opened.

    def _warm_models(self) -> None:
        """Load the models off the first search's critical path."""
        worker = CallableWorker(self._engine.warm_up, component="ui.warmup")
        worker.signals.failed.connect(
            lambda error: self.notify(error.message, 10_000, level="warning")
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
            self.notify(str(counts))

    # -- actions ------------------------------------------------------------

    def _open_result(self, row: Any, *, reveal: bool = False) -> None:
        """Open a result row, or show it in its folder, without waiting.

        **This was the "too slow" report**: `explorer /select,` and the stat
        before it ran on the UI thread. 2026-10-04: every page's Open now
        takes one route with its row (`workers.open_row_async`), which
        resolves a catalogued drive, finds a recording's moment and a code
        hit's line, opens a message in Outlook and an attachment from a copy.
        """
        open_row_async(self._store, row, reveal=reveal, on_error=self._show_error)

    def _reveal_path(self, path: str) -> None:
        """Show `path` in its folder - the Photos tab's "Show in folder"."""
        self._open_path(path, reveal=True)

    def _show_photos(self, *, naming: bool = False) -> None:
        """Bring the Photos tab forward - on its naming page when `naming`."""
        view = getattr(self, "photos_view", None)
        if view is None:
            return
        self._show(view)
        if naming:
            view.show_naming()

    def _open_path(self, path: str, *, reveal: bool = False) -> None:
        """The same, for a caller with a path and no row - the log, a chip.
        An attachment, a zip member or a message opens as it does from a row."""
        open_row_async(self._store, Place(str(path or "")), reveal=reveal,
                       on_error=self._show_error)

    def _open_code_at(self, path: str, line: int) -> None:
        """A code result, in the person's editor, at its line. Order 0y §2c -
        the editor is the setting in force (`_editor_choice`), read at the click."""
        open_row_async(self._store, Place(str(path or ""), int(line or 0)),
                       on_error=self._show_error)

    def _editor_choice(self) -> tuple:
        """`(CODE_EDITOR, CODE_EDITOR_COMMAND)` as they are now - a choice just
        made in Settings applies to the next Enter, without a restart."""
        chosen = {key: str(self._settings_overrides.get(
                      key, getattr(self._settings, key, "")) or "")
                  for key in ("code_editor", "code_editor_command")}
        return chosen["code_editor"] or "auto", chosen["code_editor_command"]

    def _lend_open_context(self) -> None:
        """What every page's Open borrows from the window (2026-10-04) - see
        `workers.OpenContext`. Errors and notes go to this window's own box
        and toast; a message nothing can open is searched inside."""
        from app.ui.widgets.preview_window import set_describe_options
        from app.ui.workers import OpenContext, set_open_context

        set_open_context(OpenContext(
            store=self._store, cache_path=getattr(self._settings, "cache_path", None),
            engine=self._engine, editor=self._editor_choice,
            on_error=self._show_error, on_note=lambda text: self.notify(text, 10_000),
            search_inside=self._search_inside))
        # The pop-outs' Describe settings, the same for a pinned window and the lightbox.
        set_describe_options(
            ollama_url=str(getattr(self._settings, "ollama_url", "http://127.0.0.1:11434")),
            ollama_vision_model=str(getattr(self._settings, "ollama_vision_model", "llava")),
            chat_engine=str(getattr(self._settings, "chat_engine", "onnx")))

    def _reindex_for(self, row: Any) -> None:
        folder = str(Path(row.path).parent)
        self._show(getattr(self, "indexing_view", None))
        self._start_indexing(roots=[folder])


    def _front_self(self) -> None:
        r"""Bring this window forward. A second launch asked for it
        (`run_lock.FRONT_STATE_KEY`) after finding the GUI mutex already
        held by this one - reported live as "the box is hard to get to"
        when that used to do nothing at all. Same three calls `_run_link`
        ends on, pulled out because a plain "somebody double-clicked the
        icon again" carries no query to run first.
        """
        bring_forward(self)       # keeps a maximised window maximised


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
        self.notify(
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
        self.notify(
            f"Searching {name} - type what you are looking for.", 8_000)


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
        event.acceptProposedAction()
        if not paths:
            return
        # 2026-10-04, code review: which dropped path is a folder is a stat
        # each - `tasks.dropped_roots`, on a worker, never here.
        from app.ui.tasks import dropped_roots

        # Counted, so a replay of requests made before the Indexing page was
        # built waits for this drop's folders rather than starting without them.
        self._drops_in_flight = getattr(self, "_drops_in_flight", 0) + 1
        worker = CallableWorker(dropped_roots, paths, component="ui.drop")
        worker.signals.finished.connect(self._index_dropped)
        worker.signals.failed.connect(lambda _error: self._index_dropped([]))
        run(QThreadPool.globalInstance(), worker)

    def _index_dropped(self, folders: Any) -> None:
        """What `dropEvent`'s worker found: index it, at top priority - joined
        with anything asked for while it was being found, as one run, the way
        `_replay_index_requests` joins requests made before the page existed."""
        self._drops_in_flight = max(0, getattr(self, "_drops_in_flight", 1) - 1)
        if folders:
            self._queued_index_requests.append((list(folders), False))
        if getattr(self, "indexing_view", None) is not None:
            self._replay_index_requests()

    #: How long closing waits for background threads to notice and stop. Long
    #: enough for a batch to finish and commit; short enough that nobody reaches
    #: for Task Manager. A worker that ignores it is left to Qt, which is the
    #: same outcome as before - just without the wait.
    SHUTDOWN_GRACE_MS = 4_000

    #: The same wait for an index run, which has more to finish: the file in
    #: flight, the vectors already handed to the feeder, the final flush. The
    #: window is hidden before this starts, so the wait costs nobody anything
    #: they can see - only the single-instance lock, held until it is over.
    INDEX_SHUTDOWN_GRACE_MS = 30_000

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
        from PySide6.QtCore import QEvent

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

        *Note, 5 October 2026 (order 202626270238 §2c):* Leasha runs on PySide6 now,
        where shiboken plays sip's part; PySide6 words the error "Internal C++ object
        ... already deleted". The guard is unchanged and was re-verified by the whole
        suite and `test_worker_signal_owner.py` under PySide6. Closing mid-search and
        mid-index by hand is the owner's check.

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
        # **Synchronous on purpose - the one UI-thread store write allowed**
        # (`test_ui_never_blocks.KEYED_WRITE_ALLOWED_IN`). The window is about
        # to hide and there is no event loop left to hand a result back to.
        # Every write queued earlier through `state_writes` is drained in
        # `_drain_workers` below, before the store closes.
        # 2026-10-05: `gui:window` cleared in the same write - from here on
        # this copy is closing, so a launch now waits for it rather than
        # trying to bring it forward (`run_lock.WINDOW_STATE_KEY`).
        from app.core.run_lock import WINDOW_STATE_KEY

        self._store.set_states({"ui:window_geometry": geometry_b64,
                                WINDOW_STATE_KEY: ""})

        # §3b: Hide the window first (perceived instant close). User sees the
        # app gone from the screen immediately, then the staged teardown runs
        # invisibly while holding the lock.
        self.hide()
        # 2026-10-04: nothing opens through a closed window's store.
        from app.ui.workers import set_open_context

        set_open_context(None)
        # 2026-10-04: AI access stops with the window - it runs only while Leasha is open.
        mcp_ctl = getattr(self, "mcp_ctl", None)
        if mcp_ctl is not None:
            mcp_ctl.shutdown()
        # 2026-10-04: the copies "Open" made of attachments. After the hide, so
        # the person never waits on it; a copy still open in Excel is left.
        try:
            from app.ui.attachment_open import clear_opened

            clear_opened(self._settings.cache_path)
        except Exception as exc:                 # a leftover is not a failure
            _log.debug("attachment copies not cleared: {}", exc)

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
        # `getattr(..., None)` for Mail/Code: Order 0r item 2b builds both a
        # beat after the window appears, and a close arriving before that
        # callback has run (an automated close sent immediately after
        # `show()`, with no event-loop turn in between) must not raise here -
        # skipping a `shutdown()` that has nothing to shut down yet is
        # correct, not a gap, since neither view has started any timer or
        # worker by then.
        for view in (self.search_view, self.files_view,
                     getattr(self, "mail_view", None),
                     getattr(self, "code_view", None),
                     getattr(self, "chat_view", None),
                     getattr(self, "photos_view", None)):
            if view is None:
                continue
            stage(type(view).__name__, view.shutdown)
        # A ceiling changed in the last third of a second is still sitting in a
        # timer. Closing without this loses it - which would be a worse bug than
        # the sluggishness the debounce was added to fix.
        # `getattr` for Indexing as for Mail/Code above: a close before
        # `_construct_deferred_pages` has run has nothing here to flush or
        # stop, and the arguments are evaluated before `stage()` can guard them.
        indexing_view = getattr(self, "indexing_view", None)
        if indexing_view is not None:
            stage("schedule", indexing_view.schedule_box.flush_pending)
            stage("tuning", indexing_view.tuning.flush_pending)
            stage("indexing", indexing_view.stop)
        # A pop-out or the log window left visible keeps the event loop alive
        # after this window is gone - see `close_windows` (order 202626191300 6d).
        from app.ui.close_windows import close_other_windows
        stage("other windows", lambda: close_other_windows(self))
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
        photo_tagger = getattr(self, "_photo_tagger", None)
        if photo_tagger is not None:
            stage("photo tagger", photo_tagger.close)
        stage("workers", self._drain_workers)
        # Work order 0x §2c. **An indexer in a child process must not outlive
        # the window.** It was asked to stop with everything else above, and
        # the drain gave its run the index grace to finish; one still going
        # now is given a few seconds more and then ended. Nothing to do (and
        # nothing imported that is not already loaded) on a normal close.
        from app.index.child_run import end_all_children
        stage("index process", end_all_children)
        # 0z F1: nor may the folder watch. Asked to stop, then given a few
        # seconds; nothing to do when the switch is off.
        from app.index.watch_child import end_all_watch_children
        stage("folder watch", self.folder_watch.shutdown)
        stage("folder watch process", end_all_watch_children)
        stage("recorder", self.recorder.close)
        stage("engine", self._engine.close)

        slow = ", ".join(f"{name} {seconds:.1f}s"
                         for name, seconds in stages if seconds >= 0.2)
        _log.info("closing: took {:.1f}s{}", time.monotonic() - began,
                  f" ({slow})" if slow else "")
        super().closeEvent(event)
        # Set by `main()` only - see `app/ui/exit_watchdog.py`. A closed window
        # that leaves a process behind is diagnosed and then ended.
        after_close = getattr(self, "after_close", None)
        if after_close is not None:
            try:
                after_close()
            except Exception as exc:                     # noqa: BLE001
                _log.warning("closing: the exit watchdog did not start: {}", exc)
        # **2026-09-30: end the event loop outright; do not wait for Qt to notice.**
        # This is a real shutdown (close-to-tray returned at the top), and the
        # teardown above is done. Both of the owner's closes that day logged
        # "closing: took 20.2s" and then sat in `application.exec()` until the
        # exit watchdog ended the process 300 s later - holding the stores and
        # the single-instance lock, so a relaunch in that window was refused.
        # Leaving it to "last visible window closed" has failed before with no
        # visible pop-out (see `close_windows.py`, the nine-hour incident), and
        # the tray icon on real Windows is one more thing that can count as a
        # window. Posted, not called, so `closeEvent` finishes first.
        from PySide6.QtWidgets import QApplication

        application = QApplication.instance()
        if application is not None:
            # Not reproduced off the owner's session (the close harness exits
            # either way), so what else Qt still counts as open is logged: the
            # next lingering close names its holder instead of being guessed at.
            try:
                from PySide6.QtGui import QGuiApplication

                still = [f"{type(w).__name__}:{w.objectName() or '-'}"
                         for w in QApplication.topLevelWidgets() if w.isVisible()]
                still += [f"QWindow:{w.objectName() or w.title() or '-'}"
                          for w in QGuiApplication.topLevelWindows()
                          if w.isVisible() and w is not self.windowHandle()]
                _log.info("closing: asking the event loop to end; still visible: {}",
                          ", ".join(still) or "nothing")
            except Exception:                            # noqa: BLE001 - a log line
                pass
            QTimer.singleShot(0, application.quit)

    def _drain_workers(self) -> None:
        """Wait for the thread pool, keeping the UI alive while it empties.

        `waitForDone` alone would block the UI thread, so a slow worker would
        freeze the window during the one operation nobody will wait out - and
        we would be back to Task Manager. Pumping events while waiting keeps
        the window painting until the threads are actually finished.
        """
        from PySide6.QtCore import QDeadlineTimer, QEventLoop
        from PySide6.QtWidgets import QApplication

        # **Both pools.** An index run lives in the indexing view's own pool,
        # not the global one, so waiting on the global pool alone returned at
        # once with a run still going: `closing: took 0.0s`, then three more
        # minutes of LibreOffice conversions with no window, and a process
        # that had to be killed from Task Manager. Sequential rather than
        # combined, because the two get different grace periods.
        index_view = getattr(self, "indexing_view", None)
        pools = [(QThreadPool.globalInstance(), self.SHUTDOWN_GRACE_MS, "")]
        if index_view is not None:
            pools.append((index_view.pool, self.INDEX_SHUTDOWN_GRACE_MS,
                          "index run "))
        # **Queued state writes last** (bug 3a): the page, the theme, a setting
        # changed a moment ago - each waits on the write lock, which an index
        # batch may hold until the run above has stopped. Draining them after
        # it gives them the best chance to land before the store closes, and
        # the grace keeps a wedged lock from holding the exit hostage.
        pools.append((state_write_pool(), self.SHUTDOWN_GRACE_MS,
                      "state write "))

        for pool, grace_ms, what in pools:
            deadline = QDeadlineTimer(grace_ms)
            while pool.activeThreadCount() and not deadline.hasExpired():
                pool.waitForDone(50)
                # **User input excluded.** Pumping *all* events here re-enters
                # the loop while the window is closing, so a keystroke or a
                # click landing in that window starts a fresh search against a
                # store that is about to be shut - the very race `shutdown()`
                # was just called to end. Paint and timer events are what keep
                # the window alive while the pool empties; input is not, and
                # there is nothing useful left to do with it.
                QApplication.processEvents(
                    QEventLoop.ProcessEventsFlag.ExcludeUserInputEvents, 10)

            remaining = pool.activeThreadCount()
            if remaining:
                _log.warning(
                    "closing with {} {}background thread(s) still running; "
                    "they will be abandoned", remaining, what,
                )
