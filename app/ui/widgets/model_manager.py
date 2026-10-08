r"""Settings, Models: every model on this computer and every one Leasha can run.

Layer: L5 view - thin. What a model *is* is `app/ort/catalogue.py`; what is on
disk and in use is `app/ort/inventory.py`; the Hugging Face list is
`app/ort/discover.py`; downloads are `model_fetch` through `DownloadRow`. Every
disk look, removal and network call runs on a worker; nothing runs by itself.

Owner, 2026-09-30: "a way to delete the model ... how do you add if new models
are released ... a mechanism to manage onnx models ... the models we know and
have tested ... make them default ... a button to update local list from
hugging face". Two lists:

* **On this computer** - what it is for, which copy, its size, and whether it is
  in use. Delete, and one button for everything nothing uses.
* **Available** - the verified models first, then what "Update the list from
  Hugging Face" found (filterable). Download, and "Use this" to make a
  downloaded model the one a job uses. "Use recommended models" puts every
  job back on the verified choice.

Removal is permanent and says so first; a model in use says what stops.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QObject, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (QAbstractItemView, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit, QMessageBox, QPushButton, QTableWidget,
                             QTableWidgetItem, QVBoxLayout)

from app.ui.widgets.model_download import DownloadRow
from app.ui.workers import CallableWorker, run

__all__ = ["ModelManagerBox"]

INTRO = ("The models Leasha runs itself. Recommended ones were checked on a real computer; "
         "others are listed so you can try them, marked 'not checked'. Nothing downloads or "
         "goes online unless you press a button here.")


class _Relay(QObject):
    """Carries discovery progress from the worker thread to the window's thread."""
    said = Signal(str)


def _cell(text: str, data: Any = None) -> QTableWidgetItem:
    """A read-only table cell, carrying `data` under `UserRole` when given."""
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if data is not None:
        item.setData(Qt.ItemDataRole.UserRole, data)
    return item


