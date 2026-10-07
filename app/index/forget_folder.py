r"""Take a folder off the list, and what it brought into the index with it.

Layer: L3

2026-10-07, the owner: "if a location is removed the data for that location
should be removed, ask confirmation at removal that the index data will be
removed too - the only way the list must reflect what is in the index."

Before this, Remove in Settings only took the folder off the list. Nothing ever
removed what had been read from it: the clean-up after a run deletes a row only
when its file has gone from the disk, and a removed folder's files are still
there. So everything it had held stayed searchable for good - on the owner's
index, 342 files from a folder no longer listed.

**What a folder brought in** is three kinds of row, and the second is the one a
path test misses:

* a file at or under the folder, with an archive's members;
* a message read out of a mailbox under it - its path is
  `pst://<mailbox name>/<entry id>`, under no folder at all, so it is found by
  the `.pst` it was read from (`messages.store_path`);
* that message's attachments, `<message>/attachments/<name>`, and their members.

A row also under any folder that stays listed stays, because that folder would
read it straight back in: with `D:\Docs` and `D:\Docs\Work` both listed,
removing either leaves `D:\Docs\Work`'s files. A catalogued drive's rows are the Offline tab's,
and a message read through Outlook has no folder at all; neither is touched.

**Leftovers** are rows from no listed folder - what an earlier Remove left
behind, or a command-line run over a folder never listed. `file_ids_outside`
finds them so Settings can say how many and offer to remove them.

**The index only.** Nothing on disk is touched.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Iterable, Sequence

from app.core.logging import logger

__all__ = ["file_ids_from_folders", "file_ids_outside", "count_from_folders",
           "count_outside", "forget_ids", "forget_folders", "forget_outside",
           "DELETE_BATCH"]

_log = logger.bind(component="index.forget_folder")

#: Rows per delete, as `Pipeline.PRUNE_BATCH`: one statement, one Lance version.
DELETE_BATCH = 2_000

#: A path on a disk: `D:\`, `D:/`, `\\server`, or `/` on a Mac or Linux.
#: Anything else - `pst://`, a drive's `leasha-volume://` - is not a place a
#: folder could hold, and is never a leftover.
_ON_A_DISK = re.compile(r"^([A-Za-z]:[\\/]|\\\\|/)")


def _message_key(path: str) -> str:
    """`pst://<mailbox>/<entry>` from a message's path or anything below it."""
    return "/".join(path.split("/", 4)[:4])


def _ids(store: Any, belongs: Callable[[str], bool]) -> list[int]:
    """Ids of every row whose origin - its path, or for mail its mailbox -
    `belongs`, with the attachments of every such message."""
    found: list[int] = []
    messages: set[str] = set()
    below_messages: list[tuple[int, str]] = []
    for file_id, path, is_message, mailbox in store.iter_file_origins():
        if path.startswith("pst://"):
            if not is_message:
                below_messages.append((file_id, path))      # decided below
            elif mailbox and _ON_A_DISK.match(mailbox) and belongs(mailbox):
                found.append(file_id)
                messages.add(_message_key(path))
        elif _ON_A_DISK.match(path) and belongs(path):
            found.append(file_id)
    found.extend(file_id for file_id, path in below_messages
                 if _message_key(path) in messages)
    return found


def file_ids_from_folders(store: Any, removed: Sequence[Any],
                          kept: Sequence[Any] = ()) -> list[int]:
    """Ids of every row these folders brought into the index and no folder in
    `kept` also covers. Reads the table once - about 0.3s for 52,000 rows."""
    from app.index.archives import files_under, normalise

    gone = sorted({normalise(folder) for folder in removed if normalise(folder)})
    if not gone:
        return []
    staying = [folder for folder in kept if normalise(folder)]
    # Under a removed folder, and under no folder that stays: a folder still
    # listed would read it straight back in on its next run.
    return _ids(store, lambda origin: (files_under(origin, gone) is not None
                                       and files_under(origin, staying) is None))


