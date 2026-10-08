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

2026-10-04: **checked against what the list already knows, never the disk.**
The menu used to stat the path here, on the interface thread - and greyed out
Open for an email attachment, a file inside a zip, a message and a file on a
catalogued drive, every one of which the open route opens. The rule is
`presenter.opening.usable`, the same one the route lives by: a list passes
its own `missing` and `offline` marks (decided on a worker), and the row.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import QMenu, QWidget
from app.ui.qtsip import open_menu

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
        index_file: Optional[Callable[[], None]] = None,
        pin: Optional[Callable[[], None]] = None,
        similar: Optional[Callable[[], None]] = None,
        explain: Optional[Callable[[], None]] = None,
        same_period: Optional[Callable[[], None]] = None,
        copy: Optional[list[tuple[str, str]]] = None,
        extra: Optional[list[tuple[str, str, Callable[[], None]]]] = None,
        view: Optional[Callable[[], None]] = None,
        row: Any = None,
        missing: bool = False,
        offline: bool = False,
    ) -> None:
        self.open_file = open_file
        #: 2026-10-04: the photo grid's lightbox, now that its "Open" opens the
        #: file as every other list's does.
        self.view = view
        #: The row the menu is for. Read for its offline mark and, by "Copy
        #: path", for a catalogued drive's real path (`workers.copy_path_async`).
        self.row = row
        #: What the list knows and the menu must not stat for: the file has gone
        #: (decided on a worker - `tasks.missing_paths`), or its drive is out.
        self.missing = missing
        self.offline = offline
        self.reveal = reveal
        self.search_inside = search_inside
        self.reindex = reindex
        #: 2026-10-07, the owner: "in the files list i want a option to index
        #: the selected file". Read this one file again, now, whatever the
        #: index already says about it. Offered only for a file that is itself
        #: on disk - an attachment or a zip member is read with what holds it.
        self.index_file = index_file
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
        #: Adoptions section 1: "Why is this here?" in plain words. Offered
        #: only when the caller passes it, which the caller does only while
        #: the `explain_results` switch is on.
        self.explain = explain
        #: Order 0n section 4b: "see everything from this month" - opens the
        #: Life Timeline at the month this file is from. Offered only where a
        #: caller can honour it; a file with no date simply has nothing to open.
        self.same_period = same_period
        #: `(label, text)` pairs to offer alongside "Copy path". For mail:
        #: subject and sender are what people actually want on the clipboard,
        #: and a message's "file name" is a synthetic key nobody would
        #: recognise, let alone want to paste anywhere.
        self.copy = copy or []
        #: `(label, tooltip, callback)` for actions only one list has - the
        #: Code tab's "Ignore this repository" (order 0y §1c). Offered last,
        #: after a separator, because each is about more than this one file.
        self.extra = extra or []


def build_menu(parent: QWidget, path: str, actions: FileActions) -> QMenu:
    """The menu for one file. Always returns a menu with at least one item.

    "Copy path" is always available and never disabled, because it works whether
    or not the file still exists - and when a file has gone missing, its path is
    often exactly what somebody needs in order to work out where it went.
    """
    from app.ui.presenter.opening import usable

    menu = QMenu(parent)
    exists = usable(actions.row if actions.row is not None else path,
                    missing=actions.missing, offline=actions.offline)

    if actions.open_file is not None:
        action = QAction("Open", parent)
        action.setEnabled(exists)
        action.triggered.connect(lambda: actions.open_file())
        menu.addAction(action)

    if actions.view is not None:
        action = QAction("View", parent)
        action.setToolTip("Shows the picture in a window of its own; the arrow keys "
                          "move through the others.")
        action.triggered.connect(lambda: actions.view())
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

    if actions.index_file is not None and _is_a_file_on_disk(path, actions.row):
        action = QAction("Index this file now", parent)
        action.setToolTip(
            "Reads this one file again and brings the index up to date with it, "
            "whatever the index already says. Nothing else is read. A mail "
            "archive is read in full, which can take a long time.")
        action.setEnabled(exists)
        action.triggered.connect(lambda: actions.index_file())
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

    if actions.explain is not None:
        action = QAction("Why is this here?", parent)
        action.setToolTip(
            "Says, in plain words, what put this result on the page - your "
            "words, meaning, how recent it is, whether you have opened it "
            "before. Facts only; it never shows a score.")
        action.triggered.connect(lambda: actions.explain())
        menu.addAction(action)

    if actions.same_period is not None:
        action = QAction("See everything from this month", parent)
        action.setToolTip(
            "Open your timeline at the month this file is from - photos, files and "
            "mail from then, wherever they are kept now.")
        action.triggered.connect(lambda: actions.same_period())
        menu.addAction(action)

    menu.addSeparator()

    copy_path = QAction("Copy path", parent)
    copy_path.triggered.connect(lambda: _copy_path(path, actions.row))
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

    if actions.extra:
        menu.addSeparator()
        for label, tip, callback in actions.extra:
            action = QAction(label, parent)
            action.setToolTip(tip)
            action.triggered.connect(lambda _checked=False, run=callback: run())
            menu.addAction(action)

    if actions.missing and actions.reindex is not None:
        menu.addSeparator()
        action = QAction("File is missing - re-index this folder", parent)
        action.triggered.connect(lambda: actions.reindex())
        menu.addAction(action)

    return menu


def _is_a_file_on_disk(path: str, row: Any) -> bool:
    """Whether `path` is a file a run can be pointed at: not an attachment, not
    a zip member, not a message, not on a catalogued drive. No I/O."""
    from app.ui.attachment_open import opens_from_a_copy

    text = str(path or "")
    if not text or "://" in text or opens_from_a_copy(text):
        return False
    return getattr(row, "volume_id", None) is None


def _copy_path(path: str, row: Any) -> None:
    """The real path: resolved for a file on a catalogued drive (2026-10-04)."""
    if row is not None and getattr(row, "volume_id", None) is not None:
        from app.ui.workers import copy_path_async

        copy_path_async(row)
        return
    _copy(path)


def _copy(text: str) -> None:
    """Plain text to the clipboard; a missing clipboard (headless) is a no-op."""
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
    open_menu(menu, widget.mapToGlobal(point))


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
