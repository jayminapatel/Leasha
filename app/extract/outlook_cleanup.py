r"""Copies of a message inside one mailbox, moved to that mailbox's Deleted Items.

Layer: L2 (through Outlook)

Order 1l (2026-10-11), the owner: "yes write the order for the outlook clean-up
and execute". Order 1j took the copies out of the index; the `.pst` still holds
both. **This is the one place Leasha writes to a mail archive**, and only when
the owner presses the button and says yes to the count. Nothing is deleted
outright: a copy is *moved* to the same mailbox's Deleted Items, where Outlook
can put it back.

* **D1** Read and moved through Outlook (classic), as `email_pst.Win32ComSession`
  reads: an archive Outlook does not have open is attached for the job and
  detached after; one it already had is left open.
* **D2** A copy is the same subject, sender, sent time, body text **and
  attachment names** as another item in the same mailbox.
* **D3** The first found stays - folders in Outlook's order, items by received
  time. Deleted Items itself is neither counted nor moved into twice.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

__all__ = ["Copy", "CleanupPlan", "find_copies", "move_copies", "plan_lines", "DELETED_ITEMS"]

#: `OlDefaultFolders.olFolderDeletedItems`.
DELETED_ITEMS = 3

#: How many examples the dry run and the question name.
EXAMPLES = 5


@dataclass(frozen=True)
class Copy:
    """One item to move, and the one it is a copy of."""

    entry_id: str
    folder: str
    subject: str
    sent_at: Optional[int]
    kept_entry_id: str
    kept_folder: str


@dataclass
class CleanupPlan:
    archive: str
    store_id: str = ""
    copies: list[Copy] = field(default_factory=list)
    #: How many items were looked at (Deleted Items not included).
    examined: int = 0

    @property
    def count(self) -> int:
        return len(self.copies)


def _session(factory: Optional[Callable[[], Any]]) -> Any:
    if factory is not None:
        return factory()
    from app.extract.email_pst import Win32ComSession

    return Win32ComSession()


def _store_for(session: Any, path: Path) -> Any:
    """The archive's store, attached first if Outlook does not have it."""
    session.attach(path)
    target = str(path).lower()
    for store in session.stores():
        if (store.file_path or "").lower() == target:
            return store
    from app.core.errors import AppErrorException, make_error

    raise AppErrorException(make_error(
        "ERR_FILE_CORRUPT", "extract.outlook_cleanup", path=str(path),
        details="Outlook opened the archive but did not list it among its stores"))


def _key(item: Any) -> tuple:
    """D2: subject, sender, sent time, the body's digest and the attachment names."""
    body = " ".join(str(item.body or "").split())
    names = tuple(sorted(str(getattr(a, "filename", "") or "") for a in item.attachments))
    return (item.subject, item.sender, item.sent_at,
            hashlib.sha1(body.encode("utf-8", "replace")).hexdigest(), names)


#: The columns asked of a folder's table (`Folder.GetTable`): what a candidate is
#: grouped by before anything is opened.
_TABLE_COLUMNS = ("EntryID", "Subject", "SenderName", "SentOn", "Size")


def _folders(folder: Any, skip_id: str) -> Iterator[Any]:
    """Every folder, in Outlook's order, leaving out Deleted Items."""
    if str(getattr(folder._folder, "EntryID", "") or "") == skip_id:
        return
    yield folder
    for child in folder.folders():
        yield from _folders(child, skip_id)


def _table_rows(folder: Any) -> Optional[list[tuple]]:
    """`(entry_id, subject, sender name, sent on, size)` for every item, from the
    folder's table - one call per row, nothing opened. None when the folder has
    no table (an old store, or a test's fake), and the slow walk is used."""
    try:
        table = folder._folder.GetTable()
        columns = table.Columns
        columns.RemoveAll()
        for name in _TABLE_COLUMNS:
            columns.Add(name)
    except Exception:                              # noqa: BLE001 - see the docstring
        return None
    rows = []
    # `GetArray` hands back up to this many rows in one call. Row by row
    # (`GetNextRow().GetValues()`) was two calls each: 345 rows took 25 s on
    # the owner's laptop while an index run was reading other archives.
    while not table.EndOfTable:
        block = table.GetArray(5000)
        if not block:
            break
        rows.extend(tuple(row) for row in block)
    return rows