def file_ids_outside(store: Any, listed: Sequence[Any]) -> list[int]:
    """Ids of every row that came from none of the `listed` folders."""
    from app.index.archives import files_under, normalise

    roots = [folder for folder in listed if normalise(folder)]
    return _ids(store, lambda origin: files_under(origin, roots) is None)


def count_from_folders(store: Any, removed: Sequence[Any],
                       kept: Sequence[Any] = ()) -> int:
    """How many rows removing these folders deletes - what Remove asks about."""
    return len(file_ids_from_folders(store, removed, kept))


def count_outside(store: Any, listed: Sequence[Any]) -> int:
    """How many leftovers there are - what Settings shows under the list."""
    return len(file_ids_outside(store, listed))


def forget_ids(store: Any, vectors: Any, image_vectors: Any,
               find: Callable[[], list[int]], *, run_lock_owner: str,
               **lock_options: Any) -> int:
    """Delete the rows `find` returns, **found and deleted under the index run
    lock**, so nothing runs beside an index run - one already walking the
    folder would write its rows back as fast as they went. Somebody else
    holding it raises `ERR_INDEX_RUNNING`, naming who, before anything is
    deleted. `lock_options` go to `IndexRunLock` (`name`, `lock_dir`): a test's
    own lock, never the machine's.

    The same order as `Pipeline._delete_in_batches`: vectors first (text, and
    pictures), then SQLite. A crash between them leaves vectors for rows that
    still exist, re-deleted next time; the reverse would orphan them.
    """
    from app.core.run_lock import IndexRunLock

    with IndexRunLock(store, owner=run_lock_owner, **lock_options):
        ids = find()
        for start in range(0, len(ids), DELETE_BATCH):
            batch = ids[start:start + DELETE_BATCH]
            for side in (vectors, image_vectors):
                if side is not None:
                    side.delete_by_file_ids(batch)
            with store.batch():
                for file_id in batch:
                    store.delete_file(file_id)
    return len(ids)


def forget_folders(store: Any, vectors: Any, image_vectors: Any,
                   removed: Sequence[Any], kept: Sequence[Any] = (), *,
                   run_lock_owner: str, **lock_options: Any) -> dict[str, Any]:
    """Delete everything these folders brought in. Returns
    `{"files": N, "folders": [...]}`.

    A removed folder's archive record goes too. Added back later and marked as
    an archive, it would otherwise be skipped as "nothing has changed" - with
    nothing of it left in the index.
    """
    count = forget_ids(store, vectors, image_vectors,
                       lambda: file_ids_from_folders(store, removed, kept),
                       run_lock_owner=run_lock_owner, **lock_options)
    _drop_archive_records(store, removed)
    _log.info("took {} folder(s) off the list and {} row(s) out of the index; "
              "nothing on disk was touched", len(removed), count)
    return {"files": count, "folders": [str(folder) for folder in removed]}


def forget_outside(store: Any, vectors: Any, image_vectors: Any,
                   listed: Sequence[Any], *, run_lock_owner: str,
                   **lock_options: Any) -> dict[str, Any]:
    """Delete every leftover - every row from no listed folder. Returns
    `{"files": N, "folders": []}`, the shape `forget_folders` returns."""
    count = forget_ids(store, vectors, image_vectors,
                       lambda: file_ids_outside(store, listed),
                       run_lock_owner=run_lock_owner, **lock_options)
    _log.info("took {} row(s) from folders no longer listed out of the index; "
              "nothing on disk was touched", count)
    return {"files": count, "folders": []}


def _drop_archive_records(store: Any, removed: Iterable[Any]) -> None:
    from app.index.archives import (
        RECORD_STATE_KEY, dump_records, load_records, normalise,
    )

    records = load_records(store.get_state(RECORD_STATE_KEY, "") or "")
    gone = {normalise(folder) for folder in removed}
    left = {key: record for key, record in records.items() if key not in gone}
    if len(left) != len(records):
        store.set_state(RECORD_STATE_KEY, dump_records(left))
