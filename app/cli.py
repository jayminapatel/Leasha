"""Headless entry point.

Layer: L0

Every layer ships a CLI before it ships UI, so it can be tested without Qt.
Layer 0 provides `stats`, `doctor` and `lock`; Layer 2 adds `extract`; `index`
and `search` are declared now and fail with an honest ERR_NOT_IMPLEMENTED naming
the layer that delivers them, rather than pretending or crashing.

    python -m app.cli stats
    python -m app.cli stats --json
    python -m app.cli init
    python -m app.cli doctor
    python -m app.cli diagnose        # troubleshooting bundle

    python -m app.cli extract "D:\\Docs\\report.pdf" --chunks
    python -m app.cli extract "D:\\Docs" --limit 200
    python -m app.cli extract "D:\\Docs" --json > extraction.json

`--json` and `--env` work either side of the subcommand: argparse would normally
demand them first, which is not the order anyone types.

`extract` is read-only: it opens no store and writes nothing. It exists so that
extraction can be checked against real documents rather than only against
synthetic fixtures, and so "why did that file not come back in a search?" has an
answer that does not need a debugger.

Exit codes:
    0  success
    1  an AppError occurred
    2  the feature is not built yet
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional, Sequence

from app.core.branding import SHORT_DESCRIPTION, banner
from app.core.config import Settings, load_settings, log_dir_for, project_root
from app.core.errors import AppError, AppErrorException, make_error
from app.core.logging import log_app_error, logger, setup_logging
from app.core.runlog import current as current_run
from app.core.runlog import start_run
from app.core.run_lock import COMMAND_LINE, IndexRunLock
from app.core.single_instance import SingleInstance
from app.core.version import build_info
from app.index.resources import limits_from_settings

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
    settings = load_settings(env_file)

    # **The one place every command gets its configuration**, and therefore the
    # only place the run log can record what the configuration actually was.
    # Writing it from `.env` instead would record the file rather than the
    # values in force, which is the mistake `doctor` made for a week.
    run = current_run()
    if run is not None:
        run.settings(settings)

    # `ocr.py` is a registered extractor reached with a path and nothing else,
    # so `EMBED_DEVICE` has to be pushed to it rather than read by it. Here,
    # because this is the one function every command's settings pass through.
    from app.extract import ocr

    ocr.configure_device(settings.embed_device)
    return settings


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


#: Directories never worth walking. Not a substitute for Layer 3's configurable
#: exclusions - just enough that pointing this at a project folder is useful.
_SKIP_DIRS = {
    "venv", ".venv", ".git", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "$RECYCLE.BIN",
}


#: Where the window keeps "Folders to index". One key, `|`-separated - the same
#: string `shell._save_roots` writes, and the reason this constant exists rather
#: than the literal appearing in two files.
ROOTS_STATE_KEY = "ui:roots"


def _saved_roots(settings: Settings) -> "list[str]":
    """The folders the window is configured to index, or an empty list.

    **Read-only and guarded.** This runs before the pipeline is built, on a
    store that may not exist yet - a first run has no database - and a missing
    setting is the normal case rather than an error. Anything that goes wrong
    here means "no saved folders", which is exactly what a fresh install has.
    """
    if not Path(settings.fts_db).is_file():
        return []
    try:
        from app.storage.sqlite_store import SqliteStore

        with SqliteStore(settings.fts_db) as store:
            raw = store.get_state(ROOTS_STATE_KEY, "") or ""
    except Exception as exc:                     # noqa: BLE001 - see docstring
        logger.bind(component="cli.index").debug(
            "could not read the saved index folders: {}", exc)
        return []
    return [part.strip() for part in raw.split("|") if part.strip()]


def _iter_targets(paths: Sequence[str],
                  own: "frozenset[str]" = frozenset()) -> "list[Path]":
    """Expand the arguments into files. A folder is walked recursively.

    Unsupported extensions are filtered out *here* rather than being reported as
    skips, because listing every .exe and .dll in a folder as "skipped" would
    bury the failures that actually matter.

    **`own` is the application's own folders, and a walk never enters them.**
    `walker.own_paths` exists because *"the indexer was reading its own log file
    while writing to it"*, and this command walks folders the same way without
    ever having been given the same guard - so pointing `extract` at the project
    folder reads the index, the vector store and the log file being written by
    the very command doing the reading. The standing rule is that a fix for one
    search area is applied to the others; this is the other.

    A file **named explicitly** is still attempted, whatever folder it is in.
    That is a deliberate choice by somebody who wants to see how a log file
    extracts, and it is the sweep, not the intent, that needs the guard.
    """
    from app.extract import extractor_for

    # Compared lower-cased: these are absolute paths from configuration, matched
    # against paths from the filesystem, and on Windows the same directory
    # routinely appears with different casing in the two.
    blocked = frozenset(str(Path(p)).rstrip("\\/").lower() for p in own)

    found: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_file():
            found.append(path)                       # named explicitly: always attempt it
            continue
        if not path.is_dir():
            found.append(path)                       # let the extractor report it missing
            continue

        for directory, subdirectories, filenames in os.walk(path):
            subdirectories[:] = [
                d for d in subdirectories
                if d not in _SKIP_DIRS
                and str(Path(directory, d)).lower() not in blocked
            ]
            if str(Path(directory)).rstrip("\\/").lower() in blocked:
                continue
            for name in sorted(filenames):
                candidate = Path(directory) / name
                if extractor_for(candidate) is not None:
                    found.append(candidate)
    return found


def _preview(text: str, width: int = 160) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def cmd_extract(args: argparse.Namespace) -> int:
    """Extract and chunk files, printing what came out. Never writes anything.

    This is Layer 2's headless entry point - the ground rule that every layer
    ships a CLI before it ships UI. It exists so extraction can be checked
    against real documents rather than only against synthetic fixtures, and so
    "why did that file not come back in a search?" has an answer that does not
    require a debugger.

    It is deliberately **read-only**: no store is opened, nothing is indexed.
    Building the index is Layer 3's job.
    """
    settings = _load(args)
    setup_logging(settings.log_path)
    log = logger.bind(component="cli.extract")

    if not args.paths and not args.mailbox:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.extract",
            key="paths", reason="give a file or folder, or --mailbox for Outlook",
        ), args.json)

    if args.mailbox:
        return _extract_mailbox(args, log)

    from app.index.walker import own_paths

    targets = _iter_targets(args.paths, own_paths(settings))
    if not targets:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.extract",
            key="paths", reason="no supported files found at " + ", ".join(args.paths),
        ), args.json)

    totals: dict[str, Any] = {
        "files_seen": 0, "extracted": 0, "chunks": 0,
        "bytes": 0, "bytes_seen": 0, "skipped_by_code": {},
    }
    started = time.perf_counter()
    chosen = targets[: args.limit] if args.limit else targets
    records = _extract_records(chosen, args, log, totals)

    if args.json or args.out:
        if args.out:
            # Written here, in UTF-8, rather than left to the shell. Windows
            # PowerShell 5.1 redirection (`> file`) emits UTF-16LE with a BOM,
            # which every JSON reader then chokes on - the same encoding trap
            # that killed install.ps1 at parse time.
            out_path = Path(args.out).expanduser()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", encoding="utf-8") as handle:
                summary = _write_extract_json(handle, records, totals, started)
            size = out_path.stat().st_size
            print(f"Wrote {out_path} ({size / 1024:.0f} KB, UTF-8)", file=sys.stderr)
        else:
            summary = _write_extract_json(sys.stdout, records, totals, started)
    else:
        for record in records:
            _print_extract_record(record)
        summary = _extract_summary(totals, started)
        seen_mb = summary["bytes_seen"] / 1_048_576
        read_mb = summary["bytes"] / 1_048_576
        unread = f", {seen_mb - read_mb:.2f} MB skipped" if seen_mb - read_mb > 0.01 else ""
        print(f"{summary['extracted']} extracted, {summary['skipped']} skipped, "
              f"{summary['chunks']} chunks from {read_mb:.2f} MB read{unread} "
              f"in {summary['elapsed_s']}s")
        if summary["throughput_mb_s"] is not None and summary["bytes"] > 0:
            print(f"  {summary['throughput_mb_s']} MB/s, {summary['files_per_s']} files/s "
                  f"(extraction only - embedding is Layer 3 and is far slower)")
        if summary["skipped_by_code"]:
            print(f"  skipped: {summary['skipped_by_code']}")
        print("  Nothing was written. Building the index is Layer 3.")

    # Flush before logging: loguru writes to stderr unbuffered, so without this
    # the summary log line jumps ahead of the report whenever stdout is piped.
    sys.stdout.flush()
    log.info("extract: {}", summary)
    return EXIT_OK if summary["extracted"] else EXIT_ERROR


def _extract_summary(totals: dict[str, Any], started: float) -> dict[str, Any]:
    elapsed = time.perf_counter() - started
    seen = totals["files_seen"]
    return {
        "files_seen": seen,
        "extracted": totals["extracted"],
        "skipped": seen - totals["extracted"],
        "skipped_by_code": totals["skipped_by_code"],
        "chunks": totals["chunks"],
        "bytes": totals["bytes"],
        "bytes_seen": totals["bytes_seen"],
        "elapsed_s": round(elapsed, 3),
        "throughput_mb_s": (round(totals["bytes"] / 1_048_576 / elapsed, 2)
                            if elapsed > 0 else None),
        "files_per_s": round(seen / elapsed, 1) if elapsed > 0 else None,
    }


def _write_extract_json(handle: Any, records: Iterator[dict[str, Any]],
                        totals: dict[str, Any], started: float) -> dict[str, Any]:
    r"""Stream the report as JSON, one record at a time.

    **`results` is written before `summary`** because the summary is not known
    until the last file has been read, and holding every record until then is
    the thing this function exists to avoid: `--chunks --full` over a corpus
    kept the full text of every chunk in memory, so a report over 20GB of
    documents needed 20GB of RAM to print. Both keys are read by name, never by
    position, so the order is a detail of the writing and not of the format.
    """
    handle.write('{\n  "results": [')
    for index, record in enumerate(records):
        handle.write(",\n" if index else "\n")
        handle.write(textwrap.indent(
            json.dumps(record, indent=2, default=str), "    "))
    handle.write("\n  ],\n")
    summary = _extract_summary(totals, started)
    handle.write('  "summary": ')
    handle.write(textwrap.indent(
        json.dumps(summary, indent=2, default=str), "  ").lstrip())
    handle.write("\n}\n")
    return summary


def _print_extract_record(record: dict[str, Any]) -> None:
    """One file's report, printed as soon as it is known.

    Printing per file rather than at the end also means a long run shows
    progress instead of sitting silent.
    """
    if record["status"] == "skipped":
        print(f"SKIP  {record['path']}")
        print(f"      [{record['code']}] {record['message']}")
        if record.get("suggestion"):
            print(f"      FIX: {record['suggestion']}")
        if record.get("detail"):
            print(f"      {_preview(str(record['detail']), 200)}")
        print()
        return

    for document in record["documents"]:
        pages = (f", pages {document['pages'][0]}-{document['pages'][-1]}"
                 if document["pages"] else "")
        print(f"OK    {document['key']}")
        print(f"      {document['characters']:,} chars, {document['segments']} segment(s)"
              f"{pages} -> {document['chunks']} chunk(s)"
              f"  [{record['elapsed_ms']}ms]")
        for key, value in document["meta"].items():
            if value not in (None, "", 0):
                print(f"      {key}: {_preview(str(value), 120)}")
        for warning in document["warnings"]:
            print(f"      WARN [{warning['code']}] {_preview(str(warning['detail']), 200)}")
        for chunk in document["chunk_detail"]:
            page = f" p{chunk['page']}" if chunk["page"] is not None else ""
            print(f"        #{chunk['ordinal']:<3}{page:<5} "
                  f"{chunk['tokens']:>4}tok  [{chunk['char_start']}:{chunk['char_end']}]")
            print(f"        {chunk['text']}")
        if document["text"] is not None:
            print("      --- full text ---")
            print(document["text"])
        print()


def _extract_records(targets: list[Path], args: argparse.Namespace, log: Any,
                     totals: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """One record per file, yielded as it is read. Nothing is accumulated."""
    from app.core.errors import AppErrorException as _AppErrorException
    from app.core.winfs import describe_placeholder, file_attributes, is_cloud_placeholder
    from app.extract import chunk_document, extract

    def skip(record: dict[str, Any], error: Any) -> dict[str, Any]:
        record.update(status="skipped", code=error.code, message=error.message,
                      suggestion=error.suggestion, detail=error.details)
        by_code = totals["skipped_by_code"]
        by_code[error.code] = by_code.get(error.code, 0) + 1
        return record

    for path in targets:
        totals["files_seen"] += 1
        record: dict[str, Any] = {"path": str(path)}
        # Sized before anything can skip it. A 100MB archive that vanishes from
        # the byte accounting because it was skipped makes the totals a lie.
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        record["size_bytes"] = size
        totals["bytes_seen"] += size

        # Checked before opening: reading a placeholder is what triggers the
        # download, so this must happen first or the check is pointless.
        attributes = file_attributes(path)
        if not args.include_cloud and is_cloud_placeholder(path, attributes):
            yield skip(record, make_error(
                "ERR_CLOUD_ONLY", "cli.extract",
                path=str(path), details=describe_placeholder(attributes),
            ))
            continue

        file_started = time.perf_counter()
        try:
            documents = list(extract(path))
        except _AppErrorException as exc:
            log_app_error(exc.error)
            yield skip(record, exc.error)
            continue

        totals["bytes"] += size
        totals["extracted"] += 1

        document_records = []
        for document in documents:
            chunks = chunk_document(document)
            totals["chunks"] += len(chunks)
            document_records.append({
                "key": document.key,
                "source_kind": document.source_kind,
                "characters": len(document.text),
                "segments": len(document.segments),
                "chunks": len(chunks),
                "pages": sorted({s.page for s in document.segments if s.page is not None}),
                "warnings": [
                    {"code": w.code, "detail": w.details} for w in document.warnings
                ],
                "meta": {k: v for k, v in document.meta.items()
                         if not isinstance(v, (list, dict))},
                "chunk_detail": [
                    {
                        "ordinal": c.ordinal,
                        "page": c.page,
                        "tokens": c.tokens,
                        "char_start": c.char_start,
                        "char_end": c.char_end,
                        "text": c.text if args.full else _preview(c.text),
                    }
                    for c in chunks
                ] if args.chunks else [],
                "text": document.text if args.text else None,
            })

        record.update(
            status="extracted",
            elapsed_ms=round((time.perf_counter() - file_started) * 1000, 1),
            documents=document_records,
        )
        yield record


def _extract_mailbox(args: argparse.Namespace, log: Any) -> int:
    """`extract --mailbox`: everything Outlook can reach, still writing nothing.

    Kept separate from the path walk because a mailbox has no paths. Counts by
    store and folder rather than by file, because "how many messages, from
    where" is the question this answers.
    """
    from app.extract.email_pst import drain_busy_folders, iter_mailbox_documents

    started = time.perf_counter()
    per_store: dict[str, int] = {}
    attachments = 0
    total_chunks = 0
    total_chars = 0

    from app.extract import chunk_document

    try:
        for document in iter_mailbox_documents(seen_hashes=set()):
            store = str(document.meta.get("store_name", "unknown"))
            per_store[store] = per_store.get(store, 0) + 1
            if "attachment_name" in document.meta:
                attachments += 1
            total_chars += len(document.text)
            total_chunks += len(chunk_document(document))
    except AppErrorException as exc:
        return _report(exc.error, getattr(args, "json", False))

    busy = drain_busy_folders()
    elapsed = time.perf_counter() - started
    total = sum(per_store.values())
    summary = {
        "messages": total - attachments,
        "attachments": attachments,
        "documents": total,
        "chunks": total_chunks,
        "characters": total_chars,
        "by_store": per_store,
        "unreadable_folders": [b.context.get("folder") for b in busy],
        "elapsed_s": round(elapsed, 2),
    }

    if args.json or args.out:
        payload = json.dumps({"summary": summary}, indent=2, default=str)
        if args.out:
            out_path = Path(args.out).expanduser()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(payload, encoding="utf-8")
            print(f"Wrote {out_path} (UTF-8)", file=sys.stderr)
        else:
            print(payload)
        log.info("extract --mailbox: {}", summary)
        return EXIT_OK if total else EXIT_ERROR

    for store, count in sorted(per_store.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>8,}  {store}")
    print()
    print(f"{summary['messages']:,} messages + {attachments:,} attachments "
          f"-> {total_chunks:,} chunks ({total_chars:,} chars) in {summary['elapsed_s']}s")
    if busy:
        print(f"  {len(busy)} folder(s) could not be read - leave Outlook open and re-run:")
        for error in busy[:5]:
            print(f"    {error.context.get('folder')}")
    print("  Nothing was written. Building the index is Layer 3.")

    sys.stdout.flush()
    log.info("extract --mailbox: {}", summary)
    return EXIT_OK if total else EXIT_ERROR


def cmd_convert(args: argparse.Namespace) -> int:
    """Export a `.pst` to a folder of `.eml` files.

    The permanent escape hatch. Once an archive is EML on disk it needs neither
    Outlook nor libpff ever again - it is a folder of files the ordinary `.eml`
    extractor already handles, and any mail client can open.
    """
    from app.extract import pst_libpff

    settings = _load(args)
    setup_logging(settings.log_path)

    source = Path(args.archive).expanduser()
    if not source.is_file():
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.convert",
            key="archive", reason=f"not found: {source}",
            suggestion="Give the path to a .pst file.",
        ), args.json)

    if not pst_libpff.available():
        return _report(make_error(
            "ERR_OUTLOOK_MISSING", "cli.convert",
            suggestion=(
                "Converting a .pst without Outlook needs libpff, which is not installed. "
                "On Windows it compiles during install and needs Build Tools for Visual "
                "Studio: pip install libpff-python. Alternatively use XstReader, or index "
                "the archive through Outlook with: app.cli index"
            ),
            details="libpff-python is not importable.",
        ), args.json)

    destination = Path(args.out).expanduser() if args.out else source.with_suffix("")
    print(f"Exporting {source.name} -> {destination}")

    def tick(count: int) -> None:
        if not args.quiet:
            print(f"  {count:,} messages…", flush=True)

    written = pst_libpff.export_to_eml(source, destination, on_progress=tick)

    if args.json:
        print(json.dumps({"archive": str(source), "destination": str(destination),
                          "messages": written}, indent=2))
        return EXIT_OK if written else EXIT_ERROR

    print()
    print(f"Wrote {written:,} message(s) to {destination}")
    print(f"  Index them with:  app.cli index \"{destination}\"")
    print("  They need no Outlook and no libpff from here on.")
    return EXIT_OK if written else EXIT_ERROR


class ProgressLine:
    """A single console line that redraws in place, and yields to log output.

    **The problem this solves.** A `\r` progress line and a logger writing to the
    same console fight each other: a warning lands on top of the progress line,
    the carriage return then overwrites the warning, and the result is a mangled
    line that stops updating - which reads exactly like "it stopped working".
    That was the report.

    So anything that writes to the console goes through `interrupt()`, which
    wipes the line first, lets the message land on its own, and repaints. It is
    the same discipline `pip` and `apt` use for the same reason.

    Not a curses dependency and not ANSI cursor codes: one carriage return and
    some spaces, which behaves identically in a plain console, in Windows
    Terminal, and when the output is piped to a file.
    """

    def __init__(self, enabled: bool = True, width: int = 118) -> None:
        self.enabled = enabled and sys.stdout.isatty()
        self.width = width
        self._text = ""

    def update(self, text: str) -> None:
        if not self.enabled:
            return
        self._text = text[: self.width]
        print("\r" + self._text.ljust(self.width), end="", flush=True)

    def clear(self) -> None:
        """Wipe the line so something else can print on it."""
        if self.enabled and self._text:
            print("\r" + " " * self.width + "\r", end="", flush=True)

    def repaint(self) -> None:
        if self.enabled and self._text:
            print("\r" + self._text.ljust(self.width), end="", flush=True)

    def finish(self) -> None:
        self.clear()
        self._text = ""


def _console_sink(progress: ProgressLine):
    """A loguru sink that never lands on top of the progress line."""
    def write(message: Any) -> None:
        progress.clear()
        print(str(message).rstrip(), file=sys.stderr, flush=True)
        progress.repaint()

    return write


def _print_vector_coverage(stats) -> None:
    r"""How many of the passages this run wrote actually got a vector.

    **The number whose absence let the embedding gap run for weeks.** A run that
    wrote 3,355 chunks and 0 vectors printed `-> 3,355 chunks` and stopped
    there, which reads as success. The two stores were only ever compared
    afterwards, by `stats` or `doctor`, and nobody runs a diagnostic against a
    run that told them it worked.

    Silent when they agree, deliberately. A line reading "3,355 of 3,355" on
    every healthy run is the noise that teaches people to skim the summary,
    which is how the real one would be missed.
    """
    chunks = int(getattr(stats, "chunks", 0) or 0)
    vectors = int(getattr(stats, "vectors", 0) or 0)
    if not chunks or vectors >= chunks:
        return
    share = vectors / chunks * 100
    print()
    print(f"Vectors   {vectors:,} of {chunks:,} passages embedded ({share:.0f}%)")
    if getattr(stats, "embed_failures", 0):
        print(f"          {stats.embed_failures} embedding batch(es) failed. Those "
              f"files stay PENDING and the next run retries them.")
    print("          Meaning-based search covers only that much of this run; "
          "keyword search is unaffected.")
    print("          `app.cli reembed` fills the gap without re-reading anything.")


def cmd_index(args: argparse.Namespace) -> int:
    """Build or update the index. Layer 3's entry point.

    Unlike `extract`, this one writes - so it takes the single-instance lock.
    Two copies indexing into one SQLite file is exactly the corruption the
    mutex exists to prevent.
    """
    from app.index.clip_embedder import ClipImageEmbedder
    from app.index.embedder import Embedder
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.walker import WalkConfig, own_paths
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import ImageVectorStore, VectorStore

    settings = _load(args)
    setup_logging(settings.log_path)
    log = logger.bind(component="cli.index")

    roots = [Path(root).expanduser() for root in (args.roots or [])]
    from_settings = False
    if not roots:
        # **Falls back to what the window is configured to index.**
        #
        # The same setting had two sources of truth: the window saves "Folders
        # to index" under `ui:roots`, and this command only ever read its own
        # arguments. So a command-line run - including the one somebody uses to
        # verify a migration - indexed whatever folder was typed rather than
        # what the application is actually set up to index, and there was no
        # way to tell the two apart afterwards. Verifying the wrong thing and
        # believing it was the right thing is the expensive kind of wrong.
        #
        # Explicit arguments still win: naming a folder is an instruction, and
        # a command that quietly ignored it in favour of a saved setting would
        # be the same bug pointing the other way.
        saved = _saved_roots(settings)
        roots = [Path(root).expanduser() for root in saved]
        from_settings = bool(roots)

    if not roots:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.index",
            key="roots", reason="no folders to index",
            suggestion=r'Name them here - app.cli index "D:\SearchData" - or '
                       r'set them once on the Settings page, under "Folders to '
                       r'index", and run this with no arguments.',
        ), args.json)

    if from_settings and not args.json:
        # **Said out loud.** A command that silently uses a setting is a command
        # whose output cannot be attributed to anything.
        print("Indexing the folders saved in Settings:")
        for root in roots:
            print(f"  {root}")

    missing = [root for root in roots if not root.exists()]
    if missing:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.index",
            key="roots", reason=f"does not exist: {', '.join(str(m) for m in missing)}",
            suggestion="Check the path and the drive. A folder on a disconnected drive looks "
                       "exactly like a folder that was deleted.",
        ), args.json)

    limits = limits_from_settings(settings)

    # **The tuning mode reaches the run, not only the screen.** Before this the
    # panel resolved `0` to `Auto (4)` for display and the run read the literal
    # `0`, so switching modes changed what was shown and nothing about what
    # happened. `resolve_for_run` is the same arithmetic the screen uses - one
    # function, so the two cannot drift.
    from app.index.resolve import resolve_for_run

    with SqliteStore(settings.fts_db) as _store:
        tuned = resolve_for_run(settings, _store)
    limits = replace(limits, workers=tuned.workers)
    _tuning_log = logger.bind(component="cli.index")
    for key, why in tuned.why.items():
        _tuning_log.debug("{}: {}", key, why)

    # `--workers` still wins: a flag typed on this command is a decision about
    # this run, and a tuning mode is a standing preference.
    if args.workers:
        limits = replace(limits, workers=args.workers)
    if args.memory_mb:
        limits = replace(limits, memory_mb=args.memory_mb)
    if args.cpu_percent is not None:
        limits = replace(limits, cpu_percent=args.cpu_percent)
    if args.full_speed:
        # An explicit opt-out for a machine nobody is using. Named for what it
        # costs rather than what it gives: this is the setting that makes the
        # computer unusable while it runs.
        limits = replace(
            limits, cpu_percent=0, pause_on_battery=False, low_priority=False,
            workers=args.workers or max(1, (os.cpu_count() or 2) - 1),
        )

    config = PipelineConfig(
        walk=WalkConfig(
            roots=roots,
            priority_roots=[Path(p).expanduser() for p in (args.first or [])],
            include_cloud=args.include_cloud,
            # Never index our own index, logs, cache or models. Indexing the
            # project folder had the run reading the log file it was writing.
            exclude_paths=own_paths(settings),
            # Every file gets a row, whether or not anything can read it - see
            # `WalkConfig.name_only`. Off makes the walk behave as it did.
            name_only=settings.index_name_only,
        ),
        limits=limits,
        min_free_gb=settings.min_free_gb,
        required_free_gb=settings.required_free_gb,
        verify_hash=not args.fast,
        prune_missing=not args.no_prune,
        force=bool(getattr(args, "force", False)),
        retry_skipped=bool(getattr(args, "retry_skipped", False)),
        # A folder marked as an archive is walked once and then checked
        # cheaply - see `app/index/archives.py`. `--all-roots` is the escape
        # hatch that ignores the modes entirely without touching the records.
        ocr_mode=_ocr_mode(args, settings),
        archives=not bool(getattr(args, "all_roots", False)),
        recheck_archives=bool(getattr(args, "recheck_archives", False)),
        recheck_days=settings.archive_recheck_days,
        # Resolved for this machine and this mode, above.
        embed_batch=tuned.embed_batch,
        dedup_chunks=settings.embed_dedup,
        two_phase=settings.index_two_phase,
        bulk_fts=settings.index_bulk_fts,
    )

    embedder = Embedder.from_settings(settings, threads=tuned.onnx_threads)
    # Work order 0h §1c item 3. **The same construction, at the same site
    # that already builds `embedder`**, so indexing from the command line
    # writes the CLIP vectors the search side (`cmd_search`, `cmd_shell`,
    # `cmd_evaluate`, the window) can now query - verified with `grep -rn
    # "image_embedder=\|image_vectors=" app/` before this change, which
    # returned only test call sites: no real run had ever written one.
    # `ClipImageEmbedder.from_settings` is already lazy (nothing loads until
    # the first image is embedded), so building it unconditionally here
    # costs nothing on a run that never reaches an image file.
    image_embedder = ClipImageEmbedder.from_settings(settings)

    progress = ProgressLine(enabled=not args.quiet and not args.json)

    # Route console logging through the progress line, so a warning about one
    # unreadable file cannot leave the heartbeat mangled and apparently frozen.
    if progress.enabled:
        # Order matters: `setup_logging` clears every handler, so the progress
        # sink has to be added *after* it, not before. Doing it the other way
        # round silently removes the sink and the warnings vanish entirely -
        # which is worse than the mangled line it was meant to fix.
        # `force=True` matters. `setup_logging` is idempotent by design - both
        # the CLI and the UI call it - so without it this second call returns
        # immediately, the original INFO console sink survives, and every line
        # gets printed twice: once by the sink that respects the progress line
        # and once by the sink that walks straight over it.
        setup_logging(settings.log_path, console_level="CRITICAL", force=True)
        logger.add(
            _console_sink(progress), level="INFO",
            format="{time:HH:mm:ss} {level: <7} {message}",
        )

    def show(stats) -> None:
        if args.json:
            return
        line = (f"  {stats.indexed:>7,} docs  {stats.unchanged:>6,} unchanged  "
                f"{stats.unchanged_documents:>7,} already current  "
                f"{stats.chunks:>8,} chunks")

        if getattr(stats, "paused", False):
            # **A pause with nothing said is a hang, as far as anyone watching
            # is concerned.** The window was given this earlier today; the
            # command line builds its own line and was not, so a real run sat
            # on an unchanging line for minutes while the governor waited for
            # memory to settle - and was reported as stuck. It was working.
            reason = getattr(stats, "pause_reason", "") or "waiting for resources"
            progress.update(f"{line}  | PAUSED - {reason[:70]}")
            return

        recent = getattr(stats, "recent_files_per_minute", None)
        if recent is not None:
            # **The last fifteen minutes, not the lifetime average.** On a run
            # of days the average stops moving, so a run that has slowed to a
            # crawl reports the rate it managed on the first morning.
            line += f"  | {recent:,.0f}/min"

        if stats.current:
            # Naming the file being read is what separates "working on a big
            # archive" from "hung". A 100MB .pst is one file and can hold the
            # line for minutes.
            waited = time.monotonic() - (stats.current_since or time.monotonic())
            line += f"  | {stats.current[:34]}"
            if stats.current_item:
                line += f" [{stats.current_item:,}]"
            if waited > 5:
                line += f" {waited:,.0f}s"
        progress.update(line)

    # **The run lock, not the process lock.** This used to take
    # `SingleInstance`, which the window holds for its whole lifetime - so
    # `app.cli index` could not run at all while Leasha was open, even though
    # the window was only reading. What must not overlap is two *writers*, and
    # that hazard lasts exactly as long as this block. See `core/run_lock.py`.
    with SqliteStore(settings.fts_db) as store, \
            IndexRunLock(store, owner=COMMAND_LINE), \
            VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors, \
            ImageVectorStore(settings.vector_path) as image_vectors:
        pipeline = Pipeline(
            store, vectors, embedder, config,
            image_embedder=image_embedder, image_vectors=image_vectors,
        )
        stats = pipeline.run(on_progress=None if args.quiet else show)

    payload = stats.as_dict()
    if args.json:
        print(json.dumps(payload, indent=2))
        return EXIT_ERROR if stats.stopped_early else EXIT_OK

    progress.finish()
    print()
    # Documents, not files. A .pst is one file and thousands of messages, and
    # calling them all "files" produced summaries like "seen 8, indexed 17"
    # where the two numbers were different units.
    print(f"Indexed   {stats.indexed:,} document(s) -> {stats.chunks:,} chunks")
    _print_vector_coverage(stats)
    print(f"Files     {stats.seen:,} seen, {stats.unchanged:,} unchanged")
    if stats.unchanged_documents:
        print(f"          {stats.unchanged_documents:,} document(s) inside them were "
              f"already up to date")
    for notice in getattr(stats, "notices", ()):
        print()
        print(f"Note      {notice}")
    if stats.skipped_roots:
        # **Said before the totals, not after.** A run that indexed 40 files
        # because three of its four folders were skipped needs to say so where
        # somebody reading the numbers cannot miss it.
        print()
        print(f"Archives  {len(stats.skipped_roots)} folder(s) were not walked at all:")
        for row in stats.skipped_roots:
            when = (
                time.strftime("%Y-%m-%d", time.localtime(row["archived_at"]))
                if row.get("archived_at") else "an unknown date"
            )
            print(f"  {row['root']}")
            print(f"    {row['reason']} - {row.get('files', 0):,} file(s), "
                  f"fully indexed on {when}")
        print("    Use --recheck-archives to walk them in full now.")
        print()
    print(f"Skipped   {stats.skipped:,}   Deleted {stats.deleted:,}")
    print(f"Read      {stats.bytes_read / 1_048_576:,.1f} MB in {stats.elapsed_s:,.1f}s")
    if stats.pauses:
        # Said plainly, because a four-hour run that was mostly waiting looks
        # identical to a four-hour run that was slow - and the fix is opposite.
        print(f"Waited    {stats.paused_seconds / 60:,.1f} min across {stats.pauses} "
              f"pause(s) to stay out of the way")
    print(f"          {stats.files_per_minute:,.0f} files/min, {stats.mb_per_minute:,.1f} MB/min")
    if stats.chunks_deduped:
        # §6e's number, said every run. Whether repeated text is worth
        # avoiding is a question about somebody's corpus, and this is the only
        # place the answer ever appears.
        share = stats.chunks_deduped / max(1, stats.chunks + stats.chunks_deduped)
        print(f"Repeated  {stats.chunks_deduped:,} passage(s) were already "
              f"embedded this run ({share:.0%}) and were not sent again")
    if stats.stages:
        # §6a, in the shape §4f shows: proportions, because the question this
        # answers is "what should I change" and that is about shares.
        total = sum(stats.stages.values()) or 1.0
        shares = " · ".join(f"{name} {seconds / total:.0%}"
                            for name, seconds in stats.stages.items())
        print(f"Time      {shares}")
        from app.index.stages import advice

        said = advice(stats.stages, on_gpu=settings.embed_device == "gpu")
        if said:
            print(f"          {said}")
    if _images_pass_follows(settings) and _ocr_mode(args, settings) == "text":
        # **Said, not started.** A second pass over a scanned corpus is hours;
        # launching it without asking, from a command somebody ran to index
        # their documents, is the kind of surprise that gets an application
        # uninstalled. The window schedules it; the command line names it.
        print()
        print("Images    Set to be read after the run. Start the second pass "
              "with:  leasha index --only-ocr")
    if stats.name_only:
        # **Not "skipped".** Nothing went wrong: there is no reader for a
        # `.mp4`. Reported with the types, because that is the number that
        # tells somebody their corpus is 30% `.dwg`.
        top = sorted(stats.name_only_by_ext.items(),
                     key=lambda row: row[1], reverse=True)[:6]
        kinds = ", ".join(f".{ext} x{count:,}" for ext, count in top)
        print()
        print(f"By name   {stats.name_only:,} file(s) indexed by name only - "
              f"nothing can read them")
        print(f"          {kinds}")
        print("          They are findable by name; their contents are not "
              "searchable.")

    pictures = stats.warned_by_code.get("ERR_MOSTLY_PICTURES", 0)
    if pictures:
        # **The evidence for a decision, not a complaint.** From
        # `WORKORDER-202626081052-ocr-strategy.md` §5: list the affected
        # documents rather than reading them, then decide with the number in
        # hand. Twenty decks: open them. Two thousand: no OCR strategy was ever
        # going to help, and the honest answer is that they are findable by
        # name and title only.
        print()
        print(f"Pictures  {pictures:,} document(s) are mostly images rather than text")
        print("          Their titles and headings are searchable; the pictures are not.")
        print("          Reading them would mean OCR per image - see the OCR work order.")

    held = stats.skipped_by_code.get("ERR_OCR_HELD", 0)
    if held:
        # **Named separately from the failures, because it is not one.** A
        # queue of 40,000 images reported inside "skipped by cause" reads as
        # 40,000 things that went wrong.
        print()
        print(f"Held      {held:,} image(s) are queued for the images pass -")
        print("          nothing is wrong with them and nothing was lost.")
        print("          Run: app.cli index --only-ocr")
    if stats.skipped_by_code:
        print(f"Skipped by cause: {stats.skipped_by_code}")
        print("  Run `app.cli stats` to see the totals, or check logs\\errors for the detail.")
    if stats.stopped_early is not None:
        print()
        print(stats.stopped_early.render())
        print("  Everything indexed so far is saved. Re-run to carry on.")
        return EXIT_ERROR

    log.info("index complete: {}", payload)
    return EXIT_OK


def _ocr_mode(args: argparse.Namespace, settings: Settings) -> str:
    """Which pass this run is: `both`, `text` or `images`.

    A flag on the command line wins over the setting, because naming one is an
    instruction. Without a flag the setting decides, so the choice made once in
    Settings applies to the scheduled runs as well - which is the whole reason
    it is a setting and not only a flag.
    """
    if getattr(args, "only_ocr", False):
        return "images"
    if getattr(args, "skip_ocr", False):
        return "text"

    from app.index.pipeline import OCR_MODES

    # **`INDEX_OCR_PASS` decides *when*, `INDEX_OCR_MODE` decides *what*.**
    # They meet here because a run is only ever one pass: asking for the images
    # to be done after the run means this run is the text one, and the images
    # pass is a second `--only-ocr` run. Neither setting can express that
    # alone, which is why the schedule half is its own control rather than a
    # fourth value squeezed into the mode.
    schedule = str(getattr(settings, "index_ocr_pass", "with-run")
                   or "with-run").strip().lower()
    if schedule in ("after-run", "manual"):
        return "text"

    stored = str(getattr(settings, "index_ocr_mode", "both") or "both").strip().lower()
    return stored if stored in OCR_MODES else "both"


def _images_pass_follows(settings: Settings) -> bool:
    """Should an images pass be started once the text pass finishes?

    `after-run` yes, `manual` no. The difference is the whole point of having
    two words for it: somebody with a scanned corpus wants the text usable
    today *and* the images eventually, and somebody on a laptop wants to choose
    the evening it happens.
    """
    return str(getattr(settings, "index_ocr_pass", "with-run")
               or "with-run").strip().lower() == "after-run"


def cmd_scan(args: argparse.Namespace) -> int:
    r"""Count the corpus without indexing it. Reads no document, writes no index.

    **The command that has to run before any of the others are worth planning.**
    Every estimate about a long run - days, index size, whether OCR dominates -
    comes from the mix of file types, and until this existed nobody had counted
    the mix. A 600GB corpus was projected at five days from a 97-second sample;
    it is three days or fifteen depending on how much of it is scanned images,
    and that is not a detail to discover on day four.

    It only `stat()`s, so it finishes in minutes on 600GB. The single exception
    is a sample of PDFs, opened to read the text layer of their first two pages,
    because "how much of this is photographs?" cannot be answered any other way
    and it is the most expensive fact about the corpus.

    The total is saved, so the *next* index run has a real percentage from its
    first tick rather than a bar that spins for a week.
    """
    from app.index.scan import SCAN_STATE_KEY, ScanConfig, format_report, scan
    from app.index.walker import own_paths

    settings = _load(args)
    setup_logging(settings.log_path)

    roots = [Path(root).expanduser() for root in (args.roots or [])]
    from_settings = False
    if not roots:
        # Same fallback as `index`, and for the same reason: a measurement of a
        # folder nobody indexes is a measurement of the wrong thing.
        roots = [Path(root).expanduser() for root in _saved_roots(settings)]
        from_settings = bool(roots)

    if not roots:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.scan",
            key="roots", reason="no folders to scan",
            suggestion=r'Name them here - app.cli scan "D:\SearchData" - or set '
                       r'them once on the Settings page, under "Folders to '
                       r'index", and run this with no arguments.',
        ), args.json)

    if from_settings and not args.json:
        print("Scanning the folders saved in Settings:")
        for root in roots:
            print(f"  {root}")
        print()

    config = ScanConfig(
        roots=roots,
        exclude_paths=own_paths(settings),
        include_excluded=bool(args.all),
        sample_pdfs=0 if args.no_sample else int(args.sample_pdfs),
        sample_archives=0 if args.no_sample else int(args.sample_archives),
    )

    progress = ProgressLine(enabled=not args.quiet and not args.json)

    def show(result) -> None:
        from app.index.scan import human_bytes

        progress.update(
            f"  {result.total.files:>12,} files  "
            f"{human_bytes(result.total.bytes):>12}  "
            f"{result.elapsed_s:,.0f}s"
        )

    result = scan(config, on_progress=None if args.quiet else show)
    progress.finish()

    # **Saved even when the scan is printed as JSON.** The number exists to make
    # the *next* run's progress bar honest, and a person who scans with --json
    # for a report has the same week-long run ahead of them as anybody else.
    saved = _save_scan_total(settings, result)

    if args.json:
        payload = result.as_dict()
        payload["saved_for_progress"] = saved
        print(json.dumps(payload, indent=2))
        return EXIT_OK

    print()
    for line in format_report(result, mb_per_minute=float(args.mb_per_minute or 0)):
        print(line)
    print()
    if saved:
        print(f"  Saved as {SCAN_STATE_KEY}, so the next index run shows a real")
        print("  percentage from its first tick instead of an endless bar.")
    else:
        print("  Not saved - there is no index database yet. Run `app.cli init`")
        print("  first if you want the next run's progress bar to use this total.")
    return EXIT_OK


def _save_scan_total(settings: Settings, result: Any) -> bool:
    """Record the scan so `progress_for` has a denominator. Never fatal.

    A failure here costs a progress bar, not a measurement - and the whole
    point of `scan` is that it can be run on a machine whose index has not been
    created yet.
    """
    from app.index.scan import SCAN_STATE_KEY

    if not Path(settings.fts_db).is_file():
        return False
    try:
        from app.storage.sqlite_store import SqliteStore

        with SqliteStore(settings.fts_db) as store:
            store.set_states({
                SCAN_STATE_KEY: json.dumps({
                    "at": int(time.time()),
                    "roots": list(result.roots),
                    "files": result.indexable.files,
                    "bytes": result.indexable.bytes,
                }),
            })
        return True
    except Exception as exc:                         # noqa: BLE001 - see the docstring
        logger.bind(component="cli.scan").warning(
            "the scan total was not saved, so the next run's progress bar will "
            "grow its own denominator: {}", exc)
        return False


def _scan_for_repos(roots: Sequence[Path], *, as_json: bool = False) -> int:
    """Which repositories a walk *would* find, without indexing anything.

    `app.cli repos` lists what the last index run attributed, which is a poor
    way to answer "are there any git repositories in my search folders" - it
    requires a full run first, and on a fresh v6 index it is empty and
    indistinguishable from "none".

    Read-only: nothing is written, nothing is embedded, no file is opened
    except a `.git` pointer file. It is the detection half of the walk, run on
    its own.
    """
    from app.index.walker import WalkConfig, enclosing_repo, repo_kind_at, walk

    found: dict[str, dict[str, Any]] = {}
    missing: list[str] = []

    for root in roots:
        if not root.exists():
            missing.append(str(root))
            continue

        # A root may sit *below* a repository root, in which case nothing
        # beneath it has a `.git` and a downward walk finds nothing.
        above = enclosing_repo(root)
        if above is not None:
            entry = {"root_path": str(above),
                     "kind": repo_kind_at(above) or "work"}
            # **"Is the root" and "is above the root" are different facts.**
            # The first version said "contains the indexed folder D:\SearchData"
            # about D:\SearchData, which reads as a bug in the scan rather than
            # as the significant thing it is.
            if str(above).rstrip("\\/").lower() == str(root).rstrip("\\/").lower():
                entry["is_root"] = True
            else:
                entry["encloses"] = str(root)
            found.setdefault(str(above), entry)

        sink: dict[str, str] = {}
        # `extensions` is a frozenset with one impossible member rather than
        # empty: empty means "every registered extractor's extensions", which
        # would stat every file in the tree for no reason. Detection does not
        # look at files.
        list(walk(WalkConfig(roots=[root],
                             extensions=frozenset({".__none__"}),
                             repo_sink=sink)))
        for path, kind in sink.items():
            found.setdefault(path, {"root_path": path, "kind": kind})

    rows = sorted(found.values(), key=lambda r: r["root_path"].lower())

    if as_json:
        print(json.dumps({"scanned": [str(r) for r in roots],
                          "not_found": missing,
                          "repositories": rows, "count": len(rows)}, indent=2))
        return EXIT_OK

    for path in missing:
        print(f"  ! {path} does not exist.")

    if not rows:
        print("No git repositories found under:")
        for root in roots:
            print(f"    {root}")
        print()
        print("  Nothing is wrong - most folders have none. `repo:` and the Code")
        print("  tab will simply have nothing to show for these.")
        return EXIT_OK

    width = max(len(r["kind"]) for r in rows)
    print(f"Found {len(rows)} git repositor{'y' if len(rows) == 1 else 'ies'}:")
    print()
    for row in rows:
        note = ""
        if row.get("is_root"):
            note = "   <- the indexed folder itself"
        elif row.get("encloses"):
            note = f"   (contains the indexed folder {row['encloses']})"
        print(f"  {row['kind']:<{width}}  {row['root_path']}{note}")

    # **An indexed root that is itself a repository swallows the `code` scope.**
    #
    # Attribution is by longest matching prefix, so every file underneath gets
    # that repository's id - the spreadsheets, the PDFs, the mail. `scope:code`
    # then means "everything", which is not what it is for: the scope exists to
    # separate a work project from the same words in a document.
    #
    # Not an error and not something to fix automatically. It may be exactly
    # what somebody wants. But it is invisible from the outside, and finding
    # out by wondering why the Code tab lists your holiday photos is worse.
    swallowing = [r for r in rows if r.get("is_root")]
    if swallowing:
        print()
        for row in swallowing:
            print(f"  ! {row['root_path']} is a git repository *and* an indexed folder.")
        print("    Every file beneath it - documents, spreadsheets, mail - will be")
        print("    attributed to it, so `scope:code` will match your whole corpus")
        print("    rather than just code. The nested repositories below it are")
        print("    still attributed to themselves, which is correct.")
        print()
        print("    Fine if deliberate. If that `.git` is there by accident, removing")
        print("    it and re-indexing makes the Code scope mean what it says.")

    print()
    print("  These are detected during a normal index run - nothing extra to do.")
    print("  Afterwards: app.cli repos, or `repo:<name>` in a search.")
    return EXIT_OK


def cmd_gitsearch(args: argparse.Namespace) -> int:
    r"""Search a repository - its files, its branches, its whole history.

    The switches are `GitSearch.txt`'s, in the `/` grammar the rest of this
    application already uses:

        app.cli gitsearch --repo D:\Project "CustomerId /history /extension cs"
        app.cli gitsearch --repo D:\Project "/class OrderService /branch develop"
        app.cli gitsearch --repo D:\Project "ApiKey /history /removed-only"

    **This ships before the UI**, per non-negotiable 8: every switch can be
    checked headless, and the window has something to compare against when it
    disagrees.

    `--measure` is the other thing this command does - see `cmd_gitmeasure`.
    """
    if getattr(args, "measure", False):
        return cmd_gitmeasure(args)

    from app.search.gitquery import build, parse_git_query
    from app.search.gitsearch import (
        DEFAULT_ROW_LIMIT, SEARCH_TIMEOUT_S, git_version, is_repository,
        run_query,
    )

    settings = _load(args)
    setup_logging(settings.log_path)

    repo = Path(args.repo).expanduser()
    if not repo.is_dir():
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.gitsearch",
            key="repo", reason=f"'{repo}' is not a folder",
            suggestion=r'Point it at a git checkout: app.cli gitsearch --repo "D:\Project" "pattern"',
        ), args.json)

    if git_version() is None:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.gitsearch",
            key="git", reason="git was not found on PATH",
            suggestion="Install git, or add it to PATH, and run this again.",
        ), args.json)

    if not is_repository(repo):
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.gitsearch",
            key="repo", reason=f"'{repo}' is not a git repository",
            suggestion="Run `app.cli repos --scan <folder>` to see which folders are.",
        ), args.json)

    query = parse_git_query(args.pattern, depth=args.depth)
    plan = build(query)

    if not args.json and plan.slow:
        # Said before it starts, not after. `git log -S` diffs every commit it
        # walks, so a history search is seconds to minutes, and a command that
        # goes quiet for two minutes reads as one that has hung.
        print(f"Searching {plan.explain}.")
        print("  This reads history, so it is the slow one - git diffs every "
              "commit it walks.", flush=True)

    found = run_query(repo, query, limit=args.limit or DEFAULT_ROW_LIMIT,
                      timeout=args.timeout or SEARCH_TIMEOUT_S)

    if args.json:
        print(json.dumps(found.as_dict(), indent=2))
        return EXIT_OK if found.ok else EXIT_ERROR

    if not found.ok:
        print(f"git could not run that search: {found.error}", file=sys.stderr)
        print(f"  command: {' '.join(found.command)}", file=sys.stderr)
        return EXIT_ERROR

    for row in found.rows:
        if row.kind == "commit":
            print(f"  {row.commit[:8]}  {row.date}  {row.author[:20]:<20}  {row.subject}")
        elif row.kind == "change":
            print(f"  {row.commit[:8]}  {row.status}  {row.path}"
                  + (f"   ({row.text})" if row.text else ""))
        elif row.status in ("+", "-"):
            print(f"  {row.status} {row.commit[:8]}  {row.path}: {row.text.strip()}")
        else:
            where = f"{row.commit}:" if row.commit else ""
            print(f"  {where}{row.path}:{row.line_no}: {row.text.strip()}")

    print()
    print(f"  {len(found.rows):,} result{'s' if len(found.rows) != 1 else ''} "
          f"{found.explain}, in {found.elapsed_s:.2f}s")
    if found.truncated:
        # **Never a quiet truncation.** A capped list that does not say so is a
        # wrong answer, and this one is capped precisely because a common word
        # can match half a repository.
        print(f"  Stopped at {args.limit or DEFAULT_ROW_LIMIT:,} results - "
              f"narrow it with /path, /extension or /depth to see the rest.")
    if not found.rows:
        print(f"  Nothing matched. The command was: {' '.join(found.command)}")
    return EXIT_OK


def cmd_gitmeasure(args: argparse.Namespace) -> int:
    """Time git history search. **A measurement, not a feature.**

    `WORKORDER-git-search-backend.md` §14 makes phase 2 conditional on these
    numbers, and `HANDOFF-ui-to-backend.md` B4 asks the same question from the
    other side. Searching a full history is O(commits x changed files) against
    a contract of p95 under 300ms warm, so there are two possible designs and
    only a measurement distinguishes them.

    Nothing here is reachable from a search. It is a command somebody runs
    deliberately, once, to decide what gets built.
    """
    from app.search.gitsearch import DEFAULT_DEPTHS, DEFAULT_TIMEOUT_S, measure

    settings = _load(args)
    setup_logging(settings.log_path)

    repo = Path(args.repo).expanduser()
    if not repo.is_dir():
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.gitsearch",
            key="repo", reason=f"'{repo}' is not a folder",
            suggestion=r'Point it at a git checkout: app.cli gitsearch --repo "D:\Project" "pattern"',
        ), args.json)

    depths = DEFAULT_DEPTHS
    if args.depths:
        try:
            depths = tuple(int(part) for part in args.depths.split(",") if part.strip())
        except ValueError:
            return _report(make_error(
                "ERR_CONFIG_INVALID", "cli.gitsearch",
                key="--depths", reason=f"'{args.depths}' is not a list of numbers",
                suggestion="Give commit counts, comma separated: --depths 1000,10000,50000",
            ), args.json)

    if not args.json:
        print(f"Timing history search in {repo}")
        print(f"  pattern: {args.pattern!r}   depths: "
              f"{', '.join(f'{d:,}' for d in depths)}")
        print("  git log -S diffs every commit, so this is deliberately the slow "
              "case. Minutes is a result, not a failure.", flush=True)

    found = measure(repo, args.pattern, depths=depths, rev=args.rev,
                    timeout=args.timeout or DEFAULT_TIMEOUT_S)

    if args.json:
        print(json.dumps(found.as_dict(), indent=2))
        return EXIT_OK if found.git else EXIT_ERROR

    if not found.git:
        for note in found.notes:
            print(f"  ! {note}")
        return EXIT_ERROR

    print()
    print(f"  {found.git}   {found.commits_total:,} commits in this repository")
    print()
    print(f"  {'MODE':<7} {'DEPTH':>8} {'SEARCHED':>9} {'ELAPSED':>9} {'MATCHES':>8}  ")
    for row in found.rows:
        elapsed = f"{row['elapsed_s']:.2f}s" if row["ok"] else "failed"
        flag = "" if row.get("representative", True) else "  (whole history)"
        print(f"  {row['mode']:<7} {row['depth_asked']:>8,} "
              f"{row['commits_searched']:>9,} {elapsed:>9} "
              f"{row['matches']:>8,}{flag}")

    if found.peak_rss_mb is not None:
        print()
        print(f"  peak RSS (this process)  {found.peak_rss_mb:,.0f}MB")
    for note in found.notes:
        print(f"  note: {note}")

    print()
    print("  Write these into HANDOFF.md. They decide whether history search can")
    print("  live behind the Enter key or has to be its own cancellable job.")
    return EXIT_OK


def _forget_repo(settings: Any, root: str, *, as_json: bool = False) -> int:
    r"""Disown one repository, and say exactly what changed.

    From `WORKORDER-202626081149-code-tab.md` §2. Attribution was a one-way
    door: nothing pruned `repos`, nothing set `files.repo_id` back to NULL, and
    the COALESCE in `upsert_file` meant even `index --force` could not clear
    one. The only route back was deleting the whole index, for what is a
    bookkeeping error.

    **Nothing is deleted and nothing is re-indexed.** Every file keeps its row,
    its chunks and its vectors; what it loses is the claim that it is code. That
    is why this is safe to offer as a one-line command rather than behind a
    confirmation: the expensive half of the accident is the re-index, and this
    avoids it entirely.
    """
    from app.storage.sqlite_store import SqliteStore

    target = str(Path(root).expanduser()).rstrip("\\/")
    with SqliteStore(settings.fts_db) as store:
        released = store.forget_repo(target)
        # Recorded even when nothing was attributed: the point is that the next
        # walk must not adopt it, and a folder can be a repository the index has
        # not reached yet.
        store.ignore_repo_root(target)
        remaining = len(store.repos_list())

    if as_json:
        print(json.dumps({"forgot": target, "files_released": released,
                          "repositories_left": remaining}, indent=2))
        return EXIT_OK

    if released:
        print(f"Released {released:,} file(s) from {target}.")
    else:
        print(f"{target} had no files attributed to it.")
    print("They are still indexed and still searchable - they are no longer code.")
    print(f"It will not be adopted again. Undo with: repos --remember \"{target}\"")
    return EXIT_OK


def cmd_repos(args: argparse.Namespace) -> int:
    """Every code repository found under an indexed root.

    Ships before the UI per non-negotiable 8, so repository detection can be
    checked headless and so the Code tab has something to compare against.

    An index with no repositories is not an error - most machines have none -
    so it says so in one line and exits 0.
    """
    from app.storage.sqlite_store import SqliteStore

    settings = _load(args)
    setup_logging(settings.log_path)

    if args.scan:
        return _scan_for_repos([Path(p).expanduser() for p in args.scan],
                               as_json=args.json)

    if args.forget:
        return _forget_repo(settings, args.forget, as_json=args.json)

    if args.remember:
        with SqliteStore(settings.fts_db) as store:
            known = store.unignore_repo_root(args.remember)
        print(f"{args.remember} may be adopted again on the next index run."
              if known else f"{args.remember} was not being ignored.")
        return EXIT_OK

    with SqliteStore(settings.fts_db) as store:
        repos = store.repos_list()
        ignored = store.ignored_repo_roots()

    if args.json:
        print(json.dumps({"repositories": repos, "count": len(repos)},
                         indent=2, default=str))
        return EXIT_OK

    if not repos:
        print("No code repositories found under the indexed folders.")
        if ignored:
            print()
            print("Disowned, and never adopted again:")
            for root in ignored:
                print(f"  {root}")
            print("  Undo with: app.cli repos --remember <root>")
        return EXIT_OK

    name_width = max(len("NAME"), max(len(str(r["name"])) for r in repos))
    kind_width = max(len("KIND"), max(len(str(r["kind"])) for r in repos))
    print(f"{'NAME':<{name_width}}  {'KIND':<{kind_width}}  {'FILES':>7}  LAST SEEN            ROOT")
    for repo in repos:
        seen = repo.get("last_seen")
        stamp = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(int(seen)))
            if seen else "-"
        )
        print(
            f"{repo['name']:<{name_width}}  {repo['kind']:<{kind_width}}  "
            f"{int(repo['files']):>7,}  {stamp:<19}  {repo['root_path']}"
        )

    attributed = sum(int(r["files"]) for r in repos)
    print()
    print(f"  {len(repos)} repositor{'y' if len(repos) == 1 else 'ies'}, "
          f"{attributed:,} indexed files attributed.")
    if attributed == 0:
        print("  Detected on the last walk but nothing attributed yet - "
              "run `app.cli index` again to attribute existing files.")
    return EXIT_OK


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
        connected_volumes, delete_volume, reconcile_moves,
        refresh_volume_statuses, resolve_file_path,
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

    if args.scan:
        return _offline_media_scan(settings, Path(args.scan).expanduser(),
                                   name=args.name, description=args.description,
                                   as_json=args.json)

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
                        description: Optional[str], as_json: bool) -> int:
    r"""The first Scan of a new source: a drive, or a network share
    (202626270514 1a - identity resolved here, the mapped letter if any
    discarded immediately after). 2b: asks for a name; here, requires one,
    because there is no dialog to ask twice."""
    from app.index.offline_media import identify_source
    from app.storage.sqlite_store import SqliteStore

    if not name:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.offline_media",
            key="name", reason="a Scan needs a name to remember this source by",
            suggestion='Give this drive a name you will remember: '
                       '--name "Projects 2019"',
        ), as_json)

    found = identify_source(root)
    if found is None:
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

    with SqliteStore(settings.fts_db) as store:
        volume_id = store.upsert_volume(
            fields["identity_key"], kind=kind, name=name, description=description,
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

        # 1c: a network share never hashes to verify - SMB makes reading
        # every byte of every file just to confirm it has not moved
        # prohibitive, where a local mtime/size settling is nearly free.
        stats = _run_offline_media_pipeline(
            settings, store, root, volume_id, quiet=as_json,
            verify_hash=(kind != "network"),
        )

    if as_json:
        print(json.dumps({"volume_id": volume_id, "kind": kind, **stats.as_dict()},
                         indent=2, default=str))
        return EXIT_OK
    print(f"Catalogued as {name!r} ({kind}): {stats.indexed:,} document(s), "
          f"{stats.seen:,} file(s) seen.")
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


def cmd_files(args: argparse.Namespace) -> int:
    """Find a file by its NAME. Not a content search.

    Deliberately a separate command rather than a flag on `search`, because it
    answers a different question against a different index: `search` finds what
    documents *say*, this finds what they are *called*. A file named
    "Invoice 2024.pdf" whose contents never use those words is invisible to one
    and the first result of the other.

    Read-only, so no lock. It never embeds and never reranks, which is why it
    returns instantly.
    """
    from app.storage.sqlite_store import SqliteStore
    from app.ui.presenter import file_rows

    settings = _load(args)
    setup_logging(settings.log_path)

    text = " ".join(args.name).strip()
    if not text:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.files",
            key="name", reason="give something to look for",
            suggestion=r'Part of a filename is enough: app.cli files invoice',
        ), args.json)

    with SqliteStore(settings.fts_db) as store:
        hits = store.search_files_by_name(
            text, limit=args.limit, ext=args.type.split(",") if args.type else None
        )
        total = store.count_named_files()

    if args.json:
        print(json.dumps({"query": text, "matches": hits, "indexed_files": total}, indent=2))
        return EXIT_OK

    if not hits:
        print(f"No file name contains '{text}'.")
        if total == 0:
            print()
            print("No filenames are indexed yet. Run:")
            print(r'  venv\Scripts\python.exe -m app.cli index "D:\SearchData"')
        else:
            print(f"  ({total:,} filenames indexed. Matching is on any part of the name.)")
        return EXIT_OK

    rows = file_rows(hits)
    width = max(len(row.name) for row in rows)
    for row in rows:
        line = f"  {row.name.ljust(width)}  {row.size:>10}  {row.modified:>13}  {row.folder}"
        print(line)
        if row.note:
            print(f"  {' ' * width}  {row.note}")
    print()
    print(f"{len(rows):,} of {total:,} indexed filenames")
    return EXIT_OK


def cmd_evaluate(args: argparse.Namespace) -> int:
    """Does a plain sentence find the right document? Ask twenty and count.

    Two corpora, and the difference matters.

    **`--builtin`** uses a small corpus with known answers, shipped with the
    application. It answers "does the mechanism work" and "did today's change
    break something", and it needs nothing but the code.

    **The default** runs against your own index, from a file of your own
    questions - one per line, `sentence | fragment-of-the-wanted-path`. That is
    the measurement that actually matters, because a real archive has
    near-duplicates, inconsistent naming and years of drift that no fixture
    reproduces. Every number from the built-in corpus is optimistic.

    Recall is reported **split by whether the sentence carried a constraint**.
    One number cannot distinguish "search is bad" from "search is fine at topics
    and blind to constraints", and those have completely different fixes.
    """
    from app.search.evaluate import evaluate

    settings = _load(args)
    setup_logging(settings.log_path)

    if args.builtin:
        return _evaluate_builtin(args, evaluate)

    questions = _read_questions(Path(args.questions)) if args.questions else []
    if not questions:
        print("Give a file of questions, or use --builtin.")
        print()
        print("  One per line:   sentence | part-of-the-wanted-path")
        print("  For example:    the safety report Dave sent | leeds-safety")
        print()
        print(r"  venv\Scripts\python.exe -m app.cli evaluate --questions mine.txt")
        print(r"  venv\Scripts\python.exe -m app.cli evaluate --builtin")
        return EXIT_ERROR

    from app.index.embedder import Embedder
    from app.search import vector
    from app.search.engine import SearchEngine
    from app.search.rerank import Reranker
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import ImageVectorStore, VectorStore

    with SqliteStore(settings.fts_db) as store, \
            VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors, \
            ImageVectorStore(settings.vector_path) as image_vectors:
        engine = SearchEngine(
            store, vectors,
            Embedder.from_settings(settings),
            # **`from_settings` also fixes an inconsistency worth naming.**
            # Three of the five construction sites left `RERANK_TOP_N` and
            # `RERANK_WINDOW_CHARS` at the module defaults, so two controls in
            # Settings applied in the window and not on the command line. One
            # constructor means one answer.
            reranker=Reranker.from_settings(settings),
            # Work order 0h §1c: the third retrieval lane, same as every
            # other real search entry point in this file.
            image_vectors=image_vectors,
            clip_text_embedder=vector.clip_text_embedder_from_settings(settings),
        )

        def search(query: str) -> list[str]:
            return [result.path for result in engine.search(query, limit=args.k).results]

        translate = None
        if args.interpret:
            from app.llm.ollama import OllamaClient
            from app.search.translate import QueryTranslator

            translator = QueryTranslator(
                OllamaClient(settings.ollama_url, settings.ollama_model)
            )
            if not translator.available():
                print("Ollama is not answering, so --interpret would measure nothing.")
                print(r"  Check it: venv\Scripts\python.exe -m app.cli ollama")
                return EXIT_ERROR
            translate = lambda sentence: translator.translate(sentence).query  # noqa: E731

        report = evaluate(
            questions, search, k=args.k,
            mode="your index, interpreted" if args.interpret else "your index",
            translate=translate,
        )

    for line in report.lines():
        print(line)
    if args.json:
        print()
        print(json.dumps(report.as_dict(), indent=2))
    return EXIT_OK


def _read_questions(path: Path) -> list:
    """`sentence | expected-path-fragment | optional-constraint` per line."""
    from app.search.evaluate import Question

    questions = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        print(f"Could not read {path}: {exc}")
        return []

    for number, line in enumerate(lines, start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            print(f"  line {number} ignored - expected 'sentence | path-fragment'")
            continue
        questions.append(Question(
            sentence=parts[0], expects=parts[1],
            constraint=parts[2] if len(parts) > 2 else "",
        ))
    return questions


def _evaluate_builtin(args: argparse.Namespace, evaluate: Any) -> int:
    """The shipped corpus. Keyword only by default; `--rerank` for the lot.

    Keyword-only is the half that needs no model, so it runs anywhere and
    measures the same thing every time - which is exactly why it cannot see a
    reranker or embedding change. `--rerank` builds the real engine for that,
    and says so in the heading so the two are never confused.

    The vector half is what the
    `--questions` mode against a real index exercises.
    """
    import tempfile

    from app.search import keyword
    from app.search.commands import expand_slashes
    from app.search.query import parse_query
    from app.storage.sqlite_store import SqliteStore

    try:
        from tests.fixtures.evaluation import CORPUS, QUESTIONS, load_into
    except ImportError:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.evaluate", key="tests",
            reason="the built-in corpus ships with the tests, which are not installed",
            suggestion="Run from a source checkout, or use --questions with your own.",
        ), args.json)

    folder = Path(tempfile.mkdtemp())
    with SqliteStore(folder / "evaluate.db") as store:
        load_into(store)

        # **`--rerank` is the only way to measure a reranker change.**
        #
        # Without it this calls the keyword retriever directly - no vectors, no
        # fusion, no cross-encoder - which is a perfectly good measurement of
        # BM25 and completely blind to the thing it was reached for. The owner
        # ran it to judge a reranker swap on my advice, and it could not have
        # detected one: the numbers came back identical because they measure a
        # stage the reranker never touches.
        mode = "built-in corpus, keyword only"
        # A flag that names a reranker and then does not use one is a setting
        # that silently does nothing - the failure this project keeps hitting.
        if getattr(args, "rerank_model", None):
            args.rerank = True
        if args.rerank:
            settings = _load(args)
            # **Comparing two models must not require editing a config file.**
            # The instruction "put this line in .env" was pasted into PowerShell
            # as a command, which is a fair reading of a line in a code block -
            # and even done correctly it is an edit, a save and a reread between
            # every measurement. One flag is the whole comparison.
            if getattr(args, "rerank_model", None):
                settings = settings.model_copy(update={"rerank_model": args.rerank_model})
            from app.index.embedder import Embedder
            from app.search.engine import SearchEngine
            from app.search.rerank import Reranker
            from app.storage.vector_store import VectorStore

            vectors = VectorStore(folder / "vectors", dim=settings.embed_dim)
            vectors.connect()
            embedder = Embedder.from_settings(settings)

            # **The vectors have to be built or this is not the full pipeline.**
            #
            # `load_into` writes to SQLite only, so the first version of this
            # ran keyword + an *empty* vector store + rerank, warned "no vector
            # hits" on all twenty questions, and still called itself "full
            # pipeline" in the heading. That is a label the measurement did not
            # earn, and the warnings were the evidence sitting right there.
            #
            # Twenty-one documents, so this costs a second.
            chunks = list(store.conn.execute(
                "SELECT id, file_id, text FROM chunks ORDER BY id"))
            if chunks:
                vectors.add(
                    chunk_ids=[row["id"] for row in chunks],
                    file_ids=[row["file_id"] for row in chunks],
                    vectors=list(embedder.embed_all([row["text"] for row in chunks])),
                )
                store.mark_embedded(row["id"] for row in chunks)

            engine = SearchEngine(
                store, vectors,
                embedder,
                # `enabled=True` explicitly: this path measures the reranker,
                # so the switch that turns it off for searching must not turn
                # off the thing being measured.
                reranker=Reranker.from_settings(settings, enabled=True),
                log_usage=False,
            )
            # The model is named because `.env` overrides the shipped default,
            # and two runs of the same model look exactly like two runs of
            # different ones if the heading does not say.
            mode = f"built-in corpus, full pipeline, {settings.rerank_model}"

            def search(query: str) -> list[str]:
                response = engine.search(expand_slashes(query), limit=args.k, rerank=True)
                return [result.path for result in response.results]
        else:
            def search(query: str) -> list[str]:
                parsed = parse_query(expand_slashes(query))
                return [hit["path"] for hit in keyword.search(store, parsed, limit=args.k)]

        report = evaluate(
            QUESTIONS, search, k=args.k, mode=mode,
            note=f"{len(CORPUS)} documents. A small clean corpus with no "
                 "near-duplicates - every number here is optimistic. Your own "
                 "questions against your own index are the measurement that counts.",
        )

    for line in report.lines():
        print(line)
    if args.json:
        print()
        print(json.dumps(report.as_dict(), indent=2))
    return EXIT_OK


def cmd_embedbench(args: argparse.Namespace) -> int:
    """Measure what embedding costs on this machine, and say what would help.

    Exists because an estimate was wrong once, expensively. "1.53 passages per
    second" was called twenty times too slow, on the assumption that a small
    model should manage tens per second - true for short sentences, false for
    the 512-token passages this app embeds. A throughput number without the
    sequence length beside it is not a number.
    """
    from app.index.embed_bench import inspect_model, project, providers, run_benchmark
    from app.storage.sqlite_store import SqliteStore

    settings = _load(args)
    setup_logging(settings.log_path)
    cache = Path(settings.model_cache)

    result = inspect_model(cache, providers(_bench_result(settings)))
    print("Embedding on this machine")
    print("=" * 68)
    print(f"  model            {settings.embed_model}")
    if result.model_file:
        print(f"  file             {Path(result.model_file).name}  ({result.model_mb:.0f}MB)")
    if result.precision:
        # Reported from the file size, so it works without `onnx` installed -
        # and as a precision rather than a yes/no, because fp16 is a real answer
        # and "not int8" would have hidden that int8 is still worth having.
        print(f"  precision        {result.precision}")
    if result.weight_types:
        shown = ", ".join(f"{name} {count/1e6:.1f}M" for name, count in
                          sorted(result.weight_types.items(), key=lambda kv: -kv[1]))
        print(f"  weights          {shown}")
    print(f"  providers        {', '.join(result.available_providers) or 'unknown'}")

    if not args.quick:
        print()
        print("  measuring…")
        result = run_benchmark(settings.embed_model, cache, result=result)
        if result.threads:
            print(f"  threads          {result.threads}")

    if result.error:
        print()
        print(f"  ! {result.error}")

    if result.throughput:
        print()
        # **Tokens per second, beside chunks per second.** A corpus is a
        # quantity of text; how it is cut into chunks is a choice. Quoting only
        # chunks/sec hides that the choice changes the total - and it hid it
        # well enough that "halving the chunk size is roughly a wash" was said
        # out loud on the strength of it, which the numbers below disprove.
        print(f"  {'chunk':>8}  {'chunks/sec':>11}  {'tokens/sec':>11}"
              f"  {'range':>18}")
        for tokens, rate in sorted(result.throughput.items()):
            low, high = result.spread.get(tokens, (rate, rate))
            print(f"  {tokens:>8}  {rate:>11.2f}  {tokens * rate:>11,.0f}"
                  f"  {low:>8.2f} - {high:<7.2f}")

        if result.unstable:
            print()
            print(f"  ! The {', '.join(str(t) for t in result.unstable)}-token"
                  " measurement varied by more than a quarter between passes.")
            print("    Something else was using the machine. Close it and run again -")
            print("    the projections below are only as good as this number.")

        rate = result.throughput.get(512) or min(result.throughput.values())
        # The same corpus, cut differently. Everything here is measured on this
        # machine; only the choice of chunk size is hypothetical.
        biggest = max(result.throughput)
        total_tokens = 800_000 * biggest
        if len(result.throughput) > 1:
            print()
            print("  The same 100GB corpus, cut into different chunk sizes:")
            for size in sorted(result.throughput):
                hours = total_tokens / (size * result.throughput[size]) / 3600
                marker = "  <- current" if size == biggest else ""
                print(f"    {size:>3}-token chunks   {hours:>5.0f} hours{marker}")
        chunks = 0
        if settings.fts_db.is_file():
            with SqliteStore(settings.fts_db) as store:
                chunks = int(store.stats()["chunks_total"])
        print()
        print("  At the 512-token rate:")
        for label, count in (("your index now", chunks), ("200K messages", 400_000),
                             ("100GB corpus", 800_000)):
            if count:
                hours = project(count, rate)["hours"]
                unit = f"{hours*60:.0f} min" if hours < 1.5 else f"{hours:.0f} hours"
                print(f"    {label:<18} {count:>9,} chunks   {unit}")

    if args.json:
        print()
        print(json.dumps(result.as_dict(), indent=2))

    print()
    for line in _embed_advice(result):
        print(line)
    return EXIT_OK


def cmd_bench_index(args: argparse.Namespace) -> int:
    r"""Time the whole pipeline here, and remember the answer.

    **The model was only ever half the question.** `embed-bench` says how fast
    the model is; a run whose model is fast and whose disk is slow is bounded
    by the disk, and a tuning screen holding only the model number will
    confidently recommend a graphics card to somebody who needs a different
    drive. This times reading, writing and the model on the same fixed
    workload, so the three are comparable.

    The numbers are stored beside the compute profile, keyed by its
    fingerprint, and that is what turns Defaults into Auto-tune. `--no-save`
    is for measuring somebody else's machine, or for a comparison you do not
    want acting on your settings.
    """
    from app.core.compute_profile import cached_profile
    from app.core.measured import remember
    from app.index.index_bench import run_index_bench
    from app.storage.sqlite_store import SqliteStore

    settings = _load(args)
    setup_logging(settings.log_path)

    devices = ("cpu", "gpu") if args.both else None
    if not args.json:
        print("Timing this machine on a fixed workload. About a minute.",
              flush=True)

    result = run_index_bench(settings, devices=devices)
    if args.json:
        print(json.dumps(result.as_dict(), indent=2))
    else:
        print()
        print("This machine, on the whole pipeline")
        print("=" * 68)
        print(f"  reading          {result.extract_per_second:,.0f} files a second, per reader")
        print(f"  writing          {result.write_per_second:,.0f} chunks a second")
        for device, rate in result.embed_per_second.items():
            print(f"  meaning ({device})   {rate:,.1f} chunks a second")
        print(f"  took             {result.seconds:.1f}s over "
              f"{result.documents:,} documents and {result.chunks:,} chunks")
        for note in result.notes:
            print(f"  note             {note}")
        if result.error:
            print(f"  STOPPED          {result.error}")

    if args.no_save or result.error:
        return EXIT_OK

    with SqliteStore(settings.fts_db) as store:
        profile = cached_profile(store, settings.data_path)
        stored = remember(store, result.as_measured(profile.fingerprint()))
    if not args.json:
        # **Said either way.** "Saved" is what makes Auto-tune mean something,
        # and a silent failure to save would leave somebody believing their
        # machine had been learned when it had not.
        print("  saved            " + ("yes - Auto-tune will use these"
                                       if stored else
                                       "NO - the index could not be written to"))
    return EXIT_OK


def _bench_result(settings: Settings):
    from app.index.embed_bench import BenchResult

    return BenchResult(model_name=settings.embed_model)


def _embed_advice(result: Any) -> list[str]:
    """What would actually help, given what was measured.

    Deliberately says "nothing to gain here" when that is the answer. Advice
    that always finds something to recommend is advice nobody can act on.
    """
    lines = ["What would help:"]
    gpu = [p for p in result.available_providers
           if any(k in p for k in ("CUDA", "Dml", "DirectML", "ROCm", "CoreML"))]
    if gpu:
        lines.append(f"  * {gpu[0]} is available and is NOT being used. That is the")
        lines.append("    largest single win here - typically five to fifteen times.")
    if result.precision == "int8":
        lines.append("  * The model is ALREADY int8. Quantisation is not a lever here -")
        lines.append("    do not spend time on it.")
    elif result.precision == "fp16":
        lines.append("  * The model is fp16 - already half the size of full precision, so")
        lines.append("    an int8 build is worth roughly another two times rather than the")
        lines.append("    four you would get from fp32. Switching invalidates every stored")
        lines.append("    vector, so it is cheapest while the index is small.")
    elif result.precision == "fp32":
        lines.append("  * The model is full precision. An int8 build is 2-4x on CPU for a")
        lines.append("    small accuracy cost, and switching invalidates every stored")
        lines.append("    vector - so it is cheapest while the index is small.")
    lines += [
        "  * Chunk size is worth about 10-15% over the same text, and the table",
        "    above shows it for THIS machine rather than in the abstract. It was",
        "    claimed here as 1.5x once, from a measurement whose spread the tool",
        "    now warns about. Unless the table shows a wide gap, this is not",
        "    worth a re-index.",
        "  * Fewer chunks beats faster chunks. Quoted replies and signatures are",
        "    already stripped; near-duplicate passages are the next candidate.",
        "  * The cost is per token, so it scales with how much text is indexed,",
        "    not with how many files. Narrowing the index roots is the bluntest",
        "    and most reliable saving available.",
    ]
    return lines


def cmd_reembed(args: argparse.Namespace) -> int:
    """Rebuild the vector store from SQLite. No re-reading of any document.

    **This is why SQLite is the authority and LanceDB is derived.** Every chunk's
    text is already in the metadata store, so the vectors can always be rebuilt
    without touching the corpus - which turns "the semantic half of search is
    broken" from a 100GB re-index into a job measured in minutes.

    Deliberately its own command rather than a flag on `index`. It answers a
    different question ("the vectors are wrong") and must not walk the disk,
    hash anything, or prune a file that happens to be on a disconnected drive.
    """
    from app.index.embedder import Embedder
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore

    settings = _load(args)
    setup_logging(settings.log_path)

    # A writer, so it takes the run lock - `reembed` and `index` must exclude
    # each other as firmly as two `index` runs do.
    with SqliteStore(settings.fts_db) as store, \
            IndexRunLock(store, owner=COMMAND_LINE), \
            VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors:
        stats = store.stats()
        total = int(stats["chunks_total"])
        if total == 0:
            print("Nothing is indexed yet, so there is nothing to embed.")
            print(r'  venv\Scripts\python.exe -m app.cli index "D:\YourFolder"')
            return EXIT_OK

        if args.all:
            # Every chunk goes back in the queue. The table is dropped rather
            # than written over: leaving stale rows behind is how a rebuild ends
            # up with more vectors than there are passages.
            print(f"Clearing the vector store and re-embedding all {total:,} passages.")
            vectors.drop()
            store.mark_all_unembedded()

        # **Say what is about to happen, before the silence starts.**
        #
        # Nothing was printed until the first batch of 256 finished. At the 4.4
        # passages/second `embed-bench` measures on a real machine that is
        # nearly a minute of a completely silent terminal, and the correct
        # response to a silent terminal is to assume it has hung and kill it -
        # which loses the work. Reported as exactly that: "this seems stuck".
        #
        # The standing rule is that nothing fails silently. A long operation
        # that says nothing is the same fault wearing a different hat: there is
        # no way to tell it from one that has died.
        outstanding = max(0, total - int(stats["chunks_embedded"]))
        if not args.quiet:
            print(f"Embedding {outstanding:,} of {total:,} passages "
                  f"({total - outstanding:,} already done).")
            print("  Loading the model, then the first batch of 256 - "
                  "the first line takes a minute or so.", flush=True)

        embedder = Embedder.from_settings(settings)
        done = 0
        started = time.time()
        # Split three ways, because the totals lie. `embed-bench` measured 4.4
        # passages/second on this machine and the loop ran at 1.5 - so roughly
        # two thirds of the time was going somewhere other than the model, and
        # no amount of choosing a faster model would have touched it. A rate
        # without a breakdown behind it sends people optimising the wrong thing.
        spent = {"read": 0.0, "embed": 0.0, "write": 0.0}

        mark = time.perf_counter()
        for batch in store.iter_unembedded(batch_size=256):
            spent["read"] += time.perf_counter() - mark

            mark = time.perf_counter()
            embedded = embedder.embed([chunk.text for chunk in batch])
            spent["embed"] += time.perf_counter() - mark

            mark = time.perf_counter()
            written = vectors.add(
                chunk_ids=[chunk.id for chunk in batch],
                file_ids=[chunk.file_id for chunk in batch],
                vectors=embedded,
            )
            # Marked only after the vectors are safely written. The other order
            # loses passages silently: a crash between the two would leave rows
            # flagged embedded with nothing in LanceDB, and nothing would ever
            # pick them up again.
            #
            # **The count, not its truthiness.** `if written:` marked the whole
            # 256-chunk batch embedded when one vector was written - which is
            # exactly the bug `78aa392` fixed in the pipeline, still standing
            # here on the path people run *to repair* that bug. A short write
            # leaves the batch unmarked so the next `reembed` retries it.
            if written is not None and written < len(batch):
                logger.bind(component="cli.reembed").error(
                    "wrote {} vectors for {} passages - the rest stay unembedded "
                    "and a later `reembed` will retry them.", written, len(batch))
            else:
                store.mark_embedded([chunk.id for chunk in batch])
            spent["write"] += time.perf_counter() - mark

            done += len(batch)
            if not args.quiet:
                elapsed = max(time.time() - started, 0.001)
                share = " ".join(
                    f"{name} {value / elapsed:.0%}" for name, value in spent.items()
                )
                print(f"  embedded {done:,}  ({done / elapsed * 60:,.0f}/min)   {share}",
                      flush=True)
            mark = time.perf_counter()

        rows = vectors.count()
        elapsed = max(time.time() - started, 0.001)
        print()
        print(f"Done. {rows:,} vectors for {total:,} passages in {elapsed/60:.1f} min.")
        if done:
            print(f"  reading SQLite   {spent['read']:>7.1f}s  {spent['read']/elapsed:>5.0%}")
            print(f"  embedding        {spent['embed']:>7.1f}s  {spent['embed']/elapsed:>5.0%}"
                  f"   ({done/max(spent['embed'], 0.001):.1f}/sec while running)")
            print(f"  writing vectors  {spent['write']:>7.1f}s  {spent['write']/elapsed:>5.0%}")
            slowest = max(spent, key=spent.get)
            if slowest != "embed":
                print()
                print(f"  Most of the time is going to {slowest}, not the model.")
                print("  A faster or smaller model would not help this run.")
        if rows < total:
            print(f"  {total - rows:,} passages still have no vector - see the log.")
        return EXIT_OK


def cmd_commands(args: argparse.Namespace) -> int:
    """List the search filters. The answer to "what can I type in that box?".

    Every one of these has worked since Layer 4 and none of them were documented
    anywhere a user would look, which made them worth exactly nothing. Printed
    from `app/search/commands.py` - the same list the `/` dropdown shows and the
    same one Layer 8a hands to the model, so the three cannot drift apart.
    """
    from app.search.commands import COMMANDS, help_lines

    catalogue = COMMANDS
    if getattr(args, "git", False):
        from app.search.gitquery import GIT_COMMANDS

        catalogue = GIT_COMMANDS

    if args.json:
        print(json.dumps([
            {"name": c.name, "aliases": list(c.aliases), "summary": c.summary,
             "example": c.example, "value": c.value_hint, "icon": c.icon,
             "scoped_by": list(c.scoped_by)}
            for c in catalogue
        ], indent=2))
        return EXIT_OK

    if getattr(args, "git", False):
        # **A second catalogue, printed the same way.** Repository search is a
        # different engine over a different store, and mixing its switches into
        # the index's list would offer `/history` in a box that cannot answer
        # it - the failure the per-tab restriction exists to prevent.
        print("Repository search filters — app.cli gitsearch, and the Code tab")
        print("=" * 70)
        width = max(len(c.name) for c in catalogue) + 2
        for command in catalogue:
            spellings = ", ".join(command.aliases)
            print(f"  {command.icon} /{command.name.ljust(width)}{command.summary}")
            print(f"      {command.example:<28} {command.value_hint}")
            if spellings:
                print(f"      also: {spellings}")
            if command.scoped_by:
                # 4e: the help, the popup and the completer describe one
                # grammar. Somebody reading this should learn that
                # `repo:leasha branch:` is narrower than `branch:` alone.
                print(f"      narrowed by: "
                      f"{', '.join(f'{n}:' for n in command.scoped_by)}")
        return EXIT_OK

    for line in help_lines():
        print(line)
    return EXIT_OK


def cmd_shell(args: argparse.Namespace) -> int:
    r"""An interactive session with a real dropdown.

    **The only way a terminal gets one.** A shell prompt is owned by the shell,
    so no completer script can draw a menu that follows the keystrokes; a
    session can. And because it is a persistent process with the store already
    open, completion is an in-process call - no sidecar, no cold-start budget,
    and the scoping from §1 works, because the parser is right here.
    """
    from app.index.embedder import Embedder
    from app.search import vector
    from app.search.engine import SearchEngine
    from app.search.rerank import Reranker
    from app.shell.repl import run_shell
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import ImageVectorStore, VectorStore

    settings = _load(args)
    setup_logging(settings.log_path)

    if not settings.fts_db.is_file():
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.shell",
            key="index", reason="there is no index yet",
            suggestion="Run `leasha index` first, or open the window and "
                       "choose a folder to index.",
        ), args.json)

    embedder = Embedder.from_settings(settings)
    reranker = Reranker.from_settings(settings)
    # Work order 0h §1c: the third retrieval lane, same as every other real
    # search entry point in this file.
    clip_text_embedder = vector.clip_text_embedder_from_settings(settings)

    with SqliteStore(settings.fts_db) as store, \
            VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors, \
            ImageVectorStore(settings.vector_path) as image_vectors:
        engine = SearchEngine(
            store, vectors, embedder, reranker=reranker,
            image_vectors=image_vectors, clip_text_embedder=clip_text_embedder,
        )
        # Warmed before the first prompt, for the reason `search` warms before
        # its clock starts: the first query would otherwise pay for two model
        # loads and look like the session is slow.
        try:
            engine.warm_up()
        except Exception as exc:                 # noqa: BLE001 - optional
            _log_shell_warmup(exc)
        return run_shell(engine, settings)


def _log_shell_warmup(exc: BaseException) -> None:
    """A model that will not load costs the meaning half, not the session."""
    logger.bind(component="cli.shell").debug("warm-up failed: {}", exc)


def cmd_open(args: argparse.Namespace) -> int:
    r"""Act on a `leasha://` link. Adoptions §7a.

    This is what Windows runs for a `leasha://search?q=...` URL, and it is
    **not a way to start the window**. If a window is already open, the query
    is left in `index_state` for it to pick up and this process exits; if one
    is not, the link is still recorded, so the next start runs it. Either way
    the person gets their search, which is the only thing they asked for.

    `register` and `unregister` write and remove the per-user scheme. Both are
    no-ops off Windows, and both say so rather than pretending.
    """
    from app.core.deeplink import (
        SCHEME, handover, parse, register, registry_values, unregister,
    )
    from app.core.deeplink import open_command as _open_command

    action = str(getattr(args, "url", "") or "").strip()

    if action == "register":
        target = str(getattr(args, "path", "") or "") or _launcher_path()
        if register(target):
            print(f"{SCHEME}:// links now open Leasha.")
            return EXIT_OK
        print(f"Could not register {SCHEME}:// links. "
              f"This only works on Windows. The command it would have "
              f"written is:\n  {_open_command(target)} \"%1\"")
        return EXIT_OK

    if action == "unregister":
        print(f"{SCHEME}:// links no longer open Leasha."
              if unregister() else
              f"Nothing to remove: {SCHEME}:// was not registered here.")
        return EXIT_OK

    if action == "show":
        for sub, value in registry_values(
                _open_command(_launcher_path())).items():
            print(f"{sub or '(default)':<20} {value}")
        return EXIT_OK

    request = parse(action)
    if request is None:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.open", key="url",
            reason=f"{action!r} is not a Leasha link",
            suggestion=(
                f"A link looks like {SCHEME}://search?q=safety%20report\n"
                f"  leasha open register     make Windows open these links\n"
                f"  leasha open unregister   stop it"),
        ), args.json)

    settings = load_settings()
    from app.storage.sqlite_store import SqliteStore

    with SqliteStore(settings.fts_db) as store:
        handover(store, request)
    print(f"Searching for {request.query!r} in Leasha.")
    return EXIT_OK


def _launcher_path() -> str:
    """What Windows should run for a link: the installed `leasha.cmd`."""
    found = project_root() / "leasha.cmd"
    return str(found if found.exists() else Path(sys.executable))


def cmd_completions(args: argparse.Namespace) -> int:
    r"""Emit or install the PowerShell tab completer.

    `--powershell` prints it; `install` appends a dot-source line to the
    profile and `install --remove` takes it out again - the contract
    `add-to-path.ps1` established, including the no-administrator-rights rule.

    Generated from the catalogue every time, so regenerating after a change to
    the filters is the whole update path.
    """
    from app.search.pwsh_completer import completer_script, install_into

    root = project_root()
    script = completer_script(project_path=root)

    if getattr(args, "action", "") != "install":
        print(script)
        return EXIT_OK

    target = Path(args.path).expanduser() if getattr(args, "path", "") else None
    if target is None:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.completions",
            key="--path",
            reason="the PowerShell profile to change was not given",
            suggestion=(
                "PowerShell knows where its own profile is. Run:\n"
                "  leasha completions install --path $PROFILE\n"
                "Add --remove to take it out again."),
        ), args.json)

    # **Written where the completer can find it, not into the profile.** A
    # profile holding the whole script would have to be edited again on every
    # catalogue change; a dot-source of a generated file does not.
    generated = root / "leasha-completions.ps1"
    generated.write_text(script, encoding="utf-8")

    updated = install_into(target, generated, remove=bool(args.remove))
    if updated is None:
        print("Nothing to change - "
              + ("it was not installed." if args.remove else "already installed."))
        return EXIT_OK

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(updated, encoding="utf-8")
    print(("Removed from " if args.remove else "Installed into ") + str(target))
    if not args.remove:
        print("Open a new PowerShell window, then type `leasha ` and press Tab.")
    return EXIT_OK


def cmd_ollama(args: argparse.Namespace) -> int:
    """Why is Ollama not working? Four questions, answered separately.

    Each has a different fix, so a single "up / down" would send people looking
    in the wrong place - which is exactly what happened: the service was running,
    `/api/tags` answered, and enrichment then spent 200 seconds discovering that
    the model was not installed.

    **Ollama is optional and has exactly one job: the Interpret button.** It
    turns a sentence into a query, which then goes into the search box for you
    to read and edit. Plain Enter never touches it, so search works perfectly
    with Ollama switched off - and this command says so, because it is the
    natural place to look when "search did not work" and the wrong one.

    It used to type knowledge-graph entities. The graph was removed, and this
    docstring said otherwise for a while, which is its own small lesson about
    diagnostics: a stale one sends people to the wrong place with confidence.
    """
    from app.llm.ollama import OllamaClient

    settings = _load(args)
    setup_logging(settings.log_path)

    # `--model` overrides without editing .env, so a model can be tried before
    # it is committed to. The whole point of the flag: the choice is a speed
    # decision, and a speed decision needs a measurement rather than a guess.
    wanted = getattr(args, "model", None) or settings.ollama_model
    client = OllamaClient(settings.ollama_url, wanted)
    report = client.diagnose()

    if args.json:
        print(json.dumps(report, indent=2))
        return EXIT_OK if report["generated"] else EXIT_ERROR

    print(f"Ollama at {report['url']}")
    print(f"Model wanted: {report['model']}")
    print()

    def mark(ok: bool) -> str:
        return "  OK  " if ok else " FAIL "

    print(f"[{mark(report['reachable'])}] something is listening")
    if not report["reachable"]:
        print()
        print("  Ollama is not running, or is on a different address.")
        print("  Start it:      ollama serve")
        print("  Check the URL: OLLAMA_URL in your .env")
        print()
        print("  Search still works. Ollama is only used by the Interpret")
        print("  button, which rewrites a sentence into a query. Typing a")
        print("  query and pressing Enter never touches it.")
        return EXIT_ERROR

    print(f"[{mark(bool(report['models']))}] models installed: "
          f"{', '.join(report['models']) or 'none'}")
    print(f"[{mark(report['model_installed'])}] '{report['model']}' is one of them")
    if not report["model_installed"]:
        print()
        print(f"  Ollama is running but has no '{report['model']}'. Pull it:")
        print(f"    ollama pull {report['model']}")
        print("  Or point OLLAMA_MODEL at one of the models listed above.")
        return EXIT_ERROR

    took = f" in {report['elapsed_s']}s" if report["elapsed_s"] is not None else ""
    print(f"[{mark(report['generated'])}] it answered a trial question{took}")
    if not report["generated"]:
        print()
        print(f"  {report['error']}")
        print("  The model is installed but did not reply within 30 seconds.")
        print("  A first call loads the model into memory and can be slow;")
        print("  try again, and if it persists the model may be too large for")
        print("  this machine.")
        return EXIT_ERROR

    print()
    print(f"Working. Reply: {report.get('reply', '')!r}")

    # **The end-to-end check.** Everything above proves Ollama is alive; none of
    # it proves the one thing the app asks of it. A model can be installed,
    # responsive, and still return prose where a query was wanted - and the
    # translator will then quietly fall back to the raw sentence, which looks
    # like it worked. This runs the real path and prints what came back.
    sentence = getattr(args, "translate", None)
    if sentence:
        from app.search.translate import TRANSLATE_TIMEOUT_S, QueryTranslator

        budget = float(getattr(args, "timeout", 0) or TRANSLATE_TIMEOUT_S)
        print()
        print(f"Interpreting: {sentence!r}  (budget {budget:g}s)")
        # `enabled=True` because typing `--translate` *is* the request. The
        # stored preference governs the button in the window; it would be
        # obtuse for a command that exists to run one translation to refuse
        # because a checkbox elsewhere is unticked.
        result = QueryTranslator(
            client, timeout_s=budget, enabled=True).translate(sentence)
        print(f"  -> {result.query!r}  [{result.elapsed_s:.1f}s]")
        # `changed`, not `used_model`: the latter is False for a cache hit,
        # and a cached translation is a working one. What matters here is
        # whether anything came back that differs from what went in.
        if result.changed:
            cached = " (from cache)" if result.from_cache else ""
            print(f"  [  OK  ] the model produced it{cached}")
        else:
            print("  [ FAIL ] fell back to the raw sentence")
            print()
            # The error's own message and suggestion, not a guess. This command
            # once printed "try a different model" for a *timeout*, which is
            # sometimes right and sometimes hides that the budget is simply too
            # small - and it printed it while the trial question above had just
            # succeeded in under a second.
            if result.error is not None:
                print(f"  [{result.error.code}] {result.error.message}")
                if result.error.details:
                    print(f"  {result.error.details}")
                print()
                print(f"  FIX: {result.error.suggestion}")
            else:
                print(f"  {result.note}")
            print()
            print("  Search is unaffected. Interpret is the only thing that uses")
            print("  Ollama, and when it cannot help it passes your words through.")
            return EXIT_ERROR

    return EXIT_OK


def cmd_rerank_bench(args: argparse.Namespace) -> int:
    """What reranking costs, per model, on this machine.

    Reranking was 8.3 seconds of a 9-second search - 93% of it, against a spec
    budget of 300ms warm. The default model is 1.04GB; the smallest usable one
    is 0.08GB. This measures the difference rather than asserting it, because
    four throughput claims in this project have already been wrong and every one
    was a number quoted without its conditions.
    """
    from app.search.rerank_bench import measure
    from app.storage.sqlite_store import SqliteStore

    settings = _load(args)
    setup_logging(settings.log_path)

    models = [args.model] if getattr(args, "model", None) else []
    if not args.json:
        # **Not on stdout under `--json`.** A preamble in front of the payload
        # makes machine-readable output unparseable, which is the one thing it
        # has to be. Found by a test that ran the command rather than reading it.
        print("Timing a full rerank.")
        if not models:
            # It downloaded 1.4GB of models on the owner's first run without
            # saying so beforehand. Saying so is the least it can do.
            print("First run downloads about 1.4GB for the four candidates.")
            print("Use --model NAME to time only one.")
        print()

    with SqliteStore(settings.fts_db) as store:
        result = measure(
            store, models=models, count=args.count,
            window_chars=args.window, cache_dir=str(settings.model_cache),
            passes=args.passes,
        )

    if args.json:
        print(json.dumps(result.as_dict(), indent=2))
        return EXIT_OK

    # The download progress bars write to the same terminal and overwrite the
    # first lines of the table. A blank line and a flush lets them finish.
    sys.stdout.flush()
    print("\n")
    print(f"{result.count} candidates  ·  passages cut to {result.window_chars} "
          f"chars (mean chunk is {result.mean_passage_chars})")
    print()
    print(f"{'model':38} {'size':>8} {'per search':>11} {'per passage':>12}  load")
    print("-" * 82)
    for timing in result.timings:
        if timing.error:
            print(f"{timing.name:38} {timing.size:>8}   {timing.error}")
            continue
        per_passage = timing.median_s / max(1, result.count) * 1000
        flag = "  UNSTABLE" if timing.unstable else ""
        print(f"{timing.name:38} {timing.size:>8} {timing.median_s:>10.2f}s "
              f"{per_passage:>11.0f}ms  {timing.load_s:.1f}s{flag}")

    # **Always, not only when a model loaded.** This is the fact that misled
    # the owner: two runs looked like a comparison and were the same model
    # twice, because `.env` pinned it and nothing on screen said so.
    print()
    print(f"You are currently using: {settings.rerank_model}")

    usable = [t for t in result.timings if t.passes]
    if usable:
        best = min(usable, key=lambda t: t.median_s)
        print(f"Fastest here:            {best.name} at {best.median_s:.2f}s per search.")
        if settings.rerank_model != best.name:
            # **`.env` shadows the shipped default.** Changing a default in the
            # code does nothing for anybody who already has a `.env` - which is
            # everybody who has ever run the installer. Saying "the default is
            # now X" would have been useless advice, and was.
            env = getattr(args, "env", None) or ".env"
            print()
            print(f"  Your {env} pins RERANK_MODEL, so the shipped default does")
            print("  not apply. Edit that line to change it:")
            print(f"      RERANK_MODEL={best.name}")
        print()
        print("Speed is only half the question. `leasha evaluate --builtin` measures")
        print("whether the ordering is still good enough on your own corpus.")
    return EXIT_OK


def _formats_by_group(args: argparse.Namespace) -> int:
    """The source types, by ecosystem.

    **The flat list is unreviewable and that is the point of this view.** Four
    hundred extensions in one alphabetical run tells nobody whether Oracle
    packages are covered; twenty lines under "Oracle PL/SQL" can be read by
    somebody who knows Oracle, in a minute, and corrected.
    """
    from app.extract.source_types import (
        ALL_SOURCE_EXTENSIONS, BY_ECOSYSTEM, NAMED_FILES,
    )

    if args.json:
        print(json.dumps({
            "total": len(ALL_SOURCE_EXTENSIONS),
            "groups": {name: sorted(group)
                       for name, group in BY_ECOSYSTEM.items()},
            "named_files": sorted(NAMED_FILES),
        }, indent=2))
        return EXIT_OK

    print(f"Source and code types - {len(ALL_SOURCE_EXTENSIONS)} extensions, "
          f"all read as plain text, all on")
    print("=" * 70)
    for name, group in BY_ECOSYSTEM.items():
        print(f"\n{name}  ({len(group)})")
        line = "  "
        for extension in sorted(group):
            if len(line) + len(extension) > 76:
                print(line)
                line = "  "
            line += extension + " "
        if line.strip():
            print(line)

    print(f"\nMatched by whole name, having no extension  ({len(NAMED_FILES)})")
    print("  " + " ".join(sorted(NAMED_FILES)))
    print()
    print("  Every one of these is read by the plain-text reader, so adding a")
    print("  type costs a line and no dependency. Switch any of them off in")
    print("  Settings, or with `enabled = false` in extractors.toml.")
    return EXIT_OK


def cmd_formats(args: argparse.Namespace) -> int:
    """What gets indexed, what reads it, and what is switched off.

    The answer to "why was that file not indexed?" - which otherwise needs a
    debugger, or a guess. Read-only: it opens no store and touches no file
    beyond the two configuration files it prints the paths of.

    It also *validates*. Running it after editing `extractors.toml` reports a
    typo before the next index run finds it, and the error names the offending
    key rather than the file.
    """
    if getattr(args, "groups", False):
        return _formats_by_group(args)

    import app.extract  # noqa: F401 - importing the package populates REGISTRY
    from app.core import formats as formats_mod
    from app.extract import base as extract_base

    settings = _load(args)
    setup_logging(settings.log_path)

    try:
        rules = formats_mod.load_rules(
            settings.data_path, known_extractors=extract_base.extractor_names()
        )
    except AppErrorException as exc:
        return _report(exc.error, args.json)

    # **Pass the registry.** `describe()` says to, in its own docstring, and
    # this call did not - so the table listed only the 61 extensions some TOML
    # mentions and omitted the 56 claimed in code, including `.py`, `.cs`,
    # `.java`, `.js`, `.ts` and `.sql`. The command whose help reads "what file
    # types are indexed" was hiding most of the answer, and the owner
    # reasonably concluded from it that code was not being searched. It was.
    rows = rules.describe(registry=extract_base.REGISTRY)
    built_in = sorted(set(extract_base.supported_extensions()) - set(rules.extensions))

    if args.json:
        print(json.dumps({
            "sources": [str(p) for p in rules.sources],
            "user_file": str(formats_mod.user_path(settings.data_path)),
            "default_max_bytes": rules.default_max_bytes,
            "configured": rows,
            "built_in": built_in,
            "extractors": sorted(extract_base.extractor_names()),
        }, indent=2))
        return EXIT_OK

    print("File types")
    print("=" * 68)
    for path in rules.sources:
        print(f"  read: {path}")
    user_file = formats_mod.user_path(settings.data_path)
    if user_file not in rules.sources:
        print(f"  your overrides would go in: {user_file}  (not present)")
    print()

    # Shown by default. It was behind `--all`, which meant the default output
    # of a command called `formats` answered "which types are configured"
    # rather than "which types are indexed" - and only one of those is the
    # question anybody has.
    if built_in:
        print(f"Also built into the code, no configuration needed ({len(built_in)}):")
        print("  " + " ".join(built_in))
        print()

    off = [row for row in rows if not row["enabled"]]
    on = [row for row in rows if row["enabled"]]

    print(f"Configured and on ({len(on)}):")
    for row in on:
        cap = _human_bytes(row["max_bytes"])
        note = f"   {row['note']}" if row["note"] else ""
        print(f"  {row['extension']:<12} {row['extractor']:<22} <= {cap}{note}")

    if off:
        print()
        print(f"Off ({len(off)}) - nothing with these extensions is opened at all:")
        for row in off:
            why = f" via {row['converter']}" if row["converter"] else ""
            note = f"   {row['note']}" if row["note"] else ""
            print(f"  {row['extension']:<12} {row['extractor']}{why}{note}")

    print()
    print(f"Extractors available: {', '.join(sorted(extract_base.extractor_names()))}")
    print(f"Default size limit: {_human_bytes(rules.default_max_bytes)}")
    return EXIT_OK


def _human_bytes(count: int) -> str:
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if count >= size:
            value = count / size
            return f"{value:.0f}{unit}" if value >= 10 else f"{value:.1f}{unit}"
    return f"{count}B"


def cmd_search(args: argparse.Namespace) -> int:
    """Search the index. Layer 4's entry point.

    Read-only, so no lock: searching while an index run is in progress is a
    normal thing to want, and WAL makes it safe.
    """
    from app.index.embedder import Embedder
    from app.search import vector
    from app.search.engine import SearchEngine
    from app.search.rerank import Reranker
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import ImageVectorStore, VectorStore

    settings = _load(args)
    setup_logging(settings.log_path)

    raw = " ".join(args.query or []).strip()
    if not raw:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.search",
            key="query", reason="nothing to search for",
            suggestion=r'Give some search terms, for example: '
                       r'app.cli search "site survey" type:pdf',
        ), args.json)

    if not settings.fts_db.is_file():
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.search",
            key="index", reason="no index has been built yet",
            suggestion=r'Build one first: app.cli index "D:\SearchData"',
        ), args.json)

    embedder = Embedder.from_settings(settings)
    reranker = Reranker.from_settings(
        settings, enabled=settings.rerank_enabled and not args.no_rerank)
    # Work order 0h §1c: the third retrieval lane, same as every other real
    # search entry point in this file.
    clip_text_embedder = vector.clip_text_embedder_from_settings(settings)

    with SqliteStore(settings.fts_db) as store, \
            VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors, \
            ImageVectorStore(settings.vector_path) as image_vectors:
        engine = SearchEngine(
            store, vectors, embedder, reranker=reranker,
            image_vectors=image_vectors, clip_text_embedder=clip_text_embedder,
        )
        try:
            # **Load the models before the clock starts.**
            #
            # The window does this on a background worker at startup; the CLI
            # did not, so both models loaded lazily *inside* the timed block.
            # `retrieve` included loading the embedding model and `rerank`
            # included loading the cross-encoder, which made `timings_ms`
            # measure process startup rather than search.
            #
            # That is not a small distortion. A one-shot CLI search reported
            # `rerank: 3047ms` for work the benchmark measures at 660ms, and
            # the gap was read as the reranker being slow. It made every
            # comparison between models meaningless, which is exactly what the
            # numbers were being used for.
            engine.warm_up()
            response = engine.search(raw, limit=args.limit)
            # **The same federation the window does**, because the standing rule
            # here is that a feature added for one entry point is added for the
            # others - the shape of bug that gets reported as "it works from the
            # app and not from the command line". `git_hits` returns `[]` unless
            # a repository-only switch was typed, so this costs nothing on every
            # other search.
            try:
                from app.search.federate import git_hits

                found = git_hits(
                    store.repos_list(), raw, limit=args.limit,
                    start_rank=len(response.results) + 1,
                )
                response.results.extend(found)
            except Exception as exc:              # noqa: BLE001 - one half
                logger.bind(component="cli.search").warning(
                    "the repository half of the search failed: {}", exc)
        finally:
            engine.close()

        if args.json:
            print(json.dumps(response.as_dict(), indent=2, default=str))
            return EXIT_OK if response.results else EXIT_ERROR

        if not print_response(response, raw):
            return EXIT_ERROR
    return EXIT_OK


def print_response(response: Any, raw: str = "") -> bool:
    """Print one search's results. Returns False when there were none.

    **Extracted so there is exactly one renderer.** `leasha shell` prints
    through this rather than repeating it - the order's rule is "no second
    renderer", and a copy that starts identical is the thing that stops being
    identical. It was inline in `cmd_search` until the REPL needed it.
    """
    if response.parsed and response.parsed.unknown_operators:
        print(f"  (ignored: {', '.join(response.parsed.unknown_operators)})")

    # **Before the results, and on the way out too.** A search that quietly
    # returned worse results is the failure nobody reports, because it
    # looks exactly like one that worked.
    for notice in response.notices:
        print(f"  ! {notice.message}")
    if response.notices:
        print()

    if not response.results:
        print(f"No results for {raw!r}." if raw else "No results.")
        if response.parsed and response.parsed.has_filters:
            print("  The filters may be excluding everything - try without them.")
        return False

    for result in response.results:
        page = f" p{result.page}" if result.page is not None else ""
        print(f"{result.rank:>3}. {result.path}{page}")
        print(f"     {result.explain()}   score {result.score:.4f}")
        print(f"     {_preview(result.text, 200)}")
        print()

    print(f"{len(response.results)} result(s) in {response.elapsed_ms:.0f}ms"
          f"{' (cached)' if response.from_cache else ''}"
          f"{', reranked' if response.reranked else ''}")
    if response.timings:
        print("  " + "  ".join(f"{k} {v:.0f}ms" for k, v in response.timings.items()))
    return True


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.cli",
        description=f"{SHORT_DESCRIPTION}\n\nHeadless entry point.",
    )
    parser.add_argument("--env", help="path to an alternative .env file")
    parser.add_argument("--json", action="store_true", help="machine-readable output")

    # `--json` and `--env` are global, which in argparse means "before the
    # subcommand" - so `app.cli extract PATH --json` fails with an unhelpful
    # "unrecognized arguments". Nobody types them in that order, so they are
    # accepted after the subcommand too.
    #
    # default=SUPPRESS is what makes this safe: without it the subparser writes
    # its own default over whatever the global flag already parsed, and
    # `app.cli --json extract PATH` would silently stop being JSON.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="machine-readable output")
    common.add_argument("--env", default=argparse.SUPPRESS,
                        help="path to an alternative .env file")

    sub = parser.add_subparsers(dest="command", required=True)

    p_stats = sub.add_parser("stats", parents=[common], help="show the resolved configuration")
    p_stats.set_defaults(func=cmd_stats)

    p_init = sub.add_parser("init", parents=[common], help="create and migrate the stores (safe to re-run)")
    p_init.set_defaults(func=cmd_init)

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

    p_doctor = sub.add_parser("doctor", parents=[common], help="verify the environment")
    p_doctor.add_argument("--quick", action="store_true", help="skip model loading")
    p_doctor.set_defaults(func=cmd_doctor)

    p_diagnose = sub.add_parser(
        "diagnose", parents=[common], help="bundle logs, config and environment into one zip for troubleshooting")
    p_diagnose.add_argument("--out", help="write the bundle here instead of logs/diagnostics/")
    p_diagnose.set_defaults(func=cmd_diagnose)

    p_lock = sub.add_parser("lock", parents=[common], help="hold the single-instance lock (diagnostic)")
    p_lock.add_argument("--hold", type=float, default=2.0, help="seconds to hold the lock")
    p_lock.set_defaults(func=cmd_lock)

    p_extract = sub.add_parser(
        "extract",
        parents=[common],
        help="extract and chunk files without indexing them (Layer 2)",
        description=(
            "Read files through the extractors and show what came out. Accepts files or "
            "folders; a folder is walked recursively for supported types. Read-only: "
            "nothing is written and no store is opened."
        ),
    )
    p_extract.add_argument("paths", nargs="*", help="files or folders to extract")
    p_extract.add_argument("--mailbox", action="store_true",
                           help="walk Outlook instead: attached .pst archives plus whatever "
                                "the live mailbox has cached locally (Windows + Outlook only)")
    p_extract.add_argument("--chunks", action="store_true",
                           help="list every chunk with its page, token estimate and offsets")
    p_extract.add_argument("--text", action="store_true",
                           help="print the full extracted text of each document")
    p_extract.add_argument("--full", action="store_true",
                           help="with --chunks, print each chunk whole instead of a preview")
    p_extract.add_argument("--out", metavar="PATH",
                           help="write the JSON report here as UTF-8 (avoids PowerShell's "
                                "UTF-16 redirection); implies --json")
    p_extract.add_argument("--limit", type=int, metavar="N",
                           help="stop after N files (useful when pointed at a large folder)")
    p_extract.add_argument("--include-cloud", action="store_true",
                           help="read OneDrive placeholders too, downloading them (off by default)")
    p_extract.set_defaults(func=cmd_extract)

    p_scan = sub.add_parser(
        "scan", parents=[common],
        help="count the corpus without indexing it - run this before a long index",
        description=(
            "Walk the folders and report what is there: total files and bytes, a "
            "breakdown by file type biggest first, which reader would handle each, "
            "how much is inside .git, and how much is scanned images. Opens no "
            "document, writes no index, and finishes in minutes on 600GB because "
            "it only stats - except for a sample of PDFs, which are opened to see "
            "whether they have a text layer."
        ))
    p_scan.add_argument("roots", nargs="*",
                        help="folders to scan (default: the ones saved in Settings)")
    p_scan.add_argument("--all", action="store_true",
                        help="descend into excluded folders too (node_modules, "
                             "AppData, build) - slower, and answers 'what is on "
                             "this disk' rather than 'what would be indexed'")
    p_scan.add_argument("--sample-pdfs", type=int, default=400, metavar="N",
                        help="PDFs opened to estimate how many are scanned "
                             "(default 400; they are chosen at random)")
    p_scan.add_argument("--sample-archives", type=int, default=200, metavar="N",
                        help="archives whose index is read to estimate what is "
                             "inside the rest (default 200). Nothing is "
                             "decompressed - only the list of members at the "
                             "end of each file")
    p_scan.add_argument("--no-sample", action="store_true",
                        help="open nothing at all - then how much is scanned, "
                             "and what is inside the archives, are reported as "
                             "unknown rather than as zero")
    p_scan.add_argument("--mb-per-minute", type=float, metavar="RATE",
                        help="your measured indexing throughput, to turn the "
                             "byte count into hours. Without it no time is "
                             "estimated, because a guessed rate is worse than none")
    p_scan.add_argument("--quiet", action="store_true", help="no progress lines")
    p_scan.set_defaults(func=cmd_scan)

    p_index = sub.add_parser("index", parents=[common], help="build or update the index")
    p_index.add_argument("roots", nargs="*", help="folders to index")
    p_index.add_argument("--first", action="append", metavar="PATH",
                         help="index this folder before the others; repeatable, in order")
    p_index.add_argument("--workers", type=int, metavar="N",
                         help="files read at once (default: half your cores, capped at 4)")
    p_index.add_argument("--fast", action="store_true",
                         help="trust mtime and size without re-hashing changed files")
    p_index.add_argument("--no-prune", action="store_true",
                         help="keep rows for files that have disappeared")
    p_index.add_argument("--include-cloud", action="store_true",
                         help="index OneDrive placeholders too, downloading them")
    p_index.add_argument("--memory-mb", type=int, metavar="MB",
                         help="pause above this much memory (default from .env, 1500)")
    p_index.add_argument("--cpu-percent", type=int, metavar="PCT",
                         help="pause while the machine is busier than this; 0 disables")
    p_index.add_argument("--full-speed", action="store_true",
                         help="no CPU, battery or priority limits - for a machine "
                              "nobody is using. Will make this one feel slow.")
    p_index.add_argument(
        "--force", action="store_true",
        help="index every file found, ignoring change detection. Use when the\nindex says a file is up to date but its content is missing.")
    # **The two passes.** Mutually exclusive so `--skip-ocr --only-ocr` is
    # refused with a sentence rather than silently resolved to one of them.
    ocr_group = p_index.add_mutually_exclusive_group()
    ocr_group.add_argument(
        "--skip-ocr", "--no-ocr", dest="skip_ocr", action="store_true",
        help="index everything readable without OCR, and queue the images.\n"
             "Search becomes useful in a day or two instead of a fortnight;\n"
             "the queued files are held, not failed.")
    ocr_group.add_argument(
        "--only-ocr", dest="only_ocr", action="store_true",
        help="read only the images, and only walk the image types. This is\n"
             "the second pass - run it behind the first.")
    p_index.add_argument(
        "--recheck-archives", action="store_true",
        help="walk every folder marked as an archive in full, and record a new\n"
             "pass. Use when something has plainly changed inside one.")
    p_index.add_argument(
        "--all-roots", action="store_true",
        help="ignore the Live/Archive modes for this run only, without\n"
             "updating any archive's record")
    p_index.add_argument(
        "--retry-skipped", action="store_true",
        help="re-read files an earlier run skipped, even unchanged ones.\n"
             "Normally a skip is settled: an unchanged file cannot produce a\n"
             "different answer, and re-parsing thousands of known failures every\n"
             "run costs hours. Use this after changing what the machine can do -\n"
             "installing LibreOffice, adding a library, raising a size ceiling.\n"
             "Far cheaper than --force, which re-indexes everything.")
    p_index.add_argument("--quiet", action="store_true", help="no progress lines")
    p_index.set_defaults(func=cmd_index)

    p_convert = sub.add_parser(
        "convert", parents=[common],
        help="export a .pst to a folder of .eml files (no Outlook needed)")
    p_convert.add_argument("archive", help="the .pst file to export")
    p_convert.add_argument("--out", metavar="PATH",
                           help="destination folder (default: alongside the archive)")
    p_convert.add_argument("--quiet", action="store_true", help="no progress lines")
    p_convert.set_defaults(func=cmd_convert)


    p_files = sub.add_parser(
        "files", parents=[common],
        help="find a file by NAME (not by contents) - matches any part of the name")
    p_files.add_argument("name", nargs="*", help="part of a filename, or a folder name")
    p_files.add_argument("--limit", type=int, default=50, metavar="N",
                         help="results to return (default 50)")
    p_files.add_argument("--type", metavar="EXT",
                         help="restrict to these extensions, comma separated: pdf,docx")
    p_files.set_defaults(func=cmd_files)

    p_git = sub.add_parser(
        "gitsearch", parents=[common],
        help="search a repository: its files, its branches, its whole history")
    p_git.add_argument(
        "pattern",
        help='what to look for, with / switches: '
             '"CustomerId /history /extension cs". Run `app.cli commands --git` '
             'for the full list')
    p_git.add_argument("--repo", required=True, metavar="PATH",
                       help="the git checkout to search")
    p_git.add_argument("--depth", type=int, default=2000, metavar="N",
                       help="how many commits back to look (default: 2000)")
    p_git.add_argument("--limit", type=int, metavar="N",
                       help="stop after this many results (default: 2000)")
    p_git.add_argument("--timeout", type=float, metavar="S",
                       help="give up after this long (default: 120)")
    p_git.add_argument("--measure", action="store_true",
                       help="time history search at several depths instead of "
                            "searching - the diagnostic that decided this could exist")
    p_git.add_argument("--rev", default="HEAD", metavar="EXPR",
                       help="--measure only: revision to measure against")
    p_git.add_argument("--depths", metavar="N,N",
                       help="--measure only: commit depths to time at")
    p_git.set_defaults(func=cmd_gitsearch)

    p_repos = sub.add_parser(
        "repos", parents=[common],
        help="list the code repositories found under the indexed folders")
    p_repos.add_argument(
        "--scan", nargs="+", metavar="PATH",
        help="look for repositories under these folders without indexing "
             "anything, and report what a run would find")
    p_repos.add_argument(
        "--forget", metavar="ROOT",
        help="stop treating this folder as a code repository: release every "
             "file attributed to it, remove it from the list, and never adopt "
             "it again. Nothing is deleted and nothing is re-indexed")
    p_repos.add_argument(
        "--remember", metavar="ROOT",
        help="undo --forget, so the next index run may adopt this folder again")
    p_repos.set_defaults(func=cmd_repos)

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
    p_offline.set_defaults(func=cmd_offline_media)

    p_eval = sub.add_parser(
        "evaluate", parents=[common],
        help="measure whether plain sentences find the right documents")
    p_eval.add_argument("--questions", metavar="FILE",
                        help="your own: 'sentence | part-of-the-wanted-path' per line")
    p_eval.add_argument("--builtin", action="store_true",
                        help="use the shipped corpus with known answers (no model needed)")
    p_eval.add_argument("--interpret", action="store_true",
                        help="translate each sentence with Ollama first, to measure the gain")
    p_eval.add_argument(
        "--rerank-model", metavar="NAME",
        help="compare a different reranker without editing .env, e.g. "
             "Xenova/ms-marco-MiniLM-L-6-v2 - implies --rerank")
    p_eval.add_argument(
        "--rerank", action="store_true",
        help="run the full pipeline instead of keyword only - the only way to "
             "measure a reranker or embedding change")
    p_eval.add_argument("--k", type=int, default=1, metavar="N",
                        help="count a hit if the document is in the top N (default 1 - "
                             "did it come FIRST? higher numbers flatter the result)")
    p_eval.set_defaults(func=cmd_evaluate)

    p_bench = sub.add_parser(
        "embed-bench", parents=[common],
        help="measure what embedding costs on this machine, and what would help")
    p_bench.add_argument("--quick", action="store_true",
                         help="inspect the model but do not time it")
    p_bench.set_defaults(func=cmd_embedbench)

    p_index_bench = sub.add_parser(
        "bench-index", parents=[common],
        help="time the whole pipeline on this machine - reading, writing and "
             "the model - and remember the answer")
    p_index_bench.add_argument(
        "--both", action="store_true",
        help="time the processor and the graphics card, to find out which is "
             "actually faster here")
    p_index_bench.add_argument(
        "--no-save", action="store_true",
        help="print the numbers without storing them for auto-tuning")
    p_index_bench.set_defaults(func=cmd_bench_index)

    p_reembed = sub.add_parser(
        "reembed", parents=[common],
        help="rebuild the vector store from SQLite - no documents are re-read")
    p_reembed.add_argument("--all", action="store_true",
                           help="drop every vector and start over, not just the missing ones")
    p_reembed.add_argument("--quiet", action="store_true", help="no progress lines")
    p_reembed.set_defaults(func=cmd_reembed)

    p_commands = sub.add_parser(
        "commands", parents=[common],
        help="list the search filters you can type (/type, /from, /after ...)")
    p_commands.add_argument(
        "--git", action="store_true",
        help="the repository search switches instead (/history, /branch, "
             "/introduced ...) - a different engine over a different store")
    p_commands.set_defaults(func=cmd_commands)

    p_ollama = sub.add_parser(
        "ollama", parents=[common],
        help="check the Ollama connection (optional; only the Interpret button uses it)")
    p_ollama.add_argument(
        "--translate", metavar="SENTENCE",
        help="also run one real translation end to end, and show what came back")
    p_ollama.add_argument(
        "--model", metavar="NAME",
        help="check this model instead of OLLAMA_MODEL - try one before committing to it")
    p_ollama.add_argument(
        "--timeout", type=float, metavar="SECONDS",
        help="seconds to allow the model for --translate (default: %(default)s)"
             % {"default": "30"})
    p_ollama.set_defaults(func=cmd_ollama)

    p_shell = sub.add_parser(
        "shell", parents=[common],
        help="interactive search with a dropdown (Ctrl+D to leave)")
    p_shell.set_defaults(func=cmd_shell)

    p_open = sub.add_parser(
        "open", parents=[common],
        help="act on a leasha:// link, or register the scheme")
    p_open.add_argument(
        "url", nargs="?", default="",
        help=("the link, or one of: register, unregister, show"))
    p_open.add_argument(
        "--path", default="",
        help="what a link should run; defaults to this installation")
    p_open.set_defaults(func=cmd_open)

    p_completions = sub.add_parser(
        "completions", parents=[common],
        help="tab completion for PowerShell")
    p_completions.add_argument(
        "action", nargs="?", default="", choices=["", "install"],
        help="omit to print the script; 'install' to add it to a profile")
    p_completions.add_argument(
        "--powershell", action="store_true",
        help="emit the PowerShell completer (the default and only shell today)")
    p_completions.add_argument(
        "--path", default="",
        help="the profile to change, normally $PROFILE")
    p_completions.add_argument(
        "--remove", action="store_true",
        help="take the completer back out of the profile")
    p_completions.set_defaults(func=cmd_completions)

    p_rerank = sub.add_parser(
        "rerank-bench", parents=[common],
        # `%%`, not `%`. argparse runs every help string through `%`
        # formatting to expand `%(default)s`, and "93% of" is read as the
        # conversion `% o` - a space-flagged octal - which wants an integer and
        # gets argparse's dict. It crashed the whole top-level `--help`, not
        # just this line, and `rerank-bench --help` kept working because a
        # subparser only formats its own strings.
        help="time reranking per model - it was 93%% of one 9-second search")
    p_rerank.add_argument("--model", help="time only this one")
    p_rerank.add_argument("--count", type=int, default=30,
                          help="candidates to score (default: %(default)s)")
    p_rerank.add_argument("--window", type=int, default=600,
                          help="characters per passage (default: %(default)s)")
    p_rerank.add_argument("--passes", type=int, default=3,
                          help="runs per model, for the spread (default: %(default)s)")
    p_rerank.set_defaults(func=cmd_rerank_bench)

    p_formats = sub.add_parser(
        "formats", parents=[common],
        help="what file types are indexed, what reads them, and what is off")
    p_formats.add_argument("--all", action="store_true",
                           help="also list the extensions built into the code")
    p_formats.add_argument(
        "--groups", action="store_true",
        help="the source and code types by ecosystem - Microsoft, Oracle, IBM, "
             "industrial control - which is the form worth reviewing")
    p_formats.set_defaults(func=cmd_formats)

    p_search = sub.add_parser("search", parents=[common], help="search the index")
    p_search.add_argument("query", nargs="*",
                          help='search terms; supports type: after: before: path: from: '
                               '"phrases" and -exclusions')
    p_search.add_argument("--limit", type=int, default=20, metavar="N",
                          help="results to return (default 20)")
    p_search.add_argument("--no-rerank", action="store_true",
                          help="skip the cross-encoder even if it is enabled")
    p_search.set_defaults(func=cmd_search)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # **Opened before the command runs, not inside it.** A run that fails at
    # configuration - the single most common way this application has gone
    # wrong on the owner's machine - is exactly the run worth having a file
    # for, and by the time `_load` raises it is too late to start one.
    # `log_dir_for` answers "which folder" without validating anything, so a
    # broken `.env` still gets logged rather than losing its own evidence.
    run = start_run(log_dir_for(Path(args.env) if getattr(args, "env", None)
                                else None),
                    getattr(args, "command", "cli"),
                    argv=list(argv) if argv is not None else sys.argv[1:])

    code = EXIT_ERROR
    try:
        code = int(args.func(args))
        return code
    except AppErrorException as exc:
        # Logging may not be configured yet (a bad .env fails before setup),
        # so print unconditionally and log only on a best-effort basis.
        try:
            log_app_error(exc.error)
        except Exception:  # noqa: BLE001
            pass
        code = _report(exc.error, getattr(args, "json", False))
        return code
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        code = EXIT_ERROR
        return code
    except BaseException as exc:
        # Recorded, then re-raised unchanged. The sink never sees an exception
        # nobody caught, so without this the run log would end at whatever line
        # happened to be logged last - the least useful place to stop.
        run.unhandled(exc)
        code = "crash"
        raise
    finally:
        path = run.finish(code)
        # Named on the console only when it is worth opening. A line printed
        # after every successful `search` is furniture within a day, and
        # furniture is what people stop reading.
        if code != EXIT_OK or run.errors:
            print(f"\nRun log: {path}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
