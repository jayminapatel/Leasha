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
    "suggest_renamed_source",
    "check_renamed_source",
    "archive_volume",
    "volume_location_label",
    "find_volume",
    "run_scoped_pipeline",
    "scan_new_source",
    "rescan_source",
    "remember_hardware_serial",
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


def is_folder_not_a_drive(path: Path) -> bool:
    r"""True when `path` is a real folder that `identify_source` could not
    identify - the case of choosing `D:\Projects` rather than `D:\`. Only a
    drive root or a share has a stable identity (a letter is never stored), so
    a folder is refused; the caller says so in plain words rather than as a
    configuration fault. False for a path that does not exist (an unplugged
    drive, an unreachable share), which keeps its own message."""
    try:
        return path.is_dir()
    except OSError:
        return False


#: 1a's structure-match offer: how similar a new root's top-level names must
#: be to a catalogued-but-different source's own last-scan fingerprint before
#: it is worth suggesting at all. High enough that two shares which merely
#: both happen to contain "2019" and "Invoices" - unremarkable - never fire;
#: a renamed server's own share, whose whole top level survived the move,
#: clears it by a wide margin.
STRUCTURE_MATCH_MIN_OVERLAP = 0.6
STRUCTURE_MATCH_MIN_SHARED = 2


def suggest_renamed_source(store: Any, kind: str, new_root: Path,
                           exclude_identity_key: Optional[str] = None) -> Any:
    r"""1a: "renamed server = new source, softened by structure-match offer
    ('is this *Old NAS* at a new address?' - assist, never assume)."

    A **shallow, name-only** comparison: the top-level entries at `new_root`
    right now, against `store.volume_top_level_names` for every catalogued
    source of the same `kind` - one `os.scandir` and one already-cheap SQL
    query, never a walk and never a byte of content read. Returns the
    best-matching volume row (a `dict`, from `store.list_volumes()`'s own
    shape) if the overlap clears `STRUCTURE_MATCH_MIN_OVERLAP`, or None.

    **This only ever suggests - nothing here writes to the database.** The
    caller offers the suggestion (today, `app.cli offline_media`'s own
    notice plus `--same-as`; the Offline Media tab's interactive dialog is
    202626270513 §2's, not built - see the work order's dated note) and a
    person decides, via `SqliteStore.rename_volume_identity`. Never assumed
    automatically, because two unrelated shares sharing a couple of common
    folder names ("2019", "Invoices") is not unusual.
    """
    try:
        entries = frozenset(entry.name for entry in os.scandir(new_root))
    except OSError:
        return None
    if len(entries) < STRUCTURE_MATCH_MIN_SHARED:
        return None

    best = None
    best_overlap = 0.0
    for row in store.list_volumes():
        if row.get("kind") != kind:
            continue
        if exclude_identity_key and row.get("identity_key") == exclude_identity_key:
            continue
        volume_id = int(row["id"])
        known = store.volume_top_level_names(volume_id)
        if not known:
            continue
        shared = entries & known
        if len(shared) < STRUCTURE_MATCH_MIN_SHARED:
            continue
        union = entries | known
        overlap = len(shared) / len(union) if union else 0.0
        if overlap >= STRUCTURE_MATCH_MIN_OVERLAP and overlap > best_overlap:
            best_overlap = overlap
            best = row
    return best


def check_renamed_source(store: Any, root: Path) -> Any:
    r"""1a's offer, from a folder alone - the Offline Media tab's own worker
    step before a first Scan. `identify_source` plus `suggest_renamed_
    source`, with the one guard the CLI's inline version already applies:
    an identity already catalogued gets no suggestion at all, because that
    is an ordinary rescan, not a possible rename. Never raises - a Scan a
    person pressed the button for must not be blocked by this check
    failing; `None` (no suggestion) is what a genuine failure and a genuine
    "nothing matches" both look like to the caller, and both mean "catalogue
    as new" is the right next step.
    """
    try:
        found = identify_source(root)
        if found is None:
            return None
        kind, fields = found
        if store.get_volume_by_identity(fields["identity_key"]) is not None:
            return None
        return suggest_renamed_source(store, kind, root,
                                      exclude_identity_key=fields["identity_key"])
    except Exception as exc:                      # noqa: BLE001 - see docstring
        _log.debug("could not check for a renamed source at {}: {}", root, exc)
        return None


