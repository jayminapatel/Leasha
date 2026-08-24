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
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional, Sequence

from app.core.config import Settings, load_settings, project_root
from app.core.errors import AppError, AppErrorException, make_error
from app.core.logging import setup_logging, log_app_error, logger
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


#: Directories never worth walking. Not a substitute for Layer 3's configurable
#: exclusions - just enough that pointing this at a project folder is useful.
_SKIP_DIRS = {
    "venv", ".venv", ".git", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "$RECYCLE.BIN",
}


def _iter_targets(paths: Sequence[str]) -> "list[Path]":
    """Expand the arguments into files. A folder is walked recursively.

    Unsupported extensions are filtered out *here* rather than being reported as
    skips, because listing every .exe and .dll in a folder as "skipped" would
    bury the failures that actually matter.
    """
    from app.extract import extractor_for

    found: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if path.is_file():
            found.append(path)                       # named explicitly: always attempt it
            continue
        if not path.is_dir():
            found.append(path)                       # let the extractor report it missing
            continue
        import os

        for directory, subdirectories, filenames in os.walk(path):
            subdirectories[:] = [d for d in subdirectories if d not in _SKIP_DIRS]
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
    from app.core.errors import AppErrorException as _AppErrorException
    from app.core.winfs import describe_placeholder, file_attributes, is_cloud_placeholder
    from app.extract import chunk_document, extract

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

    targets = _iter_targets(args.paths)
    if not targets:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.extract",
            key="paths", reason="no supported files found at " + ", ".join(args.paths),
        ), args.json)

    results: list[dict[str, Any]] = []
    skipped_by_code: dict[str, int] = {}
    total_bytes = 0
    total_seen_bytes = 0
    total_chunks = 0
    started = time.perf_counter()

    for path in targets[: args.limit] if args.limit else targets:
        record: dict[str, Any] = {"path": str(path)}
        # Sized before anything can skip it. A 100MB archive that vanishes from
        # the byte accounting because it was skipped makes the totals a lie.
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        record["size_bytes"] = size
        total_seen_bytes += size

        # Checked before opening: reading a placeholder is what triggers the
        # download, so this must happen first or the check is pointless.
        attributes = file_attributes(path)
        if not args.include_cloud and is_cloud_placeholder(path, attributes):
            error = make_error(
                "ERR_CLOUD_ONLY", "cli.extract",
                path=str(path), details=describe_placeholder(attributes),
            )
            record.update(status="skipped", code=error.code, message=error.message,
                          suggestion=error.suggestion, detail=error.details)
            skipped_by_code[error.code] = skipped_by_code.get(error.code, 0) + 1
            results.append(record)
            continue

        file_started = time.perf_counter()
        try:
            documents = list(extract(path))
        except _AppErrorException as exc:
            log_app_error(exc.error)
            record.update(status="skipped", code=exc.error.code, message=exc.error.message,
                          suggestion=exc.error.suggestion, detail=exc.error.details)
            skipped_by_code[exc.error.code] = skipped_by_code.get(exc.error.code, 0) + 1
            results.append(record)
            continue

        total_bytes += size

        document_records = []
        for document in documents:
            chunks = chunk_document(document)
            total_chunks += len(chunks)
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
                "meta": {k: v for k, v in document.meta.items() if not isinstance(v, (list, dict))},
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
        results.append(record)

    elapsed = time.perf_counter() - started
    extracted = [r for r in results if r["status"] == "extracted"]
    summary = {
        "files_seen": len(results),
        "extracted": len(extracted),
        "skipped": len(results) - len(extracted),
        "skipped_by_code": skipped_by_code,
        "chunks": total_chunks,
        "bytes": total_bytes,
        "bytes_seen": total_seen_bytes,
        "elapsed_s": round(elapsed, 3),
        "throughput_mb_s": round(total_bytes / 1_048_576 / elapsed, 2) if elapsed > 0 else None,
        "files_per_s": round(len(results) / elapsed, 1) if elapsed > 0 else None,
    }

    if args.json or args.out:
        payload = json.dumps({"summary": summary, "results": results}, indent=2, default=str)
        if args.out:
            # Written here, in UTF-8, rather than left to the shell. Windows
            # PowerShell 5.1 redirection (`> file`) emits UTF-16LE with a BOM,
            # which every JSON reader then chokes on - the same encoding trap
            # that killed install.ps1 at parse time.
            out_path = Path(args.out).expanduser()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(payload, encoding="utf-8")
            print(f"Wrote {out_path} ({len(payload) / 1024:.0f} KB, UTF-8)", file=sys.stderr)
        else:
            print(payload)
        return EXIT_OK if extracted else EXIT_ERROR

    for record in results:
        if record["status"] == "skipped":
            print(f"SKIP  {record['path']}")
            print(f"      [{record['code']}] {record['message']}")
            if record.get("suggestion"):
                print(f"      FIX: {record['suggestion']}")
            if record.get("detail"):
                print(f"      {_preview(str(record['detail']), 200)}")
            print()
            continue

        for document in record["documents"]:
            pages = f", pages {document['pages'][0]}-{document['pages'][-1]}" if document["pages"] else ""
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

    seen_mb = summary["bytes_seen"] / 1_048_576
    read_mb = summary["bytes"] / 1_048_576
    unread = f", {seen_mb - read_mb:.2f} MB skipped" if seen_mb - read_mb > 0.01 else ""
    print(f"{summary['extracted']} extracted, {summary['skipped']} skipped, "
          f"{summary['chunks']} chunks from {read_mb:.2f} MB read{unread} "
          f"in {summary['elapsed_s']}s")
    if summary["throughput_mb_s"] is not None and summary["bytes"] > 0:
        print(f"  {summary['throughput_mb_s']} MB/s, {summary['files_per_s']} files/s "
              f"(extraction only - embedding is Layer 3 and is far slower)")
    if skipped_by_code:
        print(f"  skipped: {skipped_by_code}")
    print("  Nothing was written. Building the index is Layer 3.")

    # Flush before logging: loguru writes to stderr unbuffered, so without this
    # the summary log line jumps ahead of the report whenever stdout is piped.
    sys.stdout.flush()
    log.info("extract: {}", summary)
    return EXIT_OK if extracted else EXIT_ERROR


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


