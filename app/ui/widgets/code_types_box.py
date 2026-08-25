r"""What counts as *code*, for the Code tab.

Layer: L6 (UI)

Asked for: *"under the section files to index we need another config for code
opens a config of available files and the ones which come up on the code search
as at the moment its bringing files which are not code"*.

**It decides what is listed, never what is indexed**, and the panel says so in
as many words. That is the one thing somebody could reasonably get wrong here:
the file-types editor further down the same page *does* decide what is read, and
two editors of file types on one page will be confused unless each states its
job. Unticking a group here removes those types from one tab and changes nothing
else - the main search still finds every one of them.

The decision and the catalogue live in `app/core/code_types.py`, which imports
no Qt. This is a combo box, a list of checkboxes and a sentence.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QGroupBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
)

from app.core.code_types import (
    DEFAULT_PRESET,
    PRESET_LABELS,
    describe,
    group_names,
    groups_for,
)

__all__ = ["CodeTypesBox"]

#: The order they are offered in: widening, so the list reads as a dial rather
#: than as four unrelated options.
ORDER = ("source", "build", "docs", "all", "custom")


class CodeTypesBox(QGroupBox):
    """A preset, the groups behind it, and a sentence saying what it means."""

    #: `(preset, chosen groups)` - the window persists it.
    changed = pyqtSignal(str, list)

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__("Which files count as code", parent)

        self.preset = QComboBox()
        self.preset.setObjectName("CODE_TYPES_PRESET")
        self.preset.setAccessibleName("Which files count as code")
        for key in ORDER:
            self.preset.addItem(PRESET_LABELS[key], key)
        self.preset.currentIndexChanged.connect(lambda _i: self._preset_changed())

        self.groups = QListWidget()
        self.groups.setAccessibleName("Code file type groups")
        self.groups.setAlternatingRowColors(True)
        # Sized to show a few rows rather than all sixteen: this sits inside a
        # settings page that already scrolls, and a list that expands to its
        # contents pushes everything below it off the screen.
        self.groups.setMaximumHeight(190)
        for name in group_names():
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            self.groups.addItem(item)
        self.groups.itemChanged.connect(self._group_toggled)

        self.detail = QLabel("")
        self.detail.setWordWrap(True)

        # **Said plainly, because the page has two file-type editors on it.**
        # The one below decides what is read; this one decides what one tab
        # lists. Somebody who mixes them up either loses search coverage or
        # wonders why unticking something changed nothing.
        note = QLabel(
            "This only changes the Code tab. Nothing is removed from the index "
            "and the main search still finds every one of these files — for "
            "what gets read at all, see File types below."
        )
        note.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self.preset)
        layout.addWidget(self.groups)
        layout.addWidget(self.detail)
        layout.addWidget(note)

        self._loading = False
        self.load(DEFAULT_PRESET, ())

    # -- filling it in -------------------------------------------------------

    def load(self, preset: str, chosen: Any = ()) -> None:
        """Set the controls without emitting - see `IndexingSettings`."""
        self._loading = True
        try:
            index = self.preset.findData(preset)
            self.preset.setCurrentIndex(index if index >= 0 else
                                        self.preset.findData(DEFAULT_PRESET))
            self._tick(groups_for(preset, chosen))
        finally:
            self._loading = False
        self._describe()

    def _tick(self, names: Any) -> None:
        wanted = set(names or ())
        for row in range(self.groups.count()):
            item = self.groups.item(row)
            item.setCheckState(Qt.CheckState.Checked if item.text() in wanted
                               else Qt.CheckState.Unchecked)

    def current(self) -> tuple[str, list[str]]:
        return str(self.preset.currentData() or DEFAULT_PRESET), self.checked()

    def checked(self) -> list[str]:
        return [self.groups.item(row).text()
                for row in range(self.groups.count())
                if self.groups.item(row).checkState() == Qt.CheckState.Checked]

    # -- events --------------------------------------------------------------

    def _preset_changed(self) -> None:
        if self._loading:
            return
        preset = str(self.preset.currentData() or DEFAULT_PRESET)
        if preset != "custom":
            # The ticks follow the preset, so the list always shows what is
            # actually in force. A preset whose boxes disagree with it is a
            # panel nobody can read.
            self._loading = True
            try:
                self._tick(groups_for(preset))
            finally:
                self._loading = False
        self._emit()

    def _group_toggled(self, _item: Any) -> None:
        """Ticking a box by hand means "custom" - it is no longer a preset."""
        if self._loading:
            return
        index = self.preset.findData("custom")
        if index >= 0 and self.preset.currentIndex() != index:
            self._loading = True
            try:
                self.preset.setCurrentIndex(index)
            finally:
                self._loading = False
        self._emit()

    def _emit(self) -> None:
        self._describe()
        preset, chosen = self.current()
        self.changed.emit(preset, chosen)

    def _describe(self) -> None:
        preset, chosen = self.current()
        self.detail.setText(describe(preset, chosen))
