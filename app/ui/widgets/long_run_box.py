r"""The two settings that only matter once the corpus is large.

Layer: L6 (UI), driving L3

Both are answers to the same finding: **the design scales and the schedule does
not.** Indexing 600GB is roughly five days at the throughput measured here, and
1.5TB is nearly a fortnight - so what matters at that size is not making the
work faster but doing less of it, and doing the useful part first.

* **Images and scans** decides *when* pictures of text are read, not whether.
  OCR costs about 3.6 seconds a page against roughly 0.4 for embedding a
  passage, so a single pass means nothing is searchable until everything is.
* **Re-check archives** decides how long a folder declared static is trusted.
  The recurring cost of a settled corpus is the re-walk, not the indexing.

Its own widget because `indexing_settings.py` is under the 250-line guard, and
because these two belong together: neither is worth a thought at 100GB and both
are the difference between a workable schedule and an unworkable one at a
terabyte.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QComboBox, QFormLayout, QGroupBox, QSpinBox

__all__ = ["LongRunBox"]


class LongRunBox(QGroupBox):
    """Which pass to run, and how often to re-check an archive."""

    changed = pyqtSignal()

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__("Large corpora", parent)

        # **Three choices, not two checkboxes.** "Skip OCR" and "Only OCR" as
        # separate switches have a fourth state that means nothing, and neither
        # label says the thing that matters - that this is about *when* images
        # are read.
        self.ocr_mode = QComboBox()
        self.ocr_mode.setObjectName("INDEX_OCR_MODE")
        self.ocr_mode.addItem("Text and images together", "both")
        self.ocr_mode.addItem("Text first, images queued", "text")
        self.ocr_mode.addItem("Images only - the second pass", "images")
        self.ocr_mode.setToolTip(
            "Reading text out of an image takes about 3.6 seconds a page - roughly\n"
            "eight times what everything else costs - so on a large corpus it is what\n"
            "decides how long a run takes.\n\n"
            "Text first indexes everything readable without OCR and queues the images,\n"
            "so search works in a day or two rather than a fortnight. The queued files\n"
            "appear on the Indexing page as held, not failed, and the Images pass fills\n"
            "them in afterwards."
        )
        self.ocr_mode.currentIndexChanged.connect(lambda _i: self.changed.emit())

        self.archive_recheck_days = QSpinBox()
        self.archive_recheck_days.setObjectName("ARCHIVE_RECHECK_DAYS")
        self.archive_recheck_days.setRange(0, 365)
        self.archive_recheck_days.setSuffix(" days")
        self.archive_recheck_days.setSpecialValueText("Only when it changes")
        self.archive_recheck_days.setKeyboardTracking(False)
        self.archive_recheck_days.setToolTip(
            "A folder marked as an archive in Folders to index is walked once and\n"
            "then left alone - which is what makes an incremental run over a settled\n"
            "terabyte take seconds instead of hours.\n\n"
            "It is still re-walked when the folder itself changes, when you press\n"
            "Rescan, and after this many days. The interval is the backstop for a\n"
            "change the folder's own timestamp cannot show - a file edited in place,\n"
            "deep inside."
        )
        self.archive_recheck_days.valueChanged.connect(lambda _v: self.changed.emit())

        form = QFormLayout(self)
        form.addRow("Images and scans", self.ocr_mode)
        form.addRow("Re-check archives every", self.archive_recheck_days)

    def load(self, settings: Any) -> None:
        """Fill from Settings without emitting - see `IndexingSettings.load_indexing`."""
        for widget in (self.ocr_mode, self.archive_recheck_days):
            widget.blockSignals(True)
        try:
            mode = str(getattr(settings, "index_ocr_mode", "both"))
            found = self.ocr_mode.findData(mode)
            self.ocr_mode.setCurrentIndex(found if found >= 0 else 0)
            self.archive_recheck_days.setValue(
                int(getattr(settings, "archive_recheck_days", 30)))
        finally:
            for widget in (self.ocr_mode, self.archive_recheck_days):
                widget.blockSignals(False)

    def values(self) -> dict:
        """Keyed by `Settings` field name, like `IndexingSettings.current_limits`."""
        return {
            "index_ocr_mode": str(self.ocr_mode.currentData() or "both"),
            "archive_recheck_days": int(self.archive_recheck_days.value()),
        }
