"""`search`, `files`, `commands` and `shell`: finding things.

Layer: L4

Exit codes: `search` exits 1 when nothing was found, so a script can tell an
empty answer from a hit without parsing the output. That differs from
`__init__`'s "1 means an AppError" and is recorded here so an empty search in
a log is not read as a crash.

The imports from `app.ui.presenter` and `app.ui.tasks` are Qt-free row
formatters, shared so this prints exactly what the Files tab shows
(non-negotiable 8).
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from app.cli._common import EXIT_ERROR, EXIT_OK, _load, _preview, _report
from app.core.errors import make_error
from app.core.logging import logger, setup_logging


def cmd_files(args: argparse.Namespace) -> int:
    """Find a file by its NAME. Not a content search.

    Deliberately a separate command rather than a flag on `search`, because it
    answers a different question against a different index: `search` finds what
    documents *say*, this finds what they are *called*. A file named
    "Invoice 2024.pdf" whose contents never use those words is invisible to one
    and the first result of the other.

    Read-only, so no lock. It never embeds and never reranks, which is why it
    returns instantly.

    2026-10-04 - **the Files tab's search, not a second one** (the owner's
    decision that every surface searches as the window does): the line is read
    by `app.search.run.find_files`, the function the Files tab's worker runs -
    slash commands and plain English ("pdf from 2019"), the Settings search
    switches, every switch through `store.browse_files`, and the tab's spelling
    help for an empty list. The name still ranks first; a folder or the text
    can match too, as on the tab. It used to be `search_files_by_name`, which
    found something different from the tab for the same words.
    """
    from app.search.policy import preferences
    from app.search.run import find_files
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
    # `--type pdf,docx` is the switch the tab would be typed with.
    line = f"{text} type:{args.type}" if args.type else text

    with SqliteStore(settings.fts_db) as store:
        page = find_files(store, line, limit=args.limit, preferences=preferences(settings))
        total = store.count_listed_files()
        # 2026-10-04: an attachment's message (its folder and date) and a
        # drive's name, read the way the Files tab reads them - one call.
        from app.ui.tasks import file_row_context

        hits = file_row_context(store, page["rows"])

    if args.json:
        print(json.dumps({"query": text, "matches": hits, "indexed_files": total,
                          "applied": [a.label for a in page["applied"]],
                          "spelling": page.get("spelling") or None},
                         indent=2, default=str))
        return EXIT_OK

    if page.get("spelling"):
        print(f"  {page['spelling']}")
    if page["applied"]:
        print(f"  (read as: {', '.join(a.label for a in page['applied'])})")
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
    from app.search.run import rerank_wanted
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
    # Work order 0h §1c: the third retrieval lane, same as every other real
    # search entry point in this file.
    clip_text_embedder = vector.clip_text_embedder_from_settings(settings)

    with SqliteStore(settings.fts_db) as store, \
            VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors, \
            ImageVectorStore(settings.vector_path) as image_vectors:
        # The window's Rerank switch, as `search` reads it (2026-10-04).
        reranker = Reranker.from_settings(
            settings, enabled=rerank_wanted(settings, store))
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


def cmd_search(args: argparse.Namespace) -> int:
    """Search the index. Layer 4's entry point.

    Read-only, so no lock: searching while an index run is in progress is a
    normal thing to want, and WAL makes it safe.
    """
    from app.index.embedder import Embedder
    from app.search import vector
    from app.search.engine import SearchEngine
    from app.search.policy import SEARCH, preferences
    from app.search.rerank import Reranker
    from app.search.run import rerank_wanted, run_search
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
    # Work order 0h §1c: the third retrieval lane, same as every other real
    # search entry point in this file.
    clip_text_embedder = vector.clip_text_embedder_from_settings(settings)

    with SqliteStore(settings.fts_db) as store, \
            VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors, \
            ImageVectorStore(settings.vector_path) as image_vectors:
        # **The window's Rerank switch, not `.env` alone** (2026-10-04):
        # `rerank_wanted` reads what the toolbar and Settings saved, as the
        # window's engine does; `--no-rerank` can still only turn it off.
        reranker = Reranker.from_settings(
            settings, enabled=rerank_wanted(settings, store) and not args.no_rerank)
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
            # **Exactly as the Search tab searches** (the owner's decision,
            # 2026-10-04). `run_search` is the window's steps in order:
            # `expand_slashes` and `saved:name` first - the 2026-09-20 bug was
            # `/newest` reaching `parse_query` unexpanded and being silently
            # dropped, so the command line answered `report /newest` in
            # relevance order while the window sorted by date - then the
            # plain-English filters, the Settings search switches, the rerank
            # switch, repository history when a history switch was typed, and
            # one row per document. Before this the command line skipped the
            # filters, the switches and saved searches, and printed one row
            # per passage.
            found = run_search(engine, raw, surface=SEARCH,
                               preferences=preferences(settings), limit=args.limit,
                               note_saved=True)
        finally:
            engine.close()

        if args.json:
            print(json.dumps(found.as_dict(), indent=2, default=str))
            return EXIT_OK if found.documents else EXIT_ERROR

        if not print_response(found.response, raw, found=found):
            return EXIT_ERROR
    return EXIT_OK


#: What a result's mark means, in the words a person reads.
_STATUS_WORDS = {
    "missing": "no longer on disk - moved or deleted since it was indexed",
    "offline": "on a drive that is not connected - plug it in to open",
}


def print_response(response: Any, raw: str = "", *, found: Any = None) -> bool:
    """Print one search's results, one per document. False when there were none.

    **Extracted so there is exactly one renderer.** `leasha shell` prints
    through this rather than repeating it - the order's rule is "no second
    renderer", and a copy that starts identical is the thing that stops being
    identical. It was inline in `cmd_search` until the REPL needed it.

    `found` is the `run_search` answer: its documents (one per file, as the
    Search tab lists them, with the missing/offline mark) and the filters the
    line was read as. Without it the response's results are grouped here,
    unmarked (2026-10-04).
    """
    from app.search.run import documents as group_documents

    if response.parsed and response.parsed.unknown_operators:
        print(f"  (ignored: {', '.join(response.parsed.unknown_operators)})")
    # What was wrong with a date, and what would work - the same sentence the
    # window shows (order "dates" §1d, non-negotiable #8).
    for problem in getattr(response.parsed, "date_problems", ()) or ():
        print(f"  ! {problem}")
    # What the plain-English rules read - the window draws these as chips.
    applied = list(getattr(response, "applied", ()) or ())
    if applied:
        print(f"  (read as: {', '.join(str(a.label) for a in applied)})")

    # **Before the results, and on the way out too.** A search that quietly
    # returned worse results is the failure nobody reports, because it
    # looks exactly like one that worked.
    for notice in response.notices:
        print(f"  ! {notice.message}")
    if response.notices:
        print()

    shown = (found.documents if found is not None
             else group_documents(response.results, marks=False))
    if not shown:
        print(f"No results for {raw!r}." if raw else "No results.")
        if response.parsed and response.parsed.has_filters:
            print("  The filters may be excluding everything - try without them.")
        return False

    for document in shown:
        result = document.best
        page = f" p{result.page}" if result.page is not None else ""
        more = f"   ({document.matches} matches)" if document.matches > 1 else ""
        print(f"{document.rank:>3}. {result.path}{page}{more}")
        if document.status in _STATUS_WORDS:
            print(f"     ! {_STATUS_WORDS[document.status]}")
        print(f"     {result.explain()}   score {result.score:.4f}")
        print(f"     {_preview(result.text, 200)}")
        print()

    print(f"{len(shown)} document(s) in {response.elapsed_ms:.0f}ms"
          f"{' (cached)' if response.from_cache else ''}"
          f"{', reranked' if response.reranked else ''}")
    if response.timings:
        print("  " + "  ".join(f"{k} {v:.0f}ms" for k, v in response.timings.items()))
    return True


def add_files_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_files = sub.add_parser(
        "files", parents=[common],
        help="find a file by NAME (not by contents) - matches any part of the name"
             " [2026-10-04: searched as the Files tab searches, so a folder or the"
             " text can match too, after the name]")
    p_files.add_argument("name", nargs="*", help="part of a filename, or a folder name")
    p_files.add_argument("--limit", type=int, default=50, metavar="N",
                         help="results to return (default 50)")
    p_files.add_argument("--type", metavar="EXT",
                         help="restrict to these extensions, comma separated: pdf,docx")
    p_files.set_defaults(func=cmd_files)


def add_commands_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_commands = sub.add_parser(
        "commands", parents=[common],
        help="list the search filters you can type (/type, /from, /after ...)")
    p_commands.add_argument(
        "--git", action="store_true",
        help="the repository search switches instead (/history, /branch, "
             "/introduced ...) - a different engine over a different store")
    p_commands.set_defaults(func=cmd_commands)


def add_shell_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_shell = sub.add_parser(
        "shell", parents=[common],
        help="interactive search with a dropdown (Ctrl+D to leave)")
    p_shell.set_defaults(func=cmd_shell)


def add_search_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_search = sub.add_parser("search", parents=[common], help="search the index")
    p_search.add_argument("query", nargs="*",
                          help='search terms; supports type: after: before: path: from: '
                               '"phrases" and -exclusions')
    p_search.add_argument("--limit", type=int, default=20, metavar="N",
                          help="results to return (default 20)")
    p_search.add_argument("--no-rerank", action="store_true",
                          help="skip the cross-encoder even if it is enabled")
    p_search.set_defaults(func=cmd_search)
