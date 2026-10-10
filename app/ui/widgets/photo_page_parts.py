r"""The Photos tab's menus and remembered choices - kept out of `photos_view.py`,
which every view keeps under 250 lines.

Layer: L5

- `fill_view_menu` - the window's View menu, and the tab's own View icon, when
  Photos is in front: Details / Small / Medium / Large, the sort, the info panel.
- `photo_menu` - right-click on a photo: View, Open, Show in folder, Name the
  people, Write names into it, Photos from the same day, Copy.
- `ViewButton` - the tab's View icon (`view_options._as_icon`), with the
  `menu_for` the window's View menu calls.
- `library`, `read_box` - the tab's two reads - and `read_library`,
  `read_the_box` and `arrange_rows`, which start them (and the sorting step)
  on a worker and route the answer back safely.

2026-10-10: `library`, `read_box` and their starters moved here from
`photos_view.py` (they were `_library`, `_read` and three copies of the same
four lines). Moving the filtering to a worker on 2026-10-09 took the view to
274 lines, over the 250 `test_presenter` allows a view; the pure sorting step
went to `presenter.photos.arrange`, and the reads, which touch the store and so
cannot live in the presenter, came here.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Callable, Optional

from PySide6.QtWidgets import QMenu, QToolButton, QWidget

from app.ui.presenter.photos import SORTS, facets
from app.ui.widgets.photo_browser import MODES

__all__ = ["ViewButton", "fill_view_menu", "photo_menu", "STATE_KEYS", "same_day_box",
           "library", "read_box", "read_library", "read_the_box", "arrange_rows"]

#: `index_state` keys, namespaced `ui:` like every remembered choice.
STATE_KEYS = {"mode": "ui:photos_mode", "sort": "ui:photos_sort", "info": "ui:photos_info"}


def library(store: Any) -> tuple[list, int, dict]:
    """Every picture, how many faces wait for a Yes or No, and the side list's
    counts. **Worker.** The counts were taken on the window's thread until
    2026-10-09, over 46,000 pictures, on every read of the tab."""
    from app.extract.ocr import OcrExtractor

    rows = store.photo_library(OcrExtractor.extensions)
    waiting = sum(count for _p, _n, count in store.suggestion_counts())
    return rows, waiting, facets(rows)


def read_box(store: Any, text: str, reading: dict) -> tuple:
    """The box, read as every tab reads it. **Worker.**"""
    from app.search.run import read_typed, words_of

    parsed, applied = read_typed(store, text, surface="files", **reading)
    return parsed, applied, words_of(parsed)


def _start(owner: Any, worker: Any, finished: Callable[[Any], None],
           failed: Callable[[Any], None]) -> None:
    """Run `worker` on the shared thread pool and hand its answer to `finished`.

    The answer goes through `later.when_done`, so it is dropped if `owner` has been
    destroyed by the time it arrives, rather than reaching a deleted widget. Staleness
    (a newer search making an older answer useless) is the caller's to check - each
    caller tags its own request.
    """
    from PySide6.QtCore import QThreadPool

    from app.ui.later import when_done
    from app.ui.workers import run

    when_done(owner, worker, finished=finished, failed=failed)
    run(QThreadPool.globalInstance(), worker)


# The three starters below each name their `CallableWorker` here, in the module that
# defines the work, rather than taking any function: test_ui_never_blocks proves a store
# read is off the window's thread by finding it handed to a `CallableWorker` in its own
# module, and a generic "run this" helper would hide that from it (2026-10-10).

def read_library(owner: Any, store: Any, *, finished: Callable[[Any], None],
                 failed: Callable[[Any], None]) -> None:
    """`library(store)` on a worker."""
    from app.ui.workers import CallableWorker

    _start(owner, CallableWorker(library, store, component="ui.photos"), finished, failed)


def read_the_box(owner: Any, store: Any, text: str, reading: dict, *,
                 finished: Callable[[Any], None], failed: Callable[[Any], None]) -> None:
    """`read_box(store, text, reading)` on a worker."""
    from app.ui.workers import CallableWorker

    _start(owner, CallableWorker(read_box, store, text, reading, component="ui.photos"),
           finished, failed)


def arrange_rows(owner: Any, rows: list, parsed: Any, words: str, sort_key: str, *,
                 finished: Callable[[Any], None], failed: Callable[[Any], None]) -> None:
    """`presenter.photos.arrange` - narrow, sort, count - on a worker."""
    from app.ui.presenter.photos import arrange
    from app.ui.workers import CallableWorker

    _start(owner, CallableWorker(arrange, rows, parsed, words, sort_key,
                                 component="ui.photos"), finished, failed)


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
