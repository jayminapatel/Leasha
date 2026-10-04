r"""What one page of search results needs besides the passages: who sent a
message, and whether a file can still be opened. **Worker only.**

Layer: L4. No Qt, nothing from `app.ui`.

2026-10-04, the owner: "where ever possible the same code should run for
functions so they are all consistent and standard". These lived in
`app/ui/tasks.py`, so the window's results list had them and the command line
and the MCP server could not - the MCP server looked the mail up one result at
a time instead. They moved here unchanged; `tasks.py` keeps its worker-body
names (`test_ui_never_blocks` reads them there) and calls these.

Every function here reads the store or the disk, so none of them may run on
the interface thread. Each is one batched read for the whole page, never one
per row, and none raises: a missing subtitle or mark is a cosmetic loss, never
a failed search.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from app.core.logging import logger
from app.core.row_facts import ATTACHMENT_MARKER, attachment_of

__all__ = [
    "ATTACHMENT_MARKER",
    "MISSING",
    "OFFLINE",
    "OK",
    "attachment_parent_path",
    "mail_details",
    "missing_paths",
    "offline_volumes",
    "status_marks",
]

_log = logger.bind(component="search.marks")

#: A result's file, as the results list marks it: there, gone from disk, or on
#: a catalogued drive that is not plugged in.
OK = "ok"
MISSING = "missing"
OFFLINE = "offline"

#: §5b. The path convention `archive.attachment_key` writes:
#: `f"{message_key}/attachments/{name}"`. Read back rather than carried as a
#: column, because no schema holds the link - the file's own `path` already
#: says everything needed. 2026-10-04, code review: the marker and its parser
#: are `row_facts`'s, not a third copy kept here.
#: (`ATTACHMENT_MARKER` is imported at the top.)


def attachment_parent_path(path: str) -> str:
    """The message this attachment belongs to, or `""` if `path` is not one.

    **Only the PST-via-Outlook attachment convention produces this shape.**
    A standalone `.eml`/`.msg` or an mbox message never separately indexes
    its attachments - only their *names*, inside the message's own text and
    `has_attach` - so those never reach here at all; `/has attachment` still
    finds the message, just never gets a row of its own for what was
    attached to it.
    """
    return attachment_of(path)[0]


def missing_paths(paths: Any) -> set[str]:
    """Which of these no longer exist on disk.

    `Path.exists()` is a filesystem stat: microseconds on a warm local disk,
    *seconds* on a network share or a drive that has spun down. Done once per
    result set, off the interface thread. Never raises: a disconnected drive
    means "cannot open it", not a crash, and a result whose file has vanished
    is a real finding that must still be shown.
    """
    missing = set()
    for path in paths or ():
        text = str(path or "")
        # A message lives inside a .pst and has no file of its own; statting a
        # synthetic key would report every message as missing.
        # 2026-10-04: any `scheme://` key (a catalogued drive's too), not only
        # `pst://` - none is a path the disk can answer for.
        if not text or "://" in text:
            continue
        try:
            if not Path(text).exists() and not _inside_a_file(Path(text)):
                missing.add(text)
        except OSError:
            continue
    return missing


def _inside_a_file(path: Path) -> bool:
    """Whether `path` names something inside a file that is there - a member of
    a zip (`D:\\a.zip/q3/report.docx`) or a message in an mbox. 2026-10-04: each
    was marked missing, so the menu offered "re-index" for a file that opens.
    Only reached for a path that does not exist, so a found file costs nothing."""
    for parent in path.parents:
        if parent.exists():
            return parent.is_file()
    return False


def offline_volumes(store: Any, results: Any) -> dict[int, dict]:
    r"""`{file_id: {"name", "scanned_at"}}` for the rows on a catalogued
    Offline Media volume that is not connected right now.

    **Online rows are absent entirely**: they open normally, and a mark on
    every row of a drive that is plugged in would be noise. `connected_volumes`
    is a live Windows volume check, made once for the page; each volume record
    is read once however many rows it holds. Never raises.
    """
    rows = [row for row in (results or ()) if getattr(row, "volume_id", None) is not None]
    if not rows:
        return {}
    from app.index.offline_media import connected_volumes

    try:
        online = connected_volumes(store)
    except Exception:                            # noqa: BLE001 - a decoration, not the search
        online = {}

    marks: dict[int, dict] = {}
    volumes_seen: dict[int, Any] = {}
    for row in rows:
        volume_id = int(row.volume_id)
        if volume_id in online:
            continue
        if volume_id not in volumes_seen:
            try:
                volumes_seen[volume_id] = store.get_volume(volume_id)
            except Exception:                     # noqa: BLE001
                volumes_seen[volume_id] = None
        record = volumes_seen[volume_id]
        if record is None:
            continue
        marks[int(getattr(row, "file_id", 0))] = {
            "name": record.name,
            "scanned_at": int(getattr(record, "last_scanned_at", 0) or 0),
        }
    return marks


def status_marks(store: Any, results: Any) -> dict[int, str]:
    r"""`{file_id: OK | MISSING | OFFLINE}` for one page of results.

    The same two checks the results list draws its marks from
    (`tasks.decorate_results`): a stat for a row on an ordinary drive, the
    volume check for one on a catalogued drive. For a caller that needs the
    marks as data - the command line and the MCP server, 2026-10-04.
    """
    results = list(results or ())
    offline = offline_volumes(store, results)
    gone = missing_paths(getattr(row, "path", "") for row in results
                         if getattr(row, "volume_id", None) is None)
    out: dict[int, str] = {}
    for row in results:
        file_id = int(getattr(row, "file_id", 0) or 0)
        if file_id in offline:
            out[file_id] = OFFLINE
        elif str(getattr(row, "path", "") or "") in gone:
            out[file_id] = MISSING
        else:
            out[file_id] = OK
    return out


def parent_messages(store: Any, items: Any) -> Optional[dict[int, dict]]:
    """`{attachment's file_id: its message's messages row}` for one page, in
    one statement, or None when the store cannot answer in one (a test fake),
    so the caller falls back to its per-message lookup. 2026-10-04."""
    lookup = getattr(store, "messages_by_path", None)
    if not callable(lookup):
        return None
    wanted = {int(file_id or 0): attachment_parent_path(path) for file_id, path in items}
    wanted = {file_id: parent for file_id, parent in wanted.items() if parent}
    if not wanted:
        return {}
    found = lookup(sorted(set(wanted.values())))
    return {file_id: found[parent] for file_id, parent in wanted.items() if parent in found}


def mail_details(store: Any, results: Any) -> dict:
    """Subjects and senders for the messages on one page of results.

    **One query for the page, never one per row.** At the fetch depth grouping
    needs, a per-row lookup is fifty queries per keystroke - the shape of
    slowness that gets blamed on the search itself.

    **§5b's exception, and it is a real one.** An attachment's own file_id
    has no row in `messages` - it is not itself a message - so its *parent's*
    row is what supplies "its message is the context" (§5b). The parent is
    found by path (`attachment_parent_path`), which costs one indexed
    `get_file` lookup per *distinct attachment* on the page - never per row,
    and zero when a page holds no attachments at all, which is nearly every
    page.

    Never raises. A missing subtitle is a cosmetic loss; failing the search that
    produced it is not, and a store that has been closed underneath a worker is
    a normal condition during shutdown rather than an error.
    """
    if store is None or not hasattr(store, "messages_for"):
        return {}
    try:
        results = list(results or ())
        file_ids = [getattr(r, "file_id", 0) for r in results]

        parents = parent_messages(store, [(getattr(r, "file_id", 0), getattr(r, "path", ""))
                                          for r in results])
        if parents is not None:
            # 2026-10-04: one statement for the page's attachments
            # (`store.messages_by_path`), not one `get_file` per parent.
            details = dict(store.messages_for(file_ids))
            for file_id, parent in parents.items():
                details[file_id] = {**parent, "attachment_of": parent.get("file_id")}
            return details

        parent_id_of: dict[int, int] = {}
        if hasattr(store, "get_file"):
            # **Keyed by path, not by row.** Several attachments can share one
            # parent message - a reply with the same two files re-attached is
            # the ordinary case - and resolving each would be exactly the
            # per-row lookup this function's own docstring exists to avoid.
            resolved: dict[str, Optional[int]] = {}
            for result in results:
                file_id = getattr(result, "file_id", 0)
                parent_path = attachment_parent_path(getattr(result, "path", ""))
                if not parent_path:
                    continue
                if parent_path not in resolved:
                    try:
                        record = store.get_file(parent_path)
                    except Exception:              # noqa: BLE001 - a subtitle, not the search
                        record = None
                    resolved[parent_path] = record.id if record is not None else None
                parent_id = resolved[parent_path]
                if parent_id is not None:
                    parent_id_of[file_id] = parent_id

        wanted = list(dict.fromkeys([*file_ids, *parent_id_of.values()]))
        details = dict(store.messages_for(wanted))
        for file_id, parent_id in parent_id_of.items():
            parent_detail = details.get(parent_id)
            if parent_detail:
                # Marked so `_build_group` draws this as an attachment whose
                # *parent's* detail this is, never as a message in its own
                # right - the two share every other key.
                details[file_id] = {**parent_detail, "attachment_of": parent_id}
        return details
    except Exception as exc:                     # noqa: BLE001 - see docstring
        _log.debug("no mail details for this page: {}", exc)
        return {}
