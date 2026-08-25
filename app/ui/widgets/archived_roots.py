"""The folders this run deliberately did not walk.

Layer: L6 (UI)

*"Skip cheaply, but never silently."* An archival root is skipped for the best
of reasons - it saves hours on every incremental run over a settled corpus - but
a root skipped in silence is indistinguishable from one that was never indexed
at all, and the person who reaches the second conclusion will delete their index
and start a fortnight over.

So each skipped folder appears with **its file count and the date of its last
full pass**, which is exactly the evidence that separates "deliberately left
alone, and here is what is in it" from "empty".

The wording lives in `presenter.archive_summary`, where it can be tested.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import QGroupBox, QLabel, QPushButton, QVBoxLayout, QWidget

from app.ui.presenter import archive_summary

__all__ = ["ArchivedRoots"]


class ArchivedRoots(QGroupBox):
    """One line per skipped folder, and a button to walk them all now."""

    rescan_requested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("Archived folders", parent)
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(
            self.detail.textInteractionFlags().TextSelectableByMouse
        )

        self.rescan = QPushButton("Rescan these folders now")
        self.rescan.setToolTip(
            "Walk every archived folder in full, once. Use it when you know "
            "something changed inside one that the folder's own timestamp "
            "would not show."
        )
        self.rescan.clicked.connect(lambda _c=False: self.rescan_requested.emit())

        layout = QVBoxLayout(self)
        layout.addWidget(self.detail)
        layout.addWidget(self.rescan)
        self.setVisible(False)

    def show_roots(self, rows: Any) -> None:
        """Paint the skipped roots, or hide the panel when there are none."""
        title, lines = archive_summary(rows)
        self.setVisible(bool(lines))
        if not lines:
            return
        self.setTitle(title)
        self.detail.setText("\n".join(lines))
