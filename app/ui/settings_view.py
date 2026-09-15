"""Settings: index roots, exclusions, the rerank toggle, and the usage log.

Layer: L5

Two things here are not conveniences.

**Six sections, and two of them live elsewhere.** `IndexingSettings` holds the
schedule and the resource ceilings; `EnvironmentBox` holds doctor and session
recording. Both were split out when this file crossed the 250-line guard, which
was right to complain: the sections are independent and only shared a
constructor.

**"Clear search history" exists because the usage log exists.** Layer 4 records
every search and every result opened, so Layer 10 has evidence to tune from. A
record of what someone searched on their own machine is theirs to inspect and
erase, and a system that collects it with no way to clear it is not one to trust.

**Pages-reorg order, §1: five categories behind a sidebar, not twelve group
boxes on one scroll.** The owner's report that started it — "too long and
cluttered" — named the flat list, not any one box, so every box below is
built exactly as it always was and only *where it lands* changed. See
`app/ui/widgets/category_nav.py` for the sidebar mechanism (shared with
Indexing) and `_build_categories` below for which box joined which shelf.
Every label, tooltip and setting key is unchanged — the standing rule this
order does not touch.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QThreadPool, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core import settings_registry as reg
from app.core.logging import logger
from app.ui.presenter import (
    history_label_text, pst_status_text, settings_labels,
)
from app.ui.widgets.category_nav import CategoryNav
from app.ui.widgets.debug_pane import DebugPane
from app.ui.widgets.defaults import attach_resets, restore_button
from app.ui.widgets.environment_box import EnvironmentBox
from app.ui.widgets.file_types import FileTypesEditor
from app.ui.widgets.code_types_box import CodeTypesBox
from app.ui.widgets.model_box import ModelBox
from app.ui.widgets.roots_box import RootsBox
from app.ui.widgets.editor_box import EditorBox
from app.ui.widgets.search_behaviour_box import SearchBehaviourBox
from app.ui.widgets.search_box import SearchBox
from app.ui.widgets.storage_box import StorageBox
from app.ui.widgets.window_box import WindowBox

__all__ = ["SettingsView"]

_log = logger.bind(component="ui.settings")

#: The five shelves, in display order. A category with no boxes yet is not
#: possible here - every one is populated the moment it is built - but a
#: feature order landing a new box picks one of these names rather than
#: inventing a sixth; see the order's §1a for what belongs where.
CATEGORY_WHATS_INDEXED = "What's indexed"
CATEGORY_SEARCH = "Search"
CATEGORY_MODELS = "Models & AI"
CATEGORY_APPEARANCE = "Appearance"
CATEGORY_STORAGE = "Storage & maintenance"

#: The key `set_state`/`get_state` persist the last-open category under - the
#: "same app-state home as 0p §4's window state" the order asks for: small,
#: named keys through `SqliteStore.set_state`/`get_state`, the pattern every
#: other remembered UI choice in this window already uses (`ui:theme`,
#: `ui:pst_backend`, `ui:window_geometry`, ...).
CATEGORY_STATE_KEY = "ui:settings_category"


class SettingsView(QWidget):
    roots_changed = pyqtSignal(list)
    #: `{normalised root: "live"|"archive"}` - which folders never change.
    #: See `app/index/archives.py`; the saving this buys on a settled corpus is
    #: hours per incremental run.
    root_modes_changed = pyqtSignal(dict)
    #: "Rescan archived folders now" - one full walk, not a change of policy.
    rescan_archives_requested = pyqtSignal()
    #: `(preset, groups)` for the Code tab's file-type filter. A view
    #: preference: it changes what Code lists, never what is indexed.
    code_types_changed = pyqtSignal(str, list)
    pst_backend_changed = pyqtSignal(str)
    #: (enabled, model, timeout_s) for the Interpret button.
    ollama_model_changed = pyqtSignal(bool, str, int)
    convert_pst_requested = pyqtSignal(str, str)   # archive, destination
    #: The index-location flow: move it, adopt one already there, or start
    #: empty. A signal rather than a direct call because the shell owns the
    #: stores that would have to be closed before anything moves.
    move_index_requested = pyqtSignal()
    #: The meaning-model flow. Same shape and the same reason: it invalidates
    #: every vector, so it states the cost and confirms rather than applying.
    rebuild_vectors_requested = pyqtSignal()
    rerank_toggled = pyqtSignal(bool)

    cloud_toggled = pyqtSignal(bool)
    #: (minimise_to_tray, close_to_tray)
    tray_changed = pyqtSignal(bool, bool)
    #: system | light | dark. Appearance moved here from the indexing
    #: panel, where it was neither an indexing setting nor findable by
    #: anybody looking for one. The name is unchanged, so the window's
    #: handler did not have to move with it.
    theme_changed = pyqtSignal(str)
    #: `{registry key: value}` from any panel whose controls write `.env`.
    settings_changed = pyqtSignal(dict)
    history_cleared = pyqtSignal(int)
    debug_recording_toggled = pyqtSignal(bool)
    error = pyqtSignal(object)

    def __init__(self, settings: Any, store: Any = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._store = store

        # --- roots
        # Its own widget now that each folder carries a Live/Archive mode: two
        # columns, a combo per row and a rescan button is more than a view
        # should hold, and this file is already at the 250-line guard.
        self.roots_box = RootsBox()
        self.roots_box.roots_changed.connect(self.roots_changed)
        self.roots_box.modes_changed.connect(self.root_modes_changed)
        self.roots_box.rescan_requested.connect(self.rescan_archives_requested)

        # Directly under Folders to index, as asked. It answers a different
        # question from the File types editor further down - what one tab
        # *lists*, rather than what is *read* - and says so on itself.
        self.code_types = CodeTypesBox()
        self.code_types.changed.connect(self.code_types_changed)

        # --- behaviour
        # The Search group lives in its own widget: this file crossed the
        # 250-line guard, and the guard is right - a view that keeps growing is
        # a view where logic starts to live. The controls are re-exposed below
        # so callers and tests need not know where they moved to.
        self.search_box = SearchBox(settings)
        self.rerank = self.search_box.rerank
        self.rerank_top_n = self.search_box.rerank_top_n
        self.rerank_window = self.search_box.rerank_window
        self.rerank_model = self.search_box.rerank_model
        self.search_box.changed.connect(self.settings_changed)

        # §1b. What search may do on your behalf, and what each tab does with
        # it - see `widgets/search_behaviour_box.py` for why the grid is read
        # only and why the reset button matters more than it looks.
        self.search_behaviour = SearchBehaviourBox(settings)
        self.search_behaviour.changed.connect(self.settings_changed)

        # §4b. Kept out of the box above on purpose: that one is the six
        # behaviours and its reset button promises to restore exactly those.
        # An editor choice is not a search behaviour and must not be swept up
        # by a button whose tooltip says what it will not touch.
        self.editor_box = EditorBox(settings)
        self.editor_box.changed.connect(self.settings_changed)
        self.rerank.stateChanged.connect(lambda _s: self.rerank_toggled.emit(self.rerank.isChecked()))

        self.cloud = QCheckBox("Index cloud-only files (downloads them)")
        self.cloud.setToolTip(
            "OneDrive and SharePoint keep placeholders on disk. Reading one downloads the whole "
            "file, so pointing the indexer at a synced library with this on can pull down "
            "everything. Off by default for that reason."
        )
        self.cloud.stateChanged.connect(lambda _s: self.cloud_toggled.emit(self.cloud.isChecked()))

        # Index location and the meaning model are flows, not fields - see
        # StorageBox for why a text box there is a data-loss trap.
        self.storage_box = StorageBox(settings)
        self.data_path = self.storage_box.data_path
        self.storage_box.move_index_requested.connect(self.move_index_requested)
        self.storage_box.rebuild_vectors_requested.connect(self.rebuild_vectors_requested)
        self.storage_box.changed.connect(self.settings_changed)


        # --- Outlook archives
        self.pst_backend = QComboBox()
        self.pst_backend.addItem("Automatic - direct if possible, else Outlook", "auto")
        self.pst_backend.addItem("Direct file reading (no Outlook needed)", "libpff")
        self.pst_backend.addItem("Through Outlook (MAPI)", "outlook")
        self.pst_backend.setToolTip(
            "Reading an archive directly needs no Outlook, takes no file lock, and does not "
            "attach anything to your mail profile. Outlook is still used for the live mailbox, "
            "which only it can read."
        )
        self.pst_backend.currentIndexChanged.connect(
            lambda _i: self.pst_backend_changed.emit(self.pst_backend.currentData())
        )

        self.pst_status = QLabel("")
        convert = QPushButton("Convert a .pst to .eml files…")
        convert.setToolTip(
            "Exports an archive to a folder of .eml files. Afterwards the mail needs neither "
            "Outlook nor libpff - it is just files, which any mail client can open."
        )
        convert.clicked.connect(self._convert_pst)

        pst_box = QGroupBox("Outlook archives (.pst)")
        pst_layout = QVBoxLayout(pst_box)
        pst_layout.addWidget(QLabel("How to read archives:"))
        pst_layout.addWidget(self.pst_backend)
        pst_layout.addWidget(self.pst_status)
        pst_layout.addWidget(convert)

        # Window behaviour is its own group - see widgets/window_box.py for why
        # both of these were unreachable until now, and why Appearance moved in
        # there from the indexing panel it was never part of.
        self.window_box = WindowBox()
        self.minimise_to_tray = self.window_box.minimise_to_tray
        self.close_to_tray = self.window_box.close_to_tray
        self.theme = self.window_box.theme
        self.window_box.changed.connect(self.tray_changed)
        self.window_box.theme_changed.connect(self.theme_changed)

        behaviour = QGroupBox("Behaviour")
        form = QFormLayout(behaviour)
        form.addRow(self.cloud)


        # --- privacy
        self.history_label = QLabel("")
        clear = QPushButton("Clear search history")
        clear.setToolTip(
            "Delete every recorded search and every result opened.\n\n"
            "Nothing indexed is affected. What is lost is the record used to "
            "rank things you have opened before, so results may be slightly "
            "less well ordered for a while.")
        clear.clicked.connect(self._clear_history)

        # **What the console used to show.** The window is launched with
        # `pythonw.exe` now, which has no console at all - so "is it doing
        # anything" needs an answer inside the application. It is its own group
        # box; see `widgets.debug_pane`.
        self.activity = self.debug_pane = DebugPane()

        privacy = QGroupBox("Search history")
        privacy_layout = QVBoxLayout(privacy)
        privacy_layout.addWidget(QLabel(
            "Searches and which results you opened are recorded locally, so the ranking can be "
            "tuned to your documents later. Nothing leaves this machine."
        ))
        privacy_layout.addWidget(self.history_label)
        privacy_layout.addWidget(clear)

        # --- what gets indexed at all
        self.file_types = FileTypesEditor(settings)
        self.file_types.error.connect(self.error)

        # --- which Ollama model interprets a sentence (its own widget)
        #
        # A factory rather than a client: the URL can change in the box above,
        # and a client built once at startup would keep asking the old address.
        self.models = ModelBox(self._make_client)
        self.models.changed.connect(self.ollama_model_changed)
        # The address moved into the panel that can test it - see model_box.py.
        # Re-exposed because `_make_client` reads it to build a client against
        # whatever is currently typed.
        self.ollama_url = self.models.url
        self.ollama_url.setText(str(getattr(settings, "ollama_url", "")))
        self.models.url_changed.connect(
            lambda url: self.settings_changed.emit({"OLLAMA_URL": url}))

        # --- work order 0i section 3 / 0j: photo descriptions and people ---
        #
        # Plain controls, not a bespoke box like ModelBox above - none of
        # the three needs a live probe or a Test button, so a QGroupBox with
        # setObjectName-tagged fields plus the same
        # `self.settings_changed.emit({KEY: value})` shape `url_changed`
        # already uses just above is the whole of what non-negotiable #11
        # asks for here.
        self.photo_people_box = QGroupBox("People and photo descriptions")
        self.vision_model = QLineEdit()
        self.vision_model.setObjectName("OLLAMA_VISION_MODEL")
        self.vision_model.setPlaceholderText("llava")
        self.vision_model.setText(str(getattr(settings, "ollama_vision_model", "") or ""))
        self.vision_model.setToolTip(
            "The Ollama model that answers Describe on a photo. Needs a "
            "vision-capable model - llava or qwen2.5vl are common choices."
        )
        self.vision_model.editingFinished.connect(
            lambda: self.settings_changed.emit(
                {"OLLAMA_VISION_MODEL": self.vision_model.text().strip()}))

        self.caption_trickle = QCheckBox("Describe photos in the background")
        self.caption_trickle.setObjectName("CAPTION_TRICKLE_ENABLED")
        self.caption_trickle.setChecked(
            bool(getattr(settings, "caption_trickle_enabled", False)))
        self.caption_trickle.setToolTip(
            "Slowly writes an AI description for every photo that does not "
            "have one yet, a few at a time, paced the same way indexing "
            "already is. Off by default - can take hours across a whole "
            "photo collection. Needs the photo description model above."
        )
        self.caption_trickle.toggled.connect(
            lambda on: self.settings_changed.emit({"CAPTION_TRICKLE_ENABLED": on}))

        self.people_recognition = QCheckBox(
            "Recognise people in photos on this computer")
        self.people_recognition.setObjectName("PEOPLE_RECOGNITION_ENABLED")
        self.people_recognition.setChecked(
            bool(getattr(settings, "people_recognition_enabled", False)))
        self.people_recognition.setToolTip(
            "Finds faces in your photos and groups similar ones into piles "
            "you can name, so you can search for people - nothing ever "
            "leaves this computer. Off by default: stores a description of "
            "each face's shape for every photo with a person in it, and "
            "only names YOU give a pile are ever attached to it. Turning "
            "this off stops new faces being found; use 'Forget this "
            "person' on the Photo Tagger page to remove what is already "
            "stored."
        )
        self.people_recognition.toggled.connect(
            lambda on: self.settings_changed.emit({"PEOPLE_RECOGNITION_ENABLED": on}))

        photo_people_form = QFormLayout(self.photo_people_box)
        photo_people_form.addRow(self.people_recognition)
        photo_people_form.addRow(self.caption_trickle)
        photo_people_form.addRow("Photo description model", self.vision_model)

        # --- environment and diagnostics (its own widget; see the module)
        self.environment = EnvironmentBox(settings)
        self.environment.recording_toggled.connect(self.debug_recording_toggled)

        # **Getting back to a default was a one-way door.** `.env` beats the
        # code default, so a setting written once is pinned for ever - which is
        # how a reranker measured 9.2x faster shipped and reached no machine.
        # One button for all of them, and a right-click on any control for one.
        # See `widgets/defaults.py`; both send None, which removes the line.
        self.restore_defaults = restore_button(
            self, getattr(settings, "env_file", None),
            lambda values: self.settings_changed.emit(values))
        attach_resets(self, lambda values: self.settings_changed.emit(values))

        # --- §1b. The filter box, and §1a's five categories -----------------
        #
        # A new control, so its own wording (the standing rule only binds
        # *existing* labels). It walks the registry, not the widgets - see
        # `_apply_filter` - because the registry already carries every
        # label and tooltip in one machine-readable place.
        self.filter_box = QLineEdit()
        self.filter_box.setObjectName("settingsFilter")
        self.filter_box.setPlaceholderText('Filter settings (try "memory")')
        self.filter_box.setToolTip(
            "Shows only the settings whose name or description matches what "
            "you type, in every category at once. Clear it to see everything "
            "again."
        )
        self.filter_box.textChanged.connect(self._apply_filter)

        self.filter_empty = QLabel("No settings match. Try a different word.")
        self.filter_empty.setObjectName("settingsFilterEmpty")
        self.filter_empty.setVisible(False)

        self._nav = CategoryNav()
        self._nav.category_changed.connect(self._category_selected)
        self._build_categories(pst_box, behaviour, privacy)

        layout = QVBoxLayout(self)
        layout.addWidget(self.filter_box)
        layout.addWidget(self.filter_empty)
        layout.addWidget(self._nav, stretch=1)

        # **Neither of these runs during construction any more.**
        #
        # `refresh_history_count` queries the store and `refresh_pst_status`
        # imports `pst_libpff` to see whether it is installed - a `COUNT(*)`
        # and a module import, on the UI thread, inside `MainWindow.__init__`.
        # The window's own docstring states the rule they broke: *nothing runs
        # on a background thread until construction is over* - and the reason
        # is that the main thread is not free either, so store work here delays
        # the first frame and, worse, contends with `_apply_theme`.
        #
        # `shell._start_background_work` calls `refresh_slow_labels()` on the
        # first turn of the event loop instead. Until then both labels say what
        # they are doing rather than nothing, because a blank label is
        # indistinguishable from a broken one.
        self.history_label.setText("Counting…")
        self.pst_status.setText("Checking how Outlook archives can be read…")

    # -- roots --------------------------------------------------------------

    def set_roots(self, roots: list[str], modes: Optional[dict] = None) -> None:
        self.roots_box.set_roots(roots, modes)

    def add_root(self, folder: str) -> bool:
        return self.roots_box.add_root(folder)

    def current_modes(self) -> dict:
        return self.roots_box.current_modes()

    def _make_client(self):
        """A fresh OllamaClient against whatever URL is currently in the box.

        Built per call so editing the URL above takes effect without a restart,
        and so a probe never holds a reference to a client the rest of the app
        is also using.
        """
        from app.llm.ollama import OllamaClient

        url = self.ollama_url.text().strip() or self._settings.ollama_url
        return OllamaClient(url, self._settings.ollama_model)

    def current_roots(self) -> list[str]:
        return self.roots_box.current_roots()

    # -- Outlook archives ---------------------------------------------------

    def refresh_slow_labels(self) -> None:
        """Fill in both labels that need the store or an import. **Worker.**

        Called from `shell._start_background_work`, which is the first moment
        anything may touch a thread or the store - see the comment where these
        used to run, in the constructor. §1c's last-open category rides the
        same moment, for the same reason - see `_restore_last_category`.
        """
        from app.ui.workers import CallableWorker, run

        worker = CallableWorker(settings_labels, self._store,
                                component="ui.settings.labels")
        worker.signals.finished.connect(self._show_slow_labels)
        worker.signals.failed.connect(lambda _e: None)
        run(QThreadPool.globalInstance(), worker)
        self._restore_last_category()

    def _show_slow_labels(self, found: Any) -> None:
        """UI thread, no I/O - the worker fetched both."""
        searches, direct = found
        self.history_label.setText(history_label_text(int(searches)))
        self.pst_status.setText(pst_status_text(bool(direct)))
        self.pst_status.setWordWrap(True)

    # -- §1a categories -------------------------------------------------------

    def _build_categories(self, pst_box: QGroupBox, behaviour: QGroupBox,
                          privacy: QGroupBox) -> None:
        """Assemble the five shelves. Every box above is built exactly as it
        always was; only where it lands changed - see the order's §1a for
        which box joined which category and why."""

        whats_indexed = QWidget()
        whats_indexed_layout = QVBoxLayout(whats_indexed)
        whats_indexed_layout.setContentsMargins(0, 0, 0, 0)
        for widget in (self.roots_box, self.code_types, pst_box, behaviour,
                       self.file_types):
            whats_indexed_layout.addWidget(widget)
        whats_indexed_layout.addStretch(1)

        search = QWidget()
        search_layout = QVBoxLayout(search)
        search_layout.setContentsMargins(0, 0, 0, 0)
        for widget in (self.search_box, self.search_behaviour,
                       self.editor_box, privacy):
            search_layout.addWidget(widget)
        search_layout.addStretch(1)

        models = QWidget()
        models_layout = QVBoxLayout(models)
        models_layout.setContentsMargins(0, 0, 0, 0)
        models_layout.addWidget(self.models)
        models_layout.addWidget(self.photo_people_box)
        models_layout.addStretch(1)

        # §0 settled decision #2, honoured here: theme relocates to
        # Appearance verbatim - the control already lives in `WindowBox`
        # (moved out of the indexing panel by the tuning order's 4c-4), so
        # this is only where the box now sits, not a second move.
        appearance = QWidget()
        appearance_layout = QVBoxLayout(appearance)
        appearance_layout.setContentsMargins(0, 0, 0, 0)
        appearance_layout.addWidget(self.window_box)
        appearance_layout.addStretch(1)

        storage = QWidget()
        storage_layout = QVBoxLayout(storage)
        storage_layout.setContentsMargins(0, 0, 0, 0)
        storage_layout.addWidget(self.storage_box)
        storage_layout.addWidget(self.activity)
        storage_layout.addWidget(self.environment, stretch=1)
        storage_layout.addWidget(self.restore_defaults)

        self._nav.add_category(CATEGORY_WHATS_INDEXED, whats_indexed)
        self._nav.add_category(CATEGORY_SEARCH, search)
        self._nav.add_category(CATEGORY_MODELS, models)
        self._nav.add_category(CATEGORY_APPEARANCE, appearance)
        self._nav.add_category(CATEGORY_STORAGE, storage)

    def _category_selected(self, name: str) -> None:
        """A click, not a restore - see `CategoryNav.show_category`'s
        `persist` argument. Synchronous, like every other small UI-state
        write in this window (`ui:theme`, `ui:pst_backend`, ...) - a single
        keyed upsert, not the kind of store work M13 exists to keep off the
        UI thread."""
        if self._store is not None:
            self._store.set_state(CATEGORY_STATE_KEY, name)

    def _restore_last_category(self) -> None:
        """§1c: which category was open last, read off the UI thread.

        Never during construction (M13) - only reached from
        `refresh_slow_labels`, which runs once the window is running. No
        store, or nothing recorded yet, leaves the first category selected -
        "first run opens the first category".
        """
        if self._store is None:
            return
        from app.ui.workers import CallableWorker, run

        worker = CallableWorker(
            self._store.get_state, CATEGORY_STATE_KEY, "",
            component="ui.settings.category")
        worker.signals.finished.connect(self._apply_last_category)
        worker.signals.failed.connect(lambda _e: None)
        run(QThreadPool.globalInstance(), worker)

    def _apply_last_category(self, name: Any) -> None:
        """UI thread, no I/O - the worker already read it."""
        name = str(name or "")
        if name in self._nav.category_names():
            self._nav.show_category(name, persist=False)

    # -- §1b the filter box ---------------------------------------------------

    def _apply_filter(self, text: str) -> None:
        """Walks the registry, not the widgets.

        Every registered setting's control is found by the object name
        `test_settings_reachable.py` already requires it to carry, so no
        widget file needs to know filtering exists. A category with at least
        one hit is shown; the rest hide - "categories auto-expanding to show
        hits" - and a control that does not match is hidden beside its own
        form label, so the row leaves no gap shaped like a missing answer.
        """
        needle = text.strip().lower()
        if not needle:
            self._nav.set_sidebar_enabled(True)
            self._reveal_all_controls()
            self._nav.restore_single_view()
            self.filter_empty.setVisible(False)
            return

        self._nav.set_sidebar_enabled(False)
        hits: dict[str, bool] = {name: False for name in self._nav.category_names()}
        for setting in reg.SETTINGS:
            widget = self.findChild(QWidget, setting.key)
            if widget is None:
                continue
            category = self._category_of(widget)
            if category is None:
                continue
            haystack = f"{setting.label} {setting.help}".lower()
            hit = needle in haystack
            widget.setVisible(hit)
            label = self._form_label_for(widget)
            if label is not None:
                label.setVisible(hit)
            if hit:
                hits[category] = True

        visible = {name for name, hit in hits.items() if hit}
        self._nav.show_all_for_filter(visible)
        self.filter_empty.setVisible(not visible)

    def _category_of(self, widget: QWidget) -> Optional[str]:
        for name in self._nav.category_names():
            page = self._nav.page(name)
            if page is not None and page.isAncestorOf(widget):
                return name
        return None

    def _form_label_for(self, widget: QWidget) -> Optional[QLabel]:
        parent = widget.parentWidget()
        while parent is not None:
            form = parent.layout()
            if isinstance(form, QFormLayout):
                label = form.labelForField(widget)
                if label is not None:
                    return label
            parent = parent.parentWidget()
        return None

    def _reveal_all_controls(self) -> None:
        """Undo `_apply_filter`'s hiding before the sidebar takes over again -
        otherwise a control hidden by a search that has since been cleared
        would stay hidden forever inside a category nobody is filtering."""
        for setting in reg.SETTINGS:
            widget = self.findChild(QWidget, setting.key)
            if widget is None:
                continue
            widget.setVisible(True)
            label = self._form_label_for(widget)
            if label is not None:
                label.setVisible(True)

    def _convert_pst(self) -> None:
        archive, _filter = QFileDialog.getOpenFileName(
            self, "Choose an Outlook archive", "", "Outlook archives (*.pst)"
        )
        if not archive:
            return
        destination = QFileDialog.getExistingDirectory(self, "Where should the .eml files go?")
        if destination:
            self.convert_pst_requested.emit(archive, destination)

    # -- history ------------------------------------------------------------

    def refresh_history_count(self) -> None:
        """Re-count the usage log. Off-thread - see `refresh_slow_labels`."""
        self.refresh_slow_labels()

    def _clear_history(self) -> None:
        """Delete the usage log in a worker.

        A `DELETE` over a large table takes a lock and a moment. On the UI
        thread that is a window that stops repainting at the exact instant
        someone has asked for something to be erased - the worst possible time
        to look like a crash.
        """
        from app.ui.workers import CallableWorker, run

        if self._store is None:
            return
        self.history_label.setText("Clearing…")
        worker = CallableWorker(self._store.clear_usage_log, component="ui.history")
        worker.signals.finished.connect(self._history_cleared)
        worker.signals.failed.connect(
            lambda error: self.history_label.setText(getattr(error, "message", str(error)))
        )
        run(QThreadPool.globalInstance(), worker)

    def _history_cleared(self, removed: int) -> None:
        self.history_label.setText(f"Cleared. {removed:,} searches removed.")
        self.history_cleared.emit(removed)

