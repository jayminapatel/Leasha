r"""A synthetic index at the scale the Life Timeline has to survive.

Layer: L1 fixture (tests only)

Order 202626270602 (0n) section 4c / the measurement item. Builds a real
`SqliteStore` (the real schema, every index the migrations create) and fills
`files` and `messages` with a **realistic spread of dates** rather than random
noise, because a date-range query's cost depends on how many rows fall in the
range, not only on how many rows exist:

- 15 years of files (2005-2019), a slow rise then a plateau, like a real life
- ~14% photographs, most with a camera date (`taken_at_ns`), a tenth of those a
  guessed-from-a-folder year, the rest dated only by their file time
- 12% of rows on three catalogued volumes (one online, two in a drawer)
- 4% program code (`repo_id` set) - the timeline leaves it out by default
- `mail_share` messages with `sent_at` set (their file rows carry the
  container's single modified time, exactly as a real `.pst` import does)
- **one deliberately dense month** - `DENSE_MONTH` (June 2015) gets `dense`
  extra photographs, the "4,000 photos in a month" case 4c names

**Not real, and said so:** paths are synthetic, `chunks`/FTS are empty (the
timeline never reads them), and rows go in with one `executemany` inside one
transaction rather than one `upsert_file` commit each - same tables, same
indexes, same values; only the commit count differs.
"""

from __future__ import annotations

import datetime as _dt
import random
from pathlib import Path
from typing import Any

from app.storage.sqlite_store import SqliteStore

__all__ = ["build_timeline_scale_store", "DEFAULT_FILES", "DENSE_MONTH"]

DEFAULT_FILES = 200_000
DENSE_MONTH = (2015, 6)
_NS = 1_000_000_000
_VOLUMES = (("Old WD", "drive", "OFFLINE"), ("Holiday SD", "drive", "OFFLINE"),
            ("NAS", "network", "ONLINE"))


def _noon_ns(year: int, month: int, day: int, offset_s: int = 0) -> int:
    """Local noon of a day, so a day is never on the wrong side of a boundary
    whatever the machine's time zone."""
    return (int(_dt.datetime(year, month, day, 12).timestamp()) + offset_s) * _NS