def cmd_index(args: argparse.Namespace) -> int:
    """Build or update the index. Layer 3's entry point.

    Unlike `extract`, this one writes - so it takes the single-instance lock.
    Two copies indexing into one SQLite file is exactly the corruption the
    mutex exists to prevent.
    """
    from app.index.embedder import Embedder
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.walker import WalkConfig
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore

    settings = _load(args)
    setup_logging(settings.log_path)
    log = logger.bind(component="cli.index")

    roots = [Path(root).expanduser() for root in (args.roots or [])]
    if not roots:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.index",
            key="roots", reason="give at least one folder to index",
            suggestion=r'Name the folders to index, for example: '
                       r'app.cli index "D:\SearchData"',
        ), args.json)

    missing = [root for root in roots if not root.exists()]
    if missing:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.index",
            key="roots", reason=f"does not exist: {', '.join(str(m) for m in missing)}",
            suggestion="Check the path and the drive. A folder on a disconnected drive looks "
                       "exactly like a folder that was deleted.",
        ), args.json)

    limits = limits_from_settings(settings)
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
        ),
        limits=limits,
        min_free_gb=settings.min_free_gb,
        verify_hash=not args.fast,
        prune_missing=not args.no_prune,
    )

    embedder = Embedder(
        settings.embed_model, dim=settings.embed_dim, cache_dir=str(settings.model_cache)
    )

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

    with SingleInstance(), \
            SqliteStore(settings.fts_db) as store, \
            VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors:
        pipeline = Pipeline(store, vectors, embedder, config)
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
    print(f"Files     {stats.seen:,} seen, {stats.unchanged:,} unchanged")
    if stats.unchanged_documents:
        print(f"          {stats.unchanged_documents:,} document(s) inside them were "
              f"already up to date")
    print(f"Skipped   {stats.skipped:,}   Deleted {stats.deleted:,}")
    print(f"Read      {stats.bytes_read / 1_048_576:,.1f} MB in {stats.elapsed_s:,.1f}s")
    if stats.pauses:
        # Said plainly, because a four-hour run that was mostly waiting looks
        # identical to a four-hour run that was slow - and the fix is opposite.
        print(f"Waited    {stats.paused_seconds / 60:,.1f} min across {stats.pauses} "
              f"pause(s) to stay out of the way")
    print(f"          {stats.files_per_minute:,.0f} files/min, {stats.mb_per_minute:,.1f} MB/min")
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


