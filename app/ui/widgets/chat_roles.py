"""The roles grid: which installed model does which job in Chat.

Layer: L5 view

Work order 202626270611 section 4d. Model selection is per *role* with a
one-model-everywhere default; this is the Manual side of that - a row per job, each a
drop-down of the models Ollama says are **installed**, the first entry always
"Automatic" (the role inherits, which is also the performance-correct choice, because
Ollama keeps one model resident and reloading a second one costs more than it saves
on most machines).

* **Installed models only.** A name typed into a text box is a guess; this lists what
  `ollama list` reports (`app/chat/llm.py::probe_installed`, on a worker).
* **Describe offers only models that can read pictures**, greyed with the reason and
  the command that fixes it when there are none.
* **A saved name is never dropped because the list is short.** If Ollama is not
  answering, or the saved model is not installed, the saved value stays selectable
  and says why - silently swapping a model somebody chose is the fault
  `app.llm.models.choose` exists to avoid.
* **The memory line** says what the chosen models need if all are kept ready at once
  and what this computer has ("these two together need about 6 GB - you have 32").

Drawing only: which models are installed, what they weigh and what to say about it are
`app/chat/roles.py`.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QGridLayout, QLabel, QSizePolicy, QWidget

from app.chat.roles import (
    NO_VISION_MODEL_LINE, OLLAMA_UNREACHABLE_LINE, InstalledModels, size_of,
)

__all__ = ["ModelCombo", "RolesGrid", "AUTOMATIC"]

AUTOMATIC = "Automatic"


def _size_words(n_bytes: int) -> str:
    """A model's size for the drop-down, or `""` when unknown."""
    # 2026-10-04, code review: `row_facts.format_size`, the one size wording
    # ("512 MB" now reads "512.0 MB").
    if n_bytes <= 0:
        return ""
    from app.core.row_facts import format_size

    return format_size(n_bytes)


class ModelCombo(QComboBox):
    """One role's drop-down. `value()` is the model name, or `""` for Automatic."""

    def __init__(self, key: str, *, vision: bool = False, automatic: str = AUTOMATIC,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.key = key
        self.vision = vision
        self.automatic = automatic
        self._saved = ""
        self.reason = ""
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(18)
        self.populate(None, "")

    def value(self) -> str:
        return str(self.currentData() or "")

    def populate(self, installed: Optional[InstalledModels], saved: str) -> None:
        """Rebuild the list from what is installed, keeping `saved` selected.

        `installed=None` means "not asked yet"; an unreachable Ollama and an empty
        one are told apart by `reason`, which the grid shows beside the drop-down.
        """
        self._saved = str(saved or "").strip()
        self.blockSignals(True)
        try:
            self.clear()
            self.addItem(self.automatic, "")
            candidates: tuple[str, ...] = ()
            self.reason = ""
            enabled = True
            if installed is not None and installed.reachable:
                candidates = installed.vision if self.vision else installed.names
                if self.vision and not candidates:
                    self.reason = NO_VISION_MODEL_LINE
                    enabled = False
            elif installed is not None:
                self.reason = OLLAMA_UNREACHABLE_LINE
            sizes = installed.sizes if installed is not None else {}
            for name in candidates:
                size = _size_words(size_of(name, sizes))
                self.addItem(f"{name}  ({size})" if size else name, name)
            wanted = self._saved
            if self._saved and self.findData(self._saved) < 0:
                match = next((n for n in candidates
                              if n.split(":")[0] == self._saved.split(":")[0]
                              and ":" not in self._saved), None)
                if match is not None:
                    wanted = match           # "mistral" is installed as "mistral:latest": that one
                else:
                    known = installed is not None and installed.reachable
                    self.addItem(f"{self._saved}  (not installed)" if known else self._saved,
                                 self._saved)
                    if known and not self.reason:
                        self.reason = (f"{self._saved} is not installed. Install it with: "
                                       f"ollama pull {self._saved}")
            index = self.findData(wanted) if wanted else 0
            self.setCurrentIndex(index if index >= 0 else 0)
            self.setEnabled(enabled)
        finally:
            self.blockSignals(False)

    def select(self, value: str) -> None:
        """Choose `value` without announcing it (a load, not a decision)."""
        self.blockSignals(True)
        try:
            wanted = str(value or "").strip()
            index = self.findData(wanted)
            if index < 0 and wanted:
                self.addItem(wanted, wanted)
                index = self.findData(wanted)
            self.setCurrentIndex(max(0, index))
            self._saved = wanted
        finally:
            self.blockSignals(False)


class RolesGrid(QWidget):
    """Job | model | why - one row per role, then the memory line."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setColumnStretch(1, 1)
        self.rows: dict[str, tuple[QLabel, ModelCombo, QLabel]] = {}
        self.ram = QLabel("")
        self.ram.setObjectName("chatRamLine")
        self.ram.setWordWrap(True)
        self.ram.setAccessibleName("Memory these models need")
        self.ram.setToolTip("What the models chosen above need in memory if they are all "
                            "kept ready at once, against what this computer has.")
        self.ram.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        self.ram.hide()

    def add_role(self, label: str, combo: ModelCombo, help_text: str = "") -> None:
        """One row: the job's label, its drop-down, and a hidden reason line under it."""
        row = self._grid.rowCount()
        name = QLabel(label)
        name.setBuddy(combo)
        name.setToolTip(help_text or label)
        why = QLabel("")
        why.setWordWrap(True)
        why.setObjectName((combo.objectName() + "_why") if combo.objectName() else "")
        why.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        why.hide()
        self._grid.addWidget(name, row, 0)
        self._grid.addWidget(combo, row, 1)
        self._grid.addWidget(why, row + 1, 1)
        self.rows[combo.key] = (name, combo, why)

    def finish(self) -> None:
        """Put the memory line last. Call once every role is added."""
        self._grid.addWidget(self.ram, self._grid.rowCount(), 0, 1, 2)

    def show_reasons(self) -> None:
        """Each row says why its drop-down is short or greyed, or says nothing."""
        for _name, combo, why in self.rows.values():
            why.setText(combo.reason)
            why.setVisible(bool(combo.reason))

    def set_ram_line(self, text: str) -> None:
        self.ram.setText(text)
        self.ram.setVisible(bool(text))

    def texts(self) -> list[Any]:
        return [self.ram.text()] + [why.text() for _n, _c, why in self.rows.values()]
