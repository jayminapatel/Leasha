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

import os
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
    "reconcile_moves",
    "ReconcileResult",
    "identify_source",
]

_log = logger.bind(component="index.offline_media")

#: Batched the same way `Pipeline._delete_in_batches` batches an ordinary
#: prune - one LanceDB dataset version and one SQLite transaction per few
#: thousand rows, not per file. See that method's docstring for why.
DELETE_BATCH = 2_000


def identify_source(path: Path) -> Optional[tuple[str, dict[str, Any]]]:
    r"""What kind of Offline Media source `path` is, and its identity fields
    for `SqliteStore.upsert_volume` - or None if neither a drive nor a
    network share can be identified there.

    **UNC first, then a mapped letter, then a real Windows volume.** A UNC
    path (`\\server\share\...`) needs no resolution at all - it already is
    its own identity (1a, order 202626270514). A mapped drive letter is
    resolved to the UNC behind it and **the letter is discarded immediately**
    - 1a's "at add-time and never stored" is upheld by this being the only
    place the letter is ever read. Only once both network possibilities are
    ruled out does this try `identify_root` for an ordinary removable drive.
    """
    from app.core.volumes_win import identify_root, normalise_unc, resolve_unc

    unc = normalise_unc(path)
    if unc:
        return "network", {"identity_key": unc}

    drive = str(path.drive).rstrip(":")
    if drive:
        mapped = resolve_unc(drive)
        if mapped:
            unc = normalise_unc(Path(mapped)) or mapped
            return "network", {"identity_key": unc}

    identity = identify_root(path)
    if identity is not None and identity.volume_guid:
        return "drive", {
            "identity_key": identity.volume_guid,
            "volume_guid": identity.volume_guid,
            "fs_label": identity.fs_label,
        }
    return None


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
    from app.core.volumes_win import find_drive_by_guid, probe_unc_reachable

    online: dict[int, Path] = {}
    for row in store.list_volumes():
        kind = row.get("kind")
        volume_id = int(row["id"])
        if kind == "drive":
            guid = row.get("volume_guid")
            if not guid:
                continue
            root = find_drive_by_guid(guid)
            if root is not None:
                online[volume_id] = root
        elif kind == "network":
            # **A UNC path needs no letter resolution at all** - unlike a
            # drive, its identity *is* a usable filesystem root (1a: "the
            # letter's network twin"). Only reachability is in question, and
            # 1b/1c say an unconfirmed answer must read as offline, never as
            # a hang or a credential prompt.
            unc = row.get("identity_key")
            if unc and probe_unc_reachable(unc):
                online[volume_id] = Path(unc)
        # kind in (cloud, phone, archived): not yet resolvable - order
        # 202626270514's later sections. Correctly absent from `online`.
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
        if row.get("kind") in ("drive", "network"):
            status = "ONLINE" if volume_id in online else "OFFLINE"
        else:
            # Not yet resolvable (cloud/phone/archived) - last known status
            # stands rather than being overwritten with a guess.
            status = str(row.get("status") or "OFFLINE")
        statuses[volume_id] = status
        store.set_volume_status(volume_id, status)
    return statuses


class ReconcileResult:
    """What one `reconcile_moves` pass found. `moved` is what 1e is for;
    `hashed` is the honest cost - only candidates that could plausibly be a
    move are ever hashed, never the whole volume."""

    def __init__(self, moved: int = 0, hashed: int = 0) -> None:
        self.moved = moved
        self.hashed = hashed

    def as_dict(self) -> dict[str, int]:
        return {"moved": self.moved, "hashed": self.hashed}


def reconcile_moves(store: Any, volume_id: int, mount_root: Path) -> ReconcileResult:
    r"""1e: find files that moved on a reorganised drive, and repair their
    rows **before** the ordinary pipeline walk reaches them.

    **Must run before `Pipeline.run()` on the same root, never after.** Once
    a row's `relative_path`/`path`/`mtime_ns`/`size_bytes` are updated to
    where the file is *now*, the walk's ordinary incremental check - same
    path, same size, same mtime - sees it as unchanged and never opens it.
    That is 1e's own acceptance line: *"extraction count ~ 0"*. Reconciling
    afterwards would be too late - the walk would already have logged the
    old location as missing and the new one as a fresh file, and paid for a
    full re-extraction before anything here ever ran.

    A plain `os.walk` stat pass, not the full `app.index.walker.walk()` -
    this only needs size and mtime for every file on the volume, not
    extension routing, exclusions or cloud-placeholder handling, and paying
    for those twice on every rescan would be waste with no benefit.

    Hashing is the expensive part and is paid only where it can possibly pay
    off: a "new" relative path is only ever hashed against "missing" rows of
    the **same size** - `content_hash` is `blake2b` over the whole file
    (`app.index.walker.content_hash`), and two different files sharing a
    size is common; two different files sharing a size AND a blake2b digest
    is not a case worth guarding against here.
    """
    from app.index.walker import content_hash as _hash_file

    known = {
        record.relative_path: record
        for record in store.iter_files(volume_id=volume_id, source_kind="file")
        if record.relative_path
    }

    current: dict[str, tuple[int, int]] = {}
    for dirpath, _dirs, filenames in os.walk(mount_root):
        for name in filenames:
            full = Path(dirpath) / name
            try:
                stat = full.stat()
            except OSError:
                continue
            rel = str(full.relative_to(mount_root)).replace("\\", "/")
            current[rel] = (stat.st_size, stat.st_mtime_ns)

    missing = [rel for rel in known if rel not in current]
    new = [rel for rel in current if rel not in known]
    if not missing or not new:
        return ReconcileResult()

    missing_by_size: dict[int, list[str]] = {}
    for rel in missing:
        missing_by_size.setdefault(known[rel].size_bytes, []).append(rel)

    moved = 0
    hashed = 0
    for rel in new:
        size, mtime_ns = current[rel]
        candidates = missing_by_size.get(size)
        if not candidates:
            continue
        try:
            new_digest = _hash_file(mount_root / rel)
        except OSError:
            continue
        hashed += 1
        for old_rel in list(candidates):
            if known[old_rel].content_hash == new_digest:
                if store.move_volume_file(volume_id, old_rel, rel,
                                          size_bytes=size, mtime_ns=mtime_ns):
                    candidates.remove(old_rel)
                    moved += 1
                break
    if moved:
        _log.info("volume {} rescan: {} file(s) moved on disk, repaired "
                 "without re-extraction ({} hashed to confirm)",
                 volume_id, moved, hashed)
    return ReconcileResult(moved=moved, hashed=hashed)



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