def cmd_graph(args: argparse.Namespace) -> int:
    """Build, enrich, inspect or draw the knowledge graph. Layer 6's entry point.

    Takes the single-instance lock: it writes, and two builds accumulating edge
    weights into one database would double every count between them.

    `--enrich` is the only part that needs Ollama, and it is the only part that
    can be skipped entirely without losing the feature.
    """
    from app.graph import render
    from app.graph.builder import GraphBuilder
    from app.storage.sqlite_store import SqliteStore

    settings = _load(args)
    setup_logging(settings.log_path)
    log = logger.bind(component="cli.graph")

    with SingleInstance(), SqliteStore(settings.fts_db) as store:
        payload: dict[str, Any] = {}

        if not args.show_only:
            def tick(progress) -> None:
                if not args.quiet:
                    print(
                        f"  {progress.phase:<8} {progress.chunks_done:>8,} chunks  "
                        f"{progress.entities:>7,} entities  {progress.edges:>8,} connections",
                        flush=True,
                    )

            total = int(store.stats().get("chunks_total", 0))
            builder = GraphBuilder(
                store,
                min_weight=args.min_weight,
                min_npmi=args.min_pmi,
                on_progress=None if args.quiet else tick,
            )
            result = builder.build(rebuild=args.rebuild, chunks_total=total)
            payload["build"] = result.as_dict()

        if args.enrich:
            from app.graph.entities_llm import EntityEnricher
            from app.llm.ollama import OllamaClient

            client = OllamaClient(settings.ollama_url, settings.ollama_model)
            enricher = EntityEnricher(store, client)
            enriched = enricher.run(chunks_total=int(store.stats().get("chunks_total", 0)))
            payload["enrich"] = enriched.as_dict()
            if enriched.error is not None and not args.json:
                print()
                print(enriched.error.render())

        stats = store.graph_stats()
        payload["graph"] = stats

        entities = store.top_entities(max(args.top, render.MAX_RENDER_NODES if args.html else args.top))
        view = render.select_top(
            entities, store.edges_among, limit=render.MAX_RENDER_NODES if args.html else args.top
        )

        if args.html:
            out = Path(args.html).expanduser()
            try:
                render.render_html(view, out, title=f"Knowledge graph - {settings.fts_db.parent.name}")
                payload["html"] = str(out)
            except AppErrorException as exc:
                # The picture is optional; the numbers below are not. Report and
                # carry on rather than failing a command that mostly succeeded.
                return _report(exc.error, args.json)

        if args.json:
            print(json.dumps(payload, indent=2, default=str))
            return EXIT_OK

        print()
        print(f"Entities  {stats['entities']:,}   Connections {stats['edges']:,}")
        if stats["by_kind"]:
            print("By kind:  " + "  ".join(f"{k}={v:,}" for k, v in sorted(stats["by_kind"].items())))
        if stats["entities"] == 0:
            print()
            print("Nothing yet. Index some documents first, then run this again:")
            print(r'  venv\Scripts\python.exe -m app.cli index "D:\SearchData"')
            return EXIT_OK

        print()
        print(f"Most connected ({min(args.top, len(view.nodes))} of {stats['entities']:,}):")
        labels = {int(node["id"]): str(node["display"]) for node in view.nodes}
        for node in view.nodes[: args.top]:
            print(f"  {str(node['display'])[:44]:<44} {node['kind']:<9} {node['doc_count']:>6,} docs")

        if args.entity:
            wanted = args.entity.casefold()
            match = next((i for i, label in labels.items() if label.casefold() == wanted), None)
            print()
            if match is None:
                print(f"No entity named '{args.entity}'. Names above are exact.")
            else:
                print(f"'{labels[match]}' is connected to:")
                for row in render.neighbourhood(match, view.edges, labels):
                    print(f"  {row['label'][:40]:<40} {row['weight']:>4} passages  npmi {row['pmi']:.2f}")
                print()
                print("Seen in:")
                for row in store.chunks_mentioning(match, 5):
                    print(f"  {row['path']}")

        if args.html:
            print()
            print(f"Interactive graph written to {payload['html']}")
            print("  Open it in a browser. It is fully self-contained and works offline.")

        log.info("graph command complete: {}", payload.get("graph"))
        return EXIT_OK


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


