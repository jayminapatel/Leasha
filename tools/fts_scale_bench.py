r"""How keyword search, filters and the word index behave at scale, measured.

Layer: tooling, not app code.

    venv\Scripts\python.exe tools\fts_scale_bench.py build   --db C:\scratch\bench.db --files 100000
    venv\Scripts\python.exe tools\fts_scale_bench.py measure --db C:\scratch\bench.db --out C:\scratch\bench.json

Non-negotiable 9: the index-strategy questions of 2026-10-04 (filtered search
cost, composite indexes, prefix search, page cache and mmap, unmerged segments)
are answered by this script's output, not by reasoning about query plans.

**The database is the app's own.** `build` opens it through `SqliteStore`, so
every migration runs and the schema is exactly what an index has; only the rows
are synthetic. **The queries are the app's own** wherever one exists:
`keyword.search`, `SqliteStore.browse_files` and `browse_messages` are called,
not restated. Raw SQL appears only for a proposed alternative, which by
definition has no code yet.

**The corpus.** Words are built from syllables and drawn from a Zipf
distribution (s=1.07, the usual fit for English), so common words are common,
rare words are rare and prefixes expand to realistic numbers of terms. Five
words are planted at known ranks so their document frequency is known:
`pump` and `valve` (common), `invoice` (middling), `barnsley` (rare) and `the`
(everywhere). Files are 35% mail, the rest spread over document types, with
modification times across ten years.

**What it is not.** Never point `--db` at `D:\Leasha\Data` - `build` refuses a
file that exists. The OS file cache cannot be emptied from here, so every
timing is warm: the first-run figure is reported beside the best and median of
five, and is the nearest thing to cold this can give. Every result carries the
corpus size and the fraction of the 20-million-chunk design target it is, and
anything under 100% is marked `representative: false`.
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TARGET_CHUNKS = 20_000_000
NS = 1_000_000_000
NOW_S = 1_790_000_000                     # mid-2026, so "after:2024" splits the corpus
SPAN_S = 10 * 365 * 86_400

#: Planted words and the Zipf rank each is given (1 = most common).
PLANTED = {"the": 1, "pump": 40, "valve": 55, "invoice": 900, "barnsley": 40_000}

EXT_MIX = [("pdf", 30), ("docx", 15), ("txt", 10), ("xlsx", 6), ("py", 5),
           ("pptx", 4), ("md", 4), ("csv", 3), ("jpg", 3), ("dwg", 1)]
MAIL_SHARE = 0.35

_SYLLABLES = [c + v for c in "bcdfghjklmnprstvwz" for v in ("a", "e", "i", "o", "u", "ai", "ea", "ou")] + \
             [v + c for v in "aeiou" for c in ("n", "r", "s", "t", "l", "nd", "st", "ck")]


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def _vocabulary(size: int, rng: random.Random) -> list[str]:
    planted = set(PLANTED)
    words: list[str] = []
    seen: set[str] = set(planted)
    while len(words) < size - len(planted):
        word = "".join(rng.choice(_SYLLABLES) for _ in range(rng.choice((1, 2, 2, 3, 3, 4))))
        if word not in seen:
            seen.add(word)
            words.append(word)
    for word, rank in sorted(PLANTED.items(), key=lambda item: item[1]):
        words.insert(rank - 1, word)
    return words


def build(db: Path, files: int, chunks_per_file: int, words_per_chunk: int, seed: int) -> dict:
    import numpy as np

    from app.storage.sqlite_store import SqliteStore

    if db.exists():
        raise SystemExit(f"{db} exists - build only ever makes a new file")
    db.parent.mkdir(parents=True, exist_ok=True)
    rng = random.Random(seed)
    gen = np.random.default_rng(seed)

    vocab = np.array(_vocabulary(60_000, rng), dtype=object)
    ranks = np.arange(1, len(vocab) + 1, dtype=np.float64)
    weights = 1.0 / ranks ** 1.07
    weights /= weights.sum()
    senders = [f"person{n}@example{n % 40}.com" for n in range(3000)]
    sender_w = 1.0 / np.arange(1, len(senders) + 1) ** 1.1
    sender_w /= sender_w.sum()

    exts = [e for e, _ in EXT_MIX]
    ext_w = np.array([w for _, w in EXT_MIX], dtype=np.float64)
    ext_w /= ext_w.sum()

    started = time.perf_counter()
    with SqliteStore(db) as store:
        conn = sqlite3.connect(db, isolation_level=None)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = OFF")
        conn.execute("PRAGMA cache_size = -262144")
        triggers = [row[0] for row in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' "
            "AND name IN ('chunks_ai', 'chunks_ad', 'chunks_au', "
            "'messages_ai', 'messages_ad', 'messages_au')")]
        for name in ("chunks_ai", "chunks_ad", "chunks_au", "messages_ai", "messages_ad", "messages_au"):
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")

        chunk_id = 0
        batch_files = 2_000
        for first in range(0, files, batch_files):
            count = min(batch_files, files - first)
            is_mail = gen.random(count) < MAIL_SHARE
            ext_pick = gen.choice(len(exts), size=count, p=ext_w)
            mtimes = NOW_S - gen.integers(0, SPAN_S, size=count)
            n_chunks = np.maximum(1, gen.poisson(chunks_per_file, size=count))
            total = int(n_chunks.sum())
            tokens = gen.choice(len(vocab), size=(total, words_per_chunk), p=weights)
            file_rows, chunk_rows, mail_rows, name_rows = [], [], [], []
            t = 0
            for k in range(count):
                file_id = first + k + 1
                mtime_s = int(mtimes[k])
                folder = rf"D:\Bench\area{file_id % 97}\folder{file_id % 1013}"
                if is_mail[k]:
                    path = rf"D:\Mail\archive{file_id % 7}.pst::msg{file_id}"
                    file_rows.append((file_id, path, rf"D:\Mail\archive{file_id % 7}.pst", "msg",
                                      2_000, mtime_s * NS, "INDEXED", "pst_message"))
                    sender = senders[int(gen.choice(len(senders), p=sender_w))]
                    subject = " ".join(vocab[tokens[t, :6]])
                    mail_rows.append((file_id, subject, sender, sender.lower(),
                                      json.dumps([senders[file_id % 3000]]), mtime_s,
                                      f"conv{file_id // 5}"))
                else:
                    ext = exts[int(ext_pick[k])]
                    name = f"{' '.join(vocab[tokens[t, 6:8]])} {file_id}.{ext}"
                    path = folder + "\\" + name
                    file_rows.append((file_id, path, folder, ext, 50_000, mtime_s * NS,
                                      "INDEXED", "file"))
                    name_rows.append((file_id, name, folder))
                for ordinal in range(int(n_chunks[k])):
                    chunk_id += 1
                    chunk_rows.append((chunk_id, file_id, ordinal, " ".join(vocab[tokens[t]])))
                    t += 1
            conn.execute("BEGIN")
            conn.executemany(
                "INSERT INTO files (id, path, parent_dir, ext, size_bytes, mtime_ns, status, source_kind) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", file_rows)
            conn.executemany(
                "INSERT INTO chunks (id, file_id, ordinal, text, embedded) VALUES (?, ?, ?, ?, 1)",
                chunk_rows)
            conn.executemany(
                "INSERT INTO messages (file_id, subject, sender, sender_lc, recipients, sent_at, conversation) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", mail_rows)
            conn.executemany("INSERT INTO files_fts (rowid, name, folder) VALUES (?, ?, ?)", name_rows)
            conn.execute("COMMIT")
            print(f"  {first + count:,} files, {chunk_id:,} chunks, "
                  f"{time.perf_counter() - started:.0f}s", flush=True)

        print("  building the word indexes ...", flush=True)
        conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        conn.execute("INSERT INTO files_fts(files_fts) VALUES('optimize')")
        for statement in triggers:
            conn.execute(statement)
        conn.execute("ANALYZE")
        conn.close()
        with store.write() as write:
            store._forget_fts_statistics(write)

    summary = {"files": files, "chunks": chunk_id, "seconds": round(time.perf_counter() - started, 1),
               "bytes": db.stat().st_size, "seed": seed, "words_per_chunk": words_per_chunk}
    print(json.dumps(summary, indent=2))
    return summary


# ---------------------------------------------------------------------------
# measure
# ---------------------------------------------------------------------------

def _time(fn: Callable[[], Any], repeat: int = 5) -> dict:
    t = time.perf_counter()
    result = fn()
    first = (time.perf_counter() - t) * 1000
    runs = []
    for _ in range(repeat):
        t = time.perf_counter()
        fn()
        runs.append((time.perf_counter() - t) * 1000)
    rows = len(result) if hasattr(result, "__len__") else None
    return {"first_ms": round(first, 2), "best_ms": round(min(runs), 2),
            "median_ms": round(statistics.median(runs), 2), "rows": rows}


def _segments(conn: sqlite3.Connection, table: str) -> int:
    blob = conn.execute(f"SELECT block FROM {table}_data WHERE id = 10").fetchone()[0]

    def varint(i: int) -> tuple[int, int]:
        value = 0
        for n in range(9):
            byte = blob[i + n]
            if n == 8:
                return (value << 8) | byte, i + 9
            value = (value << 7) | (byte & 0x7F)
            if not byte & 0x80:
                return value, i + n + 1
        raise ValueError(table)

    i = 8 if blob[4:8] == b"\xff\x00\x00\x01" else 4
    _levels, i = varint(i)
    segments, _ = varint(i)
    return segments


def _df(conn: sqlite3.Connection, word: str) -> int:
    from app.search.query import parse_query
    expression = parse_query(word).fts_match()
    return conn.execute("SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
                        (expression,)).fetchone()[0]


KEYWORD = [
    ("rare word", "barnsley", "all"),
    ("middling word", "invoice", "all"),
    ("common word", "pump", "all"),
    ("two common words", "pump valve", "all"),
    ("everywhere word", "the", "all"),
    ("middling, type:pdf", "invoice type:pdf", "all"),
    ("common, type:pdf", "pump type:pdf", "all"),
    ("common, type:dwg (1%)", "pump type:dwg", "all"),
    ("common, after:2025-01-01", "pump after:2025-01-01", "all"),
    ("common, Mail scope", "pump", "mail"),
    ("common, Documents scope", "pump", "documents"),
    ("filter only, type:pdf", "type:pdf", "all"),
    ("filter only, type:dwg", "type:dwg", "all"),
]
PREFIX = ["pump v", "pump va", "pump val", "b", "ba", "bar", "barn"]


def _keyword_suite(store: Any, label: str = "") -> list[dict]:
    from app.search import keyword
    from app.search.query import parse_query

    out = []
    for name, raw, scope in KEYWORD:
        parsed = parse_query(raw).scoped(scope)
        result = _time(lambda: keyword.search(store, parsed, limit=100))
        out.append({"case": name, "query": raw, "scope": scope, **result})
        print(f"  {label}{name:30} {result['best_ms']:>9.2f} ms  (first {result['first_ms']:.1f}, "
              f"{result['rows']} rows)", flush=True)
    return out


def _overfetch(store: Any, expression: str, where: str, params: list, k: int, limit: int = 100) -> list:
    """The proposed shape: FTS5's own top-k first, then the filter on those."""
    return store.conn.execute(f"""
        SELECT c.id AS chunk_id, top.score
        FROM (SELECT rowid AS chunk_id, rank AS score FROM chunks_fts
              WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?) AS top
        JOIN chunks c ON c.id = top.chunk_id
        JOIN files  f ON f.id = c.file_id
        WHERE 1=1{where}
        ORDER BY top.score LIMIT ?""", [expression, k, *params, limit]).fetchall()


