r"""A chat message's right-click menu, and its actions shown only on hover.

Layer: L5 widget helper

Order 1o (2026-10-11), the owner: "the chat interface can it be like claude ...
may be buttons are not required just the context menu". **D3**: the row of
actions under each message no longer sits there all the time - it appears while
the pointer is over that message (its room is kept, so nothing jumps) - and
every action, plus Copy as plain text, is in the message's right-click menu,
which the keyboard's menu key opens too. Nothing is taken away: the same
actions, in quieter places.

UI phase B (2026-10-13): the newest answer's row and the last message's Edit are
in view without the pointer (`chat_bubbles`); hover is for the older answers.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMenu, QTextBrowser

__all__ = ["answer_menu", "user_menu", "install", "COPY_PLAIN"]

COPY_PLAIN = "Copy as plain text"


def _selection(widget: Any) -> str:
    if isinstance(widget, QTextBrowser):
        return widget.textCursor().selectedText().replace(" ", "\n")
    return ""


def answer_menu(bubble: Any, under: Any = None) -> QMenu:
    """Copy (what is selected, or the whole answer), Copy as plain text, and
    Regenerate or Try again when the answer offers them. Built by `menus.build`,
    in this order: Copy first, as under the message."""
    from app.ui.widgets.menus import Item, build

    chosen = _selection(under)
    has_words = bool(bubble.raw.strip())
    lines = [Item("Copy selection", lambda: QGuiApplication.clipboard().setText(chosen),
                  group="copy")] if chosen else []
    lines += [Item("Copy", bubble.copy, group="copy", enabled=has_words),
              Item(COPY_PLAIN, lambda: QGuiApplication.clipboard().setText(bubble.body_text()),
                   group="copy", enabled=has_words)]
    again = [(bubble.regenerate_button, "Regenerate", bubble.regenerate_requested),
             (bubble.retry_button, "Try again", bubble.retry_requested)]
    lines += [Item(label, signal.emit) for button, label, signal in again if bubble.offers(button)]
    return build(bubble, lines, grouped=False)


def user_menu(bubble: Any, under: Any = None) -> QMenu:
    """Copy, and Edit on the person's newest message."""
    from app.ui.widgets.menus import Item, build

    chosen = _selection(under)
    lines = [Item("Copy selection", lambda: QGuiApplication.clipboard().setText(chosen),
                  group="copy")] if chosen else []
    lines.append(Item("Copy", lambda: QGuiApplication.clipboard().setText(bubble.text), group="copy"))
    if bubble.is_last:
        lines.append(Item("Edit", bubble.edit_requested.emit))
    return build(bubble, lines, grouped=False)


class _Watcher(QObject):
    """On the bubble: hover shows its actions. On its text: right-click is its menu."""

    def __init__(self, bubble: Any, build: Any) -> None:
        super().__init__(bubble)
        self._bubble, self._build = bubble, build

    def eventFilter(self, watched: Any, event: Any) -> bool:      # noqa: N802 - Qt's name
        kind = event.type()
        if watched is self._bubble and kind in (QEvent.Type.Enter, QEvent.Type.Leave):
            self._bubble.set_hover(kind == QEvent.Type.Enter)
        elif kind == QEvent.Type.ContextMenu:
            # A right-click lands on a text view's viewport; the menu key on the view.
            under = watched.parent() if isinstance(watched.parent(), QTextBrowser) else watched
            menu = self._build(self._bubble, under)
            self._bubble.last_menu = menu
            menu.popup(event.globalPos())        # an event's own screen point: nothing to map
            return True
        return False


def install(bubble: Any, build: Any, row: Any) -> None:
    """Hover and the menu for one bubble. `row` keeps its room while it is hidden."""
    policy = row.sizePolicy()
    policy.setRetainSizeWhenHidden(True)
    row.setSizePolicy(policy)
    bubble._menu_watcher = _Watcher(bubble, build)
    bubble.installEventFilter(bubble._menu_watcher)
    bubble.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
    watch_text(bubble)


def watch_text(bubble: Any) -> None:
    """Give every text view in the bubble the bubble's menu (each prose piece of an
    answer is made as it streams, so this is called again after each redraw)."""
    watcher = getattr(bubble, "_menu_watcher", None)
    if watcher is None:
        return
    for view in bubble.findChildren(QTextBrowser):
        if not view.property("chatMenu"):
            view.setProperty("chatMenu", True)
            view.viewport().installEventFilter(watcher)
            view.installEventFilter(watcher)
