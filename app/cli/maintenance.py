"""`stats`, `init`, `move-index`, `doctor`, `diagnose` and `lock`."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from app.cli._common import EXIT_OK, _load, _report
from app.core.branding import banner
from app.core.config import project_root
from app.core.errors import AppErrorException, make_error
from app.core.logging import logger, setup_logging
from app.core.single_instance import SingleInstance
from app.core.version import build_info


def cmd_stats(args: argparse.Namespace) -> int:
    """Print the resolved configuration. The 'does it start at all' command."""
    settings = _load(args)
    setup_logging(settings.log_path)

    info: dict[str, Any] = {
        "version": build_info(),
        "env_file": str(settings.env_file),
        "settings": settings.describe(),
    }

    # Report the stores only if they already exist: `stats` must be a read-only
    # inspection, not something that creates an index as a side effect.
    if settings.fts_db.is_file():
        from app.storage.sqlite_store import SqliteStore

        with SqliteStore(settings.fts_db) as store:
            info["sqlite"] = store.stats()

    from app.storage.vector_store import VectorStore

    with VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors:
        # connect() opens an existing table but never creates one, so this
        # stays a read-only inspection.
        if vectors.exists:
            info["vectors"] = vectors.stats()

    if args.json:
        print(json.dumps(info, indent=2))
        return EXIT_OK

    print(banner(info["version"]["version"]))
    if "git" in info["version"]:
        print(f"  git: {info['version']['git']}")
    elif "git_error" in info["version"]:
        # Not fatal - a packaged build has no .git - but say why rather than
        # quietly omitting the line.
        print(f"  git: unavailable ({info['version']['git_error']})")
    print(f"  config: {settings.env_file}")
    print()
    width = max(len(k) for k in settings.describe())
    for key, value in settings.describe().items():
        print(f"  {key.ljust(width)}  {value}")

    if "sqlite" in info:
        sqlite_stats = info["sqlite"]
        print()
        print("  Metadata store (SQLite, the authority)")
        print(f"    schema version   {sqlite_stats['schema_version']}"
              f" (this build expects {sqlite_stats['expected_schema_version']})")
        print(f"    files            {sqlite_stats['files_total']}  {sqlite_stats['files'] or '{}'}")
        print(f"    chunks           {sqlite_stats['chunks_total']}"
              f"  ({sqlite_stats['chunks_embedded']} embedded)")
        print(f"    generation       {sqlite_stats['generation']}")
        if sqlite_stats["skipped_by_code"]:
            print(f"    skipped          {sqlite_stats['skipped_by_code']}")
    else:
        print()
        print("  Metadata store    not created yet (run: app.cli init)")

    if "vectors" in info:
        vector_stats = info["vectors"]
        print()
        print("  Vector store (LanceDB, derived - rebuildable from SQLite)")
        print(f"    rows             {vector_stats['rows']}")
        print(f"    dimensions       {vector_stats['dim']}")
        print(f"    ANN index at     {vector_stats['index_threshold']} rows")
    else:
        print()
        print("  Vector store      not created yet (run: app.cli init)")


    for line in semantic_search_warnings(info.get("sqlite"), info.get("vectors")):
        print(line)

    return EXIT_OK


def semantic_search_warnings(
    sqlite_stats: Optional[dict], vector_stats: Optional[dict]
) -> list[str]:
    """Say in words when meaning-based search cannot work. Empty when it can.

    Searching is deliberately forgiving: `vector.search` returns `[]` for an
    empty index, a failed embedding or a LanceDB hiccup, because a half-working
    search is better than none and keyword results still come back. But that
    makes the failure **invisible** - the results look thin and nobody can tell
    whether the corpus is thin or the semantic half is simply dead.

    That is the same shape as the sentinel bug that hid every PST for weeks: a
    silent degradation that reports success. So the numbers that would reveal it
    are compared here and stated plainly, rather than left as two figures on
    different lines for somebody to notice.
    """
    if not sqlite_stats:
        return []

    chunks = int(sqlite_stats.get("chunks_total", 0))
    embedded = int(sqlite_stats.get("chunks_embedded", 0))
    rows = int((vector_stats or {}).get("rows", 0))
    if chunks == 0:
        return []

    fix = r"      venv\Scripts\python.exe -m app.cli reembed"

    # **Measured against `chunks_total`, not `chunks_embedded`.** The first
    # version of this check compared the vector row count against the number of
    # chunks *flagged* as embedded, and those two agreed perfectly on a corpus
    # where only 154 of 3,355 passages had ever been embedded - so it printed
    # nothing at all, on precisely the machine it was written for.
    #
    # The flag answers "did the vectors get written for the chunks we tried",
    # which is not the question. The question is "can meaning-based search see
    # my corpus", and only the total can answer that.
    if rows == 0:
        return [
            "",
            "  [!] Meaning-based search is NOT working.",
            f"    {chunks:,} passages are indexed but the vector store is empty, so only",
            "    keyword matching is running. A question phrased in your own words will",
            "    only find documents that happen to use those exact words.",
            "",
            "    Rebuild the vectors from SQLite, which is the authority:",
            fix,
        ]

    covered = rows / chunks
    if covered < 0.95:
        missing = chunks - rows
        return [
            "",
            f"  [!] Meaning-based search covers {covered:.0%} of your corpus.",
            f"    {rows:,} of {chunks:,} passages have a vector; {missing:,} do not, and are",
            "    findable by keyword only. Embedding stops early if an index run is",
            "    paused, interrupted, or runs short of memory - and nothing has said so",
            "    until now, because the run itself reported success.",
            "",
            "    Embed what is missing (no document is re-read; minutes, not hours):",
            fix,
        ]

    if embedded and rows > embedded * 1.05:
        # More vectors than SQLite believes were embedded means orphaned rows,
        # usually from an interrupted rebuild. Searches will return chunk ids
        # that no longer resolve, which looks like results silently going missing.
        return [
            "",
            f"  [!] The vector store has {rows:,} rows but SQLite says {embedded:,} passages",
            "    were embedded. The extra rows are orphans and may produce results that",
            "    cannot be opened. Rebuild from the authority:",
            r"      venv\Scripts\python.exe -m app.cli reembed --all",
        ]
    return []


def cmd_init(args: argparse.Namespace) -> int:
    """Create and migrate both stores. Safe to re-run."""
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore

    settings = _load(args)
    setup_logging(settings.log_path)

    with SqliteStore(settings.fts_db) as store:
        sqlite_stats = store.stats()
    with VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors:
        vectors.ensure_table()
        vector_stats = vectors.stats()

    payload = {"sqlite": sqlite_stats, "vectors": vector_stats}
    if args.json:
        print(json.dumps(payload, indent=2))
        return EXIT_OK

    print(f"Metadata store ready: {sqlite_stats['db_path']}")
    print(f"  schema version {sqlite_stats['schema_version']}, "
          f"generation {sqlite_stats['generation']}")
    print(f"Vector store ready:   {vector_stats['uri']}")
    print(f"  {vector_stats['rows']} rows, {vector_stats['dim']} dimensions")
    return EXIT_OK


def cmd_diagnose(args: argparse.Namespace) -> int:
    """Collect everything needed to troubleshoot into a single zip.

    This is the "something is wrong, here is the evidence" command.
    """
    from app.core.diagnostics import build_bundle

    settings = _load(args)
    setup_logging(settings.log_path)

    out = Path(args.out) if args.out else None
    bundle = build_bundle(settings, project_root(), out_path=out)
    size_kb = bundle.stat().st_size / 1024

    if args.json:
        print(json.dumps({"bundle": str(bundle), "size_kb": round(size_kb, 1)}, indent=2))
        return EXIT_OK

    print(f"Diagnostic bundle written: {bundle}")
    print(f"  {size_kb:.0f} KB - contains report.json, summary.txt, recent logs and .env")
    print()
    print("  Send this file when asking for help. Open summary.txt first: it")
    print("  names anything already known to be wrong.")
    return EXIT_OK


def cmd_move_index(args: argparse.Namespace) -> int:
    r"""Move the index to a new folder and repoint `.env` at it.

    **The CLI is where this belongs, and the UI defers to it**, because the one
    thing a move must not do is run while the stores are open: copying SQLite
    out from under a live connection yields a database that opens, reports no
    error, and is missing whatever was in the write-ahead log. Here, nothing is
    holding the files.

    `--dry-run` prints the plan and touches nothing, which is the right way to
    start when the number is measured in hundreds of gigabytes.
    """
    from app.core.index_move import ADOPT, FRESH, MOVE, perform_move, plan_move

    settings = _load(args)
    setup_logging(settings.log_path)

    action = ADOPT if args.adopt else (FRESH if args.fresh else MOVE)
    source = Path(settings.data_path)
    destination = Path(args.destination).expanduser()

    try:
        if args.dry_run:
            report = plan_move(source, destination, action)
        else:
            report = perform_move(
                source, destination, action, Path(settings.env_file),
                on_progress=None if args.json else lambda line: print(line, flush=True),
            )
    except AppErrorException as exc:
        return _report(exc.error, args.json)

    if args.json:
        print(json.dumps({
            "action": report.action,
            "source": str(report.source),
            "destination": str(report.destination),
            "moved": list(report.moved),
            "bytes": report.bytes_moved,
            "same_volume": report.same_volume,
            "env_keys_removed": list(report.env_keys_removed),
            "performed": report.performed,
        }, indent=2))
        return EXIT_OK

    print(report.summary)
    if report.performed:
        # The five derived keys are the reason a move used to appear to do
        # nothing: `.env` pinned them absolutely, and they beat DATA_PATH.
        print(f"Unpinned {', '.join(report.env_keys_removed)} so they follow "
              "DATA_PATH from now on.")
        print("Start the application when ready.")
    else:
        print("Nothing was changed. Re-run without --dry-run to do it.")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    """Run doctor.py and pass its exit code through."""
    doctor = project_root() / "doctor.py"
    if not doctor.is_file():
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.doctor",
            key="doctor.py", reason=f"not found at {doctor}",
        ), args.json)

    command = [sys.executable, str(doctor)]
    if args.json:
        command.append("--json")
    if args.quick:
        command.append("--quick")
    return subprocess.call(command)


def cmd_lock(args: argparse.Namespace) -> int:
    """Acquire the single-instance lock and hold it.

    A diagnostic, and how the Layer 0 acceptance test proves that a second copy
    refuses to start.
    """
    settings = _load(args)
    setup_logging(settings.log_path)

    with SingleInstance():
        logger.bind(component="cli.lock").info("Lock acquired, holding {}s", args.hold)
        print(f"LOCK ACQUIRED pid={_pid()} hold={args.hold}s", flush=True)
        time.sleep(args.hold)
    print("LOCK RELEASED", flush=True)
    return EXIT_OK


def _pid() -> int:
    return os.getpid()


def add_stats_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_stats = sub.add_parser("stats", parents=[common], help="show the resolved configuration")
    p_stats.set_defaults(func=cmd_stats)


def add_init_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_init = sub.add_parser("init", parents=[common], help="create and migrate the stores (safe to re-run)")
    p_init.set_defaults(func=cmd_init)


def add_move_index_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_move = sub.add_parser(
        "move-index", parents=[common],
        help="move the index to another folder and repoint .env at it",
        description=(
            "Moves vectors, fts, cache, models and state to DESTINATION, then "
            "rewrites DATA_PATH and removes the five per-directory keys so they "
            "derive from it. Run with nothing else open. Use --dry-run first."
        ),
    )
    p_move.add_argument("destination", help=r"the new index folder, e.g. D:\Leasha\Data")
    p_move.add_argument("--dry-run", action="store_true",
                        help="print what would happen and change nothing")
    move_kind = p_move.add_mutually_exclusive_group()
    move_kind.add_argument("--adopt", action="store_true",
                           help="use the index already at DESTINATION instead of moving")
    move_kind.add_argument("--fresh", action="store_true",
                           help="start a new, empty index at DESTINATION")
    p_move.set_defaults(func=cmd_move_index)


def add_doctor_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_doctor = sub.add_parser("doctor", parents=[common], help="verify the environment")
    p_doctor.add_argument("--quick", action="store_true", help="skip model loading")
    p_doctor.set_defaults(func=cmd_doctor)


def add_diagnose_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_diagnose = sub.add_parser(
        "diagnose", parents=[common], help="bundle logs, config and environment into one zip for troubleshooting")
    p_diagnose.add_argument("--out", help="write the bundle here instead of logs/diagnostics/")
    p_diagnose.set_defaults(func=cmd_diagnose)


def add_lock_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_lock = sub.add_parser("lock", parents=[common], help="hold the single-instance lock (diagnostic)")
    p_lock.add_argument("--hold", type=float, default=2.0, help="seconds to hold the lock")
    p_lock.set_defaults(func=cmd_lock)
