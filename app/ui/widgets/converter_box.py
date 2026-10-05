r"""Old Office files - the two controls for the LibreOffice fallback.

Layer: L6 (UI)

`.doc` and `.ppt` are read by Leasha itself (`app/extract/doc.py`, `ppt.py`); only
what those decline, and the rare formats no library reads, go to LibreOffice.
LibreOffice is kept running between files (`app/extract/lo_session.py`), and this
group holds the two things about that a person could reasonably want to change:
how many copies may run, and how long one file may take. Nothing else about it is
tunable, because nothing else has evidence behind a different value.

Both are registry settings on the Index Tuning screen (`indexing.tuning`), so
this box only *builds* them; the words are the registry's own.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFormLayout, QGroupBox, QSpinBox

from app.ui.widgets.debounce import Debounced

__all__ = ["ConverterBox"]


class ConverterBox(QGroupBox):
    """How LibreOffice is used for the old Office files Leasha cannot read itself."""

    changed = Signal(dict)

    def __init__(self, settings: Any = None, parent: Optional[Any] = None) -> None:
        super().__init__("Old Office files", parent)

        self.workers = QSpinBox()
        self.workers.setObjectName("CONVERTER_WORKERS")
        self.workers.setRange(0, 4)
        self.workers.setSpecialValueText("Automatic")
        self.workers.setToolTip(
            "Old Word and PowerPoint files are read by Leasha itself, in\n"
            "milliseconds. The few it cannot read that way - locked, damaged or in\n"
            "a rare format - are handed to LibreOffice, which Leasha keeps running\n"
            "between files instead of starting it again each time.\n\n"
            "Automatic chooses for this computer's memory: one copy, or two when\n"
            "there is plenty. Each copy uses a few hundred megabytes.")

        self.timeout = QSpinBox()
        self.timeout.setObjectName("CONVERTER_TIMEOUT_S")
        self.timeout.setRange(20, 300)
        self.timeout.setSuffix(" seconds")
        self.timeout.setToolTip(
            "If LibreOffice has not finished one file in this long, that copy is\n"
            "stopped, the file is skipped and the next one carries on. A skipped\n"
            "file is still findable by its name.\n\n"
            "Raise it only if a large old file you care about keeps being skipped.")

        for widget in (self.workers, self.timeout):
            widget.setKeyboardTracking(False)

        self._save = Debounced(lambda: self.changed.emit(self.values()), parent=self)
        for widget in (self.workers, self.timeout):
            widget.valueChanged.connect(lambda _v: self._save())

        form = QFormLayout(self)
        form.addRow("Old Office files read at once", self.workers)
        form.addRow("Longest for one old Office file", self.timeout)

        if settings is not None:
            self.load(settings)

    def load(self, settings: Any) -> None:
        for widget in (self.workers, self.timeout):
            widget.blockSignals(True)
        try:
            self.workers.setValue(int(getattr(settings, "converter_workers", 0)))
            self.timeout.setValue(int(getattr(settings, "converter_timeout_s", 120)))
        finally:
            for widget in (self.workers, self.timeout):
                widget.blockSignals(False)
        self._save.cancel()

    def values(self) -> dict:
        return {
            "CONVERTER_WORKERS": int(self.workers.value()),
            "CONVERTER_TIMEOUT_S": int(self.timeout.value()),
        }

    def flush(self) -> None:
        """Persist anything still inside the debounce window, before closing."""
        self._save.flush()
