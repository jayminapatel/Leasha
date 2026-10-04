"""`formats`: which file types are indexed and what reads them."""

from __future__ import annotations

import argparse
import json

from app.cli._common import EXIT_OK, _load, _report
from app.core.errors import AppErrorException
from app.core.logging import setup_logging
from app.core.row_facts import format_size


def _formats_by_group(args: argparse.Namespace) -> int:
    """The source types, by ecosystem.

    **The flat list is unreviewable and that is the point of this view.** Four
    hundred extensions in one alphabetical run tells nobody whether Oracle
    packages are covered; twenty lines under "Oracle PL/SQL" can be read by
    somebody who knows Oracle, in a minute, and corrected.
    """
    from app.extract.source_types import (
        ALL_SOURCE_EXTENSIONS,
        BY_ECOSYSTEM,
        NAMED_FILES,
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
    # 2026-10-04, code review: `row_facts.format_size`, the one size wording.
    # This copy wrote "4.2MB" and stopped at GB; it now reads "4.2 MB".
    return format_size(count)


def add_formats_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
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
