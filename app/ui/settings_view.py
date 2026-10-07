"""Settings: index roots, exclusions, the rerank toggle, and the usage log.

Layer: L5

**Six sections, and two of them live elsewhere.** `IndexingSettings` holds the
schedule and the resource ceilings; `EnvironmentBox` holds doctor and session
recording. **"Clear search history" exists because the usage log exists**: a
record of what someone searched on their own machine is theirs to inspect and
erase, and a system that collects it with no way to clear it is not one to trust.

Everything that holds no wording - building the boxes, the five category
shelves, the filter, the store round-trips - is in `widgets/settings_shelves.py`
(`SettingsShelves`), which this class mixes in. The wording and the signals stay
here, on purpose: `test_pages_reorg.py` reads every label and tooltip in *this*
file against the commit before the reorganisation, and
`test_settings_reachable.py` reads the signals declared in it.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QThreadPool, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QLabel, QLineEdit,
    QPushButton, QVBoxLayout, QWidget,
)

from app.core.logging import logger
from app.ui.widgets.category_nav import CategoryNav
from app.ui.widgets.chat_roles import ModelCombo
from app.ui.widgets.vision_model import VisionModelField
from app.ui.widgets.settings_shelves import (  # noqa: F401 - re-exported for callers
    CATEGORY_APPEARANCE, CATEGORY_MODELS, CATEGORY_SEARCH, CATEGORY_STATE_KEY,
    CATEGORY_STORAGE, CATEGORY_WHATS_INDEXED, SettingsShelves,
)

__all__ = ["SettingsView"]

_log = logger.bind(component="ui.settings")


class SettingsView(SettingsShelves, QWidget):
    roots_changed = Signal(list)
    #: `{normalised root: "live"|"archive"}` - which folders never change.
    #: See `app/index/archives.py`; the saving this buys on a settled corpus is
    #: hours per incremental run.
    root_modes_changed = Signal(dict)
    #: `{normalised root}` opted in to cloud content indexing (202626270514
    #: §2b). See `RootsBox.cloud_content_changed` - this only relays it.
    cloud_content_roots_changed = Signal(set)
    #: 2026-09-29. "Index this folder first", in order. See
    #: `RootsBox.first_changed` - this only relays it.
    first_folders_changed = Signal(list)
    #: "Rescan archived folders now" - one full walk, not a change of policy.
    rescan_archives_requested = Signal()
    #: 2026-10-02. "Index now" on one line of the folder list: that folder.
    #: See `RootsBox.index_requested` - this only relays it.
    index_folder_requested = Signal(str)
    #: 2026-10-07. Remove on the folder list: these folders, to be asked about
    #: and taken out of the index. See `RootsBox.remove_requested`.
    remove_folders_requested = Signal(list)
    #: "Remove them from the index" under the list. See `RootsBox.set_leftovers`.
    remove_leftovers_requested = Signal()
    #: `(preset, groups)` for the Code tab's file-type filter. A view
    #: preference: it changes what Code lists, never what is indexed.
    code_types_changed = Signal(str, list)
    pst_backend_changed = Signal(str)
    #: 2026-10-07, the owner: each mail archive read its own way, and read
    #: again. See `MailArchivesBox` (widgets/mail_archives_box.py) - these
    #: only relay it: `(archive, backend)`, then the archive for each button.
    mail_archive_choice_changed = Signal(str, str)
    mail_archive_read_again_requested = Signal(str)
    mail_archive_clear_requested = Signal(str)
    #: (enabled, model, timeout_s) for the Interpret button.
    ollama_model_changed = Signal(bool, str, int)
    convert_pst_requested = Signal(str, str)   # archive, destination
    #: The index-location flow: move it, adopt one already there, or start
    #: empty. A signal rather than a direct call because the shell owns the
    #: stores that would have to be closed before anything moves.
    move_index_requested = Signal()
    #: The meaning-model flow. Same shape and the same reason: it invalidates
    #: every vector, so it states the cost and confirms rather than applying.
    rebuild_vectors_requested = Signal()
    rerank_toggled = Signal(bool)

    cloud_toggled = Signal(bool)
    #: (minimise_to_tray, close_to_tray)
    tray_changed = Signal(bool, bool)
    #: system | light | dark. Appearance moved here from the indexing
    #: panel, where it was neither an indexing setting nor findable by
    #: anybody looking for one. The name is unchanged, so the window's
    #: handler did not have to move with it.
    theme_changed = Signal(str)
    #: `{registry key: value}` from any panel whose controls write `.env`.
    settings_changed = Signal(dict)
    history_cleared = Signal(int)
    debug_recording_toggled = Signal(bool)
    #: Order 0j section 2: the doorway to the Photo Tagger window.
    open_photo_tagger_requested = Signal()
    error = Signal(object)

    def __init__(self, settings: Any, store: Any = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._store = store
        self._build_boxes(settings)

        self.cloud = QCheckBox("Index cloud-only files (downloads them)")
        self.cloud.setToolTip(
            "OneDrive and SharePoint keep placeholders on disk. Reading one downloads the whole "
            "file, so pointing the indexer at a synced library with this on can pull down "
            "everything. Off by default for that reason."
        )
        self.cloud.stateChanged.connect(lambda _s: self.cloud_toggled.emit(self.cloud.isChecked()))

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
        # 2026-10-05: said when archives go through Outlook although reading
        # them directly is available - see `show_pst_backend`.
        self.pst_note = QLabel("")
        self.pst_note.setObjectName("noticeBar")
        self.pst_note.setWordWrap(True)
        self.pst_note.setVisible(False)
        self._pst_direct_available = False
        self.pst_backend.currentIndexChanged.connect(lambda _i: self._refresh_pst_note())
        convert = QPushButton("Convert a .pst to .eml files…")
        convert.setToolTip(
            "Exports an archive to a folder of .eml files. Afterwards the mail needs neither "
            "Outlook nor libpff - it is just files, which any mail client can open."
        )
        convert.clicked.connect(self._convert_pst)

        pst_box = self.pst_box = QGroupBox("Outlook archives (.pst)")
        pst_layout = QVBoxLayout(pst_box)
        pst_layout.addWidget(QLabel("How to read archives:"))
        pst_layout.addWidget(self.pst_backend)
        pst_layout.addWidget(self.pst_status)
        pst_layout.addWidget(self.pst_note)
        pst_layout.addWidget(convert)

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

        privacy = QGroupBox("Search history")
        privacy_layout = QVBoxLayout(privacy)
        privacy_layout.addWidget(QLabel(
            "Searches and which results you opened are recorded locally, so the ranking can be "
            "tuned to your documents later. Nothing leaves this machine."
        ))
        privacy_layout.addWidget(self.history_label)
        privacy_layout.addWidget(clear)

        # --- work order 0i section 3 / 0j: photo descriptions and people ---
        #
        # Plain controls, not a bespoke box like ModelBox above - none of
        # the three needs a live probe or a Test button, so a QGroupBox with
        # setObjectName-tagged fields plus the same
        # `self.settings_changed.emit({KEY: value})` shape `url_changed`
        # already uses just above is the whole of what non-negotiable #11
        # asks for here.
        self.photo_people_box = QGroupBox("People and photo descriptions")
        # Owner, 2026-09-29: a drop-down, never a text box, for any model. The
        # same control the Chat roles grid uses for this setting, with a
        # Download menu under it - see `widgets/vision_model.py`.
        self.vision_model = ModelCombo("OLLAMA_VISION_MODEL", vision=True,
                                       automatic="Automatic (llava)")
        self.vision_model.setObjectName("OLLAMA_VISION_MODEL")
        self.vision_model.setPlaceholderText("llava")
        self.vision_model.setToolTip(
            "The Ollama model that answers Describe on a photo. Needs a "
            "vision-capable model - llava or qwen2.5vl are common choices."
        )
        self.vision_field = VisionModelField(
            self.vision_model, url=str(getattr(settings, "ollama_url", "") or ""),
            saved=str(getattr(settings, "ollama_vision_model", "") or ""))
        from app.llm.engines import engine_of
        self.vision_field.set_engine(engine_of(settings), getattr(settings, "model_cache", None))
        self.vision_model.activated.connect(
            lambda _i: self.settings_changed.emit(
                {"OLLAMA_VISION_MODEL": self.vision_model.value()}))

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
        self.name_people_button = QPushButton("Name the people in your photos…")
        self.name_people_button.setToolTip(
            "Opens the window where Leasha shows the groups of similar faces "
            "it has found, so you can give each one a name. Nothing is named "
            "until you name it, and names never leave this computer. Empty "
            "until the setting above is on and your photos have been indexed.")
        self.name_people_button.clicked.connect(
            lambda _c=False: self.open_photo_tagger_requested.emit())
        photo_people_form.addRow(self.name_people_button)
        photo_people_form.addRow(self.caption_trickle)
        photo_people_form.addRow("Photo description model", self.vision_field)

        self._build_late_boxes(settings)

        # --- The filter box, and the five categories --------------------------
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
        self.filter_elsewhere = QLabel("")             # see `_apply_filter`
        self.filter_elsewhere.setObjectName("settingsFilterElsewhere")
        self.filter_elsewhere.setWordWrap(True)
        self.filter_elsewhere.setVisible(False)

        self._nav = CategoryNav()
        self._nav.category_changed.connect(self._category_selected)
        self._build_categories(pst_box, behaviour, privacy)

        layout = QVBoxLayout(self)
        layout.addWidget(self.filter_box)
        layout.addWidget(self.filter_empty)
        layout.addWidget(self.filter_elsewhere)
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

