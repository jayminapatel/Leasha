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

import json
import subprocess
import sys
from typing import Any, Optional

from PyQt6.QtCore import pyqtSignal
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

from app.ui.indexing_settings import IndexingSettings

__all__ = ["SettingsView"]


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
        run_doctor = QPushButton("Run doctor")
        run_doctor.clicked.connect(self._run_doctor)

        doctor_box = QGroupBox("Environment")
        doctor_layout = QVBoxLayout(doctor_box)
        doctor_layout.addWidget(run_doctor)
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
        if self._store is None:
            return
        try:
            count = len(self._store.recent_searches(limit=100_000))
        except Exception:                        # noqa: BLE001
            return
        self.history_label.setText(f"{count:,} searches recorded.")

    def _clear_history(self) -> None:
        if self._store is None:
            return
        try:
            removed = self._store.clear_usage_log()
        except Exception:                        # noqa: BLE001
            return
        self.history_label.setText(f"Cleared. {removed:,} searches removed.")
        self.history_cleared.emit(removed)

    # -- doctor -------------------------------------------------------------

    def _run_doctor(self) -> None:
        from app.core.config import project_root

        doctor = project_root() / "doctor.py"
        self.doctor_output.setPlainText("Running…")
        try:
            finished = subprocess.run(
                [sys.executable, str(doctor), "--json", "--quick"],
                capture_output=True, text=True, timeout=120, check=False,
            )
            report = json.loads(finished.stdout)
        except Exception as exc:                 # noqa: BLE001
            self.doctor_output.setPlainText(f"Could not run doctor.py: {exc}")
            return

        lines = ["READY" if report.get("ready") else "NOT READY", ""]
        for check in report.get("checks", []):
            mark = "PASS" if check["ok"] else ("WARN" if check.get("optional") else "FAIL")
            lines.append(f"[{mark}] {check['name']}  {check.get('detail', '')}")
            if not check["ok"] and check.get("fix"):
                lines.append(f"       FIX: {check['fix']}")
        self.doctor_output.setPlainText("\n".join(lines))
