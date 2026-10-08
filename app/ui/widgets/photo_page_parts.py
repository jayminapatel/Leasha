r"""The Photos tab's menus and remembered choices - kept out of `photos_view.py`,
which every view keeps under 250 lines.

Layer: L5

- `fill_view_menu` - the window's View menu, and the tab's own View icon, when
  Photos is in front: Details / Small / Medium / Large, the sort, the info panel.
- `photo_menu` - right-click on a photo: View, Open, Show in folder, Name the
  people, Write names into it, Photos from the same day, Copy.
- `ViewButton` - the tab's View icon (`view_options._as_icon`), with the
  `menu_for` the window's View menu calls.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Callable, Optional

from PySide6.QtWidgets import QMenu, QToolButton, QWidget

from app.ui.presenter.photos import SORTS
from app.ui.widgets.photo_browser import MODES

__all__ = ["ViewButton", "fill_view_menu", "photo_menu", "STATE_KEYS", "same_day_box"]

#: `index_state` keys, namespaced `ui:` like every remembered choice.
STATE_KEYS = {"mode": "ui:photos_mode", "sort": "ui:photos_sort", "info": "ui:photos_info"}


def fill_view_menu(menu: QMenu, view: Any) -> None:
    """Details, Small, Medium, Large; the sort; the info panel."""
    for key, label, _edge in MODES:
        action = menu.addAction(label)
        action.setCheckable(True)
        action.setChecked(view.browser.mode == key)
        action.triggered.connect(lambda _c=False, k=key: view.set_mode(k))
    menu.addSeparator()
    sort_menu = menu.addMenu("Sort by")
    for key, label in SORTS:
        action = sort_menu.addAction(label)
        action.setCheckable(True)
        action.setChecked(view.sort_key == key)
        action.triggered.connect(lambda _c=False, k=key: view.set_sort(k))
    menu.addSeparator()
    info = menu.addAction("Info panel")
    info.setCheckable(True)
    info.setChecked(view.info.isVisible())
    info.triggered.connect(lambda on=False: view.show_info(bool(on)))


class ViewButton(QToolButton):
    """The tab's View icon; `menu_for` is what the window's View menu calls."""

    def __init__(self, view: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        from app.ui.view_options import _as_icon

        self.setText("View")
        self.setToolTip("How the photos are shown: details, small, medium or large "
                        "thumbnails, and their order")
        self._view = view
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self)
        menu.aboutToShow.connect(lambda: (menu.clear(), fill_view_menu(menu, self._view)))
        self.setMenu(menu)
        _as_icon(self)

    def menu_for(self, menu: QMenu) -> None:
        """Fill the window's View menu with this tab's entries (the window calls it)."""
        fill_view_menu(menu, self._view)


def same_day_box(row: Any) -> str:
    """`date:2023-06-28` for the day a photo is from, or "" without a date."""
    if not row.when_ns:
        return ""
    try:
        day = _dt.datetime.fromtimestamp(row.when_ns / 1e9).date()
    except (OverflowError, OSError, ValueError):
        return ""
    return f"date:{day.isoformat()}"


def photo_menu(parent: QWidget, row: Any, *, view: Callable[[], None],
               open_file: Callable[[], None], reveal: Callable[[], None],
               name: Optional[Callable[[], None]], write: Callable[[], None],
               same_day: Optional[Callable[[], None]]) -> QMenu:
    """Right-click on a photo, built on the shared file menu so Copy path and the
    rest read as they do on every tab."""
    from pathlib import PurePath

    from app.ui.widgets.file_menu import FileActions, build_menu

    extra = []
    if name is not None:
        extra.append(("Name the people in it…", "Open People to name", name))
    extra.append(("Write names into it…", "Write the people and description into the "
                  "photo's metadata", write))
    if same_day is not None:
        extra.append(("Photos from the same day", "Narrow to the day this was taken",
                      same_day))
    return build_menu(parent, str(row.path), FileActions(
        open_file=open_file, reveal=reveal, view=view, row=row,
        copy=[("Copy name", PurePath(row.path).name)], extra=extra))
