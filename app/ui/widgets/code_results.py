r"""The Code tab's results: one table, one preview, both engines' rows.

Layer: L5

Split out of `code_view.py` when it crossed the 250-line guard - the same route
`SearchBox`, `ModelBox` and `EnvironmentBox` took, and for the same reason: a
view that keeps growing is a view where logic starts to live. What stayed behind
is the box and the routing; what moved here is everything about drawing a row.

**One table for both engines.** A file in the checkout and a commit that touched
it are the same four questions - what, which repository, when, where - so they
share a table rather than being swapped between two. `presenter.git_result_row`
is what makes a git result answer them; nothing here knows the difference.

**A historical hit has no file to open**, and the menu says so by not offering
it rather than by offering a command that fails. That is the one place the two
kinds of row genuinely differ, and it is a property of the row, not of the
table.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QTableWidgetItem, QVBoxLayout, QWidget

from app.ui.view_options import apply_to_table, available_columns
from app.ui.widgets.file_menu import FileActions, show_for, viewport_point
from app.ui.widgets.preview import attach_preview
from app.ui.widgets.result_table import ResultTable

__all__ = ["CodeResults", "COLUMNS", "ALWAYS_OFFERED"]

#: (key, heading, attribute, right-aligned?).
COLUMNS: tuple[tuple[str, str, str, bool], ...] = (
    ("name", "Name", "name", False),
    ("repo", "Repository", "repo", False),
    ("kind", "Kind", "kind", False),
    ("seen", "When", "seen", False),
    ("path", "Where", "path", False),
)

#: A code list with no names is not a list.
ALWAYS_OFFERED = ("name",)


class CodeResults(QWidget):
    """The table, the preview beside it, and the menu on a row."""

    error = pyqtSignal(object)
    open_requested = pyqtSignal(str)
    reveal_requested = pyqtSignal(str)
    search_repo_requested = pyqtSignal(str)
    #: Somebody right-clicked the header. The view owns the View button.
    view_menu_requested = pyqtSignal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.available: tuple[str, ...] = tuple(key for key, *_ in COLUMNS)

        self.table = ResultTable([heading for _k, heading, _a, _r in COLUMNS])
        self.table.setAccessibleName("Code files and repository history")
        self.table.itemDoubleClicked.connect(lambda _i: self.open_selected())
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)

        header = self.table.horizontalHeader()
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(
            lambda point: self.view_menu_requested.emit(
                header.mapToGlobal(point)))

        self.preview, self.split = attach_preview(
            self.table, lambda _row: self.open_selected(), self.error.emit)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.split, 1)

    # -- drawing -------------------------------------------------------------

    def show_rows(self, rows: list[Any], prefs: Any) -> None:
        """Replace the list. `rows` are `RepoFileRow`s from either engine."""
        self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            for column, (_k, _h, attribute, _r) in enumerate(COLUMNS):
                item = QTableWidgetItem(str(getattr(row, attribute, "") or ""))
                if column == 0:
                    item.setToolTip(getattr(row, "full_path", "") or "")
                self.table.setItem(index, column, item)
        self.table.set_row_objects(rows)
        self.available = available_columns(
            rows, [(key, attribute) for key, _h, attribute, _r in COLUMNS],
            always=ALWAYS_OFFERED,
        )
        self.apply_prefs(prefs)

    def apply_prefs(self, prefs: Any) -> None:
        apply_to_table(
            self.table, prefs,
            columns=[(key, heading) for key, heading, _a, _r in COLUMNS],
            available=self.available,
        )
        self.preview.apply_preference(prefs, self.table.current_row())

    def shutdown(self) -> None:
        self.preview.shutdown()

    # -- acting on a row -----------------------------------------------------

    def current_row(self) -> Any:
        return self.table.current_row()

    def selected_repo(self) -> str:
        return str(getattr(self.table.current_row(), "repo", "") or "")

    def open_selected(self) -> None:
        path = getattr(self.table.current_row(), "full_path", "")
        if path:
            self.open_requested.emit(path)

    def _on_context_menu(self, point: Any) -> None:
        """The same menu as every other list - see widgets/file_menu.py."""
        row_at = self.table.rowAt(viewport_point(self.table, point).y())
        if row_at >= 0:
            self.table.selectRow(row_at)
        row = self.table.current_row()
        if row is None:
            return

        path = str(getattr(row, "full_path", "") or "")
        name = str(getattr(row, "repo", "") or "")
        show_for(self.table, point, path, FileActions(
            # **A historical hit has no file on disk.** The version that
            # matched is gone, so the menu leaves those out rather than
            # offering a command that fails.
            open_file=(lambda: self.open_requested.emit(path)) if path else None,
            reveal=(lambda: self.reveal_requested.emit(path)) if path else None,
            search_inside=((lambda: self.search_repo_requested.emit(name))
                           if name else None),
            copy=[("Copy repository name", name), ("Copy full path", path)],
        ))
