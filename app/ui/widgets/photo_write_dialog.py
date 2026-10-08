r""""Write names into photos" - option b, on demand only.

Layer: L5

2026-10-05, the owner: "it should also store meta data in the photos", then
"go ahead with option a but an option b button which can be used". Leasha keeps
names in its own index (option a, always); this dialog is the button for option
b - the owner's exception to non-negotiable 10, recorded above that rule. It
says plainly what will change before anything does, offers sidecar files that
change no photo at all, and keeps a copy of every photo it changes.

The work is `app.index.photo_metadata.write_photo_metadata`, on a worker; this
dialog asks, shows progress, can stop, and reports what happened.
"""

from __future__ import annotations

import datetime as _dt
import threading
from pathlib import Path
from typing import Any, Optional, Sequence

from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtWidgets import (QButtonGroup, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout,
                             QLabel, QLineEdit, QProgressBar, QPushButton, QRadioButton,
                             QVBoxLayout, QWidget)

from app.index.photo_metadata import INSIDE, SIDECAR, run_write

__all__ = ["WriteNamesDialog", "run_write"]


class WriteNamesDialog(QDialog):
    """Ask, then write. `selected` and `shown` are file ids from the page."""

    def __init__(self, store: Any, data_dir: Optional[Path], *,
                 selected: Sequence[int] = (), shown: Sequence[int] = (),
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.WindowType.Window | Qt.WindowType.WindowCloseButtonHint)
        self.setWindowTitle("Write names into photos")
        self._store = store
        self._selected, self._shown = list(selected), list(shown)
        self._pool = QThreadPool.globalInstance()
        self._progress: dict = {"total": 0, "done": 0}
        self._stop = threading.Event()
        self.result_text = ""

        intro = QLabel(
            "Leasha keeps the names you give and its descriptions in its own index - "
            "your photos are never changed for that. This writes them into the photos "
            "too, as standard XMP keywords, people and a description, so other photo "
            "programs (Windows Photos, Lightroom, digiKam, Apple Photos) can see them.")
        intro.setWordWrap(True)

        self.scope_all = QRadioButton("Every photo with someone named or a description")
        self.scope_selected = QRadioButton(f"Only the {len(self._selected):,} photo(s) selected")
        self.scope_shown = QRadioButton(f"Only the {len(self._shown):,} photo(s) shown now")
        self.scope_selected.setEnabled(bool(self._selected))
        self.scope_shown.setEnabled(bool(self._shown))
        self.scope_all.setToolTip("Every photo with a person named or a description")
        self.scope_selected.setToolTip("Only the photos selected on the Photos tab")
        self.scope_shown.setToolTip("Only the photos the Photos tab shows now")
        scope = QButtonGroup(self)
        for button in (self.scope_all, self.scope_selected, self.scope_shown):
            scope.addButton(button)
        (self.scope_selected if self._selected else self.scope_all).setChecked(True)

        self.where_sidecar = QRadioButton(
            "Beside each photo, as a .xmp file - no photo is changed")
        self.where_inside = QRadioButton(
            "Inside the photo where it can (JPEG and PNG), beside it otherwise - "
            "a copy of each photo is kept first")
        self.where_sidecar.setToolTip("A small .xmp file beside each photo; the photos "
                                      "themselves are not changed")
        self.where_inside.setToolTip("Into each JPEG and PNG itself, after a copy is kept; "
                                     "other photos get a .xmp file beside them")
        where = QButtonGroup(self)
        for button in (self.where_sidecar, self.where_inside):
            where.addButton(button)
        self.where_sidecar.setChecked(True)
        self.where_sidecar.toggled.connect(self._where_changed)

        stamp = _dt.date.today().isoformat()
        default = (Path(data_dir) / "photo_backups" / stamp) if data_dir else Path.home()
        #: Where the copies go when the field has been emptied. Found in review
        #: 2026-10-08: a blank field fell back to `Path(".")`, the process's
        #: working directory - wherever Leasha happened to be started from.
        self._default_backup = default
        self.backup = QLineEdit(str(default))
        self.backup.setPlaceholderText("Folder for the copies kept before a photo is changed")
        self.backup.setAccessibleName("Backup folder")
        self.backup.setToolTip("Every photo is copied here, under its own path, before it is "
                               "changed. Needs as much free space as the photos changed.")
        self.browse = QPushButton("Browse…")
        self.browse.setToolTip("Choose the folder for the copies")
        self.browse.clicked.connect(self._browse)
        backup_row = QHBoxLayout()
        backup_row.addWidget(QLabel("Copies kept in"))
        backup_row.addWidget(self.backup, 1)
        backup_row.addWidget(self.browse)
        self.backup_row = QWidget()
        self.backup_row.setLayout(backup_row)

        self.bar = QProgressBar()
        self.bar.setVisible(False)
        self.status = QLabel("")
        self.status.setWordWrap(True)

        self.buttons = QDialogButtonBox()
        self.go = self.buttons.addButton("Write them", QDialogButtonBox.ButtonRole.AcceptRole)
        self.go.setToolTip("Start writing; it can be stopped part-way")
        self.cancel = self.buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self.go.clicked.connect(self.start)
        self.cancel.clicked.connect(self._cancel)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(QLabel("<b>Which photos</b>"))
        for button in (self.scope_all, self.scope_selected, self.scope_shown):
            layout.addWidget(button)
        layout.addWidget(QLabel("<b>Where</b>"))
        layout.addWidget(self.where_sidecar)
        layout.addWidget(self.where_inside)
        layout.addWidget(self.backup_row)
        layout.addWidget(self.bar)
        layout.addWidget(self.status)
        layout.addStretch(1)
        layout.addWidget(self.buttons)
        self.resize(640, 420)
        self._where_changed()

        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._tick)

    def _where_changed(self, *_args: Any) -> None:
        self.backup_row.setVisible(self.where_inside.isChecked())

    def _browse(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Folder for the copies",
                                                  self.backup.text())
        if chosen:
            self.backup.setText(chosen)

    def chosen_ids(self) -> Optional[list[int]]:
        """The file ids to write, or None for every photo with a name or description."""
        if self.scope_selected.isChecked():
            return list(self._selected)
        if self.scope_shown.isChecked():
            return list(self._shown)
        return None

    def start(self) -> bool:
        """"Write them": the one place XMP is written, on a worker (`run_write`).
        Progress is polled from a shared dict every 250 ms; `_finished` reports.
        """
        from app.ui.later import when_done
        from app.ui.workers import CallableWorker, run

        where = INSIDE if self.where_inside.isChecked() else SIDECAR
        backup_root = Path(self.backup.text().strip() or self._default_backup)
        for widget in (self.scope_all, self.scope_selected, self.scope_shown,
                       self.where_sidecar, self.where_inside, self.backup_row, self.go):
            widget.setEnabled(False)
        self.cancel.setText("Stop")
        self.bar.setVisible(True)
        self.bar.setRange(0, 0)
        self.status.setText("Finding the photos to write into…")
        worker = CallableWorker(run_write, self._store, self.chosen_ids(), where=where,
                                backup_root=backup_root, progress=self._progress,
                                stop=self._stop, component="ui.photo_write")
        when_done(self, worker, finished=self._finished, failed=self._failed)
        run(self._pool, worker)
        self._timer.start()
        return True

    def _tick(self) -> None:
        """UI thread: the progress bar from the worker's counters."""
        total, done = self._progress.get("total", 0), self._progress.get("done", 0)
        if total:
            self.bar.setRange(0, total)
            self.bar.setValue(done)
            self.status.setText(f"Writing… {done:,} of {total:,}")

    def _finished(self, result: Any) -> None:
        """UI thread: say what was written, kept, skipped and copied; Cancel becomes Close."""
        self._timer.stop()
        self.bar.setVisible(False)
        parts = []
        if result.written:
            parts.append(f"{result.written:,} photo(s) now carry the names")
        if result.sidecars:
            parts.append(f"{result.sidecars:,} sidecar file(s) written")
        if result.unchanged:
            parts.append(f"{result.unchanged:,} already had them")
        if result.failed:
            parts.append(f"{result.failed:,} could not be written - "
                         + "; ".join(result.problems[:3]))
        if result.backed_up_bytes:
            parts.append(f"copies kept in {self.backup.text()} "
                         f"({result.backed_up_bytes / (1 << 20):,.0f} MB)")
        if self._stop.is_set():
            parts.insert(0, "Stopped part-way")
        self.result_text = ". ".join(parts) + "." if parts else "There was nothing to write."
        self.status.setText(self.result_text)
        self.cancel.setText("Close")
        self.cancel.clicked.disconnect()
        self.cancel.clicked.connect(self.accept)

    def _failed(self, error: Any) -> None:
        """UI thread: the worker raised. Its message in the status line, never a dialog."""
        self._timer.stop()
        self.bar.setVisible(False)
        self.status.setText(f"Writing stopped: {getattr(error, 'message', error)}")
        self.cancel.setText("Close")

    def _cancel(self) -> None:
        """Stop while writing (after the current photo); otherwise close the dialog."""
        if self._timer.isActive():
            self._stop.set()
            self.status.setText("Stopping after this photo…")
        else:
            self.reject()

    def _writing(self) -> bool:
        """True while `run_write` is on the worker - the timer runs exactly that long."""
        return self._timer.isActive()

    def reject(self) -> None:
        """Escape and the Cancel role land here. **While writing, this stops, it
        does not close.** Found in review 2026-10-08: `QDialog`'s own Escape
        hid the dialog and the worker went on writing XMP into photos with no
        stop flag and nothing on screen - in the one path allowed to touch a
        person's photos. Once the run has ended, Escape closes as before."""
        if self._writing():
            self._cancel()
            return
        super().reject()

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt's name
        """The title-bar X: the same rule as `reject`, stop first, close after."""
        if self._writing():
            self._cancel()
            event.ignore()
            return
        super().closeEvent(event)
