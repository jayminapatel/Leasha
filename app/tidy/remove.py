r"""The one place Tidy changes anything: marked files to the Recycle Bin, and back.

Layer: L4 (tidy), driving L0 (`app.core.osbridge.recycle`) and writing L1's
`tidy_journal` (schema v39).

Order 1q, the owner's third exception to non-negotiable 10 (2026-10-11, "option a"):
files on this computer, moved to the Recycle Bin, only after the owner has seen the
whole plan and said yes. What keeps that safe is here and nowhere else:

* **D2 never the last copy** - `make_plan` refuses a copy or photo set whose every
  file is marked, and `carry_out` moves an exact copy only once a copy that stays
  has been read again and still has the same bytes.
* **D3 look again** - size and modified time read again for every file, and an
  exact copy's content hash (`walker.content_hash`, the indexer's own); a file that
  changed is left where it is and the reason given.
* **D7 a big batch asks twice** - over `BIG_FILES` files or `BIG_BYTES`, `carry_out`
  moves nothing without `second_yes`.
* **D6 a record and the way back** - one `tidy_journal` row per file moved, and
  `put_back` returns a batch.

The index is not edited: a moved file is forgotten by the watcher or the next index
run, exactly as a file deleted in Explorer is, and Tidy hides it meanwhile
(`find.LOCAL`).
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from app.core.logging import logger
from app.tidy.find import TidyFile, TidySet

__all__ = ["BIG_FILES", "BIG_BYTES", "PlanItem", "Plan", "Outcome", "Batch",
           "make_plan", "carry_out", "batches", "put_back", "removed_paths"]

_log = logger.bind(component="tidy.remove")

#: D7. Either number in one go asks a second time.
BIG_FILES = 500
BIG_BYTES = 20 * 1024 ** 3


@dataclass(frozen=True)
class PlanItem:
    """One marked file, and the files of its set that stay."""

    file: TidyFile
    kind: str
    set_key: str
    stays: tuple[TidyFile, ...] = ()


@dataclass(frozen=True)
class Plan:
    """Everything the owner marked that may go, and every set refused by D2."""

    items: tuple[PlanItem, ...] = ()
    refused: tuple[str, ...] = ()                 # set keys with every file marked

    @property
    def files(self) -> int:
        return len(self.items)

    @property
    def size_bytes(self) -> int:
        return sum(i.file.size_bytes for i in self.items)

    @property
    def needs_second_yes(self) -> bool:
        return self.files > BIG_FILES or self.size_bytes > BIG_BYTES


@dataclass
class Outcome:
    """What `carry_out` did: the batch, what moved, and each file left with its reason."""

    batch_id: str = ""
    moved: list[PlanItem] = field(default_factory=list)
    left: list[tuple[str, str]] = field(default_factory=list)
    asked_again: bool = False

    @property
    def size_moved(self) -> int:
        return sum(i.file.size_bytes for i in self.moved)


@dataclass(frozen=True)
class Batch:
    """One press of Remove, read back from the journal for History."""

    batch_id: str
    at: int
    files: int
    size_bytes: int
    waiting: int                                   # still in the Recycle Bin, not put back


def make_plan(sets: Iterable[TidySet], marked: set[str]) -> Plan:
    """The plan for `marked` across `sets`. Pure: nothing is read or moved."""
    items: list[PlanItem] = []
    refused: list[str] = []
    seen: set[str] = set()
    for tset in sets:
        chosen = [f for f in tset.files if f.path in marked and f.path not in seen]
        if not chosen:
            continue
        stays = tuple(f for f in tset.files if f.path not in marked)
        if tset.kind in ("copies", "similar") and not stays:
            refused.append(tset.key)               # D2: every file of the set was marked
            continue
        for f in chosen:
            seen.add(f.path)
            items.append(PlanItem(file=f, kind=tset.kind, set_key=tset.key, stays=stays))
    return Plan(items=tuple(items), refused=tuple(refused))


def carry_out(store: Any, plan: Plan, *, yes: bool, second_yes: bool = False, bin: Any = None,
              on_progress: Optional[Callable[[str], None]] = None,
              hasher: Optional[Callable[[str], str]] = None) -> Outcome:
    """Move the plan's files to the Recycle Bin, one at a time, each checked first."""
    outcome = Outcome()
    if not yes or not plan.items:
        return outcome
    if plan.needs_second_yes and not second_yes:
        outcome.asked_again = True                 # D7: nothing moves on the first yes
        return outcome
    if bin is None:
        from app.core.osbridge.recycle import RecycleBin
        bin = RecycleBin()
    if hasher is None:
        from pathlib import Path

        from app.index.walker import content_hash
        hasher = lambda path: content_hash(Path(path))     # noqa: E731 - the indexer's own
    outcome.batch_id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    verified: dict[str, bool] = {}
    for n, item in enumerate(plan.items, 1):
        if on_progress is not None:
            on_progress(f"Moving {n:,} of {plan.files:,}...")
        path = item.file.path
        reason = _why_it_stays(item, bin, hasher, verified)
        if reason:
            outcome.left.append((path, reason))
            continue
        try:
            ref = bin.prepare(path)
            row_id = _journal(store, outcome.batch_id, item, "pending:" + ref)
        except (OSError, sqlite3.Error) as exc:
            outcome.left.append((path, f"The undo record could not be saved: {exc}"))
            continue
        try:
            actual_ref = bin.send_prepared(path, ref)
        except OSError as exc:
            # The intent remains recoverable even if the OS moved the file before
            # reporting an error. Never remove it on an ambiguous failure.
            outcome.left.append((path, str(exc)))
            continue
        outcome.moved.append(item)
        try:
            with store.write() as conn:
                conn.execute("UPDATE tidy_journal SET bin_ref = ? WHERE id = ?", (actual_ref, row_id))
        except (OSError, sqlite3.Error):
            _log.warning("move completed; durable undo intent retained for {}", path)
    _log.info("tidy batch {}: {} moved ({} bytes), {} left", outcome.batch_id,
              len(outcome.moved), outcome.size_moved, len(outcome.left))
    return outcome


