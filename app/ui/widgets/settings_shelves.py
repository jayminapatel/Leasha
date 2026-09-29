"""What `SettingsView` does that holds no wording: build the boxes, shelve them,
filter them, remember the open category, fill the two slow labels.

Layer: L5

`settings_view.py` reached the 250-line guard while it was still holding the
five-shelf assembly, the filter walk and the store round-trips, and the guard
is right about what it is for: a view that keeps growing is where logic starts
to live. This is that logic, as a mixin so every method keeps being a method of
`SettingsView` - `view._apply_filter`, `view._nav`, `view.roots_box` and the
rest are exactly the names the tests and `shell.py` already reach for.

**No user-facing string is in this file.** Every label, tooltip and placeholder
stays in `settings_view.py`, where `test_pages_reorg.py` reads it against the
commit before the reorganisation. **Every signal stays there too**, because
`test_settings_reachable.py` scans that file for what a settings page declares.

Pages-reorg order, section 1: five categories behind a sidebar, not twelve
group boxes on one scroll. The owner's report that started it - "too long and
cluttered" - named the flat list, not any one box, so every box is built
exactly as it always was and only *where it lands* changed. See
`category_nav.py` for the sidebar mechanism (shared with Indexing) and
`_build_categories` for which box joined which shelf.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QThreadPool
from PyQt6.QtWidgets import QFormLayout, QGroupBox, QLabel, QVBoxLayout, QWidget

from app.core import settings_registry as reg
from app.ui.presenter import history_label_text, pst_status_text, settings_labels
from app.ui.state_writes import save_state
from app.ui.widgets.chat_box import ChatBox
from app.ui.widgets.code_types_box import CodeTypesBox
from app.ui.widgets.debug_pane import DebugPane
from app.ui.widgets.defaults import attach_resets, restore_button
from app.ui.widgets.editor_box import EditorBox
from app.ui.widgets.environment_box import EnvironmentBox
from app.ui.widgets.file_types import FileTypesEditor
from app.ui.widgets.media_box import MediaBox
from app.ui.widgets.model_box import ModelBox
from app.ui.widgets.roots_box import RootsBox
from app.ui.widgets.search_behaviour_box import SearchBehaviourBox
from app.ui.widgets.search_box import SearchBox
from app.ui.widgets.storage_box import StorageBox
from app.ui.widgets.window_box import WindowBox

__all__ = [
    "CATEGORY_APPEARANCE", "CATEGORY_MODELS", "CATEGORY_SEARCH",
    "CATEGORY_STATE_KEY", "CATEGORY_STORAGE", "CATEGORY_WHATS_INDEXED",
    "SettingsShelves",
]

#: The five shelves, in display order. A category with no boxes yet is not
#: possible here - every one is populated the moment it is built - but a
#: feature order landing a new box picks one of these names rather than
#: inventing a sixth; see the order's section 1a for what belongs where.
CATEGORY_WHATS_INDEXED = "What's indexed"
CATEGORY_SEARCH = "Search"
CATEGORY_MODELS = "Models & AI"
CATEGORY_APPEARANCE = "Appearance"
CATEGORY_STORAGE = "Storage & maintenance"

#: The key `set_state`/`get_state` persist the last-open category under - the
#: "same app-state home as 0p section 4's window state" the order asks for:
#: small, named keys through `SqliteStore.set_state`/`get_state`, the pattern
#: every other remembered UI choice in this window already uses (`ui:theme`,
#: `ui:pst_backend`, `ui:window_geometry`, ...).
CATEGORY_STATE_KEY = "ui:settings_category"


class SettingsShelves:
    """Mixed into `SettingsView` (a `QWidget` with the signals declared)."""

    # -- construction ---------------------------------------------------------

    def _build_boxes(self, settings: Any) -> None:
        """Every self-contained box, wired to the view's own signals.

        Each is its own widget because the view crossed the 250-line guard and
        the guard is right - a view that keeps growing is a view where logic
        starts to live. The controls are re-exposed as attributes so callers
        and tests need not know where they moved to.
        """
        self.roots_box = RootsBox()
        self.roots_box.roots_changed.connect(self.roots_changed)
        self.roots_box.modes_changed.connect(self.root_modes_changed)
        self.roots_box.cloud_content_changed.connect(self.cloud_content_roots_changed)
        self.roots_box.rescan_requested.connect(self.rescan_archives_requested)
        self.roots_box.first_changed.connect(self.first_folders_changed)

        self.code_types = CodeTypesBox()
        self.code_types.changed.connect(self.code_types_changed)

        self.search_box = SearchBox(settings)
        self.rerank = self.search_box.rerank
        self.rerank_top_n = self.search_box.rerank_top_n
        self.rerank_window = self.search_box.rerank_window
        self.rerank_model = self.search_box.rerank_model
        self.search_box.changed.connect(self.settings_changed)
        self.rerank.stateChanged.connect(
            lambda _s: self.rerank_toggled.emit(self.rerank.isChecked()))

        self.search_behaviour = SearchBehaviourBox(settings)
        self.search_behaviour.changed.connect(self.settings_changed)
        self.editor_box = EditorBox(settings)
        self.editor_box.changed.connect(self.settings_changed)
        self.chat_box = ChatBox(settings)
        self.chat_box.changed.connect(self.settings_changed)
        self.media_box = MediaBox(settings)
        self.media_box.changed.connect(self.settings_changed)

        self.storage_box = StorageBox(settings)
        self.data_path = self.storage_box.data_path
        self.storage_box.move_index_requested.connect(self.move_index_requested)
        self.storage_box.rebuild_vectors_requested.connect(self.rebuild_vectors_requested)
        self.storage_box.changed.connect(self.settings_changed)

        self.window_box = WindowBox()
        self.minimise_to_tray = self.window_box.minimise_to_tray
        self.close_to_tray = self.window_box.close_to_tray
        self.theme = self.window_box.theme
        self.window_box.changed.connect(self.tray_changed)
        self.window_box.theme_changed.connect(self.theme_changed)

        self.activity = self.debug_pane = DebugPane()
        self.file_types = FileTypesEditor(settings)
        self.file_types.error.connect(self.error)

        # A factory rather than a client: the URL can change in the box, and a
        # client built once at startup would keep asking the old address.
        self.models = ModelBox(self._make_client)
        self.models.changed.connect(self.ollama_model_changed)
        # The address lives in the panel that can test it. Re-exposed because
        # `_make_client` reads it to build a client against what is typed now.
        self.ollama_url = self.models.url
        self.ollama_url.setText(str(getattr(settings, "ollama_url", "")))
        self.models.url_changed.connect(
            lambda url: self.settings_changed.emit({"OLLAMA_URL": url}))

    def _build_late_boxes(self, settings: Any) -> None:
        """What has to exist after every control that carries a registry key.

        **Getting back to a default was a one-way door.** `.env` beats the code
        default, so a setting written once is pinned for ever - which is how a
        reranker measured 9.2x faster shipped and reached no machine. One button
        for all of them, and a right-click on any control for one; see
        `widgets/defaults.py`. Both send None, which removes the line.
        """
        self.environment = EnvironmentBox(settings)
        self.environment.recording_toggled.connect(self.debug_recording_toggled)
        self.restore_defaults = restore_button(
            self, getattr(settings, "env_file", None),
            lambda values: self.settings_changed.emit(values))
        attach_resets(self, lambda values: self.settings_changed.emit(values))

    # -- roots ----------------------------------------------------------------

    def set_roots(
        self, roots: list[str], modes: Optional[dict] = None,
        cloud_content: Optional[set] = None, first: Optional[list] = None,
    ) -> None:
        self.roots_box.set_roots(roots, modes, cloud_content, first)

    def add_root(self, folder: str) -> bool:
        return self.roots_box.add_root(folder)

    def current_modes(self) -> dict:
        return self.roots_box.current_modes()

    def current_cloud_content_roots(self) -> set:
        return self.roots_box.current_cloud_content_roots()

    def current_roots(self) -> list[str]:
        return self.roots_box.current_roots()

    def current_first_folders(self) -> list[str]:
        return self.roots_box.current_first_folders()

    def _make_client(self):
        """A fresh OllamaClient against whatever URL is currently in the box.

        Built per call so editing the URL takes effect without a restart, and so
        a probe never holds a reference to a client the rest of the app is also
        using.
        """
        from app.llm.ollama import OllamaClient

        url = self.ollama_url.text().strip() or self._settings.ollama_url
        return OllamaClient(url, self._settings.ollama_model)

    # -- the two labels that need the store -----------------------------------

    def refresh_slow_labels(self) -> None:
        """Fill in both labels that need the store or an import. **Worker.**

        Called from `shell._start_background_work`, which is the first moment
        anything may touch a thread or the store. The last-open category rides
        the same moment, for the same reason - see `_restore_last_category`.
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

    # -- the five categories ----------------------------------------------------

    def _build_categories(self, pst_box: QGroupBox, behaviour: QGroupBox,
                          privacy: QGroupBox) -> None:
        """Assemble the five shelves. Every box is built exactly as it always
        was; only where it lands changed - see the order's section 1a for which
        box joined which category and why.

        Theme relocates to Appearance verbatim (settled decision #2): the
        control already lives in `WindowBox`, so this is only where the box now
        sits, not a second move.
        """
        shelves = (
            (CATEGORY_WHATS_INDEXED, (self.roots_box, self.code_types, pst_box,
                                      behaviour, self.file_types)),
            (CATEGORY_SEARCH, (self.search_box, self.search_behaviour,
                               self.editor_box, privacy)),
            (CATEGORY_MODELS, (self.models, self.photo_people_box,
                               self.chat_box, self.media_box)),
            (CATEGORY_APPEARANCE, (self.window_box,)),
        )
        for name, boxes in shelves:
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(0, 0, 0, 0)
            for widget in boxes:
                page_layout.addWidget(widget)
            page_layout.addStretch(1)
            self._nav.add_category(name, page)

        storage = QWidget()
        storage_layout = QVBoxLayout(storage)
        storage_layout.setContentsMargins(0, 0, 0, 0)
        storage_layout.addWidget(self.storage_box)
        storage_layout.addWidget(self.activity)
        storage_layout.addWidget(self.environment, stretch=1)
        storage_layout.addWidget(self.restore_defaults)
        self._nav.add_category(CATEGORY_STORAGE, storage)

    def _category_selected(self, name: str) -> None:
        """A click, not a restore - see `CategoryNav.show_category`'s
        `persist` argument. Synchronous, like every other small UI-state
        write in this window (`ui:theme`, `ui:pst_backend`, ...) - a single
        keyed upsert, not the kind of store work M13 exists to keep off the
        UI thread."""
        # Correction, 2026-09-27 (bug 3a): no longer synchronous. A keyed
        # upsert is cheap to run but not to wait for - it takes the store's
        # write lock, which an index batch holds - so it is queued on the
        # ordered state writer like every other small UI-state write now.
        save_state(self._store, CATEGORY_STATE_KEY, name, component="ui.settings")

    def _restore_last_category(self) -> None:
        """Which category was open last, read off the UI thread.

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

    # -- the filter box -----------------------------------------------------------

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