def measure(db: Path, out: Path | None) -> dict:
    from app.search import keyword
    from app.search.query import parse_query
    from app.storage.filters import file_filter_sql
    from app.storage.sqlite_store import SqliteStore

    report: dict[str, Any] = {"db": str(db), "sqlite": sqlite3.sqlite_version}
    with SqliteStore(db) as store:
        conn = store.conn
        chunks = conn.execute("SELECT max(id) FROM chunks").fetchone()[0] or 0
        files = conn.execute("SELECT count(*) FROM files").fetchone()[0]
        report["corpus"] = {
            "files": files, "chunks": chunks, "bytes": db.stat().st_size,
            "fraction_of_target": round(chunks / TARGET_CHUNKS, 4),
            "representative": chunks >= TARGET_CHUNKS,
            "document_frequency": {w: _df(conn, w) for w in PLANTED},
            "segments": {t: _segments(conn, t) for t in ("chunks_fts", "files_fts", "messages_fts")},
            "cache_size": conn.execute("PRAGMA cache_size").fetchone()[0],
            "mmap_size": conn.execute("PRAGMA mmap_size").fetchone()[0],
        }
        print(json.dumps(report["corpus"], indent=2), flush=True)

        print("\n== 1. keyword search as the app runs it")
        report["keyword"] = _keyword_suite(store)

        print("\n== 2. filtered search: today's shape against top-k first, then filter")
        rows = []
        for name, raw, scope in [c for c in KEYWORD if c[1].split()[-1].startswith(("type:", "after:"))
                                 or c[2] != "all"]:
            parsed = parse_query(raw).scoped(scope)
            expression = parsed.fts_match()
            if not expression:
                continue
            where, params = file_filter_sql(parsed)
            today = [r["chunk_id"] for r in keyword.search(store, parsed, limit=100)]
            entry = {"case": name, "today": _time(lambda: keyword.search(store, parsed, limit=100))}
            for k in (1_000, 10_000):
                got = _overfetch(store, expression, where, params, k)
                ids = [r[0] for r in got]
                entry[f"top{k}"] = {**_time(lambda: _overfetch(store, expression, where, params, k)),
                                    "filled": len(ids) >= min(100, len(today)),
                                    "same_ids": len(set(ids) & set(today))}
            rows.append(entry)
            print(f"  {name:30} today {entry['today']['best_ms']:>9.2f} ms | "
                  f"top1k {entry['top1000']['best_ms']:>8.2f} ms filled={entry['top1000']['filled']} "
                  f"same={entry['top1000']['same_ids']} | "
                  f"top10k {entry['top10000']['best_ms']:>8.2f} ms filled={entry['top10000']['filled']} "
                  f"same={entry['top10000']['same_ids']}", flush=True)
        report["overfetch"] = rows

        print("\n== 3. as-you-type prefix (prefix_last=True, unfiltered)")
        report["prefix"] = []
        for raw in PREFIX:
            parsed = parse_query(raw)
            result = _time(lambda: keyword.search(store, parsed, limit=100, prefix_last=True))
            report["prefix"].append({"query": raw, "match": parsed.fts_match(prefix_last=True), **result})
            print(f"  {raw!r:14} {result['best_ms']:>9.2f} ms  ({result['rows']} rows)", flush=True)

        print("\n== 4. Files and Mail tabs")
        thread = (conn.execute("SELECT conversation FROM messages ORDER BY file_id LIMIT 1 OFFSET ?",
                               (files // 10,)).fetchone() or ("none",))[0]
        tabs = []
        for name, call in [
            ("Files tab, empty box", lambda: store.browse_files(parse_query(""), limit=200)),
            ("Files tab, 'pump'", lambda: store.browse_files(parse_query("pump"), limit=200)),
            ("Files tab, 'invoice' type:pdf", lambda: store.browse_files(parse_query("invoice type:pdf"), limit=200)),
            ("Mail tab, newest", lambda: store.browse_messages(limit=500)),
            ("Mail tab, words 'pump'", lambda: store.browse_messages(words="pump", limit=500)),
            ("Mail tab, from person7", lambda: store.browse_messages(sender="person7@", limit=500)),
            ("Mail thread by conversation", lambda: conn.execute(
                "SELECT file_id FROM messages WHERE conversation = ? ORDER BY sent_at", (thread,)).fetchall()),
        ]:
            result = _time(call)
            tabs.append({"case": name, **result})
            print(f"  {name:32} {result['best_ms']:>9.2f} ms  ({result['rows']} rows)", flush=True)
        report["tabs"] = tabs

        print("\n== 5. composite indexes (created, measured, dropped)")
        composite = []
        for index, ddl, cases in [
            ("ext_mtime", "files(ext, mtime_ns)",
             [("filter only, type:pdf", lambda: keyword.search(store, parse_query("type:pdf"), limit=100)),
              ("filter only, type:dwg", lambda: keyword.search(store, parse_query("type:dwg"), limit=100))]),
            ("conv_sent", "messages(conversation, sent_at)",
             [("Mail thread by conversation", lambda: conn.execute(
                 "SELECT file_id FROM messages WHERE conversation = ? ORDER BY sent_at",
                 (thread,)).fetchall())]),
        ]:
            before = {case: _time(fn) for case, fn in cases}
            t = time.perf_counter()
            with store.write() as write:
                write.execute(f"CREATE INDEX bench_{index} ON {ddl}")
                write.execute(f"ANALYZE {ddl.split('(')[0]}")
            created = round(time.perf_counter() - t, 1)
            after = {case: _time(fn) for case, fn in cases}
            with store.write() as write:
                write.execute(f"DROP INDEX bench_{index}")
                write.execute(f"ANALYZE {ddl.split('(')[0]}")
            for case, _ in cases:
                composite.append({"index": ddl, "case": case, "before": before[case],
                                  "after": after[case], "create_s": created})
                print(f"  {ddl:34} {case:30} {before[case]['best_ms']:>9.2f} -> "
                      f"{after[case]['best_ms']:>9.2f} ms", flush=True)
        report["composite"] = composite

        print("\n== 6. page cache 64 MB and mmap 256 MB on the search connection")
        conn.execute("PRAGMA cache_size = -65536")
        conn.execute("PRAGMA mmap_size = 268435456")
        report["memory"] = _keyword_suite(store, "mem  ")
        conn.execute(f"PRAGMA cache_size = {report['corpus']['cache_size']}")
        conn.execute("PRAGMA mmap_size = 0")

        print("\n== 7. unmerged segments: 2% more chunks written as the indexer writes them")
        last_file = conn.execute("SELECT max(id) FROM files").fetchone()[0]
        last_chunk = chunks
        sample = [r[0] for r in conn.execute(
            "SELECT text FROM chunks WHERE id % 97 = 0 LIMIT 2000").fetchall()]
        extra = max(400, chunks // 50)
        for batch in range(200):
            with store.write() as write:
                for n in range(extra // 200):
                    last_chunk += 1
                    write.execute("INSERT INTO chunks (id, file_id, ordinal, text, embedded) "
                                  "VALUES (?, ?, ?, ?, 1)",
                                  (last_chunk, (batch % last_file) + 1, 10_000 + last_chunk,
                                   sample[(batch * 31 + n) % len(sample)]))
        report["unmerged_segments"] = _segments(conn, "chunks_fts")
        print(f"  chunks_fts now has {report['unmerged_segments']} segments", flush=True)
        report["unmerged"] = _keyword_suite(store, "unmerged  ")
        t = time.perf_counter()
        store.optimize_fts()
        report["merge_s"] = round(time.perf_counter() - t, 1)
        print(f"  merged in {report['merge_s']}s, segments now {_segments(conn, 'chunks_fts')}")
        report["merged"] = _keyword_suite(store, "merged  ")
        with store.write() as write:
            write.execute("DELETE FROM chunks WHERE id > ?", (chunks,))
        store.optimize_fts()

    if out:
        out.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nwritten to {out}")
    return report


def _inside_real_data(db: Path) -> bool:
    """Whether `db` is under the configured `DATA_PATH` (or the default one)."""
    roots = [Path(r"D:\Leasha\Data")]
    try:
        from app.core.config import load_settings
        roots.append(Path(load_settings().data_path))
    except Exception:                                        # noqa: BLE001 - the default still guards
        pass
    target = db.resolve()
    return any(target == root.resolve() or root.resolve() in target.parents for root in roots)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="make a synthetic index at --db (never an existing file)")
    b.add_argument("--db", type=Path, required=True)
    b.add_argument("--files", type=int, default=100_000)
    b.add_argument("--chunks-per-file", type=int, default=10)
    b.add_argument("--words-per-chunk", type=int, default=120)
    b.add_argument("--seed", type=int, default=20261004)
    m = sub.add_parser("measure", help="time the app's queries against --db")
    m.add_argument("--db", type=Path, required=True)
    m.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if _inside_real_data(args.db):
        raise SystemExit(f"refusing {args.db}: it is inside the real index's DATA_PATH")
    if args.command == "build":
        build(args.db, args.files, args.chunks_per_file, args.words_per_chunk, args.seed)
    else:
        measure(args.db, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
