r"""Saving, renaming and deleting a search. Adoptions section 3 (`202626271137`).

Layer: L5

Saved searches were finished underneath - the table, `saved:name`, the `/saved`
menu, the empty-box list - and the order's own note admits *"the save/rename/
delete dialog ... doesn't exist yet"*. So a person could reach a saved search
but never make one. These are the two missing dialogs.

**Nothing saves itself and nothing suggests saving** (section 3b's rule).
`suggest_name` only fills the field of a dialog the person opened, and they can
replace every character of it. Every write goes through `SavedSearches`, which
does it on a worker - the interface thread never touches the store.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from app.search.saved import clean_name, suggest_name
from app.ui.widgets.buttons import style_all

__all__ = ["ask_to_save", "SavedSearchesDialog"]

ROLE_NAME = int(Qt.ItemDataRole.UserRole)


def ask_to_save(parent: QWidget, saved: Any, query: str, scope: str = "all") -> bool:
    """Ask for a name, then save the current search under it.

    Returns True when a save was started. An empty box says so rather than
    saving nothing quietly, and a name that cleans to nothing is refused
    plainly - `clean_name` strips only what the grammar would misread.
    """
    text = str(query or "").strip()
    if not text:
        QMessageBox.information(
            parent, "Save this search", "Type a search first, then save it.")
        return False
    name, accepted = QInputDialog.getText(
        parent, "Save this search", "Name it, so you can find it again:",
        text=suggest_name(text))
    if not accepted:
        return False
    if not clean_name(name):
        QMessageBox.information(
            parent, "Save this search", "That name has nothing usable in it. Try a few words.")
        return False
    saved.save(name, text, scope)
    return True


class SavedSearchesDialog(QDialog):
    """The list of saved searches, with run, rename and delete."""

    #: `saved:name` for the search to run now - the window decides how.
    run_requested = pyqtSignal(str)

    def __init__(self, saved: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._saved = saved
        self.setWindowTitle("Saved searches")
        self.resize(460, 360)

        self.list = QListWidget()
        self.list.setAccessibleName("Saved searches")
        self.list.itemDoubleClicked.connect(lambda _item: self._run())
        self.empty = QLabel(
            "Nothing saved yet. Search for something, then choose "
            "Edit > Save this search.")
        self.empty.setWordWrap(True)

        self.run_button = QPushButton("Search")
        self.run_button.setToolTip("Run the selected saved search now.")
        self.run_button.clicked.connect(self._run)
        self.rename_button = QPushButton("Rename…")
        self.rename_button.setToolTip(
            "Give the selected saved search a different name. The search "
            "itself is not changed.")
        self.rename_button.clicked.connect(self._rename)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setToolTip(
            "Forget the selected saved search. Nothing else is touched.")
        self.delete_button.clicked.connect(self._delete)
        close = QPushButton("Close")
        close.setToolTip("Close this list. Nothing is changed by closing it.")
        close.clicked.connect(self.accept)

        buttons = QHBoxLayout()
        for button in (self.run_button, self.rename_button, self.delete_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(close)

        layout = QVBoxLayout(self)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.empty)
        layout.addLayout(buttons)
        self.list.itemSelectionChanged.connect(self._sync)
        self.reload()
        # The button system (widgets/buttons.py): every action button in
        # here gets its icon, its kind and its natural width.
        style_all(self)

    def reload(self) -> None:
        """Redraw from what `SavedSearches` currently holds."""
        self.list.clear()
        for one in self._saved.all:
            scope = f"  ({one.scope})" if one.scope and one.scope != "all" else ""
            item = QListWidgetItem(f"{one.name}  -  {one.query}{scope}")
            item.setData(ROLE_NAME, one.name)
            self.list.addItem(item)
        self.empty.setVisible(self.list.count() == 0)
        if self.list.count():
            self.list.setCurrentRow(0)
        self._sync()

    def _selected(self) -> Optional[str]:
        item = self.list.currentItem()
        return str(item.data(ROLE_NAME)) if item is not None else None

    def _sync(self) -> None:
        has = self._selected() is not None
        for button in (self.run_button, self.rename_button, self.delete_button):
            button.setEnabled(has)

    def _run(self) -> None:
        name = self._selected()
        if name is None:
            return
        for one in self._saved.all:
            if one.name == name:
                self.run_requested.emit(one.as_token())
                self.accept()
                return

    def _rename(self) -> None:
        old = self._selected()
        if old is None:
            return
        new, accepted = QInputDialog.getText(
            self, "Rename saved search", "New name:", text=old)
        if accepted and clean_name(new) and clean_name(new) != old:
            self._saved.rename(old, new, self.reload)

    def _delete(self) -> None:
        name = self._selected()
        if name is not None:
            self._saved.delete(name, self.reload)
