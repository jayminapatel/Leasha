r"""Put a `SideList` on a page's left - its switch, its View entry, its counts - in one call.

Layer: L5 widget helper

Order 1p (2026-10-11). Every page's list is attached the same way (D4): split left of
the page's main area, a `panel-left` switch on the page's top row (before Preview and
View, as the owner's guidelines put it), a View-menu entry with the same words, the
choice remembered as `ui:<page>_side`. Counts are read on a worker when the page is
shown, and the ticks follow what is typed in the box.

Called from the window (`MainWindow`), not from the views: Files and Code are at their
250-line guard, and a view should not have to know its list is there.
"""

from __future__ import annotations

import weakref
from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QObject, QThreadPool, QTimer, Qt
from PySide6.QtWidgets import QLayout, QSplitter, QWidget

from app.ui.widgets.side_list import SideList

__all__ = ["attach", "wire_files", "wire_code", "wire_browse", "row_of", "holder_of"]


def holder_of(layout: QLayout, widget: QWidget) -> Optional[QLayout]:
    """The layout, at any depth under `layout`, that holds `widget` directly."""
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item.widget() is widget:
            return layout
        inner = item.layout()
        if inner is not None:
            found = holder_of(inner, widget)
            if found is not None:
                return found
    return None


row_of = holder_of


class _OnShow(QObject):
    """Calls `then` each time the page comes forward."""

    def __init__(self, page: QWidget, then: Callable[[], None]) -> None:
        super().__init__(page)
        self._then = then

    def eventFilter(self, _watched: Any, event: Any) -> bool:     # noqa: N802 - Qt's name
        if event.type() == QEvent.Type.Show:
            self._then()
        return False


def attach(view: Any, side: SideList, *, target: QWidget, row: Any, before: Any,
           page: str, words: str, tip: str, state_key: Optional[str] = None) -> Callable:
    """Split `side` left of `target`, put its switch in `row` before `before`, add the
    View entry, and remember the choice. Returns `show(on, remember=True)`."""
    from app.ui.state_writes import save_state
    from app.ui.widgets.side_toggle import side_toggle

    key = state_key or f"ui:{page}_side"
    ref = weakref.ref(view)
    holder = holder_of(view.layout(), target)
    outer = QSplitter(Qt.Orientation.Horizontal)
    outer.setObjectName(f"{page}SideSplit")
    if holder is not None:
        holder.replaceWidget(target, outer)
    outer.addWidget(side)
    outer.addWidget(target)
    outer.setStretchFactor(1, 1)
    outer.setSizes([220, 900])
    view.side_split = outer
    view.side_list = side

    def show(on: bool, *, remember: bool = True) -> None:
        page_view = ref()
        side.setVisible(bool(on))
        toggle.set_quietly(bool(on))
        if remember and page_view is not None:
            save_state(page_view._store, key, "1" if on else "0", component=f"ui.{page}")

    toggle = side_toggle(words, tip=tip, accessible=f"{words} list", name=f"toggle_{page}_side",
                         on_click=lambda on: show(on))
    at = row.indexOf(before) if before is not None else -1
    row.insertWidget(at if at >= 0 else row.count(), toggle)
    view.toggles = {**getattr(view, "toggles", {}), "side": toggle, "side_list": side}
    view.show_side = show

    button = getattr(view, "view_button", None)
    making = getattr(button, "menu_for", None)
    if making is not None:
        def menu_for(parent: Any) -> Any:
            menu = making(parent)
            if menu is not None:
                entry = menu.addAction(words)
                entry.setCheckable(True)
                entry.setChecked(not side.isHidden())
                entry.setToolTip(tip)
                entry.triggered.connect(lambda on=False: show(bool(on)))
            return menu

        button.menu_for = menu_for
    try:
        shown = str(view._store.get_state(key, "") or "1") == "1"
    except Exception:                                          # noqa: BLE001 - shown by default
        shown = True
    show(shown, remember=False)
    return show


#: How often an open page looks at the index's write counter.
FOLLOW_MS = 15_000


