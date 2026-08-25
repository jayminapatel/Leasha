r"""Repositories, with the files inside them.

Layer: L5

**Two levels, one set of columns.** A repository and a file are different kinds
of thing, but the five questions asked of both are the same: what is it called,
how big is it, what kind is it, when did it last change, where does it live. So
a repository's `Indexed files` column holds a count and a file's holds a size,
and column three is a kind either way. Two tables stacked would have needed two
headers and taught the reader two layouts to say the same thing.

**Children load when a repository is opened, and not before.** Counting files is
cheap - `repos_list` does it in the same query - but listing them is not, and
most repositories in the list are not the one being looked for. Building 48,000
items for every repository on the chance one gets expanded is the difference
between a tab that opens instantly and one that appears to hang. The parent asks
via `files_requested`; this widget never touches the store.

**A capped list, honestly labelled.** Past `REPO_FILE_LIMIT` the tree stops and
says so, and points at the search box - which is one tab away, takes `repo:` and
was built for the question "which file says this". A tree is for looking at what
is there, not for scrolling through fifty thousand rows.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QHeaderView, QTreeWidget, QTreeWidgetItem, QWidget

from app.ui.presenter import repo_files_summary, repo_visibility
from app.ui.widgets.sortable_item import SORT_ROLE, SortableTreeItem

__all__ = ["RepoTree", "COLUMNS", "ROW_ROLE"]

#: (key, heading, right-aligned?). The order somebody scans.
COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("name", "Repository / file", False),
    ("files", "Files / size", True),
    ("kind", "Kind", False),
    ("seen", "Last seen", True),
    ("path", "Location", False),
)

#: Holds the `RepoRow` or `RepoFileRow` the item was built from. Reading the row
#: back rather than the cell text is what lets the tree sort and still act on
#: the right thing: after a click on a header, visual row three is not row three.
ROW_ROLE = Qt.ItemDataRole.UserRole


class RepoTree(QTreeWidget):
    """Repositories at the top, their indexed files underneath."""

    #: A repository whose files are wanted. Carries the `RepoRow`; the parent
    #: fetches on a worker and answers with `set_files`.
    files_requested = pyqtSignal(object)
    #: A repository row was activated - search inside it. Carries the name.
    repo_activated = pyqtSignal(str)
    #: A file row was activated - open it. Carries the full path.
    file_activated = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._loaded: set[str] = set()

        self.setColumnCount(len(COLUMNS))
        self.setHeaderLabels([heading for _key, heading, _right in COLUMNS])
        self.setRootIsDecorated(True)
        self.setAlternatingRowColors(True)
        self.setUniformRowHeights(True)          # the tree may hold thousands
        self.setSelectionBehavior(QTreeWidget.SelectionBehavior.SelectRows)
        self.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)
        self.setSortingEnabled(True)
        self.sortByColumn(1, Qt.SortOrder.DescendingOrder)   # most files first

        header = self.header()
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        header.setSectionsMovable(True)

        self.itemExpanded.connect(self._on_expanded)
        self.itemActivated.connect(lambda item, _c: self._activate(item))
        self.itemDoubleClicked.connect(lambda item, _c: self._activate(item))

    # -- building ------------------------------------------------------------

    def show_repos(self, rows: Sequence[Any]) -> None:
        """Replace the whole tree. Expansion state does not survive a refresh.

        Deliberately: a refresh means the walk found something different, and
        re-expanding to a stale file list would be worse than a closed row.
        """
        self.setSortingEnabled(False)
        self.clear()
        self._loaded.clear()
        for row in rows:
            item = SortableTreeItem([
                row.name, row.files, row.kind, row.seen, row.path,
            ])
            item.setData(0, ROW_ROLE, row)
            item.setData(1, SORT_ROLE, row.file_count)
            item.setData(3, SORT_ROLE, row.seen_at)
            item.setToolTip(0, row.root)
            self._align(item)
            self.addTopLevelItem(item)
            if row.file_count:
                # A placeholder is what gives the row its expand arrow. Without
                # one Qt draws a leaf, and there is nothing to click to ask for
                # the children - the lazy load would be unreachable.
                item.addChild(QTreeWidgetItem(["Loading…", "", "", "", ""]))
        self.setSortingEnabled(True)

    def set_files(self, repo_name: str, rows: Sequence[Any], *, truncated: int = 0) -> None:
        """Fill in one repository's children, replacing the placeholder."""
        parent = self._repo_item(repo_name)
        if parent is None:
            return                                # refreshed out from under us
        self.setSortingEnabled(False)
        parent.takeChildren()
        for row in rows:
            child = SortableTreeItem([
                row.name, row.size, row.kind, row.seen, row.path,
            ])
            child.setData(0, ROW_ROLE, row)
            child.setData(1, SORT_ROLE, row.size_bytes)
            child.setData(3, SORT_ROLE, row.seen_at)
            child.setToolTip(0, row.full_path)
            self._align(child)
            parent.addChild(child)

        if truncated:
            note = QTreeWidgetItem([repo_files_summary(truncated), "", "", "", ""])
            note.setFlags(Qt.ItemFlag.ItemIsEnabled)      # not selectable
            note.setFirstColumnSpanned(True)
            parent.addChild(note)
        if not rows and not truncated:
            empty = QTreeWidgetItem(["No indexed files in this repository.",
                                     "", "", "", ""])
            empty.setFlags(Qt.ItemFlag.ItemIsEnabled)
            parent.addChild(empty)
        self._loaded.add(repo_name)
        self.setSortingEnabled(True)

    def failed(self, repo_name: str, message: str) -> None:
        """Say why the children are missing, in the row that has none.

        An expand arrow that opens onto nothing is indistinguishable from a
        repository with no files, and the difference matters.
        """
        parent = self._repo_item(repo_name)
        if parent is None:
            return
        parent.takeChildren()
        item = QTreeWidgetItem([f"Could not list these files: {message}",
                                "", "", "", ""])
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        parent.addChild(item)
        self._loaded.discard(repo_name)           # so collapsing and retrying works

    @staticmethod
    def _align(item: QTreeWidgetItem) -> None:
        for column, (_key, _heading, right) in enumerate(COLUMNS):
            if right:
                item.setTextAlignment(
                    column, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    # -- filtering -----------------------------------------------------------

    def apply_filter(self, chosen: Any) -> int:
        """Hide what does not match. Returns how many repositories are shown.

        The rule itself lives in `presenter.repo_visibility`, where it can be
        tested without a window; this walks the tree and applies the answer.
        """
        shown = 0
        for index in range(self.topLevelItemCount()):
            item = self.topLevelItem(index)
            row = item.data(0, ROW_ROLE)
            if row is None:
                continue
            children = [item.child(n) for n in range(item.childCount())]
            files = [(child.data(0, ROW_ROLE).ext,
                      f"{child.text(0)} {child.text(2)} {child.text(4)}".lower())
                     for child in children if child.data(0, ROW_ROLE) is not None]

            visible, flags = repo_visibility(
                chosen, row.name, f"{row.name} {row.kind} {row.path}".lower(), files)
            item.setHidden(not visible)
            shown += 1 if visible else 0

            hidden_flags = iter(flags)
            for child in children:
                if child.data(0, ROW_ROLE) is None:
                    continue                      # a note, a placeholder, an error
                child.setHidden(not next(hidden_flags, True))
        return shown

    # -- acting on a row -----------------------------------------------------

    def selected_row(self) -> Any:
        """The `RepoRow` or `RepoFileRow` under the selection, or None."""
        items = self.selectedItems()
        return items[0].data(0, ROW_ROLE) if items else None

    def selected_repo(self) -> str:
        """The repository the selection belongs to, whichever level it is on.

        A file's context menu should offer to search the repository holding it,
        so this walks up rather than returning nothing for a child row.
        """
        items = self.selectedItems()
        if not items:
            return ""
        item: Optional[QTreeWidgetItem] = items[0]
        while item is not None:
            row = item.data(0, ROW_ROLE)
            if row is not None and hasattr(row, "root"):
                return str(row.name)
            item = item.parent()
        return ""

    def _repo_item(self, name: str) -> Optional[QTreeWidgetItem]:
        for index in range(self.topLevelItemCount()):
            item = self.topLevelItem(index)
            row = item.data(0, ROW_ROLE)
            if row is not None and row.name == name:
                return item
        return None

    def _on_expanded(self, item: QTreeWidgetItem) -> None:
        row = item.data(0, ROW_ROLE)
        if row is None or not hasattr(row, "root") or row.name in self._loaded:
            return
        self.files_requested.emit(row)

    def _activate(self, item: QTreeWidgetItem) -> None:
        """Enter and double-click. What that means depends on the level.

        On a repository it searches inside it; on a file it opens the file. Both
        are the obvious next thing to want from the row you are on, which is why
        one key does both rather than one key and a modifier.
        """
        row = item.data(0, ROW_ROLE)
        if row is None:
            return
        if hasattr(row, "root"):
            self.repo_activated.emit(row.name)
        else:
            self.file_activated.emit(row.full_path)

    def keyPressEvent(self, event: Any) -> None:           # noqa: N802 - Qt's naming
        """Return and Enter both activate - the keypad sends the other one."""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            items = self.selectedItems()
            if items:
                self._activate(items[0])
                event.accept()
                return
        super().keyPressEvent(event)
