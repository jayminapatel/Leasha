"""Enter opens the chosen line of a list on a Mac, as it does on Windows.

Layer: L5

2026-10-05, the first whole-suite runs on macOS. Every list in the window acts
on Qt's `activated` signal - a search result opens or unfolds, a photo opens, a
message is chosen. On Windows Qt sends that signal for a double-click and for
Enter. **On macOS it does not send it for Enter**: there Enter starts editing
the line's name, and "open" is Cmd+O. Nothing in Leasha's lists can be renamed,
so on a Mac Enter did nothing at all, and the keyboard-only journey through
Search stopped at its sixth step
(`test_the_search_page_start_to_finish_with_the_keyboard_alone`).

One filter for the whole application rather than a key handler per list: the
list somebody adds next year gets it without knowing this note exists.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QAbstractItemView, QApplication

from app.core.osbridge._platform import is_macos

__all__ = ["EnterActivates", "install"]

_installed: Optional["EnterActivates"] = None


class EnterActivates(QObject):
    """Says `activated` for Enter on a list's current line, if nothing else did.

    **The key goes to the list first, and this speaks only afterwards**
    (2026-10-05, the second version). The first version sent `activated` and
    kept the key, for every list. Four tests failed on macOS: the Code page and
    Files hear Enter through an event filter of their own, the timeline in its
    `keyPressEvent`, Mail in the page around its table - and a filter on the
    application runs before all of them, so the key never arrived. Now the key
    is delivered as it always was, and `activated` is sent here only if nobody
    took the key and the list did not send it itself (as it does on Windows).
    """

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self._delivering = False

    def eventFilter(self, watched: Any, event: Any) -> bool:  # noqa: N802 - Qt's name
        if self._delivering:
            return False                         # our own delivery, on its way through
        try:
            if event.type() != QEvent.Type.KeyPress:
                return False
            if event.key() not in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                return False
            if event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier:
                return False                     # Ctrl+Enter and the rest keep their own meaning
            view = watched if isinstance(watched, QAbstractItemView) else None
            if view is None:
                return False
            if view.state() == QAbstractItemView.State.EditingState:
                return False                     # typing into a cell: Enter finishes it
            index = view.currentIndex()
            if not index.isValid():
                return False
        except Exception:                        # noqa: BLE001 - a key press must never raise
            return False
        said: list = []

        def heard(_index: Any) -> None:
            said.append(1)

        try:
            view.activated.connect(heard)
            self._delivering = True
            try:
                QApplication.sendEvent(view, event)
            finally:
                self._delivering = False
                view.activated.disconnect(heard)
            if not said and not event.isAccepted():
                view.activated.emit(index)
        except Exception:                        # noqa: BLE001 - e.g. the key closed the list
            pass
        return True                              # delivered above; not a second time


def install(app: Any = None, *, force: bool = False) -> Optional[EnterActivates]:
    """Install the filter on the application, once. Only on macOS unless `force`.

    Windows and Linux already send `activated` for Enter; filtering there
    would send it twice.
    """
    global _installed
    if not (force or is_macos()):
        return None
    app = app or QApplication.instance()
    if app is None:
        return None
    if _installed is None:
        _installed = EnterActivates(app)
        app.installEventFilter(_installed)
    return _installed
