"""`report`: read-only reports over the existing index.

Layer: L4 (reports - `app.reports`, which reads L1 and never writes to it)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from app.cli._common import EXIT_OK, _load, _report, _saved_roots
from app.core.errors import make_error
from app.core.logging import setup_logging


def cmd_report(args: argparse.Namespace) -> int:
    r"""Reports: read-only surfaces over the existing index.

    Layer 4's entry point for order 202626270602 (0n), shipped before the
    Reports page per non-negotiable 8. Text output here; the PDF export
    named in the order's own 2b is a UI-only concern (`QPrinter`), so the
    CLI proves the report's content and wording headlessly, which is what
    non-negotiable 8 actually asks for.
    """
    from dataclasses import replace as _replace

    from app.reports.inheritance import (
        catalogue_sources,
        render_inheritance_document,
        report_generated_at,
    )
    from app.storage.sqlite_store import SqliteStore

    settings = _load(args)
    setup_logging(settings.log_path)

    name = str(getattr(args, "name", "") or "").strip().lower()
    if name in ("space", "space-report"):
        return _cmd_report_space(settings, args)
    if name not in ("inheritance", "digital-inheritance"):
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.report",
            key="name", reason=f"no report named {name!r}",
            suggestion="Try: leasha report inheritance, or leasha report space",
        ), args.json)

    excluded = {
        part.strip().lower() for part in str(args.exclude or "").split(",")
        if part.strip()
    }

    with SqliteStore(settings.fts_db) as store:
        roots = _saved_roots(settings)
        sources = catalogue_sources(store, roots=roots)
        generated_at = report_generated_at(store)

    if excluded:
        sources = [
            s if s.name.strip().lower() not in excluded else _replace(s, include=False)
            for s in sources
        ]

    document = render_inheritance_document(sources, generated_at=generated_at)

    if args.json:
        print(json.dumps({
            "sources": [
                {"name": s.name, "kind": s.kind, "file_count": s.file_count,
                 "size_bytes": s.size_bytes, "status": s.status,
                 "include": s.include}
                for s in sources
            ],
            "generated_at": generated_at,
        }, indent=2, default=str))
        return EXIT_OK

    if args.out:
        Path(args.out).write_text(document, encoding="utf-8")
        print(f"Written to {args.out}")
        return EXIT_OK

    print(document)
    return EXIT_OK


def _cmd_report_space(settings: Any, args: argparse.Namespace) -> int:
    r"""Order 202626270602 (0n) section 3: duplicates and the "only copy"
    warning. Split out from `cmd_report` for the same reason `cmd_repos`
    stands apart from `cmd_report` itself - a second report is a second
    function, not a second set of branches threaded through the first one.
    """
    from app.reports.inheritance import report_generated_at
    from app.reports.space import (
        find_duplicate_groups,
        find_near_duplicate_photo_groups,
        find_source_duplicate_share,
        find_source_uniqueness,
        hash_coverage,
        render_space_document,
        total_reclaimable_bytes,
    )
    from app.storage.sqlite_store import SqliteStore

    with SqliteStore(settings.fts_db) as store:
        groups = find_duplicate_groups(store)
        reclaimable = total_reclaimable_bytes(store)
        uniqueness = find_source_uniqueness(store)
        near_duplicates = find_near_duplicate_photo_groups(store)
        duplicate_share = find_source_duplicate_share(store)
        generated_at = report_generated_at(store)
        coverage = hash_coverage(store)

    if args.json:
        print(json.dumps({
            **coverage,
            "duplicate_groups": [
                {"content_hash": g.content_hash, "size_bytes": g.size_bytes,
                 "reclaimable_bytes": g.reclaimable_bytes,
                 "copies": [{"path": c.path, "source_name": c.source_name,
                            "source_kind": c.source_kind} for c in g.copies]}
                for g in groups
            ],
            "total_reclaimable_bytes": reclaimable,
            "near_duplicate_photo_groups": [
                {"representative_phash": g.representative_phash,
                 "copies": [{"path": c.path, "source_name": c.source_name,
                            "source_kind": c.source_kind} for c in g.copies],
                 "sizes_bytes": list(g.sizes_bytes)}
                for g in near_duplicates
            ],
            "source_duplicate_share": [
                {"name": s.name, "kind": s.kind, "status": s.status,
                 "duplicate_count": s.duplicate_count, "total_count": s.total_count,
                 "share": s.share} for s in duplicate_share
            ],
            "source_uniqueness": [
                {"name": u.name, "kind": u.kind, "status": u.status,
                 "file_count": u.file_count} for u in uniqueness
            ],
            "generated_at": generated_at,
        }, indent=2, default=str))
        return EXIT_OK

    document = render_space_document(
        groups, uniqueness, total_reclaimable=reclaimable, generated_at=generated_at,
        near_duplicates=near_duplicates, duplicate_share=duplicate_share, **coverage)

    if args.out:
        Path(args.out).write_text(document, encoding="utf-8")
        print(f"Written to {args.out}")
        return EXIT_OK

    print(document)
    return EXIT_OK


def add_report_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_report = sub.add_parser(
        "report", parents=[common],
        help="a read-only report over the existing index - the catalogue, "
             "not a search")
    p_report.add_argument(
        "name", metavar="NAME",
        help="which report: inheritance (the Digital Inheritance catalogue), "
             "space (duplicates and the only-copy warning)")
    p_report.add_argument(
        "--out", metavar="FILE",
        help="write the report's text to this file instead of stdout")
    p_report.add_argument(
        "--exclude", metavar="NAME,NAME",
        help="202626270602 2c: leave these sources out of the report by name, "
             "comma-separated")
    p_report.set_defaults(func=cmd_report)