def build_timeline_scale_store(path: Path, files: int = DEFAULT_FILES, *,
                               dense: int = 4_000, mail_share: float = 0.10,
                               analyse: bool = True, seed: int = 20260920) -> tuple[SqliteStore, dict[str, Any]]:
    """`(open store, facts)`. `facts` is what was planted, for assertions and
    for stating honestly what a measurement was taken over."""
    rng = random.Random(seed)
    store = SqliteStore(path).connect()
    volume_ids = [store.upsert_volume(f"tl-{name}", kind=kind, name=name, status=status)
                  for name, kind, status in _VOLUMES]
    with store.write() as conn:
        repo_id = conn.execute(
            "INSERT INTO repos (root_path, name, kind, last_seen) VALUES (?, ?, 'work', 0)",
            ("D:\\proj", "proj")).lastrowid

    def a_day() -> tuple[int, int, int]:
        # Weight toward later years, like a life that got more digital.
        year = 2005 + min(14, int(rng.betavariate(2.2, 1.6) * 15))
        month = rng.randint(1, 12)
        return year, month, rng.randint(1, 28)

    photo_exts = ("jpg", "jpg", "jpg", "jpeg", "png", "heic")
    other_exts = ("pdf", "docx", "txt", "xlsx", "mp3", "mp4", "mov")
    file_rows: list[tuple] = []
    n = 0

    def add(ext: str, when_ns: int, *, taken: int | None, hint: int = 0,
            volume_id: int | None = None, repo: int | None = None,
            source_kind: str = "file", phash: str | None = None) -> None:
        nonlocal n
        n += 1
        folder = f"D:\\Archive\\{n % 997:03d}"
        name = f"file{n:07d}.{ext}" if ext else f"msg{n:07d}"
        if volume_id is None:
            path_text, rel = f"{folder}\\{name}", None
        else:
            rel = f"{n % 997:03d}/{name}"
            path_text, folder = f"leasha-volume://{volume_id}/{rel}", f"{n % 997:03d}"
        file_rows.append((path_text, folder, ext, 1000 + n % 5000, when_ns, f"h{n:012x}",
                          "INDEXED", source_kind, volume_id, rel, phash, 1_700_000_000 + n,
                          taken, hint, repo))

    mail_count = int(files * mail_share)
    ordinary = files - mail_count - dense
    for _ in range(ordinary):
        year, month, day = a_day()
        roll = rng.random()
        volume = rng.choice(volume_ids) if rng.random() < 0.12 else None
        repo = repo_id if (repo_id is not None and rng.random() < 0.04) else None
        if roll < 0.14:
            when = _noon_ns(year, month, day, rng.randint(0, 40_000))
            # Copy date years after the shot, as a real library has.
            copied = _noon_ns(min(2019, year + rng.randint(0, 4)), month, day)
            if rng.random() < 0.70:
                add(rng.choice(photo_exts), copied, taken=when, volume_id=volume, repo=repo,
                    phash=f"{rng.getrandbits(64):016x}")
            elif rng.random() < 0.5:
                add(rng.choice(photo_exts), _noon_ns(year, month, day), taken=None,
                    volume_id=volume, repo=repo, phash=f"{rng.getrandbits(64):016x}")
            else:
                add(rng.choice(photo_exts), copied, taken=_noon_ns(year, 1, 1), hint=1,
                    volume_id=volume, repo=repo)
        else:
            add(rng.choice(other_exts), _noon_ns(year, month, day, rng.randint(0, 40_000)),
                taken=None, volume_id=volume, repo=repo)
    # The dense month: photographs shot across its 30 days, in bursts of a few.
    dense_year, dense_month = DENSE_MONTH
    burst_hash = 0
    for i in range(dense):
        day = 1 + (i * 30) // dense
        if i % 5 == 0:
            burst_hash = rng.getrandbits(64)
        add("jpg", _noon_ns(dense_year + 2, 1, 1),
            taken=_noon_ns(dense_year, dense_month, day, (i % 5) * 2 + (i * 7) % 3000),
            phash=f"{burst_hash ^ (1 << (i % 5)):016x}")

    with store.write() as conn:
        conn.executemany(
            "INSERT INTO files (path, parent_dir, ext, size_bytes, mtime_ns, content_hash, "
            "status, source_kind, volume_id, relative_path, phash, indexed_at, taken_at_ns, "
            "taken_at_is_hint, repo_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", file_rows)

    # Mail: every message's *file row* carries the container's one modified time.
    container = _noon_ns(2021, 3, 3)
    mail_rows = [(f"pst://tl/{i:07d}", "pst://tl", "", 100, container, None, "INDEXED",
                  "pst_message", None, None, None, 1_700_000_000 + i, None, 0, None)
                 for i in range(mail_count)]
    with store.write() as conn:
        conn.executemany(
            "INSERT INTO files (path, parent_dir, ext, size_bytes, mtime_ns, content_hash, "
            "status, source_kind, volume_id, relative_path, phash, indexed_at, taken_at_ns, "
            "taken_at_is_hint, repo_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", mail_rows)
        ids = [row[0] for row in conn.execute(
            "SELECT id FROM files WHERE source_kind = 'pst_message' ORDER BY id")]
        sent = []
        for file_id in ids:
            year, month, day = a_day()
            sent.append((file_id, f"Subject {file_id}", "a@example.com",
                         _noon_ns(year, month, day, rng.randint(0, 40_000)) // _NS))
        conn.executemany(
            "INSERT INTO messages (file_id, subject, sender, sent_at, has_attach) "
            "VALUES (?, ?, ?, ?, 0)", sent)
    if analyse:
        with store.write() as conn:
            conn.execute("ANALYZE")

    facts = {
        "files": len(file_rows) + len(mail_rows), "mail": mail_count, "dense": dense,
        "dense_month": DENSE_MONTH, "volumes": len(volume_ids), "seed": seed,
    }
    return store, facts
