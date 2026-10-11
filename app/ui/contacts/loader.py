"""Everything the Contacts page reads from the hub. Every function here runs on a worker.

Layer: L5

Rule 5: the window's thread never queries. The page hands one of these to a
`CallableWorker` (`run_task`) and gets the answer back by signal. Nothing here touches Qt
widgets; the hub's connections are per-thread (`SqliteStore.conn`), so a worker reading beside
the indexer is safe.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from PySide6.QtCore import QThreadPool

from app.contacts.card import Card
from app.contacts.hub import ContactFilter, ContactRow
from app.ui.later import when_done
from app.ui.workers import CallableWorker, run

__all__ = ["Snapshot", "Detail", "run_task", "load_snapshot", "load_detail", "load_history",
           "last_change", "read_photo", "undo_last", "decode_photo"]


def run_task(owner: Any, fn: Callable, ok: Optional[Callable] = None, fail: Optional[Callable] = None,
             *, component: str = "ui.contacts") -> CallableWorker:
    """Run `fn` on the global pool; `ok(result)` / `fail(AppError)` land on the UI thread, and
    are dropped if `owner` has gone."""
    worker = CallableWorker(fn, component=component)
    if fail is None and hasattr(owner, "error"):
        fail = owner.error.emit
    when_done(owner, worker, finished=ok, failed=fail)
    run(QThreadPool.globalInstance(), worker)
    return worker


@dataclass
class Snapshot:
    """One read of the book: the rows for the current filter and the sidebar's numbers."""

    rows: list[ContactRow]
    counts: dict[str, int] = field(default_factory=dict)
    groups: list = field(default_factory=list)
    tags: list = field(default_factory=list)
    smarts: list = field(default_factory=list)
    accounts: list = field(default_factory=list)
    account_counts: dict[int, int] = field(default_factory=dict)
    full: bool = True
    default_account_id: Optional[int] = None


def load_snapshot(hub: Any, accounts: Any, flt: ContactFilter, *, full: bool = True) -> Snapshot:
    rows = hub.list_rows(flt)
    snap = Snapshot(rows=rows, full=full)
    if not full:
        return snap
    snap.counts = hub.counts()
    snap.groups = hub.groups()
    snap.tags = hub.tags()
    snap.smarts = hub.smart_groups()
    snap.accounts = accounts.list()
    default = accounts.default_account()
    snap.default_account_id = default.id if default is not None else None
    snap.account_counts = {
        int(r[0]): int(r[1]) for r in hub.conn.execute(
            "SELECT l.account_id, COUNT(DISTINCT l.contact_id) FROM contact_links l "
            "JOIN contacts c ON c.id = l.contact_id WHERE c.deleted_at IS NULL "
            "AND l.state IN ('synced','pending_create','deleted_remote') GROUP BY l.account_id")}
    return snap


@dataclass
class Detail:
    """One contact, whole, for the editor."""

    id: int
    card: Card
    kept_in: list[int]
    tags: list[str]
    sources: dict
    meta: dict
    conflicts: list
    row: Optional[ContactRow] = None


def load_detail(hub: Any, cid: int) -> Optional[Detail]:
    card = hub.get_card(cid)
    if card is None:
        return None
    return Detail(cid, card, hub.kept_in(cid), hub.tags_of(cid), hub.sources(cid),
                  hub.contact_meta(cid) or {}, hub.open_conflicts(cid), hub.get_row(cid))


def load_history(hub: Any, cid: int) -> list:
    return hub.history(cid)


def last_change(hub: Any) -> Optional[Any]:
    """The newest journal entry made by the person that has not been undone, or None."""
    r = hub.conn.execute(
        "SELECT id FROM contact_journal WHERE undone_at IS NULL AND actor = 'user' "
        "AND action NOT IN ('undo','conflict') ORDER BY id DESC LIMIT 1").fetchone()
    return hub.journal_entry(int(r[0])) if r else None


def undo_last(hub: Any) -> tuple[Optional[Any], Any]:
    """Ctrl+Z: put back the person's newest change - the whole bulk action when it was one.

    Returns (the entry, the `UndoResult`), or (None, None) when there is nothing to undo.
    """
    entry = last_change(hub)
    if entry is None:
        return None, None
    if entry.batch_id:
        return entry, hub.undo_batch(entry.batch_id)
    return entry, hub.undo_entry(entry.id)


def read_photo(path: str) -> tuple[str, bytes]:
    """A picture file's (mime, bytes). Worker: it reads a file."""
    import mimetypes
    from pathlib import Path

    data = Path(path).read_bytes()
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    return mime, data


def decode_photo(hub: Any, cid: int, side: int) -> Any:
    """The contact's photo as a QImage no bigger than `side` pixels, or None. Worker."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage

    row = hub.conn.execute("SELECT data FROM contact_photos WHERE contact_id = ?", (cid,)).fetchone()
    if row is None:
        return None
    image = QImage.fromData(bytes(row[0]))
    if image.isNull():
        return None
    return image.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                        Qt.TransformationMode.SmoothTransformation)
