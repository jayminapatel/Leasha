r"""`app.cli photos` - the Photos tab's library and "Write names into photos".

Layer: L3 (CLI before UI - non-negotiable 8)

2026-10-05. `photos` lists the library as the Photos tab narrows it, with the
same box words (`who:Jason date:2019 only:unnamed`). `photos --write-names`
is option b, the owner's exception to non-negotiable 10: it writes the people
and description into the photos' XMP - sidecar files by default, into JPEG and
PNG themselves with `--inside`, after a copy of each is kept.

    python -m app.cli photos "who:Jason date:2022" --limit 20
    python -m app.cli photos --write-names --dry-run
    python -m app.cli photos --write-names --inside --backup D:\Leasha\photo_backups
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import threading
from pathlib import Path
from typing import Any

from app.cli._common import EXIT_OK, _load, _report
from app.core.errors import AppErrorException
from app.core.logging import setup_logging

__all__ = ["cmd_photos", "add_photos_parser"]


def _list(store: Any, args: argparse.Namespace) -> int:
    from app.extract.ocr import OcrExtractor
    from app.search.run import read_typed, words_of
    from app.ui.presenter.photos import date_text, narrow, people_text, sort_rows

    rows = store.photo_library(OcrExtractor.extensions)
    parsed, _applied = read_typed(store, args.query or "", surface="files")
    shown = sort_rows(narrow(rows, parsed, words_of(parsed)), parsed.sort or "newest")
    if args.json:
        print(json.dumps({"total": len(rows), "shown": len(shown), "photos": [
            {"path": r.path, "taken": date_text(r), "people": list(r.people),
             "place": r.place, "described": r.described} for r in shown[:args.limit]]},
            indent=2))
        return EXIT_OK
    print(f"{len(shown):,} of {len(rows):,} photo(s)")
    for row in shown[:args.limit]:
        extra = "  ".join(p for p in (people_text(row), row.place or "") if p)
        print(f"  {date_text(row):<26} {row.path}  {extra}")
    return EXIT_OK


def _write(store: Any, args: argparse.Namespace, settings: Any) -> int:
    from app.index.photo_metadata import INSIDE, SIDECAR, run_write

    if args.dry_run:
        rows = store.photo_metadata_rows()
        print(f"{len(rows):,} photo(s) would get names or a description:")
        for _file_id, path, people, description in rows[:args.limit]:
            print(f"  {path}  {', '.join(people)}  {description[:60]}")
        return EXIT_OK
    backup = Path(args.backup) if args.backup else (
        Path(settings.data_path) / "photo_backups" / _dt.date.today().isoformat())
    progress: dict = {}
    result = run_write(store, None, where=INSIDE if args.inside else SIDECAR,
                       backup_root=backup, progress=progress, stop=threading.Event())
    print(f"written into {result.written:,} photo(s), {result.sidecars:,} sidecar(s), "
          f"{result.unchanged:,} already had them, {result.failed:,} failed")
    for problem in result.problems[:10]:
        print(f"  {problem}")
    if result.backed_up_bytes:
        print(f"copies kept in {backup}")
    return EXIT_OK


def cmd_photos(args: argparse.Namespace) -> int:
    from app.storage.sqlite_store import SqliteStore

    settings = _load(args)
    setup_logging(settings.log_path)
    try:
        with SqliteStore(settings.fts_db) as store:
            if args.write_names:
                return _write(store, args, settings)
            return _list(store, args)
    except AppErrorException as exc:
        return _report(exc.error, getattr(args, "json", False))


def add_photos_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p = sub.add_parser("photos", parents=[common],
                       help="the photo library, narrowed as the Photos tab narrows it; "
                            "and writing names into photos")
    p.add_argument("query", nargs="?", default="",
                   help='box words, e.g. "who:Jason date:2019 only:unnamed"')
    p.add_argument("--limit", type=int, default=50, help="how many to print (default 50)")
    p.add_argument("--write-names", action="store_true",
                   help="write people and descriptions into the photos' XMP metadata")
    p.add_argument("--inside", action="store_true",
                   help="with --write-names: into JPEG and PNG themselves, after a copy "
                        "(default: .xmp sidecar files, no photo changed)")
    p.add_argument("--backup", metavar="FOLDER",
                   help="with --inside: where the copies go (default: the data "
                        "folder's photo_backups)")
    p.add_argument("--dry-run", action="store_true",
                   help="with --write-names: say what would be written, write nothing")
    p.set_defaults(func=cmd_photos)
