r"""The Photos page's left panel: people, years, places and kinds, with counts.

Layer: L5

2026-10-05, the owner: "this photos view should be like other tabs where i can
narrow by year name location etc etc". **A click writes into the box**, the way
the year strip does on Search: Jason becomes `who:Jason`, 2019 `date:2019`,
London `place:London`, "Faces not yet named" `only:unnamed`. A second click
takes it out again. The box stays the one truth, the chips under it show what
is in force, and anything clicked can be typed and the other way round.

Ticked entries are the ones in the box now (`set_box`). "People to name" at
the top is not a narrowing: it opens the naming page, and says how many faces
are waiting for a Yes or No.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QAbstractItemView, QTreeWidget, QTreeWidgetItem, QWidget

from app.ui.presenter.photos import KINDS, box_has

__all__ = ["PhotoSidebar", "SECTIONS"]

#: `(section key, heading, box switch)`, top to bottom.
SECTIONS: tuple[tuple[str, str, str], ...] = (
    ("people", "People", "who"),
    ("years", "Years", "date"),
    ("places", "Places", "place"),
    ("kinds", "Only", "only"),
    ("types", "Types", "type"),
)

ROLE_SWITCH = int(Qt.ItemDataRole.UserRole) + 1
ROLE_VALUE = int(Qt.ItemDataRole.UserRole) + 2
ROLE_ACTION = int(Qt.ItemDataRole.UserRole) + 3

#: Entries shown per section before the rest wait behind "More…" - a library
#: with 300 places would otherwise push Years off the screen.
SHOWN = 12


class PhotoSidebar(QTreeWidget):
    """Narrowing lists. `toggled(switch, value)`; `action(name)` for the rest."""

    toggled = Signal(str, object)
    action = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("photo_sidebar")
        self.setHeaderHidden(True)
        self.setColumnCount(2)
        self.setRootIsDecorated(True)
        self.setIndentation(10)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setToolTip("Click a person, a year or a place to narrow the photos to it; "
                        "click again to take it away")
        self.setMinimumWidth(190)
        from PySide6.QtWidgets import QHeaderView

        self.header().setStretchLastSection(False)
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.itemClicked.connect(self._clicked)
        self._expanded: set[str] = set()
        self._box = ""
        self._counts: dict = {}
        self._waiting = 0
        self.fill({}, 0)

    def fill(self, counts: dict, waiting: int) -> None:
        """Rebuild from `presenter.photos.facets` counts and the faces waiting."""
        self._counts, self._waiting = counts, int(waiting or 0)
        open_sections = {self.topLevelItem(i).data(0, ROLE_SWITCH)
                         for i in range(self.topLevelItemCount())
                         if self.topLevelItem(i).isExpanded()} or {"people", "years", "kinds"}
        self.clear()
        bold = QFont(self.font())
        bold.setBold(True)

        naming = QTreeWidgetItem(["People to name", f"{self._waiting:,}" if self._waiting else ""])
        naming.setData(0, ROLE_ACTION, "name_people")
        naming.setToolTip(0, "Name the people Leasha has found, and answer its "
                             "\"Is this ...?\" questions")
        naming.setFont(0, bold)
        self.addTopLevelItem(naming)
        everything = QTreeWidgetItem(["All photos", ""])
        everything.setData(0, ROLE_ACTION, "clear")
        everything.setToolTip(0, "Take every narrowing away")
        self.addTopLevelItem(everything)

        kinds = dict(KINDS)
        for key, heading, switch in SECTIONS:
            found = counts.get(key) or {}
            section = QTreeWidgetItem([heading, ""])
            section.setData(0, ROLE_SWITCH, key)
            section.setFont(0, bold)
            section.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.addTopLevelItem(section)
            if key == "years":
                ordered = sorted(found.items(), key=lambda kv: -kv[0])
            elif key == "kinds":
                ordered = [(k, found.get(k, 0)) for k, _label in KINDS]
            else:
                ordered = sorted(found.items(), key=lambda kv: (-kv[1], str(kv[0]).casefold()))
            limit = None if key in self._expanded or key == "kinds" else SHOWN
            for value, count in ordered[:limit]:
                label = kinds.get(value, str(value).upper() if key == "types" else str(value))
                item = QTreeWidgetItem([label, f"{count:,}"])
                item.setData(0, ROLE_SWITCH, switch)
                item.setData(0, ROLE_VALUE, value)
                item.setTextAlignment(1, Qt.AlignmentFlag.AlignRight)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Unchecked)
                section.addChild(item)
            if limit is not None and len(ordered) > limit:
                more = QTreeWidgetItem([f"More… ({len(ordered) - limit:,})", ""])
                more.setData(0, ROLE_ACTION, f"more:{key}")
                section.addChild(more)
            if not ordered:
                none = QTreeWidgetItem(["None yet", ""])
                none.setFlags(Qt.ItemFlag.NoItemFlags)
                section.addChild(none)
            section.setExpanded(key in open_sections)
        self.set_box(self._box)

    def set_box(self, text: str) -> None:
        """Tick what the box now holds."""
        self._box = text or ""
        for top in range(self.topLevelItemCount()):
            section = self.topLevelItem(top)
            for n in range(section.childCount()):
                item = section.child(n)
                switch = item.data(0, ROLE_SWITCH)
                if not switch:
                    continue
                on = box_has(self._box, switch, item.data(0, ROLE_VALUE))
                item.setCheckState(0, Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)

    def _clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        action = item.data(0, ROLE_ACTION)
        if action and str(action).startswith("more:"):
            self._expanded.add(str(action)[5:])
            self.fill(self._counts, self._waiting)
            return
        if action:
            self.action.emit(str(action))
            return
        switch = item.data(0, ROLE_SWITCH)
        if switch and item.data(0, ROLE_VALUE) is not None:
            self.toggled.emit(str(switch), item.data(0, ROLE_VALUE))
        elif item.childCount():
            item.setExpanded(not item.isExpanded())
