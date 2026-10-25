r"""How long a restart takes to start reading new work, measured.

Layer: tooling, not app code.

    venv\Scripts\python.exe tools\resume_bench.py build   --dir C:\scratch\resume --files 140000 --indexed 34000 --backlog 750000
    venv\Scripts\python.exe tools\resume_bench.py measure --dir C:\scratch\resume --repeat 3

The owner, 2026-10-25: "when you stop and start index the resume is too slow
... we have the data in the database already so not sure what takes the time".
On his run that morning (`logs/runs/run-20261025-113053-index.log`) the time
from Start to the first file read was 5 min 24 s. This reproduces the shape of
that restart on a synthetic index so each part can be timed before and after a
change:

* **files on disk** - `--files` small text files under `<dir>\corpus`, in
  folders of 200, so the walk, the `stat` and the change check are real;
* **an index that already knows them** - `--indexed` rows read in full
  (`PARTIAL`, the state a text-first run leaves a file in until its vectors
  exist, with their real size and date), the rest `PENDING` rows as the scan of
  an interrupted run writes them (`add_waiting_files`);
* **a backlog for the meaning model** - `--backlog` passages of about 600
  characters with `embedded = 0`, spread over the read files.

`measure` runs the real `Pipeline.run` over it - the same class the window
runs, with the CPU governor off, so a busy machine cannot add a pause - and
records the time of each phase change. The meaning model is a made-up one
unless `--model-cache` names a folder holding the real one (copy it out of the
data folder first: this never reads or writes there). **The real one matters
for the scan**: with a backlog parked, the model embeds it *during* the scan,
and that is what the walker thread competes with. The run is stopped the moment it starts reading. Each
repeat starts from a fresh copy of the built index, so nothing one run writes
changes the next.

Reported per repeat: `catch_up` (the start-of-run repairs, the backlog parked
for the model), `scanning` (walk and change check of every file) and
`to_reading` (Start to the first file handed to a reader), plus two direct
timings of the pieces under suspicion: `store.unembedded_by_file` alone, and
the change check (`Pipeline._classify`, `hash_now=False`) over every file the
walk finds.

**What it is not.** The OS file cache cannot be emptied from here, so every
figure is warm; the owner's index is 8 GB and is not. Never point `--dir` at
`D:\LeashaIndex\Data` - `build` refuses a folder that exists.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FILES_PER_FOLDER = 200
DIM = 8
PASSAGE = ("pump station commissioning report for the northern site valve "
           "pressure readings were taken at every stage of the start up and the "
           "results are summarised below together with the open actions ") * 3


def _corpus_paths(root: Path, files: int) -> list[Path]:
    return [root / f"folder{n // FILES_PER_FOLDER:05d}" / f"doc{n:07d}.txt"
            for n in range(files)]


def build(work: Path, files: int, indexed: int, backlog: int) -> dict[str, Any]:
    """Write the corpus and the index. Refuses a folder that exists."""
    from app.storage.sqlite_store import SqliteStore

    if work.exists():
        raise SystemExit(f"{work} exists - build only into a new folder")
    corpus = work / "corpus"
    started = time.perf_counter()
    paths = _corpus_paths(corpus, files)
    for folder in sorted({p.parent for p in paths}):
        folder.mkdir(parents=True)
    for number, path in enumerate(paths):
        path.write_text(f"document {number} pump valve", encoding="utf-8")
    wrote_files = time.perf_counter() - started

    started = time.perf_counter()
    db = work / "index.db"
    with SqliteStore(db) as store:
        conn = store.conn
        trigger = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='chunks_ai'"
        ).fetchone()[0]
        with store.write() as w:
            w.execute("DROP TRIGGER chunks_ai")       # the word index is not measured
            rows = []
            for number, path in enumerate(paths):
                st = path.stat()
                read = number < indexed
                rows.append((str(path), str(path.parent), "txt", st.st_size, st.st_mtime_ns,
                             f"hash{number}" if read else None,
                             "PARTIAL" if read else "PENDING", "file",
                             time.time() - 3600 if read else None))
            w.executemany(
                "INSERT INTO files (path, parent_dir, ext, size_bytes, mtime_ns, "
                "content_hash, status, source_kind, indexed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                rows)
            ids = [r[0] for r in w.execute(
                "SELECT id FROM files WHERE status = 'PARTIAL' ORDER BY id")]
            per_file = max(1, backlog // max(1, len(ids)))
            batch = []
            made = 0
            for file_id in ids:
                for ordinal in range(per_file):
                    if made >= backlog:
                        break
                    batch.append((file_id, ordinal, PASSAGE, 0))
                    made += 1
                if len(batch) >= 50_000:
                    w.executemany("INSERT INTO chunks (file_id, ordinal, text, embedded) "
                                  "VALUES (?,?,?,?)", batch)
                    batch.clear()
            if batch:
                w.executemany("INSERT INTO chunks (file_id, ordinal, text, embedded) "
                              "VALUES (?,?,?,?)", batch)
            w.execute(trigger)
    shutil.copyfile(db, work / "pristine.db")
    built = {"files": files, "indexed": indexed, "backlog": made,
             "seconds_files": round(wrote_files, 1),
             "seconds_index": round(time.perf_counter() - started, 1),
             "db_mb": round(db.stat().st_size / 2**20)}
    (work / "built.json").write_text(json.dumps(built, indent=2), encoding="utf-8")
    return built


class _Vectors:
    """Stands in for LanceDB: nothing is embedded before reading starts."""

    def ensure_table(self): pass
    def delete_by_file_ids(self, _ids): pass
    def add(self, *, chunk_ids, file_ids, vectors): return len(list(chunk_ids))
    def maybe_compact(self, **_k): return False
    def maybe_create_index(self, **_k): return False
    def count(self): return 0


def _encode(texts):
    return [[1.0] + [0.0] * (DIM - 1) for _ in texts]


#: `--model-cache`: the real meaning model, from a folder that holds it. None:
#: the made-up one. Set once by `main`.
_MODEL_CACHE: Path | None = None


def _embedder():
    from app.index.embedder import Embedder

    if _MODEL_CACHE is None:
        return Embedder(dim=DIM, encoder=_encode)
    # As the owner's run has it (`logs/runs/run-20261025-113053-index.log`):
    # bge-small, quantised, on the processor, four threads.
    return Embedder("BAAI/bge-small-en-v1.5", dim=384, cache_dir=str(_MODEL_CACHE),
                    device="cpu", quantised=True, group_quantised=True, threads=4)


def _pipeline(store, corpus: Path):
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.resources import ResourceLimits
    from app.index.walker import WalkConfig

    config = PipelineConfig(
        walk=WalkConfig(roots=[corpus]), workers=1,
        limits=ResourceLimits(pause_on_battery=False, cpu_percent=0, min_free_gb=0))
    return Pipeline(store, _Vectors(), _embedder(), config)


def _one_run(work: Path) -> dict[str, float]:
    from app.storage.sqlite_store import SqliteStore

    db = work / "index.db"
    for leftover in (db.with_name("index.db-wal"), db.with_name("index.db-shm")):
        leftover.unlink(missing_ok=True)
    shutil.copyfile(work / "pristine.db", db)
    marks: dict[str, float] = {}
    with SqliteStore(db) as store:
        pipeline = _pipeline(store, work / "corpus")
        started = time.perf_counter()

        def on_progress(stats: Any) -> None:
            phase = str(getattr(stats, "phase", "") or "")
            if phase and phase not in marks:
                marks[phase] = time.perf_counter() - started
            if phase == "reading":
                pipeline.request_stop()

        pipeline.run(on_progress=on_progress)
        marks["ended"] = time.perf_counter() - started
        parked = len(pipeline._parked)
    order = ["model", "word_index_check", "catch_up", "planning", "scanning", "reading"]
    seen = [p for p in order if p in marks]
    out = {"to_reading": round(marks.get("reading", float("nan")), 2), "parked_batches": parked}
    for here, after in zip(seen, seen[1:]):
        out[here] = round(marks[after] - marks[here], 2)
    return out


def _direct(work: Path) -> dict[str, float]:
    """`unembedded_by_file` and `_classify` on their own, on the built index."""
    from app.storage.sqlite_store import SqliteStore

    db = work / "index.db"
    shutil.copyfile(work / "pristine.db", db)
    with SqliteStore(db) as store:
        started = time.perf_counter()
        batches = store.unembedded_by_file(batch_size=256)
        backlog_s = time.perf_counter() - started
        pipeline = _pipeline(store, work / "corpus")
        started = time.perf_counter()
        candidates = [c for c in pipeline._candidates() if hasattr(c, "path")]
        walk_s = time.perf_counter() - started
        prepare = getattr(pipeline, "_load_scan_rows", None)   # after 2026-10-25
        started = time.perf_counter()
        if prepare is not None:
            prepare()
        for candidate in candidates:
            pipeline._classify(candidate, hash_now=False)
        classify_s = time.perf_counter() - started
    return {"unembedded_by_file_s": round(backlog_s, 2),
            "backlog_batches": len(batches),
            "walk_s": round(walk_s, 2), "classify_s": round(classify_s, 2),
            "classify_us_per_file": round(classify_s / max(1, len(candidates)) * 1e6, 1),
            "files_walked": len(candidates)}


def measure(work: Path, repeat: int) -> dict[str, Any]:
    built = json.loads((work / "built.json").read_text(encoding="utf-8"))
    runs = [_one_run(work) for _ in range(repeat)]
    direct = [_direct(work) for _ in range(repeat)]
    median = {key: statistics.median(r[key] for r in runs)
              for key in runs[0] if all(key in r for r in runs)}
    median_direct = {key: statistics.median(d[key] for d in direct) for key in direct[0]}
    return {"conditions": {**built, "machine": platform.platform(),
                           "processor": platform.processor(), "cpus": os.cpu_count(),
                           "cache": "warm (the OS file cache is not emptied)",
                           "embedder": (f"REAL bge-small int8 on the processor, 4 threads, "
                                        f"from {_MODEL_CACHE}" if _MODEL_CACHE else
                                        "FAKE - made-up vectors, no model cost"),
                           "governor": "CPU check off (cpu_percent=0)"},
            "runs": runs, "median": median,
            "direct": direct, "median_direct": median_direct}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--dir", type=Path, required=True)
    b.add_argument("--files", type=int, default=40_000)
    b.add_argument("--indexed", type=int, default=10_000)
    b.add_argument("--backlog", type=int, default=200_000)
    m = sub.add_parser("measure")
    m.add_argument("--dir", type=Path, required=True)
    m.add_argument("--repeat", type=int, default=3)
    m.add_argument("--out", type=Path)
    m.add_argument("--model-cache", type=Path,
                   help="a folder holding bge-small (a copy - never the owner's own)")
    args = parser.parse_args(argv)
    for given in (args.dir, getattr(args, "model_cache", None)):
        if given is not None and "leashaindex" in str(given).lower():
            raise SystemExit("never the owner's data folder")
    global _MODEL_CACHE
    _MODEL_CACHE = getattr(args, "model_cache", None)
    os.environ.setdefault("LEASHA_LOG_LEVEL", "WARNING")
    try:
        from loguru import logger
        logger.remove()
        logger.add(sys.stderr, level="WARNING")
    except Exception:                               # noqa: BLE001 - quieter, not required
        pass
    if args.command == "build":
        result = build(args.dir, args.files, args.indexed, args.backlog)
    else:
        result = measure(args.dir, args.repeat)
        if args.out:
            args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
