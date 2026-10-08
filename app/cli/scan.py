"""`scan`: counting the corpus before a long index.

Layer: L3 (`app.index.scan`; the walker's rules, none of its writes)
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from app.cli._common import EXIT_OK, _load, _report, _saved_roots
from app.cli._progress import ProgressLine
from app.core.config import Settings
from app.core.errors import make_error
from app.core.logging import logger, setup_logging


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


def add_scan_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
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
