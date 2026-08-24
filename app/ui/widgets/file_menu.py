"""The right-click menu for a file, wherever a file is shown.

Layer: L5

Two lists show files - search results and the filename browser - and until now
only one of them could be right-clicked. The other could be double-clicked to
reveal a file in Explorer and nothing else: no open, no copy path, no way to
search inside it. A list of files you cannot act on is a list of disappointments.

**One menu, built from a path**, so the two lists cannot drift apart and a third
gets it for free.

**Every action is checked before it is offered.** A file that has moved or been
deleted since it was indexed cannot be opened, so "Open" is greyed out and a
"re-index this folder" action appears in its place - the offer only shows up
when it is the thing that would actually help. Offering an action that then
fails is worse than not offering it, because the person has to discover the
failure themselves.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from PyQt6.QtGui import QAction, QGuiApplication
from PyQt6.QtWidgets import QMenu, QWidget

__all__ = ["FileActions", "build_menu"]


class FileActions:
    """What the menu can do. Each is optional; an unset one is not offered.

    Callbacks rather than signals because the menu is built per click and thrown
    away - a QObject with signals would have to be owned by something, and the
    only thing to own it is the view that already has the handlers.
    """

    def __init__(
        self,
        *,
        open_file: Optional[Callable[[], None]] = None,
        reveal: Optional[Callable[[], None]] = None,
        search_inside: Optional[Callable[[], None]] = None,
        reindex: Optional[Callable[[], None]] = None,
    ) -> None:
        self.open_file = open_file
        self.reveal = reveal
        self.search_inside = search_inside
        self.reindex = reindex


def build_menu(parent: QWidget, path: str, actions: FileActions) -> QMenu:
    """The menu for one file. Always returns a menu with at least one item.

    "Copy path" is always available and never disabled, because it works whether
    or not the file still exists - and when a file has gone missing, its path is
    often exactly what somebody needs in order to work out where it went.
    """
    menu = QMenu(parent)
    exists = _exists(path)

    if actions.open_file is not None:
        action = QAction("Open", parent)
        action.setEnabled(exists)
        action.triggered.connect(lambda: actions.open_file())
        menu.addAction(action)

    if actions.reveal is not None:
        action = QAction("Show in folder", parent)
        action.setEnabled(exists)
        action.triggered.connect(lambda: actions.reveal())
        menu.addAction(action)

    if actions.search_inside is not None:
        # The bridge between the two lists: found it by name, now find what is
        # in it. Without this, the filename browser is a dead end.
        action = QAction("Search inside this file", parent)
        action.triggered.connect(lambda: actions.search_inside())
        menu.addAction(action)

    menu.addSeparator()

    copy_path = QAction("Copy path", parent)
    copy_path.triggered.connect(lambda: _copy(path))
    menu.addAction(copy_path)

    copy_name = QAction("Copy file name", parent)
    copy_name.triggered.connect(lambda: _copy(Path(path).name))
    menu.addAction(copy_name)

    if not exists and actions.reindex is not None:
        menu.addSeparator()
        action = QAction("File is missing - re-index this folder", parent)
        action.triggered.connect(lambda: actions.reindex())
        menu.addAction(action)

    return menu


def _exists(path: str) -> bool:
    """Never raises. A path on a disconnected drive, a malformed one, or one too
    long for the filesystem all mean "cannot open it", not "crash the menu"."""
    try:
        return Path(path).exists()
    except OSError:
        return False


def _copy(text: str) -> None:
    clipboard = QGuiApplication.clipboard()
    if clipboard is not None:
        clipboard.setText(text)


def show_for(widget: Any, point: Any, path: str, actions: FileActions) -> None:
    """Pop the menu at the cursor. The one line a view needs to call."""
    menu = build_menu(widget, path, actions)
    menu.exec(widget.mapToGlobal(point))