def moved(store: Any, seen: dict) -> bool:
    """Has the index been written to since `seen` was taken? Remembers the new counter. `generation` is a property
    of the store (one number), not a method."""
    try:
        generation = int(store.generation)
    except Exception:                                        # noqa: BLE001 - a counter; never stops the list
        return False
    if generation == seen.get("generation"):
        return False
    seen["generation"] = generation
    return True


def follow_index(view: Any, refresh: Callable[[], Any], *, interval_ms: int = FOLLOW_MS,
                 first: bool = False) -> QTimer:
    """Call `refresh()` while `view` is on screen and the index has been written to since the last look.

    Every list and tree that shows counts uses this, so none of them shows what the index was when the window opened
    (the owner, 2026-10-12: "make sure this is on all trees"). The check is one number (`store.generation`); the
    refresh - the page's own, on its own worker - runs only when it moved. Returns the timer, for a test."""
    seen: dict = {}
    clock = QTimer(view)
    clock.setInterval(interval_ms)

    def tick() -> None:
        if view.isVisible() and moved(getattr(view, "_store", None), seen):
            refresh()

    clock.timeout.connect(tick)
    clock.start()
    if first:
        refresh()                                      # and once now, so a page needs no line of its own for it
    return clock


def _load(view: Any, read: Callable[[Any], Any], draw: Callable[[Any], None], page: str) -> None:
    from app.ui.workers import CallableWorker, run

    store = getattr(view, "_store", None)
    if store is None:
        return
    worker = CallableWorker(read, store, component=f"ui.{page}")
    worker.signals.finished.connect(draw)
    worker.signals.failed.connect(lambda _e: None)          # the list stays as it was
    run(QThreadPool.globalInstance(), worker)


def _box_side(view: Any, side: SideList, *, page: str, words: str, tip: str,
              read: Callable[[Any], Any], build: Callable[[Any, str], list],
              switches: tuple, target: QWidget) -> SideList:
    """Files and Code: a click writes its switch into the box (D2); ticks follow the box."""
    from app.ui.presenter.photos import toggle_in_box
    from app.ui.presenter.side_entries import clear_from_box

    ref = weakref.ref(view)
    data: dict = {"counts": None}

    def draw(counts: Any = None) -> None:
        if counts is not None:
            data["counts"] = counts
        page_view = ref()
        if page_view is not None and data["counts"] is not None:
            side.set_entries(build(data["counts"], page_view.input.text()))

    def picked(entry: Any) -> None:
        page_view = ref()
        if page_view is None:
            return
        text = page_view.input.text()
        if entry.kind == "all":
            page_view.input.setText(clear_from_box(text, switches))
        elif entry.value:
            switch, value = entry.value
            page_view.input.setText(toggle_in_box(text, switch, value))

    side.picked.connect(picked)
    #: Typing and clicking both change the box; one redraw after they settle, not one per keystroke.
    settle = QTimer(view)
    settle.setSingleShot(True)
    settle.setInterval(120)
    settle.timeout.connect(draw)
    view.input.textChanged.connect(lambda _t: settle.start())
    toggles = getattr(view, "toggles", {})
    before = toggles.get("inspector") or getattr(view, "view_button", None)
    attach(view, side, target=target, row=holder_of(view.layout(), before), before=before,
           page=page, words=words, tip=tip)
    watcher = _OnShow(view, lambda: _load(view, read, draw, page))
    view.installEventFilter(watcher)
    view._side_watcher = watcher
    _load(view, read, draw, page)
    # The counts follow the index while the page is open, as Mail's do (see `follow_index`).
    follow_index(view, lambda: _load(view, read, draw, page))
    return side


def _files_counts(store: Any) -> tuple:
    """On a worker: the counts, and the Places tree built from them - once, not at every click."""
    from app.storage import side_counts as c
    from app.ui.presenter.side_entries import folder_tree

    roots, folders = c.saved_roots(store), c.folder_counts(store)
    shown, always = _kinds_setting()
    return (roots, folders, c.drive_counts(store), c.kind_counts(store, limit=shown, always=always),
            folder_tree(roots, folders))