def volume_location_label(record: Any) -> str:
    r"""3a/3b-1's results decoration text for one source - "on **<name>**
    (offline, scanned <date>)" for an ordinary offline volume, "on tape
    **<name>** (archived <month year>)" for one 3b-1 detached.

    A pure formatting function, deliberately free of any store or search
    dependency, so it can be reused wherever a source needs to say what it
    is without duplicating the wording - and unit-testable on its own before
    any results surface actually calls it. **Not yet wired into search
    results display**: 202626270513 §3's own decoration path (the row that
    reads "on <name> (offline, scanned <date>)") is itself unbuilt - see
    that order's §3a, still unchecked - so there is nothing here to ride
    yet; this exists so that wiring, whenever it happens, does not also have
    to invent the archived-source wording from scratch.
    """
    name = str(getattr(record, "name", "") or "")
    kind = str(getattr(record, "kind", "") or "")
    if kind == "archived":
        scanned = getattr(record, "last_scanned_at", None)
        when = (time.strftime("%b %Y", time.localtime(int(scanned)))
                if scanned else "date unknown")
        return f"on tape {name} (archived {when})"
    status = str(getattr(record, "status", "") or "OFFLINE")
    if status == "ONLINE":
        return f"on {name}"
    scanned = getattr(record, "last_scanned_at", None)
    when = (time.strftime("%Y-%m-%d", time.localtime(int(scanned)))
            if scanned else "date unknown")
    return f"on {name} (offline, scanned {when})"


