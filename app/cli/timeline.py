"""`timeline`: everything from a stretch of time, across every source.

Layer: L4 entry point. Order 202626270602 (0n) section 4, shipped before the
window per non-negotiable 8.

    leasha timeline                          what the timeline holds, by year and month
    leasha timeline --month 2015-06          everything from June 2015
    leasha timeline --year 2015 --kind photos
    leasha timeline --after 2015-06-01 --before 2015-08-31
    leasha timeline --month 2015-06 --limit 50 --cursor <printed at the end of a page>

Read-only: it opens the index and reads it, and writes nothing anywhere.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from app.cli._common import EXIT_OK, _load, _report
from app.core.errors import make_error
from app.core.logging import setup_logging


def _bad(key: str, reason: str, suggestion: str, as_json: bool) -> int:
    return _report(make_error(
        "ERR_CONFIG_INVALID", "cli.timeline", key=key, reason=reason,
        suggestion=suggestion), as_json)


def _period(args: argparse.Namespace) -> Any:
    """The period the flags name, or None for "just tell me what is there".
    Raises ValueError with a plain sentence when a flag is not a date."""
    from app.reports.timeline import Period
    from app.reports.timeline_words import BAD_DATE

    month, year = str(args.month or "").strip(), args.year
    after, before = str(args.after or "").strip(), str(args.before or "").strip()
    if month:
        found = Period.from_words(month, month)          # 2015-06 -> the whole of June
        if found is None or not found.is_month:
            raise ValueError(BAD_DATE)
        return found
    if year:
        return Period.year(int(year))
    if after or before:
        found = Period.from_words(after, before)
        if found is None:
            raise ValueError(BAD_DATE)
        return found
    return None


def _entry_json(entry: Any) -> dict:
    return {"file_id": entry.file_id, "path": entry.path, "name": entry.name,
            "kind": entry.kind, "when": entry.when.isoformat(timespec="seconds"),
            "date_from": entry.basis, "source": entry.source_name or None,
            "source_status": entry.source_status or None,
            "reachable_now": entry.reachable, "size_bytes": entry.size_bytes}


def _print_overview(overview: Any, as_json: bool) -> None:
    from app.reports.timeline_words import summary_sentence, thin_data_notes

    if as_json:
        print(json.dumps({
            "total": overview.total,
            "months": [{"year": y, "month": m, "count": c} for y, m, c in overview.months],
            "dated_by": {"camera": overview.by_camera, "folder_guess": overview.by_folder_guess,
                         "file_date": overview.by_file_date, "sent": overview.by_sent_date},
            "undated": overview.undated, "generated_at": overview.generated_at,
        }, indent=2))
        return
    print(summary_sentence(overview))
    for year, count in overview.years:
        cells = "  ".join(f"{month:02d}:{n}" for month, n in overview.months_of(year).items() if n)
        print(f"  {year}  {count:>8,}   {cells}")
    for note in thin_data_notes(overview):
        print(f"\nNote: {note}")


def _print_page(period: Any, page: Any, kind: str, as_json: bool) -> None:
    from app.reports.timeline_words import (
        badge_words, basis_words, day_heading, empty_period_sentence, period_words,
    )

    if as_json:
        print(json.dumps({
            "period": period_words(period), "kind": kind,
            "items": [{**_entry_json(fold.head),
                       "also": [_entry_json(o) for o in fold.older],
                       "folded_because": fold.label() or None}
                      for fold in page.items],
            "next_cursor": page.cursor.encode() if page.cursor else None,
        }, indent=2))
        return
    print(period_words(period))
    if not page.items:
        print(empty_period_sentence(period))
        return
    day = None
    for fold in page.items:
        entry = fold.head
        if entry.when.date() != day:
            day = entry.when.date()
            print(f"\n{day_heading(entry.when)}")
        badge = badge_words(entry)
        line = f"  {entry.when:%H:%M}  {basis_words(entry.basis):<28} {entry.kind:<9} {entry.name}"
        if badge:
            line += f"   [{badge}]"
        if fold.folded:
            line += f"   ({fold.label()})"
        print(line)
    if page.cursor is not None:
        print(f"\nMore follow. Continue with: --cursor {page.cursor.encode()}")


def cmd_timeline(args: argparse.Namespace) -> int:
    from app.reports.timeline import (
        KINDS, PAGE_SIZE, Cursor, timeline_overview, timeline_page,
    )
    from app.storage.sqlite_store import SqliteStore

    as_json = bool(getattr(args, "json", False))
    kind = str(args.kind or "everything").lower()
    if kind not in KINDS:
        return _bad("kind", f"no kind named {kind!r}", f"Choose one of: {', '.join(KINDS)}", as_json)
    try:
        period = _period(args)
    except ValueError as exc:
        return _bad("date", str(exc), "Try --month 2015-06, --year 2015, or --after/--before.", as_json)
    cursor = None
    if args.cursor:
        cursor = Cursor.decode(args.cursor)
        if cursor is None:
            return _bad("cursor", "that is not a place Leasha printed",
                        "Use the --cursor line printed at the end of the previous page.", as_json)

    settings = _load(args)
    setup_logging(settings.log_path)
    limit = max(1, int(args.limit or PAGE_SIZE))
    with SqliteStore(settings.fts_db) as store:
        if period is None:
            _print_overview(timeline_overview(store, kind=kind), as_json)
            return EXIT_OK
        page = timeline_page(store, period, cursor=cursor, limit=limit, kind=kind,
                             fold=not args.no_fold)
    _print_page(period, page, kind, as_json)
    return EXIT_OK


def add_timeline_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p = sub.add_parser(
        "timeline", parents=[common],
        help="browse everything from a stretch of time - photos, files and mail "
             "together, oldest first. With no dates given, says what there is")
    p.add_argument("--year", type=int, metavar="YYYY", help="a whole year")
    p.add_argument("--month", metavar="YYYY-MM", help="one month, e.g. 2015-06")
    p.add_argument("--after", metavar="DATE", help="from this date (2015, 2015-06 or 2015-06-01)")
    p.add_argument("--before", metavar="DATE", help="up to and including this date")
    p.add_argument("--kind", default="everything", metavar="KIND",
                   help="everything (not code), photos, videos, documents, mail or code")
    p.add_argument("--limit", type=int, metavar="N", help="items per page (default 200)")
    p.add_argument("--cursor", metavar="CURSOR",
                   help="carry on from the end of the previous page")
    p.add_argument("--no-fold", action="store_true",
                   help="list near-identical photos and identical copies separately")
    p.set_defaults(func=cmd_timeline)
