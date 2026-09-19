"""`gitsearch` and `repos`: code repositories and their history."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from app.cli._common import EXIT_ERROR, EXIT_OK, _load, _report
from app.core.errors import make_error
from app.core.logging import setup_logging


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
        DEFAULT_ROW_LIMIT,
        SEARCH_TIMEOUT_S,
        git_version,
        is_repository,
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


def add_gitsearch_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
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


def add_repos_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
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