def archive_volume(store: Any, volume_id: int, location_note: str) -> dict[str, Any]:
    r"""3b-1: "Mark as archived" - the scan-before-archive workflow. Detaches
    a catalogued source into kind='archived': a name and this free-text
    location, nothing more. Rescan stops meaning anything from here -
    `connected_volumes` never attempts an archived source, the same safe
    default it already applies to kind='cloud'/'phone'; Browse and Delete
    both keep working, because `SqliteStore.archive_volume` touches only
    `kind`/`status`/`location_note` and every `files` row is untouched.

    Generalises past tape, per the order's own wording: DVDs, a destroyed
    drive, media handed to a third party - anything for which the catalogue
    is now the only surviving record of what was once there.
    """
    record = store.get_volume(volume_id)
    if record is None:
        return {"archived": False, "name": None}
    ok = store.archive_volume(volume_id, location_note)
    if ok:
        _log.info("archived Offline Media source {!r}: {}", record.name, location_note)
    return {"archived": ok, "name": record.name, "location_note": location_note}


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
    # 2026-10-05, order `offline-drives-on-a-mac`: this said "not Windows, so
    # nothing is connected", which made every source read as unplugged on a
    # Mac for ever - Rescan was never offered. A Mac answers now; any other
    # system still has no way to tell, and says nothing is connected.
    if sys.platform not in ("win32", "darwin"):
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
            # **Never a BitLocker probe here.** This function is resolved
            # fresh on every call and sits under `resolve_file_path` - the
            # hot path a search result's Open/preview and every pipeline
            # walk goes through - and `is_bitlocker_locked` is a PowerShell
            # subprocess costing whole seconds, not microseconds. 2d's check
            # belongs only in `refresh_volume_statuses`, which 2a already
            # scopes to "once, on panel refresh" - see its docstring. A
            # locked drive slipping through here as "online" costs one
            # walk that finds every file access-denied and skips it, which
            # the pipeline already handles; the alternative costs every
            # caller of this function seconds per volume, every time.
            if root is not None:
                online[volume_id] = root
        elif kind == "network":
            # **A UNC path needs no letter resolution at all** - unlike a
            # drive, its identity *is* a usable filesystem root (1a: "the
            # letter's network twin"). Only reachability is in question, and
            # 1b/1c say an unconfirmed answer must read as offline, never as
            # a hang or a credential prompt.
            unc = row.get("identity_key")
            # A share is a Windows address (`\\server\share`). On a Mac it is
            # not one, and is not asked about: shares there are a later order.
            if unc and sys.platform == "win32" and probe_unc_reachable(unc):
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
    notification.

    **2d, resolved.** `connected_volumes` already will not call a locked
    BitLocker drive reachable - it cannot be walked - so a drive missing
    from its `online` dict is ambiguous between "not plugged in" and
    "plugged in but locked", and 2d asks for the two to read as different
    sentences. This function alone pays the extra `find_drive_by_guid` +
    `is_bitlocker_locked` probe to tell them apart, and only for a drive
    that actually needs the answer - never for one `connected_volumes`
    already resolved as online, and never for `network`/`cloud`/`phone`,
    where BitLocker does not apply.
    """
    from app.core.volumes_win import find_drive_by_guid

    online = connected_volumes(store)
    statuses: dict[int, str] = {}
    for row in store.list_volumes():
        volume_id = int(row["id"])
        kind = row.get("kind")
        if kind == "drive" and volume_id not in online:
            guid = row.get("volume_guid")
            root = find_drive_by_guid(guid) if guid else None
            status = "LOCKED" if root is not None and _drive_locked(root) else "OFFLINE"
        elif kind in ("drive", "network"):
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

def _drive_locked(root: Path) -> bool:
    r"""2d: a BitLocker-locked drive is mounted but unreadable - never
    treated as reachable by the walker, the same way an unreadable network
    share never is. `Path(root).drive` reads the letter back off whatever
    `find_drive_by_guid` returned, so this needs no assumption about the
    root's exact spelling.
    """
    from app.core.volumes_win import is_bitlocker_locked

    letter = Path(root).drive.rstrip(":")
    if not letter:
        return False
    return bool(is_bitlocker_locked(letter))


def find_volume(store: Any, identifier: Any) -> Any:
    r"""By numeric id, or by exact name (case-insensitive) - whichever a
    caller has at hand. Shared by the CLI's own lookup and the tab, so a
    name that resolves on one resolves the same way on the other.
    """
    try:
        return store.get_volume(int(identifier))
    except (TypeError, ValueError):
        pass
    for row in store.list_volumes():
        if str(row["name"]).lower() == str(identifier).lower():
            return store.get_volume(int(row["id"]))
    return None


def run_scoped_pipeline(settings: Any, store: Any, root: Path, volume_id: int, *,
                        run_lock_owner: str, verify_hash: bool = True,
                        on_progress: Optional[Any] = None) -> Any:
    r"""One Pipeline run scoped to a single catalogued volume's current mount
    point - the CLI and the tab's shared core for Scan/Rescan, so the two
    never drift into two different ideas of what a Scan does.

    Trimmed the way `app.cli`'s own version always was: no multi-root
    priority list, no hand-tuned resource flags - `resolve_for_run`'s Auto
    numbers are enough for a single-source run somebody is watching.

    `run_lock_owner` is `app.core.run_lock.COMMAND_LINE` from the CLI or
    `GUI` from the window - carried here rather than hidden behind a
    default, because it must be taken on whichever thread is actually
    doing the run (see `app.ui.workers.IndexWorker.run`'s own note on the
    same point) and this function does not know which caller it is.
    """
    from dataclasses import replace as _replace

    from app.core.run_lock import IndexRunLock
    from app.index.clip_embedder import ClipImageEmbedder
    from app.index.embedder import Embedder
    from app.index.pipeline import Pipeline
    from app.index.resolve import resolve_for_run
    from app.index.run_setup import NOW, build_pipeline_config, pass_for
    from app.index.walker import volume_root_key
    from app.storage.sqlite_store import volume_synthetic_path
    from app.storage.vector_store import ImageVectorStore, VectorStore

    tuned = resolve_for_run(settings, store)
    # *Corrected 4 October 2026, the owner: "the same code should run".* This
    # built its own `PipelineConfig`, and had drifted from every other run:
    # no pass (every picture read on a text pass), no time limits, no reader
    # processes, no junk-picture filter or attachment rule, no archive
    # recheck interval or read order - and no picture embedder, so the photos
    # on a catalogued drive never reached picture search. Now it is the
    # configuration every run uses (`run_setup.build_pipeline_config`),
    # scoped to the one mount point. The pass is `NOW`: the drive is
    # unplugged afterwards, so an images pass later would not find it.
    # *2026-10-04, code review:* and its clean-up is this drive's rows only
    # (`prune_under`, the drive's own `leasha-volume://<id>` key). It was the
    # whole index's: a Scan of a USB stick deleted the rows of every indexed
    # folder whose files were not on disk at that moment.
    config = build_pipeline_config(
        settings, [root], tuned=tuned, verify_hash=verify_hash,
        ocr_mode=pass_for(settings, NOW),
        prune_under=(volume_synthetic_path(volume_id, ""),))
    config = _replace(config, walk=_replace(
        config.walk, volume_roots={volume_root_key(root): volume_id}))
    embedder = Embedder.from_settings(settings, threads=tuned.onnx_threads)

    with IndexRunLock(store, owner=run_lock_owner), \
            VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors, \
            ImageVectorStore(settings.vector_path) as image_vectors:
        pipeline = Pipeline(store, vectors, embedder, config,
                            image_embedder=ClipImageEmbedder.from_settings(settings),
                            image_vectors=image_vectors)
        return pipeline.run(on_progress=on_progress)


def scan_new_source(settings: Any, store: Any, root: Path, *, name: str,
                    description: Optional[str] = None, run_lock_owner: str,
                    on_progress: Optional[Any] = None,
                    same_as: Optional[str] = None) -> dict[str, Any]:
    r"""Catalogue `root` as a new Offline Media source and run its first
    Scan. The shared core of `app.cli offline-media --scan` and 2a/2b's
    Scan button - one place that identifies, validates, catalogues and
    walks, so a name typed into the CLI and a name typed into the dialog
    are caught by exactly the same checks.

    `same_as` is 202626270514 1a's offer, accepted: the caller (the tab's
    own `RenameSuggestionDialog`, or the CLI's `--same-as`) has confirmed
    this identity IS a catalogued source, renamed or moved - the same
    `store.rename_volume_identity` reattachment `app.cli`'s own
    `_offline_media_scan` already does inline, lifted here so the tab does
    not have to duplicate it a second time.

    Raises `AppErrorException` for anything the caller must show - a
    missing name, an unreadable path, an unreachable share, or (`same_as`)
    a name that matches no catalogued source. Never prints or shows
    anything itself; that is the caller's job on both sides.
    """
    from app.core.errors import raise_error
    from app.core.volumes_win import hardware_serial_for_root

    if not name:
        raise_error(
            "ERR_CONFIG_INVALID", "index.offline_media",
            key="name", reason="a Scan needs a name to remember this source by",
            suggestion="Give this drive a name you will remember.",
        )
    found = identify_source(root)
    if found is None:
        if is_folder_not_a_drive(root):
            raise_error("ERR_SOURCE_NOT_A_DRIVE", "index.offline_media", path=str(root))
        raise_error(
            "ERR_CONFIG_INVALID", "index.offline_media",
            key="path", reason=f"could not read a volume or network identity for {root!r}",
            suggestion="Check the drive is plugged in, or the share is "
                       "reachable, and the path is right.",
        )
    kind, fields = found
    if kind == "drive" and not root.exists():
        raise_error(
            "ERR_CONFIG_INVALID", "index.offline_media",
            key="path", reason=f"{root!r} does not exist or is not reachable",
            suggestion="Check the drive is plugged in and the path is right.",
        )
    if kind == "network":
        from app.core.volumes_win import probe_unc_reachable

        if not probe_unc_reachable(fields["identity_key"]):
            raise_error(
                "ERR_UNEXPECTED", "index.offline_media",
                details=f"{fields['identity_key']} is offline - not signed "
                        "in or not reachable.",
                suggestion="Reconnect it the way you always do in Windows, "
                          "then scan again.",
            )

    if same_as is not None:
        target = find_volume(store, same_as)
        if target is None:
            raise_error(
                "ERR_CONFIG_INVALID", "index.offline_media",
                key="same_as", reason=f"no catalogued source matches {same_as!r}",
            )
        existing = store.get_volume_by_identity(fields["identity_key"])
        if existing is not None and existing.id != target.id:
            raise_error(
                "ERR_CONFIG_INVALID", "index.offline_media",
                key="same_as",
                reason=f"{fields['identity_key']!r} is already catalogued as "
                       f"{existing.name!r}, not {target.name!r}",
            )
        store.rename_volume_identity(target.id, fields["identity_key"])
        volume_id = target.id
    else:
        volume_id = store.upsert_volume(
            fields["identity_key"], kind=kind, name=name, description=description,
            **{k: v for k, v in fields.items() if k != "identity_key"},
        )
    if kind == "drive":
        serial = hardware_serial_for_root(root)
        if serial:
            store.upsert_volume(fields["identity_key"], kind=kind, name=name,
                               hardware_serial=serial)

    stats = run_scoped_pipeline(
        settings, store, root, volume_id, run_lock_owner=run_lock_owner,
        verify_hash=(kind != "network"), on_progress=on_progress,
    )
    return {"volume_id": volume_id, "kind": kind, "stats": stats}


def rescan_source(settings: Any, store: Any, identifier: Any, *,
                  run_lock_owner: str, on_progress: Optional[Any] = None) -> dict[str, Any]:
    r"""Rescan a catalogued source by id or name - the shared core of
    `app.cli offline-media --rescan` and 2a's Rescan button.

    Raises `AppErrorException` when the source is unknown, or not currently
    reachable - a drive that is unplugged, or a share that is not signed
    in - naming which, since the fix is different for each.
    """
    from app.core.errors import raise_error

    record = find_volume(store, identifier)
    if record is None:
        raise_error(
            "ERR_CONFIG_INVALID", "index.offline_media",
            key="identifier", reason=f"no catalogued source matches {identifier!r}",
        )
    online = connected_volumes(store)
    root = online.get(record.id)
    if root is None:
        if record.kind == "network":
            details = f"{record.name!r} is offline - not signed in or not reachable."
            suggestion = ("Reconnect it the way you always do in Windows, "
                         "then rescan again. Nothing about its existing "
                         "catalogue entry has changed.")
        else:
            details = f"{record.name!r} is not currently connected."
            suggestion = ("Plug it in, then rescan again. Nothing about its "
                         "existing catalogue entry has changed.")
        raise_error("ERR_UNEXPECTED", "index.offline_media",
                   details=details, suggestion=suggestion)

    remember_hardware_serial(store, record, root)
    reconciled = reconcile_moves(store, record.id, root)
    stats = run_scoped_pipeline(
        settings, store, root, record.id, run_lock_owner=run_lock_owner,
        verify_hash=(record.kind != "network"), on_progress=on_progress,
    )
    return {"volume_id": record.id, "moved": reconciled.moved, "stats": stats}


def remember_hardware_serial(store: Any, record: Any, root: Path) -> Optional[str]:
    r"""Ask Windows for a drive's disk serial if none is stored yet. 2026-10-02.

    The serial was read once, at the first Scan, and never again - so a drive
    whose first Scan found none (a slow provider, an enclosure that was not
    answering) showed no hardware ID for good. A Rescan now asks again, for a
    drive only and only while the row holds none. Returns what was stored, or
    None. **Never raises**: this is advisory data and must not cost a Rescan.
    """
    try:
        if record is None or record.kind != "drive" or record.hardware_serial:
            return None
        from app.core.volumes_win import hardware_serial_for_root

        serial = hardware_serial_for_root(root)
        if serial:
            store.upsert_volume(record.identity_key, kind=record.kind,
                                name=record.name, hardware_serial=serial)
        return serial or None
    except Exception as exc:                        # noqa: BLE001 - see the docstring
        _log.debug("no hardware serial read for {}: {}", root, exc)
        return None
