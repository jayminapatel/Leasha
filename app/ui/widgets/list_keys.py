r"""One list behaviour: the keys and the double-click every list answers the same way.

Layer: L5

UI phase B (the owner: every screen should feel like one app). Each list had
its own keys: Files and Code opened on Enter through an event filter, Mail
through the page's `keyPressEvent`, the timeline in its own `keyPressEvent`,
Browse and Tidy through `activated`, and some lists through
`itemDoubleClicked` *and* a key handler - two routes to the same action, which
is how a double-click once opened the photo viewer twice. A view now installs
this once and says what its actions are:

* **Double-click and Enter run the default action, from one signal**:
  `activated`, which Qt sends for a double-click; Enter is answered here by
  sending the same `activated`, so the action has exactly one source.
* **Space shows the preview** where the page has one - the page's own Preview
  toggle (`view_button.toggle_preview`, the one Ctrl+Shift+P presses), found
  by walking up from the list. On a line that carries a tick box Space stays
  Qt's, which ticks it: Tidy's lists are marked from the keyboard that way.
* **Delete runs the destructive line** where there is one, through the same
  function the menu calls - so its confirmation is the one it always asked.
* **F2 renames** where a rename exists.
* **Ctrl+A selects everything** (Qt's own, for a list that allows more than
  one) and **Ctrl+C copies the path or paths** where the list has paths.

Each key is claimed at ShortcutOverride as well as answered at KeyPress, so a
window-wide shortcut on the same key can never take it from a list that has
focus. Keys a list does not use are left exactly as Qt handles them.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QAbstractItemView

from app.ui.widgets.menus import weak_call

__all__ = ["ListKeys", "install", "PAGE_PREVIEW", "page_preview"]

#: `preview=PAGE_PREVIEW`: use the page's own Preview toggle, found at the key
#: press - a list does not know which page it is on, and Chat's sources list is
#: the same widget as Search's.
PAGE_PREVIEW = "page"

_ENTER = (Qt.Key.Key_Return, Qt.Key.Key_Enter)


def page_preview(widget: Any) -> Optional[Callable[[], Any]]:
    """The Preview toggle of the page `widget` is on, or None.

    The same toggle the window's Ctrl+Shift+P presses (`shell._toggle_preview`):
    a page's `view_button.toggle_preview`, or a page's own `toggle_preview`
    (Chat). The nearest one wins.
    """
    here = widget
    while here is not None:
        button = getattr(here, "view_button", None)
        toggle = getattr(button, "toggle_preview", None) or getattr(here, "toggle_preview", None)
        if callable(toggle):
            return toggle
        here = here.parentWidget()
    return None


class ListKeys(QObject):
    """The behaviour on one list. A child of the list, so it goes with it."""

    def __init__(self, view: QAbstractItemView, *, default: Any = None, preview: Any = None,
                 delete: Any = None, rename: Any = None, copy: Any = None) -> None:
        super().__init__(view)
        # Nothing here holds the view: it is `self.parent()`. The view holds this
        # (`install`), and a reference back would be a cycle only the collector
        # could end (`tests/unit/test_views_are_freed.py`).
        # Weakly: a list's own child must never keep the page alive.
        self._default = weak_call(default) if default is not None else None
        self._preview = preview if preview == PAGE_PREVIEW else (
            weak_call(preview) if preview is not None else None)
        self._delete = weak_call(delete) if delete is not None else None
        self._rename = weak_call(rename) if rename is not None else None
        self._copy = weak_call(copy) if copy is not None else None
        if self._default is not None:
            view.activated.connect(self._activated)
        view.installEventFilter(self)

    def _activated(self, index: Any) -> None:
        if self._default is not None:
            self._default(index)

    def _handler(self, event: Any) -> Optional[Callable[[], Any]]:
        """What this key does on this list, or None when it is Qt's."""
        key, mods = event.key(), event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        view = self.parent()
        if key in _ENTER and not mods and self._default is not None:
            current = view.currentIndex()
            return (lambda: view.activated.emit(current)) if current.isValid() else None
        if key == Qt.Key.Key_Space and not mods and self._preview is not None:
            current = view.currentIndex()
            # A line that shows a tick box: Space ticks it, as it always has. Asked of the
            # box itself, not the flag - a table cell is "checkable" by default and shows none.
            if current.isValid() and current.data(Qt.ItemDataRole.CheckStateRole) is not None:
                return None
            if self._preview == PAGE_PREVIEW:
                return page_preview(view)
            return self._preview
        if key == Qt.Key.Key_Delete and not mods:
            return self._delete
        if key == Qt.Key.Key_F2 and not mods:
            return self._rename
        if event.matches(QKeySequence.StandardKey.Copy):
            return self._copy
        return None

    def eventFilter(self, watched: Any, event: Any) -> bool:      # noqa: N802 - Qt's naming
        kind = event.type()
        if watched is not self.parent() or kind not in (QEvent.Type.KeyPress,
                                                     QEvent.Type.ShortcutOverride):
            return False
        handler = self._handler(event)
        if handler is None:
            return False
        if kind == QEvent.Type.ShortcutOverride:
            event.accept()                        # the list has it, not the window
            return True
        handler()
        return True


def install(view: QAbstractItemView, **actions: Any) -> ListKeys:
    """Give `view` the one list behaviour. Once per list. See the module docstring.

    `default(index)` is what a double-click and Enter do; `preview()`, `delete()`,
    `rename()` and `copy()` take nothing. Any left out is not offered, and its key
    stays Qt's. `preview=PAGE_PREVIEW` uses the page's own Preview toggle.
    """
    keys = ListKeys(view, **actions)
    # Held by the view: a filter with only a C++ parent can lose its Python half,
    # and Qt then calls a fresh wrapper that has none of the actions.
    view._list_keys = keys
    view.setProperty("listKeys", True)
    return keys
