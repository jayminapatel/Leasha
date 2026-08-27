r"""The folders to index, and which of them never change.

Layer: L6 (UI), driving L3

**Why this is a table and not a list.** It used to be a `QListWidget` of paths,
which is right while every folder costs the same to re-walk. It stopped being
right at a terabyte: the recurring cost of a large corpus is the walk that
re-discovers, on every pass, that a fifteen-year archive is still fifteen years
old. Declaring a folder static is the single largest saving available, and there
is no way for the application to work it out for itself - only the owner knows
that `D:\Archive\2009` is finished.

**Per folder, never globally.** A corpus is nearly always both: the mail folder
that changes hourly sits beside twelve years of project files that do not. One
switch for the lot would be turned off by whoever owns the first of those, and
would then protect nothing.

The decision itself lives in `app/index/archives.py` and is tested there. This
is widgets: two columns, a combo box per row, and a button that asks for a full
rescan when somebody knows something the mtime check cannot see.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from app.index.archives import ARCHIVE, LIVE, normalise
from app.ui.presenter import nothing_indexed_yet, suggested_roots
from app.ui.widgets.result_table import align_headers

__all__ = ["RootsBox"]

#: The wording is the feature. "Archive" alone invites the reading "put this
#: away", which is the opposite of what it does - the folder stays indexed and
#: searchable, it is only re-checked cheaply.
CHOICES = (
    ("Live - re-check every run", LIVE),
    ("Archive - it does not change", ARCHIVE),
)


class RootsBox(QGroupBox):
    """Folders to index, each marked Live or Archive."""

    roots_changed = pyqtSignal(list)
    modes_changed = pyqtSignal(dict)
    rescan_requested = pyqtSignal()

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__("Folders to index", parent)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Folder", "How it is indexed"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        # §2b. **Not sortable**, and the reason is that the order is the
        # person's: `current_roots` serialises what is on screen, so a header
        # click would silently rewrite the saved list - and column 1 is a
        # `QComboBox` per row through `setItemWidget`, which Qt does not move
        # when it sorts. The heading still points the way its column reads.
        align_headers(self.tree)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)

        add = QPushButton("Add folder…")
        add.setToolTip(
            "Add a folder to index. Everything beneath it is included; your "
            "files are only ever read.")
        add.clicked.connect(self._add_root)
        remove = QPushButton("Remove")
        remove.setToolTip(
            "Stop indexing this folder.\n\n"
            "What has already been indexed from it stays searchable until the "
            "next run, which removes it.")
        remove.clicked.connect(self._remove_root)

        # **Separate from the mode, deliberately.** Changing a folder back to
        # Live would also cause a full walk, but it would leave it walking in
        # full for ever - and somebody who dropped one folder into an archive
        # wants one full pass, not a permanent change of policy.
        self.rescan = QPushButton("Rescan archived folders now")
        self.rescan.setToolTip(
            "Walk every folder marked as an archive in full, once.\n"
            "Archives are re-walked automatically when the folder itself changes\n"
            "and after the interval in the Indexing settings - this is for when\n"
            "you know something changed deeper inside one."
        )
        self.rescan.clicked.connect(lambda _checked=False: self.rescan_requested.emit())

        buttons = QHBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        buttons.addWidget(self.rescan)

        self.note = QLabel(
            "An archive is walked once and then checked with a single look at the "
            "folder's own timestamp, so an incremental run over a settled corpus "
            "takes seconds instead of hours. It is still indexed and still "
            "searchable - and it is still re-walked when it changes."
        )
        self.note.setWordWrap(True)

        # **The first-run offer. Offered, never taken.** Nothing is ever added
        # to somebody's index without them clicking it - see the privacy
        # defaults order. Hidden the moment there is a root, so an existing
        # install never sees this at all.
        self.empty = QLabel(nothing_indexed_yet([]))
        self.empty.setWordWrap(True)

        self.suggestions = QHBoxLayout()
        self.suggest_all = QPushButton("Add all four")
        self.suggest_all.setToolTip(
            "Add Documents, Desktop, Downloads and Pictures.\n\n"
            "You can remove any of them afterwards, and nothing is indexed "
            "until you press Index.")
        self.suggest_all.clicked.connect(self._add_every_suggestion)

        layout = QVBoxLayout(self)
        layout.addWidget(self.empty)
        layout.addLayout(self.suggestions)
        layout.addWidget(self.tree)
        layout.addLayout(buttons)
        layout.addWidget(self.note)
        self._offer_suggestions()
        self._sync_rescan()

    # -- the first-run offer ------------------------------------------------

    def _offer_suggestions(self) -> None:
        """One button per profile folder that exists, plus "add all four".

        The list comes from the presenter, which is where the rule that none of
        them may point outside this account lives - a view deciding that for
        itself is how a default ends up in somebody else's profile.
        """
        while self.suggestions.count():
            item = self.suggestions.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        self._suggested = suggested_roots()
        for folder in self._suggested:
            button = QPushButton(folder)
            button.setToolTip(f"Add {folder} to the folders Leasha indexes.")
            button.clicked.connect(
                lambda _checked=False, path=folder: self._accept_suggestion(path))
            self.suggestions.addWidget(button)
        if self._suggested:
            self.suggestions.addWidget(self.suggest_all)
        self.suggestions.addStretch(1)

    def _accept_suggestion(self, folder: str) -> None:
        if self.add_root(folder):
            self._sync_empty()

    def _add_every_suggestion(self) -> None:
        added = False
        for folder in list(self._suggested):
            added = self.add_root(folder) or added
        if added:
            self._sync_empty()

    def _sync_empty(self) -> None:
        """Show the offer only while there is nothing to search."""
        empty = self.tree.topLevelItemCount() == 0
        self.empty.setText(nothing_indexed_yet(self.current_roots()))
        self.empty.setVisible(empty)
        self.suggest_all.setVisible(empty and bool(getattr(self, "_suggested", ())))
        for index in range(self.suggestions.count()):
            widget = self.suggestions.itemAt(index).widget()
            if widget is not None:
                widget.setVisible(empty)

    # -- filling it in ------------------------------------------------------

    def set_roots(self, roots: list[str], modes: Optional[dict[str, str]] = None) -> None:
        """Replace the list, without emitting on the way in.

        Loading nine folders would otherwise fire nine change notifications
        before the panel has been shown, each writing a partial list over the
        stored one - the same trap `IndexingSettings.load_indexing` blocks
        signals for.
        """
        modes = modes or {}
        self.tree.blockSignals(True)
        try:
            self.tree.clear()
            for root in roots:
                self._append(root, modes.get(normalise(root), LIVE))
        finally:
            self.tree.blockSignals(False)
        self._sync_empty()
        self._sync_rescan()

    def _append(self, root: str, mode: str = LIVE) -> QTreeWidgetItem:
        item = QTreeWidgetItem([str(root), ""])
        item.setToolTip(0, str(root))
        self.tree.addTopLevelItem(item)

        combo = QComboBox()
        combo.setToolTip(
            "How often this folder is re-read.\n\n"
            "Live is re-walked every run. Archive is walked once and then "
            "checked with a single stat - the largest saving available on a "
            "settled corpus, and still re-walked when the folder changes, when "
            "you press Rescan, or after the interval in Settings.")
        for label, value in CHOICES:
            combo.addItem(label, value)
        index = combo.findData(mode)
        combo.setCurrentIndex(index if index >= 0 else 0)
        # A wheel over a combo inside a scrolling panel changes the value while
        # the person is trying to scroll past it - already fixed once across
        # Settings, and re-applied here rather than rediscovered.
        combo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        combo.currentIndexChanged.connect(lambda _i: self._modes_changed())
        self.tree.setItemWidget(item, 1, combo)
        return item

    # -- what the shell reads -----------------------------------------------

    def current_roots(self) -> list[str]:
        return [
            self.tree.topLevelItem(row).text(0)
            for row in range(self.tree.topLevelItemCount())
        ]

    def current_modes(self) -> dict[str, str]:
        """`{normalised root: mode}` for every row, including the live ones.

        The *writer* drops the live entries - see `archives.dump_modes` - but
        this returns them, because a caller comparing two states needs to see
        that a folder moved back to Live rather than merely disappearing.
        """
        found: dict[str, str] = {}
        for row in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(row)
            combo = self.tree.itemWidget(item, 1)
            mode = str(combo.currentData()) if combo is not None else LIVE
            found[normalise(item.text(0))] = mode
        return found

    def add_root(self, folder: str) -> bool:
        """Add one folder if it is not already there. Returns whether it was."""
        if not folder or normalise(folder) in {
            normalise(existing) for existing in self.current_roots()
        }:
            return False
        self._append(folder)
        self._emit()
        return True

    # -- events -------------------------------------------------------------

    def _add_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder to index")
        if folder:
            self.add_root(folder)

    def _remove_root(self) -> None:
        for item in self.tree.selectedItems():
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
        self._emit()

    def _modes_changed(self) -> None:
        self.modes_changed.emit(self.current_modes())
        self._sync_rescan()

    def _emit(self) -> None:
        # Roots first: a mode for a folder that is not in the list yet would be
        # written and then have nothing to attach to.
        self.roots_changed.emit(self.current_roots())
        self.modes_changed.emit(self.current_modes())
        self._sync_rescan()

    def _sync_rescan(self) -> None:
        """A button that does nothing is worse than one that is not there.

        Disabled rather than hidden, with a tooltip that says why - a control
        that appears and disappears as rows change reads as a glitch.
        """
        any_archive = ARCHIVE in self.current_modes().values()
        self.rescan.setEnabled(any_archive)
        if not any_archive:
            self.rescan.setToolTip("No folder is marked as an archive yet.")
