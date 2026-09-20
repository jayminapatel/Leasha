r"""The Offline Media tab: drives in drawers, catalogued once, found forever.

Layer: L5, driving L3

Work order 202626270513 §2. **Fully MANUAL** (the owner's model, stated in
the order's own header): nothing happens to a removable drive unless this
tab's own Scan/Rescan/Delete buttons are pressed. No device watcher, no
arrival prompt, no automatic anything - this view does not even poll; its
list is refreshed only when the tab is shown, the same "refresh on forward,
never on a timer" rule `shell.py` already applies to Mail and Code.

**This view decides nothing about storage.** It shows what it is given
and emits what a person asked for - `scan_requested`, `rescan_requested`,
`delete_requested` - exactly the division `IndexingView` already draws
with `shell.py`: the view owns no store and starts no worker, because
`app/ui/` may call the layers below it and nothing below may import it
back. `shell.py` owns `self._settings`/`self._store` and is the one place
that turns a request into a `CallableWorker` calling
`app.index.offline_media.scan_new_source`/`rescan_source`/`delete_volume`.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, QThreadPool, pyqtSignal
from PyQt6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.ui.presenter import (
    VolumeRow, offline_media_empty_state, offline_media_help_text, volume_rows,
)
from app.ui.widgets.offline_media_dialogs import DeleteVolumeDialog, ScanNameDialog
from app.ui.widgets.result_table import align_headers
from app.ui.workers import CallableWorker, run

__all__ = ["OfflineMediaView", "COLUMNS"]

#: Name | Status | Size | Files | Scanned - 2a's own list, in the order it
#: names them.
COLUMNS = ("Name", "Status", "Size", "Files", "Scanned")

_STATUS_COLUMN = 1


def _status_tooltip(row: VolumeRow) -> str:
    r"""The sentence behind a short status word - kept off the column itself
    (2a's own list stays scannable) and onto the tooltip, the standing rule
    every control in this project follows: state the effect, do not make
    the label carry it.
    """
    if row.status_code == "LOCKED":
        return ("This drive is BitLocker-encrypted and locked. Plug it in "
                "and unlock it in Windows, then Rescan.")
    if row.status_code == "ONLINE":
        return "Plugged in right now - Rescan is available."
    return ("Not currently connected. Plug it back in to Rescan - its "
            "catalogue and everything already indexed from it stay exactly "
            "as they are.")


class OfflineMediaView(QWidget):
    """Scan · Rescan · Delete - and nothing else. 2a-2d."""

    #: (root folder, name, description) - a folder just chosen, named, ready
    #: for its first Scan.
    scan_requested = pyqtSignal(str, str, str)
    rescan_requested = pyqtSignal(int)
    delete_requested = pyqtSignal(int)
    error = pyqtSignal(object)

    def __init__(self, store: Any = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._rows: list[VolumeRow] = []

        intro = QLabel(
            "Drives and folders catalogued for offline search - plug one in "
            "later and its files are found by name and content even while "
            "it is out of reach. Nothing here happens on its own."
        )
        intro.setWordWrap(True)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(list(COLUMNS))
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.tree.setAccessibleName("Offline Media sources")
        align_headers(self.tree, ("left", "left", "right", "right", "left"))
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, len(COLUMNS)):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemSelectionChanged.connect(self._sync_buttons)
        self.tree.itemDoubleClicked.connect(lambda _item, _col: self._rescan_selected())

        self.scan = QPushButton("Scan a drive…")
        self.scan.setToolTip(
            "Choose a drive or folder to catalogue for offline search.\n\n"
            "Nothing is scanned until you choose a folder and confirm a "
            "name - this never runs on its own.")
        self.scan.clicked.connect(lambda _c=False: self._choose_and_scan())

        self.rescan = QPushButton("Rescan")
        self.rescan.setToolTip(
            "Walk this source again for what has changed since its last "
            "Scan.\n\nOnly available while it is plugged in.")
        self.rescan.clicked.connect(lambda _c=False: self._rescan_selected())

        self.delete = QPushButton("Delete")
        self.delete.setToolTip(
            "Remove this source's catalogue from Leasha's index.\n\n"
            "This removes the catalogue only - nothing on the drive itself "
            "is touched, and it can be scanned again at any time.")
        self.delete.clicked.connect(lambda _c=False: self._delete_selected())

        buttons = QHBoxLayout()
        buttons.addWidget(self.scan)
        buttons.addWidget(self.rescan)
        buttons.addWidget(self.delete)
        buttons.addStretch(1)

        self.empty = QLabel(offline_media_empty_state())
        self.empty.setWordWrap(True)

        self.status_line = QLabel("")
        self.status_line.setWordWrap(True)

        # 202626270514 1d + 3b-3: the tab's own help line - network shares
        # and backup formats, neither obvious from the three buttons alone.
        self.help_line = QLabel(offline_media_help_text())
        self.help_line.setWordWrap(True)
        self.help_line.setObjectName("offlineMediaHelp")

        # **Text keeps its own height; the table takes the rest.** With the table
        # hidden (no drive catalogued yet) the four wrapped labels were the only
        # things that could grow, so the page's spare height was shared out
        # between them and the intro, buttons and help sat a screen apart.
        for label in (intro, self.empty, self.status_line, self.help_line):
            label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(buttons)
        layout.addWidget(self.empty)
        layout.addWidget(self.tree, 1)
        layout.addWidget(self.status_line)
        layout.addWidget(self.help_line)
        layout.addStretch(0)     # the spare height when the table is hidden

        self._sync_buttons()

    # -- filling it in --------------------------------------------------

    def refresh(self) -> None:
        r"""Re-read the sources and their live status, off a worker. Runs on
        every tab switch - the same "refresh on forward, never on a timer"
        rule `code_view.refresh` already follows, and 2a's own words:
        "status checked passively on panel refresh - no device watcher".
        """
        if self._store is None:
            return
        worker = CallableWorker(_offline_media_snapshot, self._store,
                                component="ui.offline_media")
        worker.signals.finished.connect(lambda snapshot: self.load(*snapshot))
        worker.signals.failed.connect(self.error.emit)
        run(QThreadPool.globalInstance(), worker)

    def load(self, store_rows: Any, online: Optional[Any] = None) -> None:
        r"""Paint the list from store rows. **Never fetches anything itself**
        - `store_rows`/`online` are handed in already read, off a worker,
        the same division `results_view.show_results` draws against
        `missing_paths`."""
        self._rows = volume_rows(store_rows, online)
        selected = self._selected_volume_id()
        self.tree.clear()
        for row in self._rows:
            item = QTreeWidgetItem([row.name, row.status, row.size, row.files, row.scanned])
            item.setData(0, Qt.ItemDataRole.UserRole, row.volume_id)
            if row.description:
                item.setToolTip(0, row.description)
            item.setToolTip(_STATUS_COLUMN, _status_tooltip(row))
            self.tree.addTopLevelItem(item)
            if row.volume_id == selected:
                item.setSelected(True)
        self.empty.setVisible(not self._rows)
        self.tree.setVisible(bool(self._rows))
        self._sync_buttons()

    def set_busy(self, message: str) -> None:
        """2a has no progress bar - a status line and disabled buttons are
        the whole of it, matching the order's own plainness."""
        self.status_line.setText(message)
        for button in (self.scan, self.rescan, self.delete):
            button.setEnabled(not message)
        if not message:
            self._sync_buttons()

    # -- selection --------------------------------------------------------

    def _selected_volume_id(self) -> Optional[int]:
        items = self.tree.selectedItems()
        if not items:
            return None
        value = items[0].data(0, Qt.ItemDataRole.UserRole)
        return int(value) if value is not None else None

    def _selected_row(self) -> Optional[VolumeRow]:
        volume_id = self._selected_volume_id()
        if volume_id is None:
            return None
        for row in self._rows:
            if row.volume_id == volume_id:
                return row
        return None

    def _sync_buttons(self) -> None:
        row = self._selected_row()
        self.rescan.setEnabled(row is not None and row.status_code == "ONLINE")
        self.delete.setEnabled(row is not None)
        if row is not None and row.status_code != "ONLINE":
            self.rescan.setToolTip(
                "Only available while this source is plugged in.\n\n"
                f"Current status: {row.status}.")

    # -- actions ------------------------------------------------------------

    def _choose_and_scan(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a drive or folder to catalogue")
        if not folder:
            return
        dialog = ScanNameDialog(folder, self)
        if dialog.exec() != ScanNameDialog.DialogCode.Accepted:
            return
        self.scan_requested.emit(folder, dialog.chosen_name(), dialog.chosen_description())

    def _rescan_selected(self) -> None:
        row = self._selected_row()
        if row is not None and row.status_code == "ONLINE":
            self.rescan_requested.emit(row.volume_id)

    def _delete_selected(self) -> None:
        row = self._selected_row()
        if row is None:
            return
        dialog = DeleteVolumeDialog(row.name, row.file_count, self)
        if dialog.exec() == DeleteVolumeDialog.DialogCode.Accepted:
            self.delete_requested.emit(row.volume_id)


def _offline_media_snapshot(store: Any) -> tuple[Any, Any]:
    r"""The read-only half of a refresh, off the worker thread. **Never
    called on the interface thread** - `refresh_volume_statuses` is a
    Windows volume enumeration per catalogued drive and `list_volumes` is
    a join, and both were exactly the shape of store call
    `test_ui_never_blocks` exists to catch.

    Returns `(rows, online)` so `OfflineMediaView.load` can be called
    directly with the tuple unpacked - one worker, one paint, the same
    shape `decorate_results_async` already uses for a search result page.
    """
    from app.index.offline_media import connected_volumes, refresh_volume_statuses

    refresh_volume_statuses(store)
    return store.list_volumes(), connected_volumes(store)
