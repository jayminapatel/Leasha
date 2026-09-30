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
from PyQt6.QtWidgets import QVBoxLayout, QWidget

from app.ui.editors import copyable
from app.ui.view_options import apply_to_table, available_columns, weak_slot
from app.ui.widgets.file_menu import FileActions, show_for, viewport_point
from app.ui.widgets.preview import attach_preview
from app.ui.widgets.result_table import ResultTable
from app.ui.widgets.status_column import fill_rows

__all__ = ["CodeResults", "COLUMNS", "ALWAYS_OFFERED"]

#: (key, heading, attribute, right-aligned?).
#:
#: **Every column a code row can fill**, asked for directly: *"on code list all
#: all columns which can be viewed for code"*. Which of them are *offered* is
#: still decided by the rows on screen - `available_columns` hides a column no
#: row can fill, because a column of blanks takes width from the ones that
#: matter and reads as a broken index.
#:
#: `full` is the whole path where `path` is shortened for the column. Both are
#: offered because they answer different questions: "where in the repository"
#: and "where on this machine", and the second is the one somebody copies.
COLUMNS: tuple[tuple[str, str, str, bool], ...] = (
    ("name", "Name", "name", False),
    # Order 0y §2: why the row is here, the line, and the line of code. Only
    # offered once something is typed - `available_columns` hides a column no
    # row fills, so a plain file list looks exactly as it did.
    ("match", "Match", "match", False),
    ("line", "Line", "line", True),
    ("code", "Code", "code", False),
    ("repo", "Repository", "repo", False),
    ("kind", "Kind", "kind", False),
    ("size", "Size", "size", True),
    ("seen", "When", "seen", False),
    ("status", "Status", "status", False),
    ("path", "Where", "path", False),
    ("full", "Full path", "full_path", False),
)

#: A code list with no names is not a list, and one that cannot say which
#: repository a file is in is the tree this replaced.
ALWAYS_OFFERED = ("name", "repo")

#: What each column sorts on when it is not the text in it - attribute on
#: `RepoFileRow`. `size` reads "12.4 KB" and `seen` reads "2 hours ago", and
#: sorted as text both are wrong in the way people notice immediately.
SORT_KEYS: dict[str, str] = {"size": "size_bytes", "seen": "seen_at", "line": "line_no"}


class CodeResults(QWidget):
    """The table, the preview beside it, and the menu on a row."""

    error = pyqtSignal(object)
    open_requested = pyqtSignal(str)
    #: Order 0y §2c: a row that knows its line - the path, and the line.
    open_at_requested = pyqtSignal(str, int)
    reveal_requested = pyqtSignal(str)
    search_repo_requested = pyqtSignal(str)
    #: Order 0y §1c: "Ignore this repository", with the repository's name.
    ignore_repo_requested = pyqtSignal(str)
    #: Somebody right-clicked the header. The view owns the View button.
    view_menu_requested = pyqtSignal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.available: tuple[str, ...] = tuple(key for key, *_ in COLUMNS)

        # **Ranked, and now sortable.** These rows arrive in match order and a
        # header click used to be refused for that reason; the ranking is kept
        # in a hidden column instead, and a third click returns to it.
        self.table = ResultTable(
            [heading for _k, heading, _a, _r in COLUMNS], ranked=True,
            aligns=["right" if right else "left" for *_rest, right in COLUMNS])
        self.table.setAccessibleName("Code files and repository history")
        # 2026-09-30: methods and `weak_slot`, never `lambda: self...` - see
        # `view_options.weak_slot` for the view that could not be freed.
        self.table.itemDoubleClicked.connect(self.open_selected)
        # Order 0y §2c: Enter opens the row, as a double-click does. The table
        # had no key for it, which fails the keyboard-only requirement.
        self.table.installEventFilter(self)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)

        header = self.table.horizontalHeader()
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(weak_slot(self, lambda results, point: (
            results.view_menu_requested.emit(
                results.table.horizontalHeader().mapToGlobal(point)))))

        self.preview, self.split = attach_preview(
            self.table, self.open_selected, self.error.emit)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.split, 1)

    # -- drawing -------------------------------------------------------------

    def show_rows(self, rows: list[Any], prefs: Any) -> None:
        """Replace the list. `rows` are `RepoFileRow`s from either engine."""
        # **The right-aligned flag used to be destructured into a throwaway
        # here**, so `Size` was declared right-aligned in `COLUMNS` and
        # rendered left for the life of this table - the kind of thing a
        # declaration in one place and a loop in another produces. The table
        # reads the spec now. 2026-09-29: the loop itself is the one Files and
        # Mail use (`status_column.fill_rows`), which also gives the Status
        # cell its one-sentence tooltip.
        fill_rows(self.table, rows, COLUMNS, SORT_KEYS, first=lambda item, row:
                  item.setToolTip(getattr(row, "full_path", "") or ""))
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

    def open_selected(self, _from: Any = None) -> None:
        """Open the highlighted row: at its line when it has one (order 0y §2c).

        `_from` is whatever a signal sent along - the cell double-clicked, the
        preview pane's row - and is not used.

        A row that knows its line - a Definition, a Mention, a git hit in the
        checkout - is a place, and goes to the person's editor. A row with no
        line opens as it always did. A historical row has no file and opens
        nothing; its commit is in the preview pane.
        """
        row = self.table.current_row()
        path = str(getattr(row, "full_path", "") or "")
        line = int(getattr(row, "line_no", 0) or 0)
        if path and line > 0:
            self.open_at_requested.emit(path, line)
        elif path:
            self.open_requested.emit(path)

    def eventFilter(self, watched: Any, event: Any) -> bool:   # noqa: N802 - Qt's naming
        """Enter opens the selected row - the same rule `files_view` has."""
        from PyQt6.QtCore import QEvent

        if watched is self.table and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self.open_selected()
                return True
        return super().eventFilter(watched, event)

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
        line = int(getattr(row, "line_no", 0) or 0)
        show_for(self.table, point, path, FileActions(
            # **A historical hit has no file on disk.** The version that
            # matched is gone, so the menu leaves those out rather than
            # offering a command that fails.
            open_file=self.open_selected if path else None,
            reveal=(lambda: self.reveal_requested.emit(path)) if path else None,
            search_inside=((lambda: self.search_repo_requested.emit(name))
                           if name else None),
            copy=[("Copy repository name", name), ("Copy full path", path)]
            # Order 0y §2c: what the Settings note has long promised - `path:line`,
            # which pastes into a terminal, a chat message or another editor.
            + ([("Copy path and line", copyable(path, line))] if path and line else []),
            extra=[("Ignore this repository",
                    "This folder is not really a repository. Its files stay "
                    "indexed and searchable; they stop counting as code. "
                    "You can undo it.",
                    lambda: self.ignore_repo_requested.emit(name))] if name else [],
        ))
