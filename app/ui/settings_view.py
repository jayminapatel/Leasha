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

from app.core.logging import logger
from app.ui.indexing_settings import IndexingSettings
from app.ui.widgets.environment_box import EnvironmentBox
from app.ui.widgets.file_types import FileTypesEditor
from app.ui.widgets.model_box import ModelBox
from app.ui.widgets.search_box import SearchBox
from app.ui.widgets.storage_box import StorageBox
from app.ui.widgets.window_box import WindowBox

__all__ = ["SettingsView"]

_log = logger.bind(component="ui.settings")


class SettingsView(QWidget):
    roots_changed = pyqtSignal(list)
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
        self.roots = QListWidget()
        add = QPushButton("Add folder…")
        add.clicked.connect(self._add_root)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove_root)

        root_buttons = QHBoxLayout()
        root_buttons.addWidget(add)
        root_buttons.addWidget(remove)
        root_buttons.addStretch(1)

        roots_box = QGroupBox("Folders to index")
        roots_layout = QVBoxLayout(roots_box)
        roots_layout.addWidget(self.roots)
        roots_layout.addLayout(root_buttons)

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
        self.required_free_gb = self.storage_box.required_free_gb
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

        self.indexing = IndexingSettings()

        # Window behaviour is its own group - see widgets/window_box.py for why
        # both of these were unreachable until now.
        self.window_box = WindowBox()
        self.minimise_to_tray = self.window_box.minimise_to_tray
        self.close_to_tray = self.window_box.close_to_tray
        self.window_box.changed.connect(self.tray_changed)

        behaviour = QGroupBox("Behaviour")
        form = QFormLayout(behaviour)
        form.addRow(self.cloud)


        # --- privacy
        self.history_label = QLabel("")
        clear = QPushButton("Clear search history")
        clear.clicked.connect(self._clear_history)

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

        # --- environment and diagnostics (its own widget; see the module)
        self.environment = EnvironmentBox(settings)
        self.environment.recording_toggled.connect(self.debug_recording_toggled)

        layout = QVBoxLayout(self)
        layout.addWidget(roots_box)
        layout.addWidget(self.indexing)
        layout.addWidget(pst_box)
        layout.addWidget(self.search_box)
        layout.addWidget(behaviour)
        layout.addWidget(self.window_box)
        layout.addWidget(self.storage_box)
        layout.addWidget(privacy)
        layout.addWidget(self.models)
        layout.addWidget(self.file_types)
        layout.addWidget(self.environment, stretch=1)

        self.refresh_history_count()
        self.refresh_pst_status()

    # -- roots --------------------------------------------------------------

    def set_roots(self, roots: list[str]) -> None:
        self.roots.clear()
        self.roots.addItems(roots)

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
        return [self.roots.item(i).text() for i in range(self.roots.count())]

    def _add_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder to index")
        if folder and folder not in self.current_roots():
            self.roots.addItem(folder)
            self.roots_changed.emit(self.current_roots())

    def _remove_root(self) -> None:
        for item in self.roots.selectedItems():
            self.roots.takeItem(self.roots.row(item))
        self.roots_changed.emit(self.current_roots())

    # -- Outlook archives ---------------------------------------------------

    def refresh_pst_status(self) -> None:
        """Say plainly which route is available, and what it would cost to add
        the other - a greyed-out option with no explanation is a dead end."""
        from app.extract import pst_libpff

        if pst_libpff.available():
            self.pst_status.setText(
                "Direct reading is available - archives can be indexed without Outlook."
            )
        else:
            self.pst_status.setText(
                "Direct reading is not installed, so archives go through Outlook. "
                "To read them without it: pip install libpff-python "
                "(needs Build Tools for Visual Studio on Windows)."
            )
        self.pst_status.setWordWrap(True)

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
        """Count the usage log without reading it.

        This used to be `len(recent_searches(limit=100_000))` - a hundred
        thousand rows fetched, decoded into dictionaries and thrown away, on the
        UI thread, to produce one number. `COUNT(*)` answers the same question
        from an index, and the store now has a method for it.
        """
        if self._store is None:
            return
        try:
            count = self._store.count_searches()
        except Exception as exc:                 # noqa: BLE001 - a label is not worth failing over
            _log.debug("search history not counted: {}", exc)
            return
        self.history_label.setText(f"{count:,} searches recorded.")

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

