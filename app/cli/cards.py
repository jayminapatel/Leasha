r"""`app.cli cards` - the document cards: how many, one file's, make some now, clear them.

Layer: L3 (CLI before UI - non-negotiable 8)

Order `smarter-answers` 2e (owner, 2026-10-12: "this index is still test until
we get it perfect"). An index run writes cards at its end; this is the same
loop (`app/index/card_pass.py`) on demand, and the way to start again.

    python -m app.cli cards                       how many are made, how many had nothing to say
    python -m app.cli cards --show "D:\Docs\invoice.pdf"
    python -m app.cli cards --make 20             write up to 20 now, and time them
    python -m app.cli cards --clear --yes         forget every card (they are made again)

`--make` and `--clear` write, so they take the index run lock: they and an
index run exclude each other. Cards made here are searchable by their words at
once and by meaning after the next index run embeds them.
"""

from __future__ import annotations

import argparse
import json
import threading
from typing import Any

from app.cli._common import EXIT_ERROR, EXIT_OK, _load, _report
from app.core.errors import AppErrorException
from app.core.logging import logger, setup_logging

__all__ = ["cmd_cards", "add_cards_parser"]


def _counts(store: Any, as_json: bool) -> int:
    counts = store.doc_card_counts()
    if as_json:
        print(json.dumps(counts))
    else:
        print(f"{counts['made']:,} document card(s) made, "
              f"{counts['nothing']:,} document(s) with nothing a card would add")
    return EXIT_OK


def _show(store: Any, path: str, as_json: bool) -> int:
    record = store.get_file(path)
    if record is None:
        print(f"Not in the index: {path}")
        return EXIT_ERROR
    card = store.doc_card(int(record.id))
    if as_json:
        print(json.dumps(card, default=str))
        return EXIT_OK
    if card is None:
        print(f"No card yet for {path}")
        return EXIT_OK
    print(f"{path}")
    print(f"  kind           {card['kind']}")
    for key in ("people", "organisations", "dates", "amounts"):
        if card[key]:
            print(f"  {key:<14} {', '.join(card[key])}")
    if card.get("summary"):
        print(f"  summary        {card['summary']}")
    print(f"  written by     {card.get('model') or 'the chat model'} (AI-written)")
    return EXIT_OK


def _make(store: Any, settings: Any, limit: int, as_json: bool) -> int:
    from app.index.card_pass import run_cards
    from app.index.run_setup import card_model_factory
    from app.storage.vector_store import VectorStore

    stop = threading.Event()
    with VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors:
        try:
            result = run_cards(
                store, card_model_factory(settings), interrupted=stop.is_set,
                on_card=None if as_json else (lambda name: print(f"  {name}")),
                delete_vectors=vectors.delete_by_chunk_ids, log=logger, limit=limit,
                scope=str(getattr(settings, "doc_cards_scope", "documents")))
        except KeyboardInterrupt:
            stop.set()
            print("Stopped. The cards made so far are kept.")
            return EXIT_OK
    each = result.seconds_asking / max(1, result.made + result.failed)
    if as_json:
        print(json.dumps({"made": result.made, "nothing": result.nothing,
                          "failed": result.failed, "seconds_each": round(each, 2),
                          "gave_up": result.gave_up}))
        return EXIT_OK
    print(f"{result.made} card(s) made, {result.nothing} with nothing to add, "
          f"{result.failed} failed; {each:.1f} s each on average")
    if result.gave_up:
        print(f"Stopped early: {result.gave_up}.")
    return EXIT_OK


def _clear(store: Any, settings: Any) -> int:
    from app.index import doc_cards as cards
    from app.storage.vector_store import VectorStore

    rows, removed = store.clear_doc_cards(cards.CARD_LABEL)
    with VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors:
        vectors.delete_by_chunk_ids(removed)
    print(f"Forgot {rows:,} card(s); the next index run writes them again.")
    return EXIT_OK


def cmd_cards(args: argparse.Namespace) -> int:
    from app.core.run_lock import COMMAND_LINE, IndexRunLock
    from app.storage.sqlite_store import SqliteStore

    settings = _load(args)
    setup_logging(settings.log_path)
    as_json = bool(getattr(args, "json", False))
    try:
        with SqliteStore(settings.fts_db) as store:
            if args.show:
                return _show(store, args.show, as_json)
            if args.clear:
                if not args.yes:
                    print("This forgets every document card. Add --yes to do it.")
                    return EXIT_ERROR
                with IndexRunLock(store, owner=COMMAND_LINE):
                    return _clear(store, settings)
            if args.make:
                with IndexRunLock(store, owner=COMMAND_LINE):
                    return _make(store, settings, int(args.make), as_json)
            return _counts(store, as_json)
    except AppErrorException as exc:
        return _report(exc.error, as_json)


def add_cards_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p = sub.add_parser("cards", parents=[common],
                       help="document cards: how many, one file's, make some now, clear them")
    p.add_argument("--show", metavar="PATH", help="print one file's card")
    p.add_argument("--make", type=int, metavar="N", help="write up to N cards now")
    p.add_argument("--clear", action="store_true", help="forget every card")
    p.add_argument("--yes", action="store_true", help="with --clear: do it")
    p.set_defaults(func=cmd_cards)
