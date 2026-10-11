r"""`leasha dedupe-mail` - messages stored twice in one archive leave the index.

Order 1j section 3 (2026-10-11). `--dry-run` reads only and says how many rows
would go, by archive, with examples. Without it, the index is copied aside
first (`<DATA_PATH>\backups\<stamp>\`), then the extra copies and their
attachments are removed under the index run lock, then the word index is
checked. The mail archives themselves are never opened for writing.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import time
from pathlib import Path

from app.cli._common import EXIT_ERROR, EXIT_OK, _load
from app.core.logging import setup_logging
from app.core.run_lock import COMMAND_LINE, IndexRunLock

__all__ = ["add_dedupe_mail_parser", "cmd_dedupe_mail", "backup_index",
           "add_outlook_dedupe_parser", "cmd_outlook_dedupe"]


def backup_index(fts_db: Path, vector_path: Path, store: object) -> Path:
    """Copy the database (SQLite's own online backup) and the vector folder aside.

    Under the run lock the caller holds, so nothing is writing either while it is
    copied. Returns the folder they went to.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = Path(fts_db).resolve().parent.parent / "backups" / f"before-dedupe-mail-{stamp}"
    target.mkdir(parents=True, exist_ok=False)
    with sqlite3.connect(str(target / Path(fts_db).name)) as copy:
        store.conn.backup(copy)
    if Path(vector_path).exists():
        shutil.copytree(vector_path, target / Path(vector_path).name)
    return target


def cmd_dedupe_mail(args: argparse.Namespace) -> int:
    from app.index.mail_duplicates import find_mail_duplicates, remove_mail_duplicates, summary_lines
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import ImageVectorStore, VectorStore

    settings = _load(args)
    setup_logging(settings.log_path)
    with SqliteStore(settings.fts_db) as store:
        found = find_mail_duplicates(store)
        for line in summary_lines(found):
            print(line)
        if args.dry_run or not found.all_ids:
            return EXIT_OK
        if not args.yes:
            print("Nothing removed. Add --yes to remove them (the index is copied aside first).")
            return EXIT_OK

        started = time.time()
        with IndexRunLock(store, owner=COMMAND_LINE):
            print("Copying the index aside first...", flush=True)
            target = backup_index(Path(settings.fts_db), Path(settings.vector_path), store)
            print(f"  Saved in {target}  ({time.time() - started:,.0f} s)", flush=True)
        with VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors, \
                ImageVectorStore(settings.vector_path) as image_vectors:
            removed = remove_mail_duplicates(store, vectors, image_vectors,
                                             run_lock_owner=COMMAND_LINE)
        print(f"Removed {removed:,} index rows.", flush=True)

        left = find_mail_duplicates(store)
        if left.all_ids:
            print(f"  {len(left.all_ids):,} rows still match - this should be none.")
            return EXIT_ERROR
        print("Checking the word index...", flush=True)
        try:
            for table in ("chunks_fts", "files_fts", "messages_fts"):
                store.conn.execute(f"INSERT INTO {table}({table}) VALUES('integrity-check')")
            print("  The word index is consistent.")
        except sqlite3.DatabaseError as exc:
            print(f"  The word index check failed: {exc}")
            return EXIT_ERROR
    print(f"Done in {time.time() - started:,.0f} s. The mail archives were not touched.")
    return EXIT_OK


def add_dedupe_mail_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_dedupe = sub.add_parser(
        "dedupe-mail", parents=[common],
        help="remove messages stored twice in one mail archive from the index (the archive is not touched)")
    p_dedupe.add_argument("--dry-run", action="store_true",
                          help="say how many would go, by archive, and change nothing")
    p_dedupe.add_argument("--yes", action="store_true",
                          help="remove them, after copying the index aside")
    p_dedupe.set_defaults(func=cmd_dedupe_mail)


def cmd_outlook_dedupe(args: argparse.Namespace) -> int:
    """Order 1l: copies inside one mailbox, moved to its Deleted Items through Outlook."""
    from app.core.errors import AppErrorException
    from app.extract.outlook_cleanup import find_copies, move_copies, plan_lines

    settings = _load(args)
    setup_logging(settings.log_path)
    try:
        plan = find_copies(Path(args.archive))
    except AppErrorException as exc:
        print(f"{exc.error.message} {exc.error.suggestion or ''}".strip())
        return EXIT_ERROR
    for line in plan_lines(plan):
        print(line)
    if args.dry_run or not plan.copies:
        return EXIT_OK
    if not args.yes:
        print("Nothing moved. Add --yes to move them to Deleted Items.")
        return EXIT_OK
    moved, left = move_copies(plan)
    print(f"Moved {moved:,} to Deleted Items" + (f"; {left:,} had changed or gone and were left." if left else "."))
    return EXIT_OK


def add_outlook_dedupe_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_outlook = sub.add_parser(
        "outlook-dedupe", parents=[common],
        help="move copies of a message inside one mailbox to its Deleted Items, through Outlook")
    p_outlook.add_argument("archive", help="the .pst file")
    p_outlook.add_argument("--dry-run", action="store_true", help="count them and move nothing")
    p_outlook.add_argument("--yes", action="store_true", help="move them to Deleted Items")
    p_outlook.set_defaults(func=cmd_outlook_dedupe)
