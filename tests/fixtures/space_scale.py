r"""A synthetic index at the scale the Space Report has to survive.

Layer: L1 fixture (tests only)

Order 202626270602 (0n) section 3c - "measured on the scale fixture". This
builds a real `SqliteStore` (the real schema at `CURRENT_VERSION`, every
index the migrations create, `upsert_volume` for the catalogued sources) and
fills `files` with `files` rows carrying a *realistic duplicate structure*
rather than random noise, because a GROUP BY's cost depends on the shape of
the groups, not just the row count.

**What is and is not realistic here, stated rather than implied:**

- Realistic: the mix of groups (most content is held once, a long tail of
  2-copy pairs, a few widely-copied files), copies spread across a local
  computer and three catalogued volumes, sizes log-normal, ~15% of rows
  photographs with a 64-bit pHash, a fraction of the photographs being
  recompressed variants of another (a few flipped bits, different bytes),
  ~10% of rows unhashed (a name-only pass leaves `content_hash` NULL).
- NOT real: nothing in `chunks`/FTS (the report never reads them, but they
  do make the real `files` table's pages sparser on a real disk), and the
  paths are synthetic. Row insertion is one `executemany` inside a single
  `store.write()` transaction rather than 200,000 `upsert_file` commits -
  same table, same indexes, same values; only the commit count differs.
"""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any

from app.storage.sqlite_store import SqliteStore

__all__ = ["build_space_scale_store", "DEFAULT_FILES"]

DEFAULT_FILES = 200_000

_PHOTO_SHARE = 0.15
_UNHASHED_SHARE = 0.10
_VOLUMES = (("Old WD", "drive", "OFFLINE"), ("Holiday SD", "drive", "OFFLINE"),
            ("NAS", "network", "ONLINE"))
#: Fraction of all rows that live on the catalogued volumes (the rest are
#: "This computer").
_VOLUME_SHARE = 0.25


def _flip_bits(phash: str, count: int, rng: random.Random) -> str:
    value = int(phash, 16)
    for bit in rng.sample(range(64), count):
        value ^= 1 << bit
    return f"{value:016x}"


def build_space_scale_store(path: Path, files: int = DEFAULT_FILES, *,
                            seed: int = 20260919) -> tuple[SqliteStore, dict[str, Any]]:
    """`(open store, facts)`. `facts` is what was planted, for assertions and
    for stating honestly what the measurement was taken over."""
    rng = random.Random(seed)
    store = SqliteStore(path).connect()
    volume_ids = [
        store.upsert_volume(f"scale-{name}", kind=kind, name=name, status=status)
        for name, kind, status in _VOLUMES
    ]

    # ---- plan the content: (content_hash, size, is_photo, phash, copies) ----
    unhashed = int(files * _UNHASHED_SHARE)
    remaining = files - unhashed
    plan: list[tuple[str | None, int, str, str | None, int]] = []
    placed = 0
    serial = 0
    photo_bases: list[str] = []
    while placed < remaining:
        roll = rng.random()
        # 55% of pieces of content are held once; 25% twice; 12% three times;
        # 7% 4-9 times; 1% a widely-copied file of 10-40 copies.
        if roll < 0.55:
            copies = 1
        elif roll < 0.80:
            copies = 2
        elif roll < 0.92:
            copies = 3
        elif roll < 0.99:
            copies = rng.randint(4, 9)
        else:
            copies = rng.randint(10, 40)
        copies = min(copies, remaining - placed)
        serial += 1
        is_photo = rng.random() < _PHOTO_SHARE
        size = max(200, int(rng.lognormvariate(11.0, 1.8)))
        phash = None
        ext = "jpg" if is_photo else rng.choice(("pdf", "docx", "txt", "xlsx", "mp3", "png"))
        if is_photo:
            # A tenth of the pictures are a recompressed/resized variant of
            # an earlier one: different bytes, a handful of flipped bits.
            if photo_bases and rng.random() < 0.10:
                phash = _flip_bits(rng.choice(photo_bases), rng.randint(1, 8), rng)
            else:
                phash = f"{rng.getrandbits(64):016x}"
                photo_bases.append(phash)
        plan.append((f"h{serial:012x}", size, ext, phash, copies))
        placed += copies

    rows: list[tuple] = []
    n = 0
    stamp = 1_700_000_000

    def add(content_hash, size, ext, phash, volume_id):
        nonlocal n
        n += 1
        folder = f"D:\\Archive\\{n % 997:03d}"
        name = f"file{n:07d}.{ext}"
        if volume_id is None:
            path_text, rel = f"{folder}\\{name}", None
        else:
            rel = f"{n % 997:03d}/{name}"
            path_text = f"leasha-volume://{volume_id}/{rel}"
            folder = f"{n % 997:03d}"
        rows.append((path_text, folder, ext, size, 1_000_000_000 + n, content_hash,
                     "INDEXED", "file", volume_id, rel, phash, stamp + n))

    for content_hash, size, ext, phash, copies in plan:
        for _ in range(copies):
            on_volume = rng.random() < _VOLUME_SHARE
            add(content_hash, size, ext, phash,
                rng.choice(volume_ids) if on_volume else None)
    for _ in range(unhashed):
        add(None, max(200, int(rng.lognormvariate(11.0, 1.8))),
            rng.choice(("txt", "log", "eml")), None,
            rng.choice(volume_ids) if rng.random() < _VOLUME_SHARE else None)

    with store.write() as conn:
        conn.executemany(
            "INSERT INTO files (path, parent_dir, ext, size_bytes, mtime_ns, "
            "content_hash, status, source_kind, volume_id, relative_path, phash, "
            "indexed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    with store.write() as conn:
        conn.execute("ANALYZE")

    facts = {
        "files": len(rows),
        "distinct_hashes": len(plan),
        "duplicate_hashes": sum(1 for p in plan if p[4] > 1),
        "photos": sum(1 for r in rows if r[10]),
        "photo_hashes": sum(1 for p in plan if p[3]),
        "volumes": len(volume_ids),
        "seed": seed,
    }
    return store, facts
