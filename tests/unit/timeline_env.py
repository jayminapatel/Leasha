r"""Shared builders for the Life Timeline's tests: real store, real rows.

Layer: L4 test support

Every date is **local noon** of a day, because the timeline (like the
``after:``/``before:`` operators and the indexer's EXIF dates) reads dates in
local time and this project's machine sits in Nepal (UTC+5:45): a UTC midnight
would land on the wrong side of a month boundary and the test would pass or
fail by time zone. Noon is safe in every zone.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Optional

from app.storage.sqlite_store import SqliteStore

NS = 1_000_000_000


def noon(year: int, month: int, day: int, seconds: int = 0) -> int:
    """Local noon of a day (plus `seconds`), in epoch nanoseconds."""
    return (int(dt.datetime(year, month, day, 12).timestamp()) + seconds) * NS


def add_file(store: SqliteStore, path: str, *, mtime: int, taken: Optional[int] = None,
             hint: bool = False, ext: Optional[str] = None, volume_id: Optional[int] = None,
             relative_path: Optional[str] = None, phash: Optional[str] = None,
             content_hash: Optional[str] = None, repo_id: Optional[int] = None,
             source_kind: str = "file", size: int = 100, place: Optional[str] = None) -> int:
    ext = ext if ext is not None else (path.rsplit(".", 1)[-1].lower() if "." in path else "")
    file_id = store.upsert_file(
        path, size_bytes=size, mtime_ns=mtime, ext=ext, parent_dir=str(Path(path).parent),
        status="INDEXED", source_kind=source_kind, taken_at_ns=taken, taken_at_is_hint=hint,
        volume_id=volume_id, relative_path=relative_path, content_hash=content_hash,
        repo_id=repo_id, place=place)
    if phash:
        with store.write() as conn:
            conn.execute("UPDATE files SET phash = ? WHERE id = ?", (phash, file_id))
    return file_id


def add_mail(store: SqliteStore, subject: str, *, sent: Optional[int], container_mtime: int,
             sender: str = "dave@example.com") -> int:
    """A message the way a `.pst` import writes one: its *file row* carries the
    container's single modified time, and the real date is `messages.sent_at`
    (whole seconds)."""
    file_id = store.upsert_file(
        f"pst://box/{subject}", size_bytes=10, mtime_ns=container_mtime, ext="",
        parent_dir="pst://box", status="INDEXED", source_kind="pst_message")
    with store.write() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO messages (file_id, subject, sender, sent_at, has_attach) "
            "VALUES (?, ?, ?, ?, 0)",
            (file_id, subject, sender, None if sent is None else sent // NS))
    return file_id


def june_2015(tmp_path: Path) -> tuple[SqliteStore, dict[str, Any]]:
    r"""The order's own fixture: **June 2015**, seen from a decade later.

    - a photograph *shot* on 10 June 2015 whose file was copied in 2019
      (EXIF date says June 2015; the file's own time says 2019) - it must be
      found in June 2015 and *not* in 2019
    - a letter, dated only by its file time, 20 June 2015
    - a photograph on **an offline drive** ("Old WD"), 15 June 2015, with a
      camera date, so it is in the month and carries its source badge
    - a message sent 25 June 2015 whose container's modified time is 2021
    - things that must NOT be in June 2015: a file from July, a photograph
      shot in 2019, program code from June 2015, an undated file
    """
    store = SqliteStore(tmp_path / "june.db").connect()
    drive = store.upsert_volume("guid-old-wd", kind="drive", name="Old WD", status="OFFLINE")
    with store.write() as conn:
        repo = conn.execute(
            "INSERT INTO repos (root_path, name, kind, last_seen) VALUES ('D:\\code', 'code', 'work', 0)"
        ).lastrowid
    ids = {
        "photo": add_file(store, r"D:\Pictures\lake.jpg", mtime=noon(2019, 3, 1),
                          taken=noon(2015, 6, 10)),
        "letter": add_file(store, r"D:\Letters\to-the-bank.docx", mtime=noon(2015, 6, 20)),
        "offline": add_file(store, "leasha-volume://1/Holiday/beach.jpg",
                            mtime=noon(2020, 1, 1), taken=noon(2015, 6, 15),
                            volume_id=drive, relative_path="Holiday/beach.jpg"),
        "mail": add_mail(store, "Wedding plans", sent=noon(2015, 6, 25),
                         container_mtime=noon(2021, 3, 3)),
        "july": add_file(store, r"D:\Letters\july.docx", mtime=noon(2015, 7, 2)),
        "later_photo": add_file(store, r"D:\Pictures\new.jpg", mtime=noon(2019, 3, 1),
                                taken=noon(2019, 5, 5)),
        "code": add_file(store, r"D:\code\main.py", mtime=noon(2015, 6, 12), repo_id=repo),
        "undated": add_file(store, r"D:\Misc\odd.txt", mtime=0),
    }
    return store, ids
