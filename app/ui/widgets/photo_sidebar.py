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
from PySide6.QtWidgets import QWidget

from app.ui.widgets.side_list import SideList

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


#: Each section's icon (order 1p: drawn the Contacts way).
SECTION_ICONS = {"people": "user", "years": "calendar", "places": "map-pin", "kinds": "tag",
                 "types": "image"}


class PhotoSidebar(SideList):
    """Narrowing lists. `toggled(switch, value)`; `action(name)` for the rest.

    Order 1p (2026-10-11): drawn by `SideList`, so it looks like every other page's list
    and the Contacts one - an icon on every line, a faint count, sections that fold.
    What a click does is unchanged.
    """

    toggled = Signal(str, object)
    action = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("photo_sidebar", accessible="People, years and places",
                         tip="Click a person, a year or a place to narrow the photos to it; "
                             "click again to take it away",
                         shown=SHOWN, open_sections=("People", "Years", "Only"), parent=parent)
        self.setMinimumWidth(190)
        self._box = ""
        self._counts: dict = {}
        self._waiting = 0
        self.picked.connect(self._picked)
        self.door.connect(lambda entry: self.action.emit(str(entry.value)))
        self.fill({}, 0)

    def fill(self, counts: dict, waiting: int) -> None:
        """Rebuild from `presenter.photos.facets` counts and the faces waiting."""
        self._counts, self._waiting = counts, int(waiting or 0)
        self._draw()

    def set_box(self, text: str) -> None:
        """Tick what the box now holds."""
        self._box = text or ""
        self._draw()

    def _draw(self) -> None:
        from app.ui.presenter.side_entry import SideEntry

        entries = [
            SideEntry("name_people", "People to name", icon="users",
                      count=self._waiting or None, kind="door", value="name_people",
                      tip="Name the people Leasha has found, and answer its \"Is this ...?\" questions"),
            SideEntry("all", "All photos", icon="image", kind="all", tip="Take every narrowing away"),
        ]
        kinds = dict(KINDS)
        for key, heading, switch in SECTIONS:
            found = self._counts.get(key) or {}
            if key == "years":
                ordered = sorted(found.items(), key=lambda kv: -kv[0])
            elif key == "kinds":
                ordered = [(k, found.get(k, 0)) for k, _label in KINDS]
            else:
                ordered = sorted(found.items(), key=lambda kv: (-kv[1], str(kv[0]).casefold()))
            if not ordered:
                entries.append(SideEntry(f"{key}:none", "None yet", section=heading, kind="note"))
            for value, count in ordered:
                label = kinds.get(value, str(value).upper() if key == "types" else str(value))
                entries.append(SideEntry(
                    f"{key}:{value}", label, section=heading, icon=SECTION_ICONS.get(key, ""),
                    count=count, value=(switch, value), checked=box_has(self._box, switch, value)))
        self.set_entries(entries)

    def _picked(self, entry: Any) -> None:
        if entry.kind == "all":
            self.action.emit("clear")
        elif entry.kind == "filter" and entry.value:
            switch, value = entry.value
            self.toggled.emit(str(switch), value)
