r"""`leasha tidy` - copies, similar photos, large files and leftovers on this computer.

Layer: L4 (`app.tidy`), shipped before the Tidy page per non-negotiable 8.

Order 1q (2026-10-11). `leasha tidy copies|similar|large|leftovers` lists what the
keep rule would keep and mark, and reads only. `--remove` prints the plan and moves
nothing; with `--yes` the marked files go to the Recycle Bin (and a plan over 500
files or 20 GB also needs `--yes-big`, D7). `leasha tidy history` lists each batch;
`leasha tidy put-back <batch>` returns one. Large files and leftovers are never
marked by a rule (D10), so `--remove` on them needs `--path`.
"""

from __future__ import annotations

import argparse
import json
import time

from app.cli._common import EXIT_ERROR, EXIT_OK, _load
from app.core.logging import setup_logging

__all__ = ["add_tidy_parser", "cmd_tidy"]

_SHOWN = 10


def cmd_tidy(args: argparse.Namespace) -> int:
    from app.storage.sqlite_store import SqliteStore

    settings = _load(args)
    setup_logging(settings.log_path)
    with SqliteStore(settings.fts_db) as store:
        if args.what == "history":
            return _history(store, args)
        if args.what == "put-back":
            return _put_back(store, args)
        return _sets(store, args)


def _read_sets(store, what: str, limit: int) -> list:
    from app.tidy import find

    if what == "copies":
        return find.copy_sets(store, limit=limit)
    if what == "similar":
        return find.similar_sets(store, limit=limit)
    if what == "large":
        return [find.large_files(store, limit=limit)]
    return [find.leftovers(store, limit=limit)]


def _sets(store, args: argparse.Namespace) -> int:
    from app.core.row_facts import format_size
    from app.tidy import keep, remove

    sets = _read_sets(store, args.what, args.limit)
    chosen = set(args.path or [])
    marked: set[str] = set()
    for tset in sets:
        quality = keep.qualities(tset) if args.keep == "leasha" else {}
        mine = {f.path for f in tset.files if f.path in chosen}
        marked |= mine or keep.marked_by_rule(tset, args.keep, quality)
    plan = remove.make_plan(sets, marked)

    if args.json:
        print(json.dumps({
            "sets": [{"key": s.key, "kind": s.kind, "files": [f.path for f in s.files],
                      "elsewhere": list(s.elsewhere), "marked": sorted(f.path for f in s.files
                                                                       if f.path in marked)}
                     for s in sets],
            "plan": {"files": plan.files, "size_bytes": plan.size_bytes, "refused": list(plan.refused)},
        }, indent=2))
    else:
        print(f"{len(sets):,} set(s); the rule '{keep.RULE_WORDS[args.keep]}' marks "
              f"{plan.files:,} file(s), {format_size(plan.size_bytes)}.")
        for tset in sets[:_SHOWN]:
            print(f"\n{tset.key}")
            for f in tset.files[:_SHOWN]:
                print(f"  {'mark' if f.path in marked else 'keep'}  {f.path}  ({format_size(f.size_bytes)})")
            for where in tset.elsewhere:
                print(f"  also  {where}  (on a catalogued drive - never touched)")
        for key in plan.refused:
            print(f"\nNot offered: every file of {key} was marked, and one must stay.")
    if not args.remove or not plan.items:
        return EXIT_OK
    if not args.yes:
        print("\nNothing moved. Add --yes to move the marked files to the Recycle Bin.")
        return EXIT_OK
    outcome = remove.carry_out(store, plan, yes=True, second_yes=args.yes_big,
                               on_progress=None if args.json else print)
    if outcome.asked_again:
        print(f"\nNothing moved: {plan.files:,} files, {format_size(plan.size_bytes)} is a big batch. "
              "Add --yes-big as well to move them.")
        return EXIT_OK
    print(f"\nBatch {outcome.batch_id}: {len(outcome.moved):,} moved to the Recycle Bin "
          f"({format_size(outcome.size_moved)}); {len(outcome.left):,} left where they were.")
    for path, reason in outcome.left[:_SHOWN]:
        print(f"  left  {path}: {reason}")
    return EXIT_OK


def _history(store, args: argparse.Namespace) -> int:
    from app.core.row_facts import format_size
    from app.tidy import remove

    found = remove.batches(store)
    if args.json:
        print(json.dumps([b.__dict__ for b in found], indent=2))
        return EXIT_OK
    if not found:
        print("Tidy has not moved anything yet.")
    for b in found:
        when = time.strftime("%d %b %Y %H:%M", time.localtime(b.at))
        print(f"{b.batch_id}  {when}  {b.files:,} file(s), {format_size(b.size_bytes)}, "
              f"{b.waiting:,} still in the Recycle Bin")
    return EXIT_OK


def _put_back(store, args: argparse.Namespace) -> int:
    from app.tidy import remove

    if not args.batch:
        print("Name the batch to put back - leasha tidy history lists them.")
        return EXIT_ERROR
    outcome = remove.put_back(store, args.batch)
    print(f"{len(outcome.moved):,} put back; {len(outcome.left):,} left in the Recycle Bin.")
    for path, reason in outcome.left[:_SHOWN]:
        print(f"  left  {path}: {reason}")
    return EXIT_OK


def add_tidy_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    from app.tidy.keep import DEFAULT_RULE, RULES

    p = sub.add_parser(
        "tidy", parents=[common],
        help="copies, similar photos, large files and leftovers on this computer; "
             "--remove --yes moves the marked ones to the Recycle Bin")
    p.add_argument("what", choices=("copies", "similar", "large", "leftovers", "history", "put-back"),
                   help="what to list, or history / put-back")
    p.add_argument("batch", nargs="?", default="", help="put-back: the batch, from tidy history")
    p.add_argument("--keep", choices=RULES, default=DEFAULT_RULE,
                   help="the keep rule: leasha (Leasha picks), oldest, newest, manual (mark nothing)")
    p.add_argument("--path", action="append", metavar="PATH",
                   help="mark this file instead of the rule's choice (may be given more than once)")
    p.add_argument("--limit", type=int, default=50, help="how many sets (or files) to read")
    p.add_argument("--remove", action="store_true", help="print the plan to move the marked files")
    p.add_argument("--yes", action="store_true", help="with --remove: move them to the Recycle Bin")
    p.add_argument("--yes-big", action="store_true",
                   help="with --yes: also move a batch over 500 files or 20 GB (D7)")
    p.set_defaults(func=cmd_tidy)
