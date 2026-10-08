r"""A spreadsheet, shown as a spreadsheet. Workspace §4b.

Layer: L5

**A view over data that already exists.** Every `SheetGrid` this widget draws
was read on a worker inside `preview_loader._spreadsheet_preview`; nothing
here opens a file or a workbook. That split is what keeps `data()` and
`sizeHint()` - the paint path `test_ui_never_blocks.py` watches - free of the
one thing that would freeze the window: reading cells while a row is being
painted.

**Sheet tabs, one `QTableView` per sheet.** A workbook rarely has more than a
handful of sheets, so building one small, cheap model per sheet up front
costs nothing and means switching tabs is instant rather than a second read.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import QTabWidget, QTableView

from app.extract.cells import cached_letter

__all__ = ["SheetTableModel", "SpreadsheetView"]


class SheetTableModel(QAbstractTableModel):
    """One `SheetGrid`, as a Qt table model. Read-only - this is a preview."""

    def __init__(self, sheet: Any, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self._rows: tuple[tuple[str, ...], ...] = tuple(getattr(sheet, "rows", ()) or ())
        self._columns = max((len(row) for row in self._rows), default=0)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:      # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:   # noqa: N802
        return 0 if parent.isValid() else self._columns

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        # **Arithmetic only.** `self._rows` was built off the UI thread; this
        # is a list index and a bounds check, which is what makes it safe to
        # call once per visible cell.
        if role != Qt.ItemDataRole.DisplayRole or not index.isValid():
            return None
        row = self._rows[index.row()]
        column = index.column()
        return row[column] if column < len(row) else ""

    def headerData(self, section: int, orientation: Qt.Orientation,     # noqa: N802
                   role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        """Column letters across (A, B ... AA) and row numbers down, as a spreadsheet shows."""
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            # The same lettering a spreadsheet already uses - A, B, ... AA -
            # so a locator like `Q3!B14` points at a column somebody can find
            # by eye rather than by counting.
            return cached_letter(section + 1)
        return str(section + 1)


class SpreadsheetView(QTabWidget):
    """One tab per sheet, each holding a read-only `QTableView`."""

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self.setAccessibleName("Spreadsheet preview")
        self.setDocumentMode(True)

    def show_sheets(self, sheets: Any) -> None:
        """Rebuild the tabs from a list of `SheetGrid`. UI thread; no I/O -
        every sheet's cells already arrived from the worker."""
        while self.count():
            widget = self.widget(0)
            self.removeTab(0)
            widget.deleteLater()

        for sheet in sheets or ():
            table = QTableView()
            table.setAccessibleName(f"Sheet: {getattr(sheet, 'name', '')}")
            table.setModel(SheetTableModel(sheet, table))
            table.setAlternatingRowColors(True)
            table.setEditTriggers(QTableView.EditTrigger.NoEditTriggers)
            table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
            table.horizontalHeader().setStretchLastSection(False)
            self.addTab(table, str(getattr(sheet, "name", "") or "Sheet"))
