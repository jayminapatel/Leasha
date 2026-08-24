r"""The file-types editor: what gets indexed, switchable without a text editor.

Layer: L5

Step 6 of the file-types work order, and the point of the whole tier system:
`config/extractors.toml` decides what is read, and until now changing it meant
finding a file on disk and editing TOML correctly.

**Only `enabled` is editable here, deliberately.** Size caps and extractor
routing are per-format decisions with real consequences - an OCR cap raised to
100MB is minutes of work per image - and the file is where they belong, with the
comments explaining each one. A checkbox that could silently make indexing
twenty times slower is not a kindness.

**Changes are saved as differences, never as a snapshot.** The packaged file is
replaced on upgrade, so writing the whole current state would pin today's
defaults forever: a format added in a later release would arrive switched off,
or with an old size limit, and nobody would know why. `save_overrides` writes
only what differs.

**Nothing takes effect until the next index run**, and it says so. A format
switched on does not retrospectively index the files already skipped, and a
person who is not told that concludes it did not work.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

__all__ = ["FileTypesEditor"]


class FileTypesEditor(QGroupBox):
    """A checkbox per file type, and one Save."""

    #: `{extension: enabled}` for everything that differs from the defaults.
    changes_saved = pyqtSignal(dict)
    error = pyqtSignal(object)

    def __init__(self, settings: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__("File types", parent)
        self._settings = settings
        self._rules: Any = None
        self._boxes: dict[str, QCheckBox] = {}

        self.summary = QLabel("")
        self.summary.setWordWrap(True)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Index", "Type", "Read by", "Limit"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        # Tall enough to be usable, short enough that Settings stays scrollable.
        self.table.setMinimumHeight(220)

        self.save_button = QPushButton("Save file types")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(lambda _checked=False: self.save())

        self.status = QLabel("")
        self.status.setWordWrap(True)

        buttons = QHBoxLayout()
        buttons.addWidget(self.save_button)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        layout.addWidget(self.status)

        self.reload()

    # -- reading -------------------------------------------------------------

    def reload(self) -> None:
        """Read the rules and rebuild the table. Never raises.

        A configuration file with a mistake in it must not stop Settings
        opening - Settings is where somebody would go to fix it.
        """
        try:
            from app.core.formats import load_rules

            self._rules = load_rules(self._settings.data_path)
        except Exception as exc:                 # noqa: BLE001 - reported, not raised
            self.summary.setText(
                f"Could not read the file-type settings: {exc}\n"
                "Delete extractors.toml in your index folder to start again - "
                "the shipped defaults are always restored by removing it."
            )
            self.table.setRowCount(0)
            return

        rows = self._rules.describe()
        self.table.setRowCount(len(rows))
        self._boxes.clear()

        for index, row in enumerate(rows):
            extension = row["extension"]

            box = QCheckBox()
            box.setChecked(bool(row["enabled"]))
            box.stateChanged.connect(lambda _s: self._mark_dirty())
            holder = QWidget()
            centred = QHBoxLayout(holder)
            centred.setContentsMargins(0, 0, 0, 0)
            centred.addWidget(box, alignment=Qt.AlignmentFlag.AlignCenter)
            self.table.setCellWidget(index, 0, holder)
            self._boxes[extension] = box

            self.table.setItem(index, 1, QTableWidgetItem(extension))
            reader = QTableWidgetItem(row["extractor"])
            if row["note"]:
                reader.setToolTip(row["note"])
            self.table.setItem(index, 2, reader)
            self.table.setItem(index, 3, QTableWidgetItem(_human(row["max_bytes"])))

        on = sum(1 for row in rows if row["enabled"])
        self.summary.setText(
            f"{on} of {len(rows)} file types are indexed. "
            "Anything switched off is never opened at all.\n"
            "Size limits and which reader handles a type are set in "
            "config\\extractors.toml, where the reasons for each are written down."
        )
        self.save_button.setEnabled(False)
        self.status.setText("")

    def _mark_dirty(self) -> None:
        self.save_button.setEnabled(True)

    # -- saving --------------------------------------------------------------

    def current(self) -> dict[str, bool]:
        return {extension: box.isChecked() for extension, box in self._boxes.items()}

    def save(self) -> None:
        """Write the differences and say what happens next.

        Off the UI thread would be over-engineering: this writes a file of a few
        dozen lines. It is wrapped instead, because a read-only index folder
        should produce a sentence rather than a traceback.
        """
        if self._rules is None:
            return

        try:
            from app.core.formats import differences, save_overrides, with_override

            updated = self._rules
            for extension, enabled in self.current().items():
                if extension in updated.extensions:
                    updated = with_override(updated, extension, enabled=enabled)

            changes = differences(updated)
            path = save_overrides(self._settings.data_path, changes)
        except Exception as exc:                 # noqa: BLE001
            self.status.setText(f"Could not save: {exc}")
            return

        self._rules = updated
        self.save_button.setEnabled(False)
        self.changes_saved.emit(changes)

        # **Says what it does not do.** A format switched on does not
        # retrospectively index the files already skipped, and somebody not told
        # that concludes the setting did not work.
        if changes:
            self.status.setText(
                f"Saved {len(changes)} change(s) to {path.name}. "
                "They apply to the next index run - files already skipped are "
                "not re-read until then."
            )
        else:
            self.status.setText(
                "Nothing differs from the defaults, so no overrides are stored."
            )


def _human(count: int) -> str:
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if count >= size:
            value = count / size
            return f"{value:.0f}{unit}" if value >= 10 else f"{value:.1f}{unit}"
    return f"{count}B"