def cmd_search(args: argparse.Namespace) -> int:
    """Search the index. Layer 4's entry point.

    Read-only, so no lock: searching while an index run is in progress is a
    normal thing to want, and WAL makes it safe.
    """
    from app.index.embedder import Embedder
    from app.search.engine import SearchEngine
    from app.search.rerank import Reranker
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore

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

    embedder = Embedder(
        settings.embed_model, dim=settings.embed_dim, cache_dir=str(settings.model_cache)
    )
    reranker = Reranker(
        settings.rerank_model, cache_dir=str(settings.model_cache),
        enabled=settings.rerank_enabled and not args.no_rerank,
    )

    with SqliteStore(settings.fts_db) as store, \
            VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors:
        engine = SearchEngine(store, vectors, embedder, reranker=reranker)
        try:
            response = engine.search(raw, limit=args.limit)
        finally:
            engine.close()

        if args.json:
            print(json.dumps(response.as_dict(), indent=2, default=str))
            return EXIT_OK if response.results else EXIT_ERROR

        if response.parsed and response.parsed.unknown_operators:
            print(f"  (ignored: {', '.join(response.parsed.unknown_operators)})")

        if not response.results:
            print(f"No results for {raw!r}.")
            if response.parsed and response.parsed.has_filters:
                print("  The filters may be excluding everything - try without them.")
            return EXIT_ERROR

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
    return EXIT_OK


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

    p_graph = sub.add_parser(
        "graph", parents=[common],
        help="build and inspect the knowledge graph over the indexed chunks")
    p_graph.add_argument("--rebuild", action="store_true",
                         help="discard the existing graph and start over "
                              "(safe: it is derived from the index)")
    p_graph.add_argument("--show-only", action="store_true",
                         help="report what is already stored without building")
    p_graph.add_argument("--enrich", action="store_true",
                         help="also run typed entity extraction through Ollama, "
                              "resuming where the last run stopped")
    p_graph.add_argument("--top", type=int, default=25, metavar="N",
                         help="entities to list (default 25)")
    p_graph.add_argument("--entity", metavar="NAME",
                         help="show what one entity connects to, and where it appears")
    p_graph.add_argument("--html", metavar="PATH",
                         help="also write a self-contained interactive page")
    p_graph.add_argument("--min-weight", type=int, default=2, metavar="N",
                         help="drop connections seen in fewer than N passages (default 2)")
    p_graph.add_argument("--min-pmi", type=float, default=0.0, metavar="X",
                         help="drop connections at or below this normalised PMI (default 0.0)")
    p_graph.add_argument("--quiet", action="store_true", help="no progress lines")
    p_graph.set_defaults(func=cmd_graph)

    p_files = sub.add_parser(
        "files", parents=[common],
        help="find a file by NAME (not by contents) - matches any part of the name")
    p_files.add_argument("name", nargs="*", help="part of a filename, or a folder name")
    p_files.add_argument("--limit", type=int, default=50, metavar="N",
                         help="results to return (default 50)")
    p_files.add_argument("--type", metavar="EXT",
                         help="restrict to these extensions, comma separated: pdf,docx")
    p_files.set_defaults(func=cmd_files)

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
