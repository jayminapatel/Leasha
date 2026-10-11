"""A copy of the contact book, written before anything that could change it a lot.

Layer: L7

Order 1k, 2026-10-11 (the owner: "bulletproof"). Two files per backup, side by side:

* ``contacts-<time>-<reason>.vcf`` - every live contact as a vCard, readable by any address book;
* ``contacts-<time>-<reason>.db`` - the contact tables of the index (contacts, fields, links to
  each account, tags, groups, the journal, the sync log) as a small SQLite file, so a whole
  state - who is linked to which account - can be put back, which a vCard cannot say.

The newest ``keep`` of each are kept. A backup never blocks a sync: a failure is logged and the
caller carries on.
"""

from __future__ import annotations

import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from app.contacts import exporters
from app.core.logging import logger

__all__ = ["TABLES", "write_backup", "backups", "newest_age_s"]

_log = logger.bind(component="contacts.backup")

#: The tables that make up the contact book and what ties it to the accounts.
TABLES = ("accounts", "account_sync_state", "sync_runs", "contacts", "contact_fields", "contact_photos",
          "contact_links", "contact_groups", "contact_group_members", "contact_tags", "contact_tag_members",
          "contact_smart_groups", "contact_journal", "contact_sources", "contact_rejected",
          "contact_pile_links", "contact_conflicts")

_NAME = re.compile(r"^contacts-(\d{8}-\d{6})-([a-z0-9-]+)\.(vcf|db)$")


def backups(folder: Path) -> list[Path]:
    """The backup files in `folder`, oldest first."""
    folder = Path(folder)
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir() if _NAME.match(p.name)), key=lambda p: p.name)


def newest_age_s(folder: Path, now: Optional[float] = None) -> Optional[float]:
    """Seconds since the newest backup was written, or None when there is none."""
    found = backups(folder)
    if not found:
        return None
    return max(0.0, (now if now is not None else time.time()) - max(p.stat().st_mtime for p in found))


def write_backup(store: Any, hub: Any, folder: Path, *, reason: str = "manual", keep: int = 10,
                 now: Optional[float] = None) -> Path:
    """Write both files and prune. Returns the `.db` path. `reason` is a short word for the name."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now if now is not None else time.time()))
    word = re.sub(r"[^a-z0-9-]+", "-", reason.lower()).strip("-") or "manual"
    stem = folder / f"contacts-{stamp}-{word}"
    cards = [card for _cid, card in hub.all_cards()]
    stem.with_suffix(".vcf").write_text(exporters.export_vcard(cards), encoding="utf-8", newline="")
    target = stem.with_suffix(".db")
    tmp = target.with_suffix(".db.part")
    tmp.unlink(missing_ok=True)
    out = sqlite3.connect(tmp)
    try:
        for table in TABLES:
            ddl = store.conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                                     (table,)).fetchone()
            if not ddl or not ddl[0]:
                continue
            out.execute(ddl[0])
            rows = [tuple(r) for r in store.conn.execute(f"SELECT * FROM {table}")]
            if rows:
                out.executemany(f"INSERT INTO {table} VALUES ({','.join('?' * len(rows[0]))})", rows)
        out.commit()
    finally:
        out.close()
    tmp.replace(target)                                   # written last, whole or not at all
    _prune(folder, keep)
    _log.info("contact backup written: {} ({} contacts)", target.name, len(cards))
    return target


def _prune(folder: Path, keep: int) -> None:
    keep = max(1, int(keep))
    by_suffix: dict[str, list[Path]] = {}
    for p in backups(folder):
        by_suffix.setdefault(p.suffix, []).append(p)
    for files in by_suffix.values():
        for old in files[:-keep]:
            try:
                old.unlink()
            except OSError:
                _log.warning("could not remove the old backup {}", old.name)
