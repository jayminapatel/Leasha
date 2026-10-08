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

2026-10-08, the owner: "the columns on this page are not sizeable and should
autofit by default.. also the buttons size is big they are getting clipped".
Every column can be dragged and is fitted to its contents when the rows
change; Folder takes the spare width and elides in the middle; each line is
as tall as its controls (`fitted_tree.py`).
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.index.archives import ARCHIVE, LIVE, normalise
from app.ui.presenter import nothing_indexed_yet, suggested_roots
from app.ui.widgets.buttons import icon_button, put_on_row, style_button
from app.ui.widgets.fitted_tree import FittedColumns
from app.ui.widgets.result_table import align_headers
from app.ui.qtsip import open_menu

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

    roots_changed = Signal(list)
    modes_changed = Signal(dict)
    #: 202626270514 §2b: `{normalised root}` currently opted in for cloud
    #: content. Only fires the way `modes_changed` does - user action, never
    #: `set_roots`'s own load.
    cloud_content_changed = Signal(set)
    rescan_requested = Signal()
    #: 2026-09-29. The folders marked "Index this folder first", **in order**
    #: - the order they were marked in, which is the order they are read in.
    #: Fires on the person's action only, like the two above.
    first_changed = Signal(list)
    #: 2026-10-02. "Index now" on one line: the folder, exactly as its row
    #: spells it. The shell starts a run that reads that folder and no other.
    index_requested = Signal(str)
    #: 2026-10-07, the owner: removing a folder removes what was read from it.
    #: The folders selected when Remove was pressed, as their rows spell them.
    #: Fired instead of removing them when `confirms_removal` is set - the
    #: window then asks, deletes, and calls `remove_roots` once that is done.
    remove_requested = Signal(list)
    #: "Remove them from the index" on the leftovers line (`set_leftovers`).
    leftovers_requested = Signal()

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__("Folders to index", parent)

        #: The "first" folders, in the order they were marked. Kept here rather
        #: than read back from the rows, because the order is the setting and
        #: the rows are in the order the folders were added.
        self._first: list[str] = []
        #: Set by the Settings page, which asks before removing and takes the
        #: folder's data out of the index. Left False, Remove takes the row off
        #: at once - a box on its own has no index to ask about.
        self.confirms_removal = False

        self.tree = QTreeWidget()
        self.tree.setColumnCount(5)
        # 2026-10-02: a fifth column, after the four that were there - each
        # line's own "Index now" (`_append`).
        self.tree.setHeaderLabels(
            ["Folder", "How it is indexed", "Cloud content", "Read first", "Index now"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        # §2b. **Not sortable**, and the reason is that the order is the
        # person's: `current_roots` serialises what is on screen, so a header
        # click would silently rewrite the saved list - and columns 1 and 2
        # are a `QComboBox`/`QCheckBox` per row through `setItemWidget`,
        # which Qt does not move when it sorts. The heading still points the
        # way its column reads.
        align_headers(self.tree)
        # 2026-10-08: every column draggable, fitted to its contents whenever
        # the rows change. Folder takes the spare width - the one column that
        # needs it - and the last column stays a button's own width.
        self.columns = FittedColumns(self.tree, stretch=0)
        # 2026-09-29. The row action, on the row: right-click a folder.
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._row_menu)

        add = QPushButton("Add folder…")
        add.setToolTip(
            "Add a folder to index. Everything beneath it is included; your "
            "files are only ever read.")
        add.clicked.connect(self._add_root)
        # 2026-10-03, the owner: "can it be file to index" - one archive out of
        # a folder of them, without taking the folder. A file entry is walked,
        # classified and pruned by the same rules as a file met inside a
        # folder (`walker.walk`); it can be Live or Archive, "first", and
        # "Index now" like any line. Not watched by "Index files as they are
        # saved" - that listens to folders (`folder_watch.live_roots`).
        add_file = QPushButton("Add file…")
        add_file.setToolTip(
            "Add one file to index - a mail archive, say - without adding the "
            "folder it sits in. It is indexed with the folders; everything "
            "else in its folder is left alone.")
        add_file.clicked.connect(self._add_file)
        remove = QPushButton("Remove")
        remove.setToolTip(
            "Stop indexing this folder, and take what was read from it out of "
            "the index.\n\n"
            "Leasha says how much first and asks. Your files are not touched.")
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

        # 2026-09-29. A new control. The same action as the row's own menu,
        # for the selected rows - a menu nobody knows to right-click for is a
        # feature nobody has.
        self.first = QPushButton("Index this folder first")
        self.first.setToolTip(
            "Read the selected folder before everything else, next time an\n"
            "index runs. Mark several and they are read in the order you\n"
            "marked them; the rest follow, newest first. Press again on a\n"
            "marked folder to take the mark off.")
        self.first.clicked.connect(lambda _checked=False: self.toggle_first())

        buttons = QHBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(add_file)
        buttons.addWidget(remove)
        buttons.addWidget(self.first)
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

        # 2026-10-07, the owner: "the list must reflect what is in the index".
        # What is in it from no listed folder - an earlier Remove's leftovers,
        # or a command-line run elsewhere - is said here, with the way out.
        # Hidden while there is none (`set_leftovers`).
        self.leftovers = QLabel()
        self.leftovers.setWordWrap(True)
        self.clear_leftovers = QPushButton("Remove them from the index")
        style_button(self.clear_leftovers, "folder-minus", "secondary")
        self.clear_leftovers.setToolTip(
            "Take what was read from folders that are no longer on this list out "
            "of the index. Leasha asks first. Your files are not touched.")
        self.clear_leftovers.clicked.connect(
            lambda _checked=False: self.leftovers_requested.emit())
        leftover_row = QHBoxLayout()
        leftover_row.addWidget(self.leftovers, 1)
        leftover_row.addWidget(self.clear_leftovers)
        self.set_leftovers(0)

        layout = QVBoxLayout(self)
        layout.addWidget(self.empty)
        layout.addLayout(self.suggestions)
        layout.addWidget(self.tree)
        layout.addLayout(buttons)
        layout.addLayout(leftover_row)
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

        # Four `is_dir` checks inside this account's profile, once at construction.
        self._suggested = suggested_roots()
        for folder in self._suggested:
            button = QPushButton(folder)
            # Named after a folder, so the button table cannot list it: its
            # icon and kind are said here (the button system, buttons.py).
            style_button(button, "folder-plus", "secondary")
            button.setToolTip(f"Add {folder} to the folders Leasha indexes.")
            button.clicked.connect(
                lambda _checked=False, path=folder: self._accept_suggestion(path))
            self.suggestions.addWidget(button)
        if self._suggested:
            self.suggestions.addWidget(self.suggest_all)
        self.suggestions.addStretch(1)

    def _accept_suggestion(self, folder: str) -> None:
        """A suggested profile folder clicked: add it and hide the offer if it was the first."""
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

    def set_roots(
        self, roots: list[str], modes: Optional[dict[str, str]] = None,
        cloud_content: Optional[set[str]] = None,
        first: Optional[list[str]] = None,
    ) -> None:
        """Replace the list, without emitting on the way in.

        Loading nine folders would otherwise fire nine change notifications
        before the panel has been shown, each writing a partial list over the
        stored one - the same trap `IndexingSettings.load_indexing` blocks
        signals for.
        """
        modes = modes or {}
        cloud_content = cloud_content or set()
        if first is not None:
            self._first = [str(folder) for folder in first]
        self.tree.blockSignals(True)
        try:
            self.tree.clear()
            for root in roots:
                self._append(root, modes.get(normalise(root), LIVE),
                            normalise(root) in cloud_content)
        finally:
            self.tree.blockSignals(False)
        self._sync_empty()
        self._sync_rescan()
        self._sync_first()

    def _append(self, root: str, mode: str = LIVE, cloud_content: bool = False) -> QTreeWidgetItem:
        """One line: the folder, its Live/Archive drop-down, the cloud tick box, the
        "first" place and the Index now button. Emits nothing.
        """
        item = QTreeWidgetItem([str(root), "", ""])
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
        # As wide as its longest choice: fitted to the column, "Archive - it
        # does not change" lost its last letters (2026-10-08).
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        index = combo.findData(mode)
        combo.setCurrentIndex(index if index >= 0 else 0)
        # A wheel over a combo inside a scrolling panel changes the value while
        # the person is trying to scroll past it - already fixed once across
        # Settings, and re-applied here rather than rediscovered.
        combo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        combo.currentIndexChanged.connect(lambda _i: self._modes_changed())
        self.tree.setItemWidget(item, 1, combo)

        # §2b THE TRAP: reading a cloud placeholder downloads it, so this
        # stays off unless both this box's own master switch (Settings) and
        # this specific row say yes - a folder is never pulled into the
        # download budget just because indexing is turned on for it.
        cloud_box = QCheckBox()
        cloud_box.setToolTip(
            "Download and index this folder's cloud-only files (OneDrive, "
            "Google Drive placeholders and the like), up to the shared cap "
            "in Settings.\n\n"
            "Off by default: reading a cloud-only file downloads it, and a "
            "whole synced library can be far bigger than this computer's "
            "free space. Also needs \"Index cloud-only files\" on in "
            "Settings - this is which folders, that is whether any are "
            "included at all.")
        cloud_box.setChecked(cloud_content)
        cloud_box.setAccessibleName(f"Download this folder's cloud content: {root}")
        cloud_box.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        cloud_box.stateChanged.connect(lambda _s: self._cloud_content_changed())
        cell = QWidget()
        cell.setObjectName("rowCell")             # the row shows through (theme)
        cell_layout = QHBoxLayout(cell)
        cell_layout.setContentsMargins(0, 0, 0, 0)
        cell_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        cell_layout.addWidget(cloud_box)
        self.tree.setItemWidget(item, 2, cell)

        # 2026-10-02. **This line, now, and nothing else.** Start reads every
        # folder in the list; with a dozen archives that is a dozen folders
        # looked at to bring one up to date. The button is on the line it
        # acts on, so there is nothing to select first, and it reads the
        # folder in full even when the line says Archive - asking for one
        # folder by name is the "I know something changed" case that
        # "Rescan archived folders now" exists for, for one folder.
        #
        # An icon and no words (the owner, the same day): the column's heading
        # carries the words once, the tooltip and the screen reader's name
        # carry them on each line.
        index_now = icon_button(
            "Index now", name=f"Index this folder now: {root}",
            tooltip=(
                "Index now - this folder only.\n\n"
                "Nothing else in the list is read, and nothing indexed from the "
                "other folders is touched. The folder is read in full even if it "
                "is marked as an archive."))
        index_now.clicked.connect(
            lambda _checked=False, row=item: self.index_requested.emit(row.text(0)))
        put_on_row(self.tree, item, 4, index_now)
        return item

    # -- what the shell reads -----------------------------------------------

    def current_roots(self) -> list[str]:
        """Every line's folder, in the order shown - which is the saved order."""
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

    def current_cloud_content_roots(self) -> set[str]:
        """`{normalised root}` for every row whose cloud-content box is
        checked - only the opted-in ones, the same shape `dump_modes`
        writes (only the non-default entries), not every row."""
        found: set[str] = set()
        for row in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(row)
            cell = self.tree.itemWidget(item, 2)
            box = cell.findChild(QCheckBox) if cell is not None else None
            if box is not None and box.isChecked():
                found.add(normalise(item.text(0)))
        return found

    def current_first_folders(self) -> list[str]:
        """The folders marked "first", in the order marked, as each row
        spells it - only those still in the list."""
        rows = {normalise(root): root for root in self.current_roots()}
        out: list[str] = []
        for folder in self._first:
            root = rows.get(normalise(folder))
            if root is not None and root not in out:
                out.append(root)
        return out

    def toggle_first(self, items: Optional[list] = None) -> None:
        """Mark the selected folders "first", or take the mark off.

        When every selected folder is already marked, the marks come off;
        otherwise the unmarked ones are added **at the end**, so marking one
        folder and then another reads them in that order.
        """
        items = list(items if items is not None else self.tree.selectedItems())
        if not items:
            return
        chosen = [item.text(0) for item in items]
        marked = {normalise(folder) for folder in self.current_first_folders()}
        current = self.current_first_folders()
        if all(normalise(folder) in marked for folder in chosen):
            drop = {normalise(folder) for folder in chosen}
            current = [f for f in current if normalise(f) not in drop]
        else:
            current += [f for f in chosen if normalise(f) not in marked]
        self._first = current
        self._sync_first()
        self.first_changed.emit(self.current_first_folders())

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

    def _add_file(self) -> None:
        chosen, _filter = QFileDialog.getOpenFileName(self, "Choose a file to index")
        if chosen:
            self.add_root(chosen)

    def _remove_root(self) -> None:
        """Remove: ask the window first when `confirms_removal`, else take the rows off."""
        folders = [item.text(0) for item in self.tree.selectedItems()]
        if not folders:
            return
        if self.confirms_removal:
            self.remove_requested.emit(folders)
        else:
            self.remove_roots(folders)

    def set_leftovers(self, count: int) -> None:
        """Say how many items in the index came from no folder on the list."""
        from app.ui.presenter import leftovers_text

        self.leftovers.setText(leftovers_text(count))
        for widget in (self.leftovers, self.clear_leftovers):
            widget.setVisible(count > 0)

    def remove_roots(self, folders: list[str]) -> None:
        """Take these folders' rows off the list, and say so once."""
        gone = {normalise(folder) for folder in folders}
        for row in reversed(range(self.tree.topLevelItemCount())):
            if normalise(self.tree.topLevelItem(row).text(0)) in gone:
                self.tree.takeTopLevelItem(row)
        self._emit()

    def _row_menu(self, point: Any) -> None:
        """Right-click on a folder: "Index this folder first" and "Index now"."""
        item = self.tree.itemAt(point)
        if item is None:
            return
        marked = normalise(item.text(0)) in {
            normalise(folder) for folder in self.current_first_folders()}
        menu = QMenu(self)
        action = QAction("Index this folder first", menu)
        action.setCheckable(True)
        action.setChecked(marked)
        action.triggered.connect(lambda _checked=False: self.toggle_first([item]))
        menu.addAction(action)
        # 2026-10-02: the same action as the line's own button.
        now = QAction("Index now", menu)
        now.triggered.connect(
            lambda _checked=False: self.index_requested.emit(item.text(0)))
        menu.addAction(now)
        open_menu(menu, self.tree.viewport().mapToGlobal(point))

    def _sync_first(self) -> None:
        """Each marked row shows its place in the order; the rest show none."""
        order = {normalise(folder): place
                 for place, folder in enumerate(self.current_first_folders(), 1)}
        for row in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(row)
            place = order.get(normalise(item.text(0)))
            item.setText(3, str(place) if place else "")
            item.setToolTip(3, (
                f"Read {'first' if place == 1 else f'number {place}'} of the "
                "folders marked \"Index this folder first\"." if place else
                "Read with everything else, newest first."))

    def _modes_changed(self) -> None:
        self.modes_changed.emit(self.current_modes())
        self._sync_rescan()

    def _cloud_content_changed(self) -> None:
        self.cloud_content_changed.emit(self.current_cloud_content_roots())

    def _emit(self) -> None:
        """Say what changed, roots first (a mode needs its folder to exist in the list)."""
        # Roots first: a mode for a folder that is not in the list yet would be
        # written and then have nothing to attach to.
        self.roots_changed.emit(self.current_roots())
        self.modes_changed.emit(self.current_modes())
        self.cloud_content_changed.emit(self.current_cloud_content_roots())
        self._sync_rescan()
        # A folder removed stops being "first"; one added is not yet.
        first = self.current_first_folders()
        if first != self._first:
            self._first = first
            self.first_changed.emit(first)
        self._sync_first()

    def _sync_rescan(self) -> None:
        """A button that does nothing is worse than one that is not there.

        Disabled rather than hidden, with a tooltip that says why - a control
        that appears and disappears as rows change reads as a glitch.
        """
        any_archive = ARCHIVE in self.current_modes().values()
        self.rescan.setEnabled(any_archive)
        if not any_archive:
            self.rescan.setToolTip("No folder is marked as an archive yet.")