def _kinds_setting() -> tuple[int, tuple[str, ...]]:
    """`(how many commonest types, the types always listed)` from Settings (`FILE_KINDS_SHOWN`, `FILE_KINDS_ALWAYS`),
    read on a worker; the defaults if the settings cannot be read."""
    try:
        from app.core.config import load_settings

        s = load_settings()
        return (max(3, int(getattr(s, "file_kinds_shown", 12))),
                tuple(e for e in str(getattr(s, "file_kinds_always", "")).replace(";", ",").split(",") if e.strip()))
    except Exception:                                      # noqa: BLE001 - the list still shows
        return 12, ("vsdx", "vsd", "mpp")


def _code_counts(store: Any) -> tuple:
    from app.storage import side_counts as c

    return (c.repo_counts(store), c.language_counts(store))


FILES_TIP = ("Show or hide your folders, drives and file types on the left. Click one to "
             "narrow the list to it; click it again, or All files, to take it away.")
CODE_TIP = ("Show or hide your repositories and languages on the left. Click one to narrow "
            "the list to it; click it again, or All code, to take it away.")
BROWSE_TIP = ("Show or hide the years and months on the left. Click a year or a month to "
              "open it; Show chooses what the timeline holds.")


def wire_files(view: Any) -> SideList:
    """Files (D3): Places, Drives, Kinds."""
    from app.ui.presenter.side_entries import files_entries

    side = SideList("filesSide", accessible="Folders, drives and kinds",
                    tip="Click to narrow the files; click again to take it away", shown=12)
    return _box_side(view, side, page="files", words="Places", tip=FILES_TIP, read=_files_counts,
                     build=lambda counts, box: files_entries(*counts[:4], box=box, tree=counts[4]),
                     switches=("path", "on", "type"), target=view.split)


def wire_code(view: Any) -> SideList:
    """Code (D3): Repositories, Languages."""
    from app.ui.presenter.side_entries import code_entries

    side = SideList("codeSide", accessible="Repositories and languages",
                    tip="Click to narrow the code; click again to take it away", shown=12)
    return _box_side(view, side, page="code", words="Repositories", tip=CODE_TIP, read=_code_counts,
                     build=lambda counts, box: code_entries(*counts, box=box),
                     switches=("repo", "type"), target=view.git_split)


def wire_browse(view: Any) -> SideList:
    """Browse (D3): Years and months, Show. A click opens that period or that kind."""
    from app.reports.timeline import KINDS, Period
    from app.ui.presenter.side_entries import browse_entries
    from app.reports.timeline_words import KIND_WORDS

    side = SideList("browseSide", accessible="Years and months",
                    tip="Click a year or a month to open it", open_sections=("Years", "Show"))
    ref = weakref.ref(view)
    kinds = [(KIND_WORDS[key], key) for key in KINDS]

    def draw(*_args: Any) -> None:
        page_view = ref()
        if page_view is not None:
            side.set_entries(browse_entries(page_view._overview, page_view.picker.kind(), kinds))

    def picked(entry: Any) -> None:
        page_view = ref()
        if page_view is None or not entry.value:
            return
        what, value = entry.value
        if what == "year":
            page_view.browse(Period.year(int(value)))
        elif what == "month":
            year, month = value
            page_view.picker.show_month(int(year), int(month))
            page_view.browse(Period.month(int(year), int(month)))
        elif what == "kind":
            box = page_view.picker.kind_box
            box.setCurrentIndex(max(0, box.findData(value)))

    side.picked.connect(picked)
    def ready(overview: Any) -> None:
        page_view = ref()
        if page_view is not None:
            type(page_view)._overview_ready(page_view, overview)   # the page's own, then the list
            draw()

    view._overview_ready = ready
    view.picker.kind_changed.connect(lambda _k: draw())
    top_row = view.picker.layout().itemAt(0).layout()
    attach(view, side, target=view.stack, row=top_row, before=None, page="browse",
           words="Years and months", tip=BROWSE_TIP)
    top_row.removeWidget(view.toggles["side"])
    top_row.insertWidget(0, view.toggles["side"])
    draw()
    return side
