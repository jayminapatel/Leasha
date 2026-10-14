"""One line of a page's left-hand list (order 1p). Qt-free, so presenters can build them.

Layer: L5 presenter. Drawn by `app.ui.widgets.side_list.SideList`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

__all__ = ["SideEntry"]


@dataclass(frozen=True)
class SideEntry:
    """One line of a side list. The same shape as Contacts' `logic.Entry`, plus
    `parent`, `checked` and `menu`."""

    key: str
    label: str
    section: str = ""
    icon: str = ""
    colour: str = ""
    count: Optional[int] = None
    tip: str = ""
    #: `filter` narrows; `all` takes every narrowing away; `door` opens something.
    kind: str = "filter"
    #: What a click means to the page - a folder, a year, `("who", "Jason")` ...
    value: Any = None
    #: The key of the entry this one nests under (a folder in its archive).
    parent: str = ""
    #: None: no tick box. True/False: ticked or not (a list whose clicks toggle).
    checked: Optional[bool] = None
    #: The right-click menu: `((label, icon, action), ...)`; `menu_chosen` says which.
    menu: tuple = ()
    #: Never put behind its section's "More...": a type the person listed in `FILE_KINDS_ALWAYS`.
    pinned: bool = False
