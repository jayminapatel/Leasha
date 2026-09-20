"""`offline-media`: cataloguing removable drives."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, Optional

from app.cli._common import EXIT_OK, _load, _report
from app.core.errors import make_error
from app.core.logging import setup_logging
from app.core.run_lock import COMMAND_LINE


def cmd_offline_media(args: argparse.Namespace) -> int:
    r"""Offline Media: catalogue a removable drive, rescan it, or forget it.

    Layer 3's entry point for order 202626270513, shipped before the tab per
    non-negotiable 8. Fully manual, per the owner's model: nothing here runs
    unless this command was typed - there is no watcher anywhere, and there
    never will be.
    """
    from app.core.volumes_win import identify_root
    from app.index.embedder import Embedder
    from app.index.offline_media import (
        connected_volumes,
        delete_volume,
        reconcile_moves,
        refresh_volume_statuses,
        resolve_file_path,
    )
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.resolve import resolve_for_run
    from app.index.walker import WalkConfig, own_paths
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore

    settings = _load(args)
    setup_logging(settings.log_path)

    if args.delete is not None:
        return _offline_media_delete(settings, args.delete, args.json,
                                     confirmed=args.yes)

    if args.archive is not None:
        return _offline_media_archive(settings, args.archive, args.location,
                                      as_json=args.json)

    if args.scan:
        return _offline_media_scan(settings, Path(args.scan).expanduser(),
                                   name=args.name, description=args.description,
                                   as_json=args.json, same_as=args.same_as,
                                   sequential_medium=args.sequential_medium)

    if args.rescan is not None:
        return _offline_media_rescan(settings, args.rescan, as_json=args.json)

    # No verb: list, refreshing status first (2a: "checked passively on panel
    # refresh").
    with SqliteStore(settings.fts_db) as store:
        refresh_volume_statuses(store)
        volumes = store.list_volumes()

    if args.json:
        print(json.dumps({"volumes": volumes, "count": len(volumes)},
                         indent=2, default=str))
        return EXIT_OK

    if not volumes:
        print("No Offline Media sources catalogued yet.")
        print('Scan one with: app.cli offline-media --scan "E:\\" --name "Projects 2019"')
        return EXIT_OK

    name_width = max(len("NAME"), max(len(str(v["name"])) for v in volumes))
    print(f"{'NAME':<{name_width}}  {'KIND':<8}  {'STATUS':<8}  {'FILES':>8}  LAST SEEN            DESCRIPTION")
    for v in volumes:
        seen = v.get("last_seen")
        stamp = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(int(seen)))
            if seen else "-"
        )
        print(
            f"{v['name']:<{name_width}}  {v['kind']:<8}  {v['status']:<8}  "
            f"{int(v['indexed_files']):>8,}  {stamp:<19}  {v.get('description') or ''}"
        )
    print()
    print(f"  {len(volumes)} source(s). Rescan with --rescan <name>, "
          f"forget with --delete <name>.")
    return EXIT_OK


def _offline_media_scan(settings: Any, root: Path, *, name: Optional[str],
                        description: Optional[str], as_json: bool,
                        same_as: Optional[str] = None,
                        sequential_medium: bool = False) -> int:
    r"""The first Scan of a new source: a drive, or a network share
    (202626270514 1a - identity resolved here, the mapped letter if any
    discarded immediately after). 2b: asks for a name; here, requires one,
    because there is no dialog to ask twice."""
    from app.index.offline_media import identify_source, suggest_renamed_source
    from app.storage.sqlite_store import SqliteStore

    if not name and not same_as:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.offline_media",
            key="name", reason="a Scan needs a name to remember this source by",
            suggestion='Give this drive a name you will remember: '
                       '--name "Projects 2019"',
        ), as_json)

    found = identify_source(root)
    if found is None:
        from app.index.offline_media import is_folder_not_a_drive

        if is_folder_not_a_drive(root):
            return _report(make_error(
                "ERR_SOURCE_NOT_A_DRIVE", "cli.offline_media", path=str(root)), as_json)
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.offline_media",
            key="path", reason=f"could not read a volume or network identity for '{root}'",
            suggestion="Check the drive is plugged in, or the share is "
                       "reachable, and the path is right.",
        ), as_json)
    kind, fields = found
    if kind == "drive" and not root.exists():
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.offline_media",
            key="path", reason=f"'{root}' does not exist or is not reachable",
            suggestion="Check the drive is plugged in and the path is right.",
        ), as_json)
    if kind == "network":
        # **Probed here, hard-timeout, before anything is catalogued or
        # walked.** Without this, a genuinely unreachable share was
        # discovered the slow way - `os.walk` itself hanging on Windows'
        # own SMB connection timeout, 13+ seconds against a dead host in
        # testing here and potentially much longer on a real corporate
        # network - and reported as "0 files found", which reads as an
        # empty share rather than an unreachable one. 1c: "availability
        # probes hard-timeout on workers."
        from app.core.volumes_win import probe_unc_reachable

        if not probe_unc_reachable(fields["identity_key"]):
            return _report(make_error(
                "ERR_UNEXPECTED", "cli.offline_media",
                details=f"{fields['identity_key']} is offline - not signed "
                        "in or not reachable.",
                suggestion="Reconnect it the way you always do in Windows, "
                          "then scan again.",
            ), as_json)

    notice: Optional[str] = None
    with SqliteStore(settings.fts_db) as store:
        existing = store.get_volume_by_identity(fields["identity_key"])

        if same_as is not None:
            # **1a's acceptance half**: the person has confirmed this new
            # identity IS a catalogued source, renamed or moved - reattach
            # rather than catalogue a second, duplicate row. `--same-as`
            # never fires silently; it is always something the caller typed.
            target = _find_volume(store, same_as)
            if target is None:
                return _report(make_error(
                    "ERR_CONFIG_INVALID", "cli.offline_media",
                    key="same_as", reason=f"no catalogued source matches {same_as!r}",
                ), as_json)
            if existing is not None and existing.id != target.id:
                return _report(make_error(
                    "ERR_CONFIG_INVALID", "cli.offline_media",
                    key="same_as",
                    reason=f"{fields['identity_key']!r} is already catalogued "
                           f"as {existing.name!r}, not {target.name!r}",
                ), as_json)
            store.rename_volume_identity(target.id, fields["identity_key"])
            volume_id = target.id
            name = target.name
        elif existing is None and kind in ("drive", "network"):
            # **1a's offer half, softened: assist, never assume.** Only
            # surfaced for a genuinely new identity - an ordinary rescan at
            # an already-known identity never reaches here at all.
            suggestion = suggest_renamed_source(store, kind, root,
                                                exclude_identity_key=fields["identity_key"])
            if suggestion is not None:
                notice = (f"This looks like {suggestion['name']!r} at a new "
                          f"address (its folders match). If it is the same "
                          f"source, rescan it with "
                          f"--same-as \"{suggestion['name']}\" instead of "
                          f"cataloguing it again.")

        if same_as is None:
            volume_id = store.upsert_volume(
                fields["identity_key"], kind=kind, name=name, description=description,
                sequential_medium=sequential_medium,
                **{k: v for k, v in fields.items() if k != "identity_key"},
            )
        if kind == "drive":
            # The advisory hardware serial (1a: reformat recognition) is
            # fetched after the row exists, so a slow or missing WMI
            # provider never stops the volume being catalogued.
            from app.core.volumes_win import hardware_serial_for_root

            serial = hardware_serial_for_root(root)
            if serial:
                store.upsert_volume(fields["identity_key"], kind=kind, name=name,
                                   hardware_serial=serial)

        if sequential_medium and not as_json:
            # 3b-2: named before the scan starts, not buried in a log line -
            # a tape read is minutes, not the instant a names-only pass over
            # an ordinary drive would suggest.
            print("This is a sequential medium (tape): a content scan reads "
                  "it end to end. Names-only cataloguing stays instant.")

        # 1c: a network share never hashes to verify - SMB makes reading
        # every byte of every file just to confirm it has not moved
        # prohibitive, where a local mtime/size settling is nearly free.
        stats = _run_offline_media_pipeline(
            settings, store, root, volume_id, quiet=as_json,
            verify_hash=(kind != "network"),
        )

    if as_json:
        payload = {"volume_id": volume_id, "kind": kind, **stats.as_dict()}
        if notice:
            payload["notice"] = notice
        print(json.dumps(payload, indent=2, default=str))
        return EXIT_OK
    print(f"Catalogued as {name!r} ({kind}): {stats.indexed:,} document(s), "
          f"{stats.seen:,} file(s) seen.")
    if notice:
        print(notice)
    return EXIT_OK


def _offline_media_rescan(settings: Any, identifier: str, *, as_json: bool) -> int:
    from app.index.offline_media import connected_volumes, reconcile_moves
    from app.storage.sqlite_store import SqliteStore

    with SqliteStore(settings.fts_db) as store:
        record = _find_volume(store, identifier)
        if record is None:
            return _report(make_error(
                "ERR_CONFIG_INVALID", "cli.offline_media",
                key="identifier", reason=f"no catalogued source matches {identifier!r}",
            ), as_json)
        online = connected_volumes(store)
        root = online.get(record.id)
        if root is None:
            # 202626270514 1b's exact wording for a share; a drive gets its
            # own plainer one. Never a credential prompt either way - this
            # command does not know why it is unreachable, only that it is.
            if record.kind == "network":
                details = (f"{record.name!r} is offline - not signed in "
                          "or not reachable.")
                suggestion = ("Reconnect it the way you always do in Windows, "
                             "then rescan again. Nothing about its existing "
                             "catalogue entry has changed.")
            else:
                details = f"{record.name!r} is not currently connected."
                suggestion = ("Plug it in, then rescan again. Nothing about "
                             "its existing catalogue entry has changed.")
            return _report(make_error(
                "ERR_UNEXPECTED", "cli.offline_media",
                details=details, suggestion=suggestion,
            ), as_json)

        reconciled = reconcile_moves(store, record.id, root)
        stats = _run_offline_media_pipeline(
            settings, store, root, record.id, quiet=as_json,
            verify_hash=(record.kind != "network"),
        )

    if as_json:
        print(json.dumps({"volume_id": record.id, "moved": reconciled.moved,
                          **stats.as_dict()}, indent=2, default=str))
        return EXIT_OK
    print(f"Rescanned {record.name!r}: {stats.indexed:,} new/changed document(s), "
          f"{reconciled.moved:,} file(s) moved on disk and repaired without "
          f"re-extraction, {stats.deleted:,} row(s) removed for files genuinely gone.")
    return EXIT_OK


def _offline_media_archive(settings: Any, identifier: str,
                           location: Optional[str], *, as_json: bool) -> int:
    r"""3b-1: "Mark as archived" - detach a catalogued source into a name +
    free-text location. Generalises past tape - DVDs, a destroyed drive,
    media handed to someone else."""
    from app.index.offline_media import archive_volume
    from app.storage.sqlite_store import SqliteStore

    if not location:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.offline_media",
            key="location", reason="archiving a source needs a location to "
                                   "remember it by",
            suggestion='Say where it physically is: --location "LTO-7 tape '
                       'B-0042, fire safe, IT room"',
        ), as_json)

    with SqliteStore(settings.fts_db) as store:
        record = _find_volume(store, identifier)
        if record is None:
            return _report(make_error(
                "ERR_CONFIG_INVALID", "cli.offline_media",
                key="identifier", reason=f"no catalogued source matches {identifier!r}",
            ), as_json)
        result = archive_volume(store, record.id, location)

    if as_json:
        print(json.dumps(result, indent=2))
        return EXIT_OK
    print(f"{result['name']!r} is now archived: {location}")
    print("Rescan is disabled. Browse and Delete still work. Nothing already "
         "indexed was touched.")
    return EXIT_OK


def _offline_media_delete(settings: Any, identifier: str, as_json: bool, *,
                          confirmed: bool) -> int:
    from app.index.offline_media import delete_volume
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore

    with SqliteStore(settings.fts_db) as store:
        record = _find_volume(store, identifier)
        if record is None:
            return _report(make_error(
                "ERR_CONFIG_INVALID", "cli.offline_media",
                key="identifier", reason=f"no catalogued source matches {identifier!r}",
            ), as_json)
        if not confirmed:
            count = len(store.volume_file_ids(record.id))
            if as_json:
                print(json.dumps({
                    "would_delete": count, "name": record.name,
                    "note": "pass --yes to actually delete",
                }, indent=2))
                return EXIT_OK
            print(f"This would remove {count:,} file(s) from Leasha's index, "
                 f"catalogued as {record.name!r}.")
            print("This removes the catalogue from Leasha's index. Nothing on "
                 "the drive itself is touched.")
            print("Pass --yes to do it.")
            return EXIT_OK

        with VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors:
            result = delete_volume(store, vectors, record.id)

    if as_json:
        print(json.dumps(result, indent=2))
        return EXIT_OK
    print(f"Deleted {result['name']!r}: {result['files']:,} file(s) removed "
         f"from the index. Nothing on the drive itself was touched.")
    return EXIT_OK


def _find_volume(store: Any, identifier: str) -> Any:
    """By numeric id, or by exact name (case-insensitive) - whichever a
    person is more likely to have at hand."""
    try:
        return store.get_volume(int(identifier))
    except (TypeError, ValueError):
        pass
    for row in store.list_volumes():
        if str(row["name"]).lower() == identifier.lower():
            return store.get_volume(int(row["id"]))
    return None


def _run_offline_media_pipeline(settings: Any, store: Any, root: Path,
                                volume_id: int, *, quiet: bool,
                                verify_hash: bool = True) -> Any:
    """One Pipeline run scoped to a single catalogued volume's current mount
    point. Thin CLI wrapper: the construction itself - trimmed the way a
    single-source Scan/Rescan needs, no multi-root priority list, no
    hand-tuned resource flags - now lives in `app.index.offline_media.
    run_scoped_pipeline`, shared with the Offline Media tab so a Scan typed
    on the console and one clicked in the window run the identical Pipeline.

    `verify_hash=False` for a network share (202626270514 1c): SMB makes
    reading every byte just to confirm nothing changed prohibitive, where
    mtime/size settling for an unmoved file is nearly free. Drives keep the
    ordinary default.
    """
    from app.index.offline_media import run_scoped_pipeline

    return run_scoped_pipeline(
        settings, store, root, volume_id, run_lock_owner=COMMAND_LINE,
        verify_hash=verify_hash, on_progress=None,
    )


def add_offline_media_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_offline = sub.add_parser(
        "offline-media", parents=[common],
        help="catalogue a removable drive, rescan it, or forget it - fully "
             "manual, nothing here runs on its own")
    p_offline.add_argument(
        "--scan", metavar="PATH",
        help='catalogue a new drive: --scan "E:\\" --name "Projects 2019"')
    p_offline.add_argument(
        "--name", metavar="NAME",
        help="--scan only: the name you will remember this drive by")
    p_offline.add_argument(
        "--description", metavar="TEXT",
        help="--scan only: optional free text")
    p_offline.add_argument(
        "--rescan", metavar="NAME_OR_ID",
        help="rescan a catalogued source that is currently connected")
    p_offline.add_argument(
        "--delete", metavar="NAME_OR_ID",
        help="forget a catalogued source: removes it from Leasha's index. "
             "Nothing on the drive itself is touched. Shows the count and "
             "asks for --yes before doing anything")
    p_offline.add_argument(
        "--yes", action="store_true",
        help="--delete only: actually delete, having seen the count")
    p_offline.add_argument(
        "--same-as", metavar="NAME_OR_ID",
        help="--scan only (202626270514 1a): this new source is the same one "
             "already catalogued as NAME_OR_ID, renamed or moved to a new "
             "address - reattach its identity instead of cataloguing a "
             "second, duplicate source. Existing files keep their history; "
             "nothing is re-indexed")
    p_offline.add_argument(
        "--archive", metavar="NAME_OR_ID",
        help="202626270514 3b-1: detach a catalogued source into a "
             "manual/archived one - a name and a free-text location "
             "(--location), nothing more. Rescan is disabled from then on; "
             "Browse and Delete still work. Requires --location")
    p_offline.add_argument(
        "--location", metavar="TEXT",
        help='--archive only: free text, e.g. "LTO-7 tape B-0042, fire '
             'safe, IT room"')
    p_offline.add_argument(
        "--sequential-medium", action="store_true",
        help="--scan only (202626270514 3b-2): this volume is a tape or "
             "other sequential medium - a content scan reads it end to end "
             "in on-tape order rather than the ordinary walk order, and "
             "Rescan says so before it starts. Names-only cataloguing is "
             "unaffected and stays instant")
    p_offline.set_defaults(func=cmd_offline_media)
