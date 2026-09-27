r"""The report export dialog: which sources go in, order 202626270602 2c.

Layer: L5

"Optional per-source include/exclude checkboxes before export (a source
can be private even from the map)." Every source starts checked; this
dialog decides nothing about what a report says, only which sources
`ReportsView._export_to` is allowed to render - the same division
`offline_media_dialogs.py`'s own docstring draws for its two dialogs.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)
from app.ui.widgets.buttons import style_all

__all__ = ["SourceSelectionDialog"]


class SourceSelectionDialog(QDialog):
    """Every catalogued source, checked by default - uncheck any to leave
    it (and everything under it) out of the exported document entirely."""

    def __init__(self, sources: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose what to include")
        self.setMinimumWidth(380)

        intro = QLabel(
            "Every source is included by default. Uncheck any you would "
            "rather leave out of this export - nothing on the drive itself "
            "is affected either way."
        )
        intro.setWordWrap(True)

        self._list = QListWidget()
        self._list.setAccessibleName("Sources to include")
        for source in sources or ():
            item = QListWidgetItem(
                f"{source.name} ({source.file_count:,} file"
                f"{'s' if source.file_count != 1 else ''})")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, source.name)
            self._list.addItem(item)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Export")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self._list)
        layout.addWidget(self.buttons)
        # The button system (widgets/buttons.py): every action button in
        # here gets its icon, its kind and its natural width.
        style_all(self)

    def excluded_names(self) -> set:
        """The names left unchecked - `ReportsView._export_to`'s own
        `include=` flag, decided here rather than guessed from a
        checkbox state the caller would have to reinterpret."""
        excluded = set()
        for index in range(self._list.count()):
            item = self._list.item(index)
            if item.checkState() != Qt.CheckState.Checked:
                excluded.add(item.data(Qt.ItemDataRole.UserRole))
        return excluded
