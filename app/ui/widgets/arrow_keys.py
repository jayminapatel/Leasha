r"""Type, then arrow: the keys a page's box hands to the page's list.

Layer: L5

MS-4 of the UX review (2026-10-14). On Search and in the quick search box the
arrow keys move the results while the cursor stays in the box - type, Down,
Down, Enter - and on every other page they stopped at the box. `forward_arrows`
is that behaviour for any box and list, installed beside `list_keys.install`:

* **Down / Up / Page Down / Page Up** in the box move the list's selection, as
  if pressed on the list - so a grid moves by a row and a table by a line, each
  in its own way. With nothing chosen yet, the first press chooses the first row.
* **Enter opens the current row** through the list's default action (its
  `activated` - the one `list_keys` gives Enter and a double-click) - **but only
  once an arrow has moved it from the box**, and until the text changes again.
  A box whose Enter runs its search (Code) still does: Enter after typing
  searches, Enter after arrowing opens.
* While the box's `/` menu is open it keeps every key.

The list may be given as a function returning the list in front (Photos swaps a
grid for a table). Nothing here holds the page: the filter is the box's child.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Union

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QAbstractItemView, QApplication, QLineEdit

__all__ = ["ArrowKeys", "forward_arrows", "MOVES"]

#: The keys handed to the list.
MOVES = (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_PageDown, Qt.Key.Key_PageUp)

_ENTER = (Qt.Key.Key_Return, Qt.Key.Key_Enter)

ViewOrGetter = Union[QAbstractItemView, Callable[[], Optional[QAbstractItemView]]]


class ArrowKeys(QObject):
    """The behaviour on one box. A child of the box, so it goes with it."""

    def __init__(self, box: QLineEdit, view: ViewOrGetter) -> None:
        super().__init__(box)
        self._view = view
        #: An arrow moved the list since the text last changed: Enter opens.
        self.armed = False
        box.textChanged.connect(self._disarm)
        box.installEventFilter(self)

    def _disarm(self, *_args: Any) -> None:
        self.armed = False

    def view(self) -> Optional[QAbstractItemView]:
        """The list the keys go to now."""
        view = self._view
        if not isinstance(view, QAbstractItemView) and callable(view):
            view = view()
        return view if isinstance(view, QAbstractItemView) else None

    @staticmethod
    def _menu_open() -> bool:
        """A popup (the box's `/` menu, set with `setWidget`, not `setCompleter`) is open."""
        return QApplication.activePopupWidget() is not None

    def move(self, key: Any) -> bool:
        """Move the list as `key` would. True when there was a row to move to."""
        view = self.view()
        model = view.model() if view is not None else None
        if model is None or model.rowCount() == 0:
            return False
        if not view.currentIndex().isValid():
            first = model.index(0, 0)
            view.setCurrentIndex(first)
            view.scrollTo(first)
        else:
            QApplication.sendEvent(view, QKeyEvent(QEvent.Type.KeyPress, key,
                                                   Qt.KeyboardModifier.NoModifier))
        self.armed = True
        return True

    def open_current(self) -> bool:
        """Enter: the current row's default action. True when there was one."""
        view = self.view()
        current = view.currentIndex() if view is not None else None
        if current is None or not current.isValid():
            return False
        view.activated.emit(current)
        return True

    def eventFilter(self, watched: Any, event: Any) -> bool:      # noqa: N802 - Qt's naming
        if watched is not self.parent() or event.type() != QEvent.Type.KeyPress:
            return False
        mods = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if mods or self._menu_open():
            return False
        if event.key() in MOVES:
            return self.move(event.key())
        if event.key() in _ENTER and self.armed:
            return self.open_current()
        return False


def forward_arrows(box: QLineEdit, view: ViewOrGetter) -> ArrowKeys:
    """Give `box` the type-then-arrow behaviour over `view`. Once per box."""
    keys = ArrowKeys(box, view)
    # Held by the box: a filter with only a C++ parent can lose its Python half.
    box._arrow_keys = keys
    return keys
