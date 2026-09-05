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
        pin: Optional[Callable[[], None]] = None,
        similar: Optional[Callable[[], None]] = None,
        copy: Optional[list[tuple[str, str]]] = None,
    ) -> None:
        self.open_file = open_file
        self.reveal = reveal
        self.search_inside = search_inside
        self.reindex = reindex
        #: Workspace §3c: gather this result into the pinned panel. Offered
        #: whatever the file's own state - even a missing one is worth
        #: keeping track of, which "Open" and "Show in folder" are not.
        self.pin = pin
        #: Work order 0h §2d: "what else looks/reads like this" - built on
        #: `SearchEngine.similar_to`, which already exists and needed no
        #: change to be reachable from a text result. Offered whatever the
        #: file's own state, same reasoning as `pin`: a vector neighbour
        #: search does not care whether the file is still on disk.
        self.similar = similar
        #: `(label, text)` pairs to offer alongside "Copy path". For mail:
        #: subject and sender are what people actually want on the clipboard,
        #: and a message's "file name" is a synthetic key nobody would
        #: recognise, let alone want to paste anywhere.
        self.copy = copy or []


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

    if actions.pin is not None:
        action = QAction("Pin", parent)
        action.triggered.connect(lambda: actions.pin())
        menu.addAction(action)

    if actions.similar is not None:
        action = QAction("More like this", parent)
        action.setToolTip(
            "Find other results with a similar meaning, using the vector "
            "already stored for this one."
        )
        action.triggered.connect(lambda: actions.similar())
        menu.addAction(action)

    menu.addSeparator()

    copy_path = QAction("Copy path", parent)
    copy_path.triggered.connect(lambda: _copy(path))
    menu.addAction(copy_path)

    if actions.copy:
        # Given explicitly, so a caller that has better things to offer than a
        # basename can say so. Mail does: subject and sender.
        for label, text in actions.copy:
            action = QAction(label, parent)
            action.triggered.connect(lambda _checked=False, value=text: _copy(value))
            menu.addAction(action)
    else:
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
    """Pop the menu at the cursor. The one line a view needs to call.

    `point` stays widget-relative here - `mapToGlobal` on the view is what
    places the popup correctly. Only the *row lookup* needs the viewport, which
    is what `viewport_point` below is for.
    """
    menu = build_menu(widget, path, actions)
    menu.exec(widget.mapToGlobal(point))


def viewport_point(view: Any, point: Any) -> Any:
    """Convert a `customContextMenuRequested` point into viewport coordinates.

    **This is why right-click appeared not to work.** The signal delivers a
    point relative to the *widget*; `itemAt`, `rowAt` and `indexAt` all expect
    the *viewport*. Between them sits the header and the frame - about 25
    pixels on a table.

    So the lookup was consistently one row low. Right-clicking the first row
    acted on the second, and right-clicking the last row produced a y past the
    end of the viewport, `rowAt` returned -1, and the handler returned without
    showing anything. Reported as "right click menu don't work", and from the
    outside that is exactly what it looks like: sometimes the wrong thing,
    sometimes nothing.

    Both symptoms are the same missing line, which is the argument for it being
    a named function that every view calls rather than a `- 25` somewhere.
    """
    viewport = view.viewport()
    if viewport is None or viewport is view:
        return point
    return viewport.mapFrom(view, point)
