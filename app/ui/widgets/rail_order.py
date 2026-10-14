r"""The rail's pages in any order: dragged, or moved from a page button's menu.

Layer: L5 widget helper

Order 1m (2026-10-11), the owner: "is there a way they can be dragged in any
order ?". Two ways, the same moves (D1):

* **Drag** a page button up or down the rail and drop it where it should go.
* **Right-click** it: Move up, Move down, Reset page order - for anybody who
  finds dragging fiddly, and for the keyboard (the menu key opens it).

Indexing (the pill) and Settings (the foot) neither move nor take a drop:
`Rail.movable` says which pages may. The rail emits `order_changed`; the window
remembers it (`ui:rail_order`). Kept out of `rail.py`, which is long already.

**IA-11 (2026-10-14): a page the owner rarely uses can leave the rail.** The same
menu has "Hide from the rail" (not on Search; Indexing and Settings have no menu)
and "Show hidden pages"; the View menu has the second too. A hidden page is still
a page - the Go menu opens it - and the keys that number the pages skip it
(`Rail.page_order`). Remembered beside the order, as titles (`HIDDEN_KEY`).
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, QMimeData, QObject, Qt
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import QApplication

__all__ = ["make_movable", "MIME", "target_index", "move_up", "move_down", "apply_order",
           "wire", "restore", "DEFAULT_ORDER", "STATE_KEY", "HIDDEN_KEY"]

#: What a dragged page button carries: the page's position, as text.
MIME = "application/x-leasha-rail-page"


def target_index(rail: Any, y: float) -> int:
    """The movable page whose button is under `y` (the rail column's own
    coordinates), or the nearest one above or below. -1 when there is none."""
    best, best_distance = -1, None
    for index in sorted(rail._buttons):
        # A hidden page's button is not on the rail, and its geometry is stale.
        if not rail.movable(index) or not rail.shown(index):
            continue
        geometry = rail._buttons[index].geometry()
        if geometry.top() <= y <= geometry.bottom():
            return index
        distance = min(abs(y - geometry.top()), abs(y - geometry.bottom()))
        if best_distance is None or distance < best_distance:
            best, best_distance = index, distance
    return best


def _neighbour(rail: Any, index: int, step: int) -> int:
    """The movable page `step` places from `index` on the rail - a hidden one is stepped over."""
    order = [i for i in sorted(rail._buttons) if rail.movable(i) and rail.shown(i)]
    if index not in order:
        return -1
    at = order.index(index) + step
    return order[at] if 0 <= at < len(order) else -1


def move_up(rail: Any, index: int) -> bool:
    target = _neighbour(rail, index, -1)
    return target >= 0 and rail.moveTab(index, target)


def move_down(rail: Any, index: int) -> bool:
    target = _neighbour(rail, index, +1)
    return target >= 0 and rail.moveTab(index, target)


def _menu(rail: Any, button: Any, point: Any) -> None:
    index = rail.index_of_button(button)
    if not rail.movable(index):
        return
    from app.ui.widgets import menus

    menu = menus.build(button, [
        menus.Item("Move up", lambda: move_up(rail, rail.index_of_button(button)),
                   enabled=_neighbour(rail, index, -1) >= 0),
        menus.Item("Move down", lambda: move_down(rail, rail.index_of_button(button)),
                   enabled=_neighbour(rail, index, +1) >= 0),
        # The order the window remembers, put back: after a separator, as before.
        # IA-11: off the rail, and every one back. Search always stays.
        menus.Item("Hide from the rail", lambda: rail.hide_page(rail.index_of_button(button)),
                   icon="circle-minus", enabled=rail.hideable(index),
                   tip="Take this page off the rail; the Go menu still opens it"),
        menus.Item("Reset page order", rail.reset_requested.emit, group="remember",
                   tip="Put the pages back in Leasha's order"),
        menus.Item("Show hidden pages", lambda: rail.set_hidden(()), group="remember",
                   icon="eye", enabled=bool(rail.hidden_pages()),
                   tip="Put every page you hid back on the rail"),
    ])
    menus.show(menu, button, point, wait=False, keep=rail)


class _Dragger(QObject):
    """Starts a drag once a pressed page button has moved far enough."""

    def __init__(self, rail: Any, button: Any) -> None:
        super().__init__(button)
        self._rail, self._button, self._pressed = rail, button, None

    def eventFilter(self, watched: Any, event: Any) -> bool:      # noqa: N802 - Qt's name
        kind = event.type()
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self._pressed = event.position().toPoint()
        elif kind == QEvent.Type.MouseButtonRelease:
            self._pressed = None
        elif (kind == QEvent.Type.MouseMove and self._pressed is not None
              and event.buttons() & Qt.MouseButton.LeftButton
              and (event.position().toPoint() - self._pressed).manhattanLength()
              >= QApplication.startDragDistance()):
            self._pressed = None
            index = self._rail.index_of_button(self._button)
            if self._rail.movable(index):
                data = QMimeData()
                data.setData(MIME, str(index).encode("ascii"))
                drag = QDrag(self._button)
                drag.setMimeData(data)
                drag.setPixmap(self._button.grab())
                drag.exec(Qt.DropAction.MoveAction)
                return True
        return False


class _DropTarget(QObject):
    """The rail column: takes a dragged page and puts it where it was dropped."""

    def __init__(self, rail: Any) -> None:
        super().__init__(rail.column)
        self._rail = rail

    def eventFilter(self, watched: Any, event: Any) -> bool:      # noqa: N802
        kind = event.type()
        if kind in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
            if event.mimeData().hasFormat(MIME) and target_index(
                    self._rail, event.position().y()) >= 0:
                event.acceptProposedAction()
                return True
        elif kind == QEvent.Type.Drop and event.mimeData().hasFormat(MIME):
            try:
                source = int(bytes(event.mimeData().data(MIME)).decode("ascii"))
            except ValueError:
                return False
            target = target_index(self._rail, event.position().y())
            if target >= 0:
                self._rail.moveTab(source, target)
            event.acceptProposedAction()
            return True
        return False


def make_movable(rail: Any, button: Any) -> None:
    """Let this page button be dragged, and give it the Move menu."""
    if getattr(rail, "_drop_target", None) is None:
        rail.column.setAcceptDrops(True)
        rail._drop_target = _DropTarget(rail)
        rail.column.installEventFilter(rail._drop_target)
    dragger = _Dragger(rail, button)
    button.installEventFilter(dragger)
    button._dragger = dragger
    button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
    button.customContextMenuRequested.connect(lambda point: _menu(rail, button, point))


# -- the window's side: remembered, applied at start, reset ----------------------

#: The owner's order (2026-10-11), and what Reset page order goes back to.
DEFAULT_ORDER = ("Search", "Mail", "Photos", "Files", "Contacts", "Code", "Browse", "Chat", "Tidy")
#: Order 1q D8 (2026-10-11): "Reports" became "Tidy"; a saved order that names the old
#: title keeps the page where the owner put it.
RENAMED = {"Reports": "Tidy"}

#: Where the order is remembered: titles, `|`-separated (D2).
STATE_KEY = "ui:rail_order"
#: IA-11: where the hidden pages are remembered, beside the order - titles, `|`-separated.
HIDDEN_KEY = "ui:rail_hidden"


def apply_order(rail: Any, titles: Any) -> None:
    """Put the named pages in this order. A title with no page is ignored; a page the
    list does not name keeps its place after the named ones. Says nothing while it
    works (`order_changed` is blocked), because applying is not choosing."""
    wanted = [str(t) for t in titles if str(t)]
    rail.blockSignals(True)
    try:
        slot = 0
        for title in wanted:
            movable = [i for i in sorted(rail._buttons) if rail.movable(i)]
            if slot >= len(movable):
                break
            source = next((i for i in movable if rail.tabText(i) == title), -1)
            if source < 0:
                continue
            if source != movable[slot]:
                rail.moveTab(source, movable[slot])
            slot += 1
    finally:
        rail.blockSignals(False)


def wire(window: Any) -> None:
    """Connect the rail to the window: a move re-reads `_tab_index` and is
    remembered; Reset goes back to `DEFAULT_ORDER` and forgets the saved one."""
    from app.ui.state_writes import save_state

    rail = window.rail

    def reindex(*_args: Any) -> None:
        for view, wrapped in window._tab_wrapped.items():
            window._tab_index[view] = rail.indexOf(wrapped)

    def remember(titles: list) -> None:
        reindex()
        save_state(window._store, STATE_KEY, "|".join(titles), component="ui.rail")

    def reset() -> None:
        apply_order(rail, DEFAULT_ORDER)
        reindex()
        save_state(window._store, STATE_KEY, "", component="ui.rail")

    def remember_hidden(titles: list) -> None:
        save_state(window._store, HIDDEN_KEY, "|".join(titles), component="ui.rail")

    rail.order_changed.connect(remember)
    rail.reset_requested.connect(reset)
    rail.hidden_changed.connect(remember_hidden)
    window.reset_page_order = reset
    window.show_hidden_pages = lambda: rail.set_hidden(())


def restore(window: Any) -> None:
    """Once every movable page exists: the saved order, if there is one."""
    try:
        saved = str(window._store.get_state(STATE_KEY, "") or "")
    except Exception:                                  # noqa: BLE001 - the default order stands
        saved = ""
    if saved:
        apply_order(window.rail, [RENAMED.get(t, t) for t in saved.split("|")])
        for view, wrapped in window._tab_wrapped.items():
            window._tab_index[view] = window.rail.indexOf(wrapped)
    try:
        hidden = str(window._store.get_state(HIDDEN_KEY, "") or "")
    except Exception:                                  # noqa: BLE001 - every page shows
        hidden = ""
    if hidden:
        # Applied without saying so: restoring is not choosing, as for the order.
        window.rail.blockSignals(True)
        try:
            window.rail.set_hidden([RENAMED.get(t, t) for t in hidden.split("|") if t])
        finally:
            window.rail.blockSignals(False)