def _candidates(store: Any, skip_id: str) -> Optional[tuple[int, list[list[tuple[str, str]]]]]:
    """Groups of `(entry_id, folder)` that agree on subject, sender, time and size,
    from the tables alone, and how many items were looked at. None if any folder
    has no table."""
    seen = 0
    groups: dict[tuple, list[tuple[str, str]]] = {}
    for folder in _folders(store.root(), skip_id):
        rows = _table_rows(folder)
        if rows is None:
            return None
        for entry_id, subject, sender, sent_on, size in rows:
            if not entry_id:
                continue
            seen += 1
            groups.setdefault((subject, sender, str(sent_on), size), []).append(
                (str(entry_id), folder.path))
    return seen, [members for members in groups.values() if len(members) > 1]


def _walk(folder: Any, skip_id: str) -> Iterator[tuple[str, Any]]:
    """Every item, folder by folder in Outlook's order, leaving out Deleted Items."""
    if str(getattr(folder._folder, "EntryID", "") or "") == skip_id:
        return
    for item in folder.items():
        yield folder.path, item
    for child in folder.folders():
        yield from _walk(child, skip_id)


def find_copies(path: Any, *, session_factory: Optional[Callable[[], Any]] = None) -> CleanupPlan:
    """1a: the copies in one mailbox by D2. **Reads only.**"""
    archive = Path(path)
    plan = CleanupPlan(archive=str(archive))
    session = _session(session_factory)
    try:
        store = _store_for(session, archive)
        com = store._store
        plan.store_id = str(getattr(com, "StoreID", "") or "")
        try:
            deleted_id = str(com.GetDefaultFolder(DELETED_ITEMS).EntryID or "")
        except Exception:                          # noqa: BLE001 - an archive without one
            deleted_id = ""
        first: dict[tuple, tuple[str, str]] = {}
        quick = _candidates(store, deleted_id)
        if quick is not None:
            # The tables named the few items that could be copies; only those are
            # opened and compared in full (D2), in the order the tables gave them.
            from app.extract.email_pst import _com_item

            plan.examined, groups = quick
            namespace = session._namespace
            for members in groups:
                for entry_id, folder_path in members:
                    try:
                        item = _com_item(namespace.GetItemFromID(entry_id, plan.store_id))
                    except Exception:              # noqa: BLE001 - one item, never the rest
                        item = None
                    if item is None or item.is_empty:
                        continue
                    key = _key(item)
                    if key not in first:
                        first[key] = (item.entry_id, folder_path)
                        continue
                    kept_id, kept_folder = first[key]
                    plan.copies.append(Copy(item.entry_id, folder_path, item.subject,
                                            item.sent_at, kept_id, kept_folder))
            return plan
        for folder_path, item in _walk(store.root(), deleted_id):
            if item.is_empty or not item.entry_id:
                continue
            plan.examined += 1
            key = _key(item)
            if key not in first:
                first[key] = (item.entry_id, folder_path)
                continue
            kept_id, kept_folder = first[key]
            plan.copies.append(Copy(item.entry_id, folder_path, item.subject, item.sent_at,
                                    kept_id, kept_folder))
    finally:
        session.close()
    return plan


def move_copies(plan: CleanupPlan, *,
                session_factory: Optional[Callable[[], Any]] = None) -> tuple[int, int]:
    """1b: move each copy to the mailbox's Deleted Items. Returns `(moved, left)`.

    An item that is gone, or whose subject or time is no longer what was counted,
    is left where it is: the owner said yes to *those* items.
    """
    from app.extract.email_pst import _com_item

    if not plan.copies:
        return 0, 0
    session = _session(session_factory)
    moved = left = 0
    try:
        store = _store_for(session, Path(plan.archive))
        deleted = store._store.GetDefaultFolder(DELETED_ITEMS)
        namespace = session._namespace
        for copy in plan.copies:
            try:
                entry = namespace.GetItemFromID(copy.entry_id, plan.store_id)
                now = _com_item(entry)
                if now is None or now.subject != copy.subject or now.sent_at != copy.sent_at:
                    left += 1
                    continue
                entry.Move(deleted)
                moved += 1
            except Exception:                      # noqa: BLE001 - one item, never the rest
                left += 1
    finally:
        session.close()
    return moved, left


def plan_lines(plan: CleanupPlan) -> list[str]:
    """The count and examples, in words."""
    name = Path(plan.archive).name
    if not plan.copies:
        return [f"{name}: no message is in it twice ({plan.examined:,} looked at). Nothing to move."]
    lines = [f"{name}: {plan.count:,} copies of messages already in it, of {plan.examined:,} looked at.",
             "For example (the copy, then where the kept one is):"]
    for copy in plan.copies[:EXAMPLES]:
        lines.append(f"  \"{(copy.subject or '(no subject)')[:60]}\" in {copy.folder or 'the top'}"
                     f" - kept in {copy.kept_folder or 'the top'}")
    lines.append("They would be moved to this mailbox's Deleted Items, where Outlook can restore them.")
    return lines
