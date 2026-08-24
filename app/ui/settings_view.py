"""Settings: index roots, exclusions, the rerank toggle, and the usage log.

Layer: L5

Two things here are not conveniences.

**"Run doctor" renders `doctor.py --json` inline.** When something is wrong, the
answer is already written - every check states what failed and how to fix it -
and making the person find a terminal to see it wastes the work.

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
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.logging import logger
from app.ui.indexing_settings import IndexingSettings
from app.ui.presenter import doctor_lines, doctor_report

__all__ = ["SettingsView"]

_log = logger.bind(component="ui.settings")


class SettingsView(QWidget):
    roots_changed = pyqtSignal(list)
    pst_backend_changed = pyqtSignal(str)
    convert_pst_requested = pyqtSignal(str, str)   # archive, destination
    rerank_toggled = pyqtSignal(bool)

    cloud_toggled = pyqtSignal(bool)
    history_cleared = pyqtSignal(int)

    def __init__(self, settings: Any, store: Any = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._store = store
        self._doctor_running = False

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
        self.rerank = QCheckBox("Rerank results (slower, more precise)")
        self.rerank.setChecked(bool(getattr(settings, "rerank_enabled", True)))
        self.rerank.stateChanged.connect(lambda _s: self.rerank_toggled.emit(self.rerank.isChecked()))

        self.cloud = QCheckBox("Index cloud-only files (downloads them)")
        self.cloud.setToolTip(
            "OneDrive and SharePoint keep placeholders on disk. Reading one downloads the whole "
            "file, so pointing the indexer at a synced library with this on can pull down "
            "everything. Off by default for that reason."
        )
        self.cloud.stateChanged.connect(lambda _s: self.cloud_toggled.emit(self.cloud.isChecked()))

        self.data_path = QLineEdit(str(getattr(settings, "data_path", "")))
        self.data_path.setReadOnly(True)
        self.ollama_url = QLineEdit(str(getattr(settings, "ollama_url", "")))
        self.ollama_url.setReadOnly(True)

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

        behaviour = QGroupBox("Behaviour")
        form = QFormLayout(behaviour)
        form.addRow(self.rerank)
        form.addRow(self.cloud)
        form.addRow("Index location", self.data_path)
        form.addRow("Ollama (optional)", self.ollama_url)

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

        # --- doctor
        self.doctor_output = QPlainTextEdit()
        self.doctor_output.setReadOnly(True)
        self.doctor_output.setPlaceholderText("Run doctor to check the environment.")
        self.run_doctor_button = QPushButton("Run doctor")
        self.run_doctor_button.clicked.connect(lambda _checked=False: self._run_doctor())

        doctor_box = QGroupBox("Environment")
        doctor_layout = QVBoxLayout(doctor_box)
        doctor_layout.addWidget(self.run_doctor_button)
        doctor_layout.addWidget(self.doctor_output)

        layout = QVBoxLayout(self)
        layout.addWidget(roots_box)
        layout.addWidget(self.indexing)
        layout.addWidget(pst_box)
        layout.addWidget(behaviour)
        layout.addWidget(privacy)
        layout.addWidget(doctor_box, stretch=1)

        self.refresh_history_count()
        self.refresh_pst_status()

    # -- roots --------------------------------------------------------------

    def set_roots(self, roots: list[str]) -> None:
        self.roots.clear()
        self.roots.addItems(roots)

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

    # -- doctor -------------------------------------------------------------

    def _run_doctor(self) -> None:
        """Run doctor.py in a worker and render its JSON when it comes back.

        **This was the frozen window.** It called `subprocess.run(timeout=120)`
        directly here, on the UI thread. Doctor probes Outlook over COM, opens
        LanceDB and may load an ONNX model - seconds at best, and the timeout
        says two minutes is possible. For all of that the event loop is not
        running, so the window does not repaint, Windows paints "Not Responding"
        over it, and Ctrl+C in the terminal does nothing because Python never
        gets a chance to see the signal. End Task is the only way out, and from
        the outside it is indistinguishable from a crash.

        Nothing about the check needed to be synchronous. It just looked
        harmless, which is how UI-thread I/O usually gets written.
        """
        from app.ui.workers import CallableWorker, run

        if self._doctor_running:
            return                               # a second click would run it twice
        self._doctor_running = True
        self.run_doctor_button.setEnabled(False)
        self.doctor_output.setPlainText("Running…")

        worker = CallableWorker(doctor_report, component="ui.doctor")
        worker.signals.finished.connect(self._doctor_done)
        worker.signals.failed.connect(
            lambda error: self.doctor_output.setPlainText(
                f"{getattr(error, 'message', error)}\n\n{getattr(error, 'suggestion', '')}"
            )
        )
        worker.signals.done.connect(self._doctor_finished)
        run(QThreadPool.globalInstance(), worker)

    def _doctor_finished(self) -> None:
        self._doctor_running = False
        self.run_doctor_button.setEnabled(True)

    def _doctor_done(self, report: dict) -> None:
        self.doctor_output.setPlainText("\n".join(doctor_lines(report)))