def _why_it_stays(item: PlanItem, bin: Any, hasher: Callable[[str], str],
                  verified: dict[str, bool]) -> str:
    """D1-D3 for one file at the moment it would move: "" to move it, else the reason."""
    f = item.file
    try:
        stat = os.stat(f.path)
    except OSError:
        return "It is no longer there."
    if not bin.on_fixed_drive(f.path):
        return "It is not on a fixed drive of this computer, which has no Recycle Bin."
    if stat.st_size != f.size_bytes or stat.st_mtime_ns != f.mtime_ns:
        return "It changed since Leasha last read it."
    if item.kind == "copies":
        try:
            if not f.content_hash or hasher(f.path) != f.content_hash:
                return "Its contents changed since Leasha last read it."
        except OSError:
            return "It could not be read to check it."
        if not any(_copy_still_there(s, hasher, verified) for s in item.stays):
            return "The copy that stays could not be checked, so this one stays too."
    elif item.kind == "similar":
        if not any(os.path.isfile(s.path) for s in item.stays):
            return "The photo that stays is no longer there, so this one stays too."
    return ""


def _copy_still_there(copy: TidyFile, hasher: Callable[[str], str], verified: dict[str, bool]) -> bool:
    """D2: a copy that stays is there and has the same bytes - the bytes read once per batch, the file's presence
    asked again before every move (a keeper deleted part way through must stop the removals that rest on it)."""
    if not os.path.isfile(copy.path):
        verified.pop(copy.path, None)
        return False
    if copy.path not in verified:
        try:
            verified[copy.path] = hasher(copy.path) == copy.content_hash
        except OSError:
            verified[copy.path] = False
    return verified[copy.path]


def _journal(store: Any, batch_id: str, item: PlanItem, ref: str) -> int:
    kept = item.stays[0].path if item.stays else None
    with store.write() as conn:
        cursor = conn.execute(
            "INSERT INTO tidy_journal (batch_id, at, kind, path, size_bytes, mtime_ns, content_hash, "
            "kept_path, bin_ref) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (batch_id, int(time.time()), item.kind, item.file.path, item.file.size_bytes,
             item.file.mtime_ns, item.file.content_hash, kept, ref))
        return int(cursor.lastrowid)


def batches(store: Any, *, limit: int = 200) -> list[Batch]:
    """Every batch, newest first, for History."""
    rows = store.conn.execute(
        "SELECT batch_id, MIN(at) AS at, COUNT(*) AS n, SUM(size_bytes) AS size, "
        "SUM(CASE WHEN put_back_at IS NULL THEN 1 ELSE 0 END) AS waiting FROM tidy_journal "
        "GROUP BY batch_id ORDER BY MIN(at) DESC, batch_id DESC LIMIT ?", (int(limit),)).fetchall()
    return [Batch(str(r["batch_id"]), int(r["at"]), int(r["n"]), int(r["size"] or 0),
                  int(r["waiting"] or 0)) for r in rows]


def removed_paths(store: Any, batch_id: str) -> list[str]:
    """The paths a batch moved, in the order they went."""
    return [str(r[0]) for r in store.conn.execute(
        "SELECT path FROM tidy_journal WHERE batch_id = ? ORDER BY id", (batch_id,))]


def put_back(store: Any, batch_id: str, *, bin: Any = None) -> Outcome:
    """D6: every file of the batch still in the Recycle Bin, back where it was."""
    if bin is None:
        from app.core.osbridge.recycle import RecycleBin
        bin = RecycleBin()
    outcome = Outcome(batch_id=batch_id)
    rows = store.conn.execute(
        "SELECT id, path, bin_ref, size_bytes, mtime_ns, content_hash, kind FROM tidy_journal "
        "WHERE batch_id = ? AND put_back_at IS NULL ORDER BY id", (batch_id,)).fetchall()
    for row in rows:
        f = TidyFile(id=0, path=str(row["path"]), size_bytes=int(row["size_bytes"]),
                     mtime_ns=int(row["mtime_ns"]), content_hash=row["content_hash"])
        ref = str(row["bin_ref"])
        try:
            if ref.startswith("pending:") and os.path.exists(f.path):
                # A crash before the OS move (or after undo) needs no second move.
                from pathlib import Path

                from app.index.walker import content_hash
                st = os.stat(f.path)
                if st.st_size != f.size_bytes or st.st_mtime_ns != f.mtime_ns or (
                        f.content_hash and content_hash(Path(f.path)) != f.content_hash):
                    raise OSError("Another file is at the original path; check the Recycle Bin before retrying.")
            else:
                bin.put_back(ref.removeprefix("pending:"), f.path)
        except OSError as exc:
            outcome.left.append((f.path, str(exc)))
            continue
        with store.write() as conn:
            conn.execute("UPDATE tidy_journal SET put_back_at = ? WHERE id = ?",
                         (int(time.time()), int(row["id"])))
        outcome.moved.append(PlanItem(file=f, kind=str(row["kind"]), set_key=""))
    return outcome