class ModelManagerBox(QGroupBox):
    """The Models box. Every disk look, removal and network call is a worker;
    `_scanned`, `_removed` and `_listed` paint on the UI thread.
    """
    #: Emitted after anything changed on disk or in the choices.
    models_changed = Signal()

    def __init__(self, settings: Any = None, parent: Any = None) -> None:
        super().__init__("Models on this computer", parent)
        self.setObjectName("modelManager")
        self._settings = settings
        self._items: list = []
        self._available: list = []
        self._asked = False
        self._busy = False
        self._note = ""            # a result sentence kept across the refresh after it
        self._relay = _Relay(self)
        self._relay.said.connect(self._say)

        intro = QLabel(INTRO)
        intro.setWordWrap(True)

        self.installed = QTableWidget(0, 5)
        self.installed.setObjectName("modelsInstalled")
        self.installed.setHorizontalHeaderLabels(["Model", "For", "Copy", "Size", "Status"])
        self._table_look(self.installed)
        self.installed.itemSelectionChanged.connect(self._selection_changed)

        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("modelsDelete")
        self.delete_button.setToolTip("Remove the chosen model from this computer. Permanent - "
                                      "it can be downloaded again from this list.")
        self.delete_button.clicked.connect(lambda _c=False: self._delete_selected())
        self.clean_button = QPushButton("Remove copies nothing uses")
        self.clean_button.setObjectName("modelsClean")
        self.clean_button.setToolTip("Remove every downloaded model that no job uses, after "
                                     "asking. Models in use are never touched; anything removed "
                                     "can be downloaded again.")
        self.clean_button.clicked.connect(lambda _c=False: self._remove_unused())
        self.recommended_button = QPushButton("Use recommended models")
        self.recommended_button.setObjectName("modelsRecommended")
        self.recommended_button.setToolTip("Every job goes back to the model checked on a real "
                                           "computer. Missing ones are offered for download.")
        self.recommended_button.clicked.connect(lambda _c=False: self._use_recommended())
        local = QHBoxLayout()
        for button in (self.delete_button, self.clean_button, self.recommended_button):
            local.addWidget(button)
        local.addStretch(1)

        available_label = QLabel("Available")
        self.filter = QLineEdit()
        self.filter.setObjectName("modelsFilter")
        self.filter.setPlaceholderText("Filter, e.g. whisper, french, qwen, gemma")
        self.filter.textChanged.connect(lambda _t: self._fill_available())
        self.available = QTableWidget(0, 5)
        self.available.setObjectName("modelsAvailable")
        self.available.setHorizontalHeaderLabels(["Model", "For", "Size", "Checked", "Licence"])
        self._table_look(self.available)
        self.available.itemSelectionChanged.connect(self._available_changed)
        self.description = QLabel("")
        self.description.setWordWrap(True)
        self.description.setObjectName("modelsDescription")

        self.download = DownloadRow("onnx")
        self.download.finished.connect(lambda _n, _r: self.refresh())
        self.use_button = QPushButton("Use this")
        self.use_button.setObjectName("modelsUse")
        self.use_button.setToolTip("Make the chosen downloaded model the one its job uses. "
                                   "Chat picks it up the next time Leasha starts.")
        self.use_button.clicked.connect(lambda _c=False: self._use_selected())
        self.update_button = QPushButton("Update the list from Hugging Face")
        self.update_button.setObjectName("modelsUpdateList")
        self.update_button.setToolTip("Asks Hugging Face which models Leasha can run today "
                                      "(lists only - nothing is downloaded). Takes a few minutes "
                                      "on a slow connection.")
        self.update_button.clicked.connect(lambda _c=False: self._update_list())
        actions = QHBoxLayout()
        actions.addWidget(self.download, 1)
        actions.addWidget(self.use_button)
        actions.addWidget(self.update_button)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setObjectName("modelsStatus")

        column = QVBoxLayout(self)
        column.addWidget(intro)
        column.addWidget(self.installed)
        column.addLayout(local)
        column.addWidget(available_label)
        column.addWidget(self.filter)
        column.addWidget(self.available)
        column.addWidget(self.description)
        column.addLayout(actions)
        column.addWidget(self.status)
        self.download.set_model_cache(getattr(settings, "model_cache", None))
        self.delete_button.setEnabled(False)
        self.use_button.setEnabled(False)

    @staticmethod
    def _table_look(table: QTableWidget) -> None:
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.verticalHeader().setVisible(False)
        table.setShowGrid(False)             # 2026-10-05: as `ResultTable`
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.setMinimumHeight(160)

    # -- loading (worker) ----------------------------------------------------------

    def showEvent(self, event: Any) -> None:            # noqa: N802 - Qt's name
        super().showEvent(event)
        if not self._asked:
            self.refresh()

    def refresh(self) -> None:
        """Scan the model folder and the catalogue on a worker; `_scanned` draws it."""
        if self._busy:
            return
        self._asked = True
        self._say("Looking at the model folder...")
        worker = CallableWorker(self._scan, component="ui.models.manager")
        worker.signals.finished.connect(self._scanned)
        worker.signals.failed.connect(lambda e: self._say(f"Could not read the model folder: {e}"))
        run(QThreadPool.globalInstance(), worker)

    def _scan(self) -> tuple[list, list]:
        """Worker thread: what is on disk, and what the catalogue offers that is not."""
        from app.ort import catalogue, inventory

        cache = getattr(self._settings, "model_cache", None)
        items = inventory.scan(cache, self._settings)
        cat = catalogue.load(getattr(self._settings, "state_path", None))
        on_disk = {i.key for i in items if i.present}
        offer = [e for e in cat.entries if e.offered and e.key not in on_disk]
        offer.sort(key=lambda e: (not e.is_verified, e.job, -e.downloads, e.key))
        return items, offer

    def _scanned(self, result: Any) -> None:
        """UI thread: redraw both tables from the scan."""
        self._items, self._available = result
        present = [i for i in self._items if i.present]
        self.installed.setRowCount(0)
        for item in sorted(present, key=lambda i: (not i.in_use, i.job, -i.bytes)):
            row = self.installed.rowCount()
            self.installed.insertRow(row)
            status = "in use" if item.in_use else ("not used - " + item.note if item.note
                                                     else "not used")
            for col, text in enumerate((item.label, item.job_words, item.copy,
                                        item.size_words, status)):
                self.installed.setItem(row, col, _cell(text, item.key if col == 0 else None))
        from app.ort import inventory

        spare = inventory.unused(self._items)
        gb = sum(i.bytes for i in spare) / 2 ** 30
        self.clean_button.setText(f"Remove copies nothing uses ({gb:.1f} GB)" if spare
                                  else "Remove copies nothing uses")
        self.clean_button.setEnabled(bool(spare))
        self._fill_available()
        total = sum(i.bytes for i in present) / 2 ** 30
        counts = (f"{len(present)} models on this computer, {total:.1f} GB. "
                  f"{len(self._available)} more can be downloaded.")
        self._say(f"{self._note} {counts}".strip())
        self._note = ""

    def _fill_available(self) -> None:
        """Redraw the Available table through the filter box's words."""
        words = [w for w in self.filter.text().lower().split() if w]
        self.available.setRowCount(0)
        for entry in self._available:
            haystack = f"{entry.key} {entry.label} {entry.description} {entry.job}".lower()
            if words and not all(w in haystack for w in words):
                continue
            row = self.available.rowCount()
            self.available.insertRow(row)
            size = (f"{entry.approx_mb / 1024:.1f} GB" if entry.approx_mb >= 1024
                    else f"{entry.approx_mb} MB")
            checked = "recommended" if entry.is_verified else "not checked"
            from app.ort.inventory import JOB_WORDS

            for col, text in enumerate((entry.label, JOB_WORDS.get(entry.job, entry.job),
                                        size, checked, entry.licence)):
                self.available.setItem(row, col, _cell(text, entry.key if col == 0 else None))

    # -- selection -------------------------------------------------------------------

    def _selected_item(self) -> Optional[Any]:
        rows = self.installed.selectionModel().selectedRows() if self.installed.selectionModel() else []
        if not rows:
            return None
        key = self.installed.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        return next((i for i in self._items if i.key == key), None)

    def _selected_entry(self) -> Optional[Any]:
        rows = self.available.selectionModel().selectedRows() if self.available.selectionModel() else []
        if not rows:
            return None
        key = self.available.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        return next((e for e in self._available if e.key == key), None)

    def _selection_changed(self) -> None:
        item = self._selected_item()
        self.delete_button.setEnabled(item is not None and not self._busy)
        self.use_button.setEnabled(item is not None and item.job in ("photo", "speech", "chat")
                                   and not item.key.startswith(("folder:", "partial")))

    def _available_changed(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            return
        verified = entry.verified or {}
        line = entry.description or entry.label
        if verified:
            line += f" Checked {verified.get('on', '')}: {verified.get('result', '')}"
        self.description.setText(line)
        self.download.set_target(entry.key)

    # -- actions ---------------------------------------------------------------------

    def _delete_selected(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        stops = (f"\n\nIt is in use: {item.job_words} will stop until a model for it is "
                 "downloaded again." if item.in_use else "")
        answer = QMessageBox.question(
            self, "Delete a model",
            f"Delete {item.label} ({item.size_words}) from this computer?{stops}\n\n"
            "This is permanent - it does not go to the Recycle Bin. It can be downloaded "
            "again from this list.")
        if answer == QMessageBox.StandardButton.Yes:
            self._run_removal([item], force=item.in_use)

    def _remove_unused(self) -> None:
        from app.ort import inventory

        spare = inventory.unused(self._items)
        if not spare:
            return
        lines = "\n".join(f"  {i.label} - {i.size_words}" for i in spare[:12])
        more = f"\n  ...and {len(spare) - 12} more" if len(spare) > 12 else ""
        gb = sum(i.bytes for i in spare) / 2 ** 30
        answer = QMessageBox.question(
            self, "Remove copies nothing uses",
            f"Remove these {len(spare)} items ({gb:.1f} GB)? None of them is in use.\n\n"
            f"{lines}{more}\n\nThis is permanent; any of them can be downloaded again.")
        if answer == QMessageBox.StandardButton.Yes:
            self._run_removal(spare, force=False)

    def _run_removal(self, items: list, *, force: bool) -> None:
        """Delete `items` on a worker. `force` removes a model that is in use."""
        self._busy = True
        self.delete_button.setEnabled(False)
        self.clean_button.setEnabled(False)
        self._say("Removing...")

        def work() -> int:
            from app.ort import inventory

            return sum(inventory.remove(i, force=force) for i in items)

        worker = CallableWorker(work, component="ui.models.manager")
        worker.signals.finished.connect(self._removed)
        worker.signals.failed.connect(self._removal_failed)
        run(QThreadPool.globalInstance(), worker)

    def _removed(self, freed: Any) -> None:
        self._busy = False
        self._note = f"Removed. {int(freed or 0) / 2 ** 30:.1f} GB freed."
        self.models_changed.emit()
        self.refresh()

    def _removal_failed(self, error: Any) -> None:
        self._busy = False
        self._say(f"Could not remove everything: {getattr(error, 'message', error)}. "
                  "A file in use by Leasha is freed when it is closed; try again then.")
        self.refresh()

    def _use_selected(self) -> None:
        """Make the selected model its job's choice. Writes the small choices file in
        Leasha's state folder on the UI thread - a few bytes, not user data.
        """
        from app.ort import catalogue

        item = self._selected_item()
        if item is None:
            return
        catalogue.choose(item.job, item.key, getattr(self._settings, "state_path", None))
        self._note = (f"{item.label} is now used for {item.job_words}"
                      + (" from the next time Leasha starts." if item.job == "chat" else "."))
        self.models_changed.emit()
        self.refresh()

    def _use_recommended(self) -> None:
        from app.ort import catalogue

        state = getattr(self._settings, "state_path", None)
        for job in ("photo", "speech", "chat"):
            catalogue.choose(job, "", state)
        missing = [e for e in self._available if e.is_verified]
        note = ""
        if missing:
            note = (" Not downloaded yet: " + ", ".join(e.label for e in missing[:4])
                    + " - choose one under Available and press Download.")
        embed = str(getattr(self._settings, "embed_model", "") or "")
        if embed and embed != "BAAI/bge-small-en-v1.5":
            note += (" The meaning model is " + embed + ", not the recommended "
                     "BAAI/bge-small-en-v1.5; changing it re-reads every file, so it is "
                     "left for you to do with 'Change the meaning model...'.")
        self._note = "Every job is back on its recommended model." + note
        self.models_changed.emit()
        self.refresh()

    def _update_list(self) -> None:
        """Ask Hugging Face for the catalogue on a worker; progress comes through the relay."""
        if self._busy:
            return
        self._busy = True
        self.update_button.setEnabled(False)
        relay = self._relay
        state = getattr(self._settings, "state_path", None)

        def work() -> str:
            from pathlib import Path

            from app.ort import discover

            def said(text: str) -> None:
                try:
                    relay.said.emit(text)
                # The relay's C++ side has gone (the box closed): the progress line
                # has nowhere to go, and discovery itself still finishes.
                except RuntimeError:
                    pass

            _count, sentence = discover.discover(Path(state), progress=said)
            return sentence

        worker = CallableWorker(work, component="ui.models.discover")
        worker.signals.finished.connect(self._listed)
        worker.signals.failed.connect(self._listed)
        run(QThreadPool.globalInstance(), worker)

    def _listed(self, sentence: Any) -> None:
        """UI thread: discovery ended (or raised); its sentence is kept across the refresh."""
        self._busy = False
        self.update_button.setEnabled(True)
        self._note = str(getattr(sentence, "message", sentence))
        self.refresh()

    def _say(self, text: str) -> None:
        self.status.setText(text)
