r"""Quick look: Space opens the current row in a window of its own; Space again closes it.

Layer: L5

Review 2026-10-14, MS-9. Space meant three things - the docked preview pane in
the lists, the info panel in Photos, the next photo in the viewer - and nothing
gave the quick, throwaway look a photo library or a file browser gives. Now, on
every result list (Search, Files, Mail, Code, Browse, Tidy) and on the Search
page's photo grid, **Space opens the current row in the lightbox**
(`PreviewWindow`, the window a pinned document and a thumbnail already open in)
and **Space closes it again**. The docked pane stays where it was, on
Ctrl+Shift+P and the View menu. A line with a tick box still ticks on Space
(`list_keys`).

**Built by `preview_window.pop_out`**, so Open and Show in folder take the row
to the one open route, Describe has the window's settings, and the window's
size and place are remembered as every pop-out's are. A picture brings its
siblings along, so Left and Right walk the photos of the same result set.

**Space inside the window closes it** - a window shortcut, so the find box,
which takes typed spaces for itself, is left alone - and focus goes back to the
list it was opened from, so the next Space opens the next look.
"""

from __future__ import annotations

import weakref
from typing import Any, Callable, Optional, Sequence

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QKeySequence, QShortcut

__all__ = ["QuickLook", "store_of", "nearest"]


def store_of(widget: Any) -> Any:
    """The store of the page `widget` sits on - a page's `_store`, or its
    engine's - found by walking up. None when there is none (a test's page)."""
    here = widget
    while here is not None:
        store = getattr(here, "_store", None)
        if store is None:
            store = getattr(getattr(here, "_engine", None), "store", None)
        if store is not None:
            return store
        here = here.parentWidget() if hasattr(here, "parentWidget") else None
    return None


def nearest(widget: Any) -> Optional["QuickLook"]:
    """The quick look of the list `widget` belongs to, found by walking up."""
    here = widget
    while here is not None:
        look = getattr(here, "quick_look", None)
        if isinstance(look, QuickLook):
            return look
        here = here.parentWidget() if hasattr(here, "parentWidget") else None
    return None


def _state_of(store: Any) -> dict:
    """Every `index_state` row, for the window's remembered size and pin."""
    if store is None:
        return {}
    try:
        return store.all_state()
    except Exception:                              # noqa: BLE001 - opens fresh
        return {}


class QuickLook(QObject):
    """One transient look window for one list. A child of the list's page."""

    def __init__(self, owner: Any, *, current: Callable[[], Any],
                 siblings: Optional[Callable[[], Sequence[Any]]] = None,
                 body_provider: Optional[Callable[[], Any]] = None,
                 store: Any = None, on_error: Any = None) -> None:
        super().__init__(owner)
        self._owner = weakref.ref(owner)
        self._current = current
        self._siblings = siblings
        self._body_provider = body_provider
        self._store = store
        self._on_error = on_error
        self.window: Any = None
        #: The widget that had focus when the look opened, given it back on close.
        self._return_to: Any = None

    def is_open(self) -> bool:
        window = self.window
        try:
            return window is not None and window.isVisible()
        except RuntimeError:                       # its C++ half is gone
            self.window = None
            return False

    def toggle(self, row: Any = None) -> Any:
        """Open a look at `row` (default: the list's current row), or close the open one.

        Returns the window opened, or None when one was closed or nothing was current.
        """
        if self.is_open():
            self.close()
            return None
        row = row if row is not None else self._current()
        if row is None:
            return None
        return self._open(row)

    def close(self) -> None:
        if self.is_open():
            self.window.close()

    def _open(self, row: Any) -> Any:
        from app.ui.state_writes import save_states
        from app.ui.widgets import preview_window

        store = self._store if self._store is not None else store_of(self._owner())
        siblings = list(self._siblings() or ()) if self._siblings is not None else []
        index = siblings.index(row) if row in siblings else 0
        if row not in siblings:
            siblings = []
        provider = self._body_provider() if self._body_provider is not None else None
        from PySide6.QtWidgets import QApplication

        self._return_to = QApplication.focusWidget()
        window = preview_window.pop_out(
            row, store=store, state=_state_of(store), siblings=siblings, index=index,
            on_error=self._on_error, body_provider=provider,
            remember=lambda values: save_states(store, values, component="ui.quick_look"),
            closed=self._closed)
        self.window = window
        shortcut = QShortcut(QKeySequence(Qt.Key.Key_Space), window)
        shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        shortcut.activated.connect(window.close)
        window._quick_look_close = shortcut          # held by the window it closes
        return window

    def _closed(self, window: Any) -> None:
        """The look closed: forget it, and give focus back to the list."""
        if window is self.window:
            self.window = None
        target = self._return_to
        self._return_to = None
        try:
            if target is not None and target.isVisible():
                target.window().activateWindow()
                target.setFocus(Qt.FocusReason.OtherFocusReason)
        except RuntimeError:                       # the list went meanwhile
            pass
