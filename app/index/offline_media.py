r"""Offline Media orchestration: connectivity, path resolution, deletion.

Layer: L3

Orders 202626270513 (drives) and 202626270514 (network, cloud, tape/archived)
share this module for the same reason they share `volumes`: a later kind must
plug into what the first one built, not force a second design onto it.

**The one rule everything here exists to keep**: a drive letter, or a mapped
network letter, is never trusted as identity, and is resolved fresh from the
current mount point every single time a real path is needed. Nothing in this
module caches a resolved path across calls.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Optional

from app.core.logging import logger

__all__ = [
    "connected_volumes",
    "resolve_file_path",
    "refresh_volume_statuses",
    "delete_volume",
]

_log = logger.bind(component="index.offline_media")

#: Batched the same way `Pipeline._delete_in_batches` batches an ordinary
#: prune - one LanceDB dataset version and one SQLite transaction per few
#: thousand rows, not per file. See that method's docstring for why.
DELETE_BATCH = 2_000


def connected_volumes(store: Any) -> dict[int, Path]:
    r"""Every catalogued volume that is reachable **right now**, with its
    current mount root.

    **Resolved fresh on every call, from the live machine - never cached
    beyond the caller's own run.** A drive unplugged and replugged under a
    different letter, or not plugged in at all, is correctly answered
    without anything having to notice the change happened; see
    `app.core.volumes_win.find_drive_by_guid`.

    Only `kind="drive"` has a live check today - order 202626270513's scope.
    A `network`/`cloud`/`phone`/`archived` source (202626270514) is not
    resolvable by this function yet and is correctly treated as **not
    connected** by every caller, which is the safe default: never claim a
    source is online it cannot actually confirm, because "online" is what
    lets `_prune_missing` treat a missing file as really gone.
    """
    if sys.platform != "win32":
        return {}
    from app.core.volumes_win import find_drive_by_guid

    online: dict[int, Path] = {}
    for row in store.list_volumes():
        if row.get("kind") != "drive":
            continue
        guid = row.get("volume_guid")
        if not guid:
            continue
        root = find_drive_by_guid(guid)
        if root is not None:
            online[int(row["id"])] = root
    return online


def resolve_file_path(store: Any, record: Any,
                      connected: Optional[dict[int, Path]] = None) -> Optional[Path]:
    r"""The real, openable path for one file row, or None if it cannot be
    reached right now.

    **1b: "resolution to a real path happens at the last moment via the
    current mount point, every time - open, reveal, rescan."** An ordinary
    file (`record.volume_id is None`) is simply `Path(record.path)` - a fixed
    internal disk keeps absolute paths untouched, per the owner's model.

    `connected` lets a caller resolving many rows (a results list, a bulk
    reveal) pass one `connected_volumes()` call in rather than paying a
    Windows volume enumeration per row.
    """
    if record.volume_id is None:
        return Path(record.path)
    if not record.relative_path:
        return None
    online = connected if connected is not None else connected_volumes(store)
    root = online.get(record.volume_id)
    if root is None:
        return None
    return root / record.relative_path


def refresh_volume_statuses(store: Any) -> dict[int, str]:
    r"""Update every volume's last-known `status`, and return it.

    **2a: "Status checked passively on panel refresh - no device watcher, no
    events."** This is the one place that happens: called when the Offline
    Media tab (or its CLI equivalent, `app.cli offline-media list`) is
    opened, never on a timer and never in response to a Windows device
    notification. `LOCKED` (2d, BitLocker) is not distinguished from
    `OFFLINE` here - a locked volume that cannot be identified looks
    identical to one that is not plugged in at all, and this function does
    not decrypt anything to find out; see the dated note in the work order
    for what that means for 2d.
    """
    online = connected_volumes(store)
    statuses: dict[int, str] = {}
    for row in store.list_volumes():
        volume_id = int(row["id"])
        if row.get("kind") == "drive":
            status = "ONLINE" if volume_id in online else "OFFLINE"
        else:
            # Not yet resolvable (network/cloud/phone/archived) - last known
            # status stands rather than being overwritten with a guess.
            status = str(row.get("status") or "OFFLINE")
        statuses[volume_id] = status
        store.set_volume_status(volume_id, status)
    return statuses


def delete_volume(store: Any, vectors: Any, volume_id: int) -> dict[str, Any]:
    r"""2c's one deliberate deletion. Full cascade; the drive itself is never
    touched - this only ever removes rows from the index.

    Returns `{"files": N, "name": ..., "deleted": True}` so the caller can
    state the count in the confirmation the order requires, or
    `{"deleted": False}` if the volume was already gone.

    The same order as `Pipeline._delete_in_batches`: vectors first, then
    SQLite. A crash between them leaves vectors for rows that still exist -
    harmless, re-deleted next time - where the reverse leaves orphaned
    vectors with no file row to lead back to them.
    """
    record = store.get_volume(volume_id)
    if record is None:
        return {"deleted": False, "files": 0, "name": None}

    ids = store.volume_file_ids(volume_id)
    for start in range(0, len(ids), DELETE_BATCH):
        batch = ids[start:start + DELETE_BATCH]
        if vectors is not None:
            vectors.delete_by_file_ids(batch)
        with store.batch():
            for file_id in batch:
                store.delete_file(file_id)
    store.delete_volume_row(volume_id)
    _log.info("deleted Offline Media source {!r} ({} file(s)); the drive "
             "itself was not touched", record.name, len(ids))
    return {"deleted": True, "files": len(ids), "name": record.name}