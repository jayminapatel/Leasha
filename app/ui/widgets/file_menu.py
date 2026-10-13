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

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QMenu, QWidget

from app.ui.widgets.menus import Item, build, show

__all__ = ["FileActions", "build_menu", "show_for", "copy_paths", "table_pairs", "REVEAL_AT_MOST"]


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
        search_words: Optional[str] = None,
        reindex: Optional[Callable[[], None]] = None,
        index_file: Optional[Callable[[], None]] = None,
        pin: Optional[Callable[[], None]] = None,
        similar: Optional[Callable[[], None]] = None,
        explain: Optional[Callable[[], None]] = None,
        same_period: Optional[Callable[[], None]] = None,
        find_copies: Optional[Callable[[], None]] = None,
        copy: Optional[list[tuple[str, str]]] = None,
        extra: Optional[list[tuple[str, str, Callable[[], None]]]] = None,
        view: Optional[Callable[[], None]] = None,
        default: str = "open",
        selected: Optional[list[tuple[str, Any]]] = None,
        reveal_one: Optional[Callable[[str, Any], None]] = None,
        row: Any = None,
        missing: bool = False,
        offline: bool = False,
    ) -> None:
        self.open_file = open_file
        #: 2026-10-04: the photo grid's lightbox, now that its "Open" opens the
        #: file as every other list's does.
        self.view = view
        #: What a double-click on the list does, so the menu shows that line first
        #: and bold: "open", or "view" where a double-click opens the lightbox.
        self.default = default
        #: UI phase B: `(path, row)` of every selected line, on a list that allows
        #: several. With more than one, Copy path copies them all and Show in folder
        #: shows each through `reveal_one(path, row)`; every other line stays the
        #: clicked row's.
        self.selected = selected
        self.reveal_one = reveal_one
        #: The row the menu is for. Read for its offline mark and, by "Copy
        #: path", for a catalogued drive's real path (`workers.copy_path_async`).
        self.row = row
        #: What the list knows and the menu must not stat for: the file has gone
        #: (decided on a worker - `tasks.missing_paths`), or its drive is out.
        self.missing = missing
        self.offline = offline
        self.reveal = reveal
        self.search_inside = search_inside
        #: What `search_inside` is called when "inside this file" is not what it does: the Code
        #: list's searches the whole repository (the owner, 2026-10-12). None: worded from the row.
        self.search_words = search_words
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
        #: Order 1q D5 (2026-10-11), the owner: "for duplicates there should be a
        #: method to do it manually too". Opens Tidy on this file's copies, with
        #: nothing marked. Offered only for a file that is itself on disk.
        self.find_copies = find_copies
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

    UI phase B (2026-10-13): built by `menus.build`, so every line has its icon,
    the line a double-click runs is first and bold, the keys are shown, and the
    lines fall into the builder's groups. The words are the ones they always were.
    """
    from app.ui.presenter.opening import usable

    exists = usable(actions.row if actions.row is not None else path,
                    missing=actions.missing, offline=actions.offline)
    many = [pair for pair in (actions.selected or ()) if pair[0]]
    several = len(many) > 1
    items: list[Item] = []
    viewing = actions.default == "view" and actions.view is not None

    if actions.open_file is not None:
        items.append(Item("Open", actions.open_file, group="open", enabled=exists,
                          default=not viewing, shortcut="" if viewing else "Enter"))
    if actions.view is not None:
        items.append(Item(
            "View", actions.view, group="open", default=viewing,
            shortcut="Enter" if viewing else "",
            tip="Shows the picture in a window of its own; the arrow keys move through the others."))
    if actions.reveal is not None:
        # Several lines selected: each one's folder, not only the line clicked (UI phase B).
        reveal = actions.reveal
        if several and actions.reveal_one is not None:
            reveal = lambda: _reveal_each(many, actions.reveal_one)       # noqa: E731
        items.append(Item("Show in folder", reveal, group="open", enabled=exists))
    if actions.missing and actions.reindex is not None:
        # In the open group: it is offered *instead of* opening, for a file that has gone.
        items.append(Item("File is missing - re-index this folder", actions.reindex, group="open"))

    if actions.search_inside is not None:
        # The bridge between the two lists: found it by name, now find what is
        # in it. Without this, the filename browser is a dead end.
        items.append(Item(actions.search_words or _search_inside_words(actions.row),
                          actions.search_inside))
    if actions.index_file is not None and _is_a_file_on_disk(path, actions.row):
        items.append(Item(
            "Index this file now", actions.index_file, enabled=exists,
            tip="Reads this one file again and brings the index up to date with it, "
                "whatever the index already says. Nothing else is read. A mail "
                "archive is read in full, which can take a long time."))
    if actions.similar is not None:
        items.append(Item("More like this", actions.similar,
                          tip="Find other results with a similar meaning, using the vector "
                              "already stored for this one."))
    if actions.explain is not None:
        items.append(Item(
            "Why is this here?", actions.explain,
            tip="Says, in plain words, what put this result on the page - your "
                "words, meaning, how recent it is, whether you have opened it "
                "before. Facts only; it never shows a score."))
    if actions.same_period is not None:
        items.append(Item(
            "See everything from this month", actions.same_period,
            tip="Open your timeline at the month this file is from - photos, files and "
                "mail from then, wherever they are kept now."))
    if actions.find_copies is not None and _is_a_file_on_disk(path, actions.row):
        items.append(Item(
            "Find copies of this…", actions.find_copies,
            tip="Open Tidy on this file's copies on this computer - and its similar photos, for a "
                "picture - with nothing marked, so you choose what goes."))
    # Actions only one list has - each is about more than this one file, so last of its group.
    for label, tip, callback in actions.extra:
        items.append(Item(label, callback, tip=tip))

    copy_path = lambda: _copy_path(path, actions.row)                     # noqa: E731
    if several:
        copy_path = lambda: copy_paths(many)                                # noqa: E731
    items.append(Item("Copy path", copy_path, group="copy", shortcut="Ctrl+C"))
    if actions.copy:
        # Given explicitly, so a caller that has better things to offer than a
        # basename can say so. Mail does: subject and sender.
        for label, text in actions.copy:
            items.append(Item(label, lambda value=text: _copy(value), group="copy"))
    else:
        items.append(Item("Copy file name", lambda: _copy(Path(path).name), group="copy"))

    if actions.pin is not None:
        items.append(Item("Pin", actions.pin, group="remember"))

    return build(parent, items)


#: How many folders one "Show in folder" opens for a selection, at most. Each is a
#: window of its own; a thousand selected files across a drive must not open a
#: thousand Explorer windows. Fixed, not a setting: a person who wants more selects
#: fewer at a time. Evidence that would change it: somebody asking for more.
REVEAL_AT_MOST = 10


def _reveal_each(pairs: list, reveal_one: Callable[[str, Any], None]) -> None:
    """Show each selected line in its folder - one window a folder, `REVEAL_AT_MOST` at most."""
    import posixpath

    seen: set[str] = set()
    for path, row in pairs:
        folder = posixpath.dirname(str(path).replace(chr(92), "/")).casefold()
        if folder in seen:
            continue
        seen.add(folder)
        reveal_one(path, row)
        if len(seen) >= REVEAL_AT_MOST:
            return


def copy_paths(pairs: list) -> None:
    """Every selected line's path on the clipboard, one a line - Copy path and Ctrl+C.

    `pairs` is `(path, row)`. A file on a catalogued drive is copied as its real
    path (`workers.copy_paths_async`), as one line's Copy path always was.
    """
    pairs = [(str(path), row) for path, row in pairs if path]
    if not pairs:
        return
    if len(pairs) == 1:
        _copy_path(*pairs[0])
        return
    if any(getattr(row, "volume_id", None) is not None for _path, row in pairs):
        from app.ui.workers import copy_paths_async

        copy_paths_async([row if row is not None else path for path, row in pairs])
        return
    _copy("\n".join(path for path, _row in pairs))


def table_pairs(table: Any, path_of: str = "path") -> list:
    """`(path, row)` for every selected line of a `ResultTable` - what `copy_paths` and
    `FileActions.selected` take. `path_of` names the row's path attribute."""
    return [(str(getattr(row, path_of, "") or ""), row) for row in table.selected_objects()]


def _search_inside_words(row: Any) -> str:
    """"Search inside this message" on a Mail row, where "this file" read wrong (the owner,
    2026-10-12); "Search inside this file" everywhere else."""
    from app.ui.presenter.rows import MailRow

    return "Search inside this message" if isinstance(row, MailRow) else "Search inside this file"


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
    """Pop the menu at the pointer. The one line a view needs to call.

    `point` is the `customContextMenuRequested` point as Qt gives it - in the
    viewport's coordinates on a list - and `menus.show` places it by the one
    rule (`menus.global_point`).
    """
    show(build_menu(widget, path, actions), widget, point)
