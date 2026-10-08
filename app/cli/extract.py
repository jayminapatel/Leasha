"""`extract` and `convert`: reading files and archives without indexing them.

Layer: L2

Exit codes: `extract` exits 1 when nothing at all could be extracted, so a
script can tell "the reader is broken" from "it read some and skipped some" -
one skipped file is normal (non-negotiable 3) and exits 0. `convert` exits 1
when no message was written.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Sequence

from app.cli._common import EXIT_ERROR, EXIT_OK, _load, _preview, _report
from app.core.errors import AppErrorException, make_error
from app.core.logging import log_app_error, logger, setup_logging

#: Directories never worth walking. Not a substitute for Layer 3's configurable
#: exclusions - just enough that pointing this at a project folder is useful.
_SKIP_DIRS = {
    "venv", ".venv", ".git", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "$RECYCLE.BIN",
}


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


def add_extract_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
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


def add_convert_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_convert = sub.add_parser(
        "convert", parents=[common],
        help="export a .pst to a folder of .eml files (no Outlook needed)")
    p_convert.add_argument("archive", help="the .pst file to export")
    p_convert.add_argument("--out", metavar="PATH",
                           help="destination folder (default: alongside the archive)")
    p_convert.add_argument("--quiet", action="store_true", help="no progress lines")
    p_convert.set_defaults(func=cmd_convert)
