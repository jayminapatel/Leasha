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

__all__ = ["SettingsView"]


class SettingsView(QWidget):
    roots_changed = pyqtSignal(list)
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
        layout.addWidget(behaviour)
        layout.addWidget(privacy)
        layout.addWidget(doctor_box, stretch=1)

        self.refresh_history_count()

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
