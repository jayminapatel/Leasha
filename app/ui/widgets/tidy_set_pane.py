r"""One Tidy set on screen: Compare, Copies, Details and Keep rule, each tab with its icon.

Layer: L5 widget. Order 1q D4, D8 (2026-10-11).

The pane shows one set at a time (Previous / Next) and says which files are marked
for the Recycle Bin; it never moves anything. Marks belong to the page
(`TidyView`), which this asks through `marked_of` and tells through `toggled`.
A tile or row ticked means "this can go"; "Keep this one" on the right-click menu,
or a double-click on a tile, unticks that file and ticks the rest of its set.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Optional

from PySide6.QtCore import QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QHBoxLayout, QLabel, QListView, QListWidget,
    QListWidgetItem, QPushButton, QRadioButton, QStackedWidget, QTextBrowser,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from app.core.row_facts import format_size
from app.tidy import keep
from app.ui.presenter.tidy import set_heading
from app.ui.widgets import list_keys, menus
from app.ui.widgets.file_menu import copy_paths

__all__ = ["TidySetPane", "TAB_ICONS"]

_PATH = int(Qt.ItemDataRole.UserRole) + 21
_SORT = int(Qt.ItemDataRole.UserRole) + 22


class _Row(QTreeWidgetItem):
    """A copies row that sorts size and date by their numbers, not by the words shown."""

    def __lt__(self, other: "QTreeWidgetItem") -> bool:
        column = self.treeWidget().sortColumn() if self.treeWidget() is not None else 0
        a, b = self.data(column, _SORT), other.data(column, _SORT)
        if a is not None and b is not None:
            return a < b
        return self.text(column).casefold() < other.text(column).casefold()


#: Tab -> Lucide glyph (`assets/icons`), drawn by `retint`.
TAB_ICONS = (("Compare", "layout-grid"), ("Copies", "list"), ("Details", "info"),
             ("Keep rule", "sliders-horizontal"), ("List", "list-checks"))
_TILE = 180
_RULE_TIPS = {
    "leasha": "Photos: the most pixels, then the sharpest. Copies: not in Downloads or a "
              "temporary folder, not named like a copy, then the oldest.",
    "oldest": "Keep the file with the oldest modified date.",
    "newest": "Keep the file with the newest modified date.",
    "manual": "Mark nothing. You tick what can go, or right-click the one to keep.",
}


class TidySetPane(QWidget):
    """The picked kind's sets, one at a time."""

    toggled = Signal(str, bool)            # path, marked
    keep_only = Signal(str)                # keep this path, mark the rest of its set
    rule_chosen = Signal(str)
    mark_all = Signal()
    #: A set is about to be drawn - the page marks it by the rule first, if it is new.
    showing = Signal(object)
    #: The List tab ticked or unticked a file of some set (not necessarily the one on show).
    toggled_in = Signal(object, str, bool)
    keep_only_in = Signal(object, str)
    error = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._sets: list = []
        self._index = 0
        self._marked_of: Callable[[str], bool] = lambda _p: False
        self._why: Callable[[Any], str] = lambda _s: ""
        self._icons: dict[str, QIcon] = {}
        #: How many sets of this kind exist in all, when more than were loaded.
        self.found = 0
        #: What a file that is not a photo shows in Compare (set by `retint`).
        self._file_icon = QIcon()

        self.heading = QLabel("")
        self.heading.setObjectName("sectionTitle")
        self.previous = QPushButton("Previous")
        self.previous.setToolTip("Show the set before this one.")
        self.next = QPushButton("Next")
        self.next.setToolTip("Show the next set. What you marked here stays marked.")
        self.previous.clicked.connect(lambda: self.show_set(self._index - 1))
        self.next.clicked.connect(lambda: self.show_set(self._index + 1))
        top = QHBoxLayout()
        top.addWidget(self.heading, 1)
        top.addWidget(self.previous)
        top.addWidget(self.next)

        self.tiles = QListWidget()
        self.tiles.setAccessibleName("Files to compare")
        self.tiles.setViewMode(QListView.ViewMode.IconMode)
        self.tiles.setIconSize(QSize(_TILE, _TILE))
        self.tiles.setResizeMode(QListView.ResizeMode.Adjust)
        self.tiles.setMovement(QListView.Movement.Static)
        self.tiles.setSpacing(8)
        self.tiles.setWordWrap(True)
        self.tiles.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.tiles.itemChanged.connect(self._tile_changed)
        # A double-click keeps the tile, and Enter does the same, from one signal (`list_keys`).
        list_keys.install(self.tiles, default=self._tile_double_clicked, copy=self._copy_tile)
        self.copies = QTreeWidget()
        self.copies.setAccessibleName("Every copy")
        self.copies.setHeaderLabels(["Can go", "Where", "Size", "Modified"])
        self.copies.setRootIsDecorated(False)
        # The same table behaviour as every other list: columns fitted to what is in them, the path
        # taking the rest of the width and shortened in the middle, and a click on a heading sorts.
        header = self.copies.header()
        header.setStretchLastSection(False)
        for column, mode in ((0, header.ResizeMode.ResizeToContents), (1, header.ResizeMode.Stretch),
                             (2, header.ResizeMode.ResizeToContents), (3, header.ResizeMode.ResizeToContents)):
            header.setSectionResizeMode(column, mode)
        self.copies.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.copies.setSortingEnabled(True)
        self.copies.sortByColumn(1, Qt.SortOrder.AscendingOrder)
        self.copies.itemChanged.connect(self._row_changed)
        list_keys.install(self.copies, copy=self._copy_row)              # Ctrl+C: the path
        for view in (self.tiles, self.copies):
            view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            view.customContextMenuRequested.connect(lambda point, v=view: self._menu(v, point))
        self.details = QTextBrowser()
        self.details.setAccessibleName("About this set")

        rule_box = QWidget()
        rule_layout = QVBoxLayout(rule_box)
        rule_layout.addWidget(QLabel("Which file stays when Leasha marks a set:"))
        self.rules = QButtonGroup(self)
        for n, rule in enumerate(keep.RULES):
            choice = QRadioButton(keep.RULE_WORDS[rule])
            choice.setToolTip(_RULE_TIPS[rule])
            self.rules.addButton(choice, n)
            rule_layout.addWidget(choice)
        self.rules.idClicked.connect(lambda n: self.rule_chosen.emit(keep.RULES[n]))
        self.mark_every = QPushButton("Mark every set by this rule")
        self.mark_every.setToolTip("Exact copies only: marks all but the kept file in every set "
                                   "you have not changed by hand. Nothing moves until you review.")
        self.mark_every.clicked.connect(self.mark_all.emit)
        rule_layout.addWidget(self.mark_every)
        rule_layout.addStretch(1)

        # 2026-10-11, the owner: the list view every other page has - every file of every set in one
        # table, with the View menu, fitted columns, sorting, a filter and the Preview pane.
        from app.ui.widgets.tidy_list_pane import TidyListPane

        self.list_pane = TidyListPane()
        self.list_pane.toggled.connect(self._list_toggled)
        self.list_pane.keep_only.connect(self._list_keep_only)
        self.list_pane.open_set.connect(self._open_in_compare)
        self.list_pane.error.connect(self.error.emit)
        self._owner: dict[str, int] = {}
        self._opened_on_list: set[str] = set()
        # The window has no QTabWidget (its pages are the rail's): the views are
        # a segmented control over a stack, the control the Search page uses.
        from app.ui.widgets.segmented import SegmentedControl

        self.tabs = SegmentedControl()
        self.tabs.setAccessibleName("How to look at this set")
        self.views = QStackedWidget()
        for widget, (name, _icon) in zip((self.tiles, self.copies, self.details, rule_box, self.list_pane),
                                         TAB_ICONS, strict=True):
            self.tabs.addItem(name, name)
            self.views.addWidget(widget)
        self.tabs.setToolTip("Compare side by side, list every copy, read about the set, choose "
                             "which file a rule keeps, or list every file of every set in one table.")
        self.tabs.currentIndexChanged.connect(self.views.setCurrentIndex)
        self.tabs.setCurrentIndex(0)
        self.why = QLabel("")
        self.why.setWordWrap(True)
        self.why.setObjectName("resultsSummary")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(self.tabs, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.views, 1)
        layout.addWidget(self.why)

    # -- what the page says ------------------------------------------------------------

    def bind(self, marked_of: Callable[[str], bool], why: Callable[[Any], str]) -> None:
        self._marked_of, self._why = marked_of, why

    def set_rule(self, rule: str) -> None:
        if rule in keep.RULES:
            self.rules.button(keep.RULES.index(rule)).setChecked(True)

    def set_sets(self, sets: list, index: int = 0) -> None:
        self._sets = list(sets)
        self._owner = {f.path: i for i, tset in enumerate(self._sets) for f in tset.files}
        self.list_pane.set_rows(self._sets, self._marked_of)
        kind = self._sets[0].kind if self._sets else ""
        if kind in ("large", "leftovers") and kind not in self._opened_on_list:
            self._opened_on_list.add(kind)          # many files in one set: a table is the first look
            self.show_list()
        self.show_set(index)

    def bind_store(self, store: Any) -> None:
        """Where the List tab's View menu remembers its choices."""
        self.list_pane.bind_store(store)

    def show_list(self) -> None:
        """Open on the List tab (the sets of large files and leftovers read best as a table)."""
        self.tabs.setCurrentIndex(len(TAB_ICONS) - 1)

    def all_sets(self) -> list:
        return list(self._sets)

    def current(self) -> Any:
        return self._sets[self._index] if 0 <= self._index < len(self._sets) else None

    def show_set(self, index: int) -> None:
        """Draw set `index` (clamped); thumbnails arrive later, on a worker."""
        self._index = max(0, min(index, len(self._sets) - 1)) if self._sets else 0
        tset = self.current()
        kind = tset.kind if tset is not None else ""
        listed = len(tset.files) if kind in ("large", "leftovers") else len(self._sets)
        self.heading.setText(set_heading(kind or "copies", self._index, listed,
                                         tset.size_bytes if tset else 0, self.found))
        self.previous.setEnabled(self._index > 0)
        self.next.setEnabled(self._index < len(self._sets) - 1)
        self.mark_every.setVisible(kind == "copies")
        if tset is not None:
            self.showing.emit(tset)
        self.refresh_marks()
        if tset is not None and kind in ("similar", "copies", "large", "leftovers"):
            self._load_thumbnails(tset)

    def refresh_marks(self) -> None:
        """Redraw ticks, rows and the sentence from the page's marks."""
        tset = self.current()
        for view in (self.tiles, self.copies):
            view.blockSignals(True)
        self.tiles.clear()
        self.copies.clear()
        if tset is not None:
            for f in tset.files:
                tile = QListWidgetItem(self._icons.get(f.path, self._file_icon), _tile_text(f))
                tile.setData(_PATH, f.path)
                tile.setToolTip(f.path)
                tile.setFlags(tile.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                tile.setCheckState(_state(self._marked_of(f.path)))
                tile.setSizeHint(QSize(_TILE + 24, _TILE + 64))
                self.tiles.addItem(tile)
                row = _Row(["", f.path, format_size(f.size_bytes), _when(f.mtime_ns)])
                row.setData(0, _PATH, f.path)
                row.setData(2, _SORT, int(f.size_bytes or 0))
                row.setData(3, _SORT, int(f.mtime_ns or 0))
                row.setToolTip(1, f.path)
                row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                row.setCheckState(0, _state(self._marked_of(f.path)))
                self.copies.addTopLevelItem(row)
            for where in tset.elsewhere:
                row = _Row(["", where, "", "on a catalogued drive - never touched"])
                row.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.copies.addTopLevelItem(row)
        for view in (self.tiles, self.copies):
            view.blockSignals(False)
        self.list_pane.refresh_marks(self._marked_of)
        self.why.setText(self._why(tset) if tset is not None else "Nothing to tidy here.")
        self.details.setMarkdown(_details(tset) if tset is not None else "")

    def retint(self, colours: dict) -> None:
        from app.ui.widgets.icons import icon

        ink = colours.get("text_dim", "#888888")
        self._file_icon = icon("file", ink)
        self.list_pane.retint(colours)
        for n, (_name, glyph) in enumerate(TAB_ICONS):
            segment = self.tabs.button(n)
            if segment is not None:
                segment.setIcon(icon(glyph, ink))
                segment.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

    # -- the List tab ------------------------------------------------------------------

    def _list_toggled(self, path: str, on: bool) -> None:
        index = self._owner.get(path)
        if index is not None:
            self.toggled_in.emit(self._sets[index], path, on)

    def _list_keep_only(self, path: str) -> None:
        index = self._owner.get(path)
        if index is not None:
            self.keep_only_in.emit(self._sets[index], path)

    def _open_in_compare(self, index: int) -> None:
        self.show_set(index)
        self.tabs.setCurrentIndex(0)

    # -- clicks ----------------------------------------------------------------------

    def _tile_changed(self, item: QListWidgetItem) -> None:
        self.toggled.emit(str(item.data(_PATH)), item.checkState() == Qt.CheckState.Checked)

    def _tile_double_clicked(self, index: Any) -> None:
        """Double-click a tile (or Enter on it): keep it, and mark the rest of its set."""
        tset = self.current()
        if tset is not None and tset.kind in ("copies", "similar") and index.isValid():
            self.keep_only.emit(str(index.data(_PATH)))

    def _copy_tile(self) -> None:
        item = self.tiles.currentItem()
        if item is not None:
            copy_paths([(str(item.data(_PATH) or ""), None)])

    def _copy_row(self) -> None:
        item = self.copies.currentItem()
        if item is not None:
            copy_paths([(str(item.data(0, _PATH) or ""), None)])

    def _row_changed(self, item: QTreeWidgetItem, column: int) -> None:
        path = item.data(0, _PATH)
        if column == 0 and path:
            self.toggled.emit(str(path), item.checkState(0) == Qt.CheckState.Checked)

    def _menu(self, view: Any, point: Any) -> None:
        item = view.itemAt(point)
        path = (item.data(_PATH) if isinstance(item, QListWidgetItem)
                else item.data(0, _PATH) if item is not None else None)
        tset = self.current()
        if not path or tset is None:
            return
        from app.core.osbridge import show_in_file_manager

        # Keep this one is what a double-click on a tile does, so there it is the bold line.
        keeps = tset.kind in ("copies", "similar")
        tile = view is self.tiles
        lines = [menus.Item("Keep this one", lambda p=path: self.keep_only.emit(str(p)), group="open",
                            default=tile, shortcut="Enter" if tile else "")] if keeps else []
        lines.append(menus.Item("Show in folder", lambda p=path: show_in_file_manager(str(p)),
                                group="open"))
        menus.show(menus.build(self, lines), view, point, wait=False, keep=self)

    def _load_thumbnails(self, tset: Any) -> None:
        from app.ui.workers import CallableWorker, run

        paths = [f.path for f in tset.files[:60]]
        worker = CallableWorker(_thumbnails, paths, component="ui.tidy.thumbnails")
        worker.signals.finished.connect(lambda images, key=tset.key: self._thumbnails_in(key, images))
        run(QThreadPool.globalInstance(), worker)

    def _thumbnails_in(self, key: str, images: dict) -> None:
        try:
            self._draw_thumbnails(key, images)
        except RuntimeError:
            return                                # the pane closed while they were read

    def _draw_thumbnails(self, key: str, images: dict) -> None:
        for path, image in images.items():
            if image is not None:
                self._icons[path] = QIcon(QPixmap.fromImage(image))
        tset = self.current()
        if tset is None or tset.key != key:
            return
        for row in range(self.tiles.count()):
            tile = self.tiles.item(row)
            found = self._icons.get(str(tile.data(_PATH)))
            if found is not None:
                tile.setIcon(found)


def _thumbnails(paths: list) -> dict:
    """Worker body: a small image of every photo among `paths` (others get none)."""
    from app.ui.thumbnail_loader import decode_thumbnail, is_image_result

    return {p: decode_thumbnail(p, edge=_TILE) for p in paths
            if is_image_result(os.path.splitext(p)[1].lstrip(".").lower())}


def _state(on: bool) -> Qt.CheckState:
    return Qt.CheckState.Checked if on else Qt.CheckState.Unchecked


def _when(mtime_ns: int) -> str:
    return time.strftime("%d %b %Y", time.localtime(mtime_ns / 1e9)) if mtime_ns else ""


def _tile_text(f: Any) -> str:
    folder = os.path.basename(os.path.dirname(f.path))
    return f"{f.name}\n{folder} - {format_size(f.size_bytes)}"


def _details(tset: Any) -> str:
    lines = [f"**{len(tset.files):,} file{'s' if len(tset.files) != 1 else ''}**, "
             f"{format_size(tset.size_bytes)} in all."]
    if tset.spare_bytes:
        lines.append(f"Keeping one gives back {format_size(tset.spare_bytes)}.")
    if tset.kind == "similar":
        lines.append("These are the same picture, not the same bytes: one may be bigger, "
                     "sharper or cropped differently. Compare before you choose.")
    if tset.elsewhere:
        lines.append(f"Also on catalogued drives ({len(tset.elsewhere):,}) - shown, never touched, "
                     "and never counted as the copy that stays.")
    return "\n\n".join(lines)
