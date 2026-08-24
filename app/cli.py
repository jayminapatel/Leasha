"""Headless entry point.

Layer: L0

Every layer ships a CLI before it ships UI, so it can be tested without Qt.
Layer 0 provides `stats`, `doctor` and `lock`; `index` and `search` are declared
now and fail with an honest ERR_NOT_IMPLEMENTED naming the layer that delivers
them, rather than pretending or crashing.

    python -m app.cli stats
    python -m app.cli stats --json
    python -m app.cli doctor
    python -m app.cli lock --hold 5

Exit codes:
    0  success
    1  an AppError occurred
    2  the feature is not built yet
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from app.core.errors import AppError, AppErrorException, make_error
from app.core.config import Settings, load_settings, project_root
from app.core.logging import setup_logging, log_app_error, logger
from app.core.single_instance import SingleInstance
from app.core.version import build_info

__all__ = ["main", "build_parser"]

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_IMPLEMENTED = 2


def _report(error: AppError, as_json: bool) -> int:
    """Print an AppError to the console in the requested shape."""
    if as_json:
        print(json.dumps(error.model_dump(mode="json"), indent=2))
    else:
        print(error.render(), file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED if error.code == "ERR_NOT_IMPLEMENTED" else EXIT_ERROR


def _load(args: argparse.Namespace) -> Settings:
    env_file = Path(args.env) if getattr(args, "env", None) else None
    return load_settings(env_file)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

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

    print(f"Local Knowledge Graph V2  version {info['version']['version']}")
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

    return EXIT_OK


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
    import os

    return os.getpid()


def cmd_index(args: argparse.Namespace) -> int:
    return _report(make_error(
        "ERR_NOT_IMPLEMENTED", "cli.index",
        feature="Indexing", layer="Layer 3 (indexing pipeline)",
    ), args.json)


def cmd_search(args: argparse.Namespace) -> int:
    return _report(make_error(
        "ERR_NOT_IMPLEMENTED", "cli.search",
        feature="Search", layer="Layer 4 (search)",
    ), args.json)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.cli",
        description="Local Knowledge Graph V2 - headless entry point.",
    )
    parser.add_argument("--env", help="path to an alternative .env file")
    parser.add_argument("--json", action="store_true", help="machine-readable output")

    sub = parser.add_subparsers(dest="command", required=True)

    p_stats = sub.add_parser("stats", help="show the resolved configuration")
    p_stats.set_defaults(func=cmd_stats)

    p_init = sub.add_parser("init", help="create and migrate the stores (safe to re-run)")
    p_init.set_defaults(func=cmd_init)

    p_doctor = sub.add_parser("doctor", help="verify the environment")
    p_doctor.add_argument("--quick", action="store_true", help="skip model loading")
    p_doctor.set_defaults(func=cmd_doctor)

    p_lock = sub.add_parser("lock", help="hold the single-instance lock (diagnostic)")
    p_lock.add_argument("--hold", type=float, default=2.0, help="seconds to hold the lock")
    p_lock.set_defaults(func=cmd_lock)

    p_index = sub.add_parser("index", help="build or update the index (Layer 3)")
    p_index.add_argument("roots", nargs="*", help="folders to index")
    p_index.set_defaults(func=cmd_index)

    p_search = sub.add_parser("search", help="search the index (Layer 4)")
    p_search.add_argument("query", nargs="*", help="search terms")
    p_search.set_defaults(func=cmd_search)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        return int(args.func(args))
    except AppErrorException as exc:
        # Logging may not be configured yet (a bad .env fails before setup),
        # so print unconditionally and log only on a best-effort basis.
        try:
            log_app_error(exc.error)
        except Exception:  # noqa: BLE001
            pass
        return _report(exc.error, getattr(args, "json", False))
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
