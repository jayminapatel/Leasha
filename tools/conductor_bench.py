r"""The conductor, measured: the same photos indexed with it on and off.

Layer: tooling, not app code.

    venv\Scripts\python.exe tools\conductor_bench.py --photos D:\Data\_Media\PhotosMaster --count 60
    venv\Scripts\python.exe tools\conductor_bench.py --docs D:\Data\Documents --doc-count 6 --out C:\scratch\conductor.json

Work order `indexing-cockpit-and-conductor` 5e: "measured before and after on
the same corpus and machine: per-lane busy time and wall time of the run's
end. A change that does not save is reverted and said so."

**What it does.** Copies a sample of photos (evenly spaced through the folder,
so years and cameras are mixed) and a few documents into a throwaway folder,
copies the models the run needs out of the person's model folder, and indexes
the sample twice with the real pipeline and the real models: once with the
conductor (`PipelineConfig.conductor` True - the budget, the face pool, the
run's end side by side) and once without (every model unmetered, the passes
one after another, as before 2026-10-14). **Each run is its own process**, so
neither inherits a loaded model, a warm decode or an OCR helper from the other,
and each pays the same model loads.

**Read-only against the person's data.** The photos, documents and models are
only read (copied); every index, log and state file is written inside the
throwaway folder, which is deleted afterwards unless `--keep`. The person's
`.env` is read for their model choices and devices only.

**Conditions travel with the numbers** (non-negotiable 9): the machine, the
priority (below normal, as an index run), whether another Leasha index run was
going at the time, the processor load before each run, the sample, and each
model's device are all in the report.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".tif", ".tiff", ".webp", ".bmp"}
DOC_EXTS = {".pdf", ".docx", ".txt", ".md", ".xlsx", ".pptx", ".rtf", ".odt"}
#: Model folders the run reads, by a piece of their name in the model cache.
MODEL_PARTS = ("bge-small-en-v1.5", "clip-ViT-B-32", "Florence-2")
#: Phases that belong to the run's end: everything after reading and the
#: meaning catch-up.
TAIL_PHASES = ("media", "photo_tags", "picture_text", "doc_cards", "tidying",
               "vector_index", "word_index")


def _below_normal() -> str:
    try:
        import psutil

        process = psutil.Process()
        if os.name == "nt":
            process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            process.nice(10)
        return "below normal"
    except Exception as exc:                      # noqa: BLE001 - said in the report
        return f"unchanged ({exc})"


def other_index_runs() -> list[dict[str, Any]]:
    """Leasha processes running besides this one, and what they are."""
    found = []
    try:
        import psutil

        me = os.getpid()
        parent = os.getppid()
        for process in psutil.process_iter(["pid", "name", "cmdline"]):
            if process.info["pid"] in (me, parent):
                continue
            cmd = " ".join(process.info.get("cmdline") or [])
            if "python" not in (process.info.get("name") or "").lower():
                continue
            if "conductor_bench" in cmd or "pytest" in cmd:
                continue
            if "app.cli" in cmd or "app.main" in cmd or "main.py" in cmd or "leasha" in cmd.lower():
                found.append({"pid": process.info["pid"], "cmd": cmd[:160],
                              "cpu_percent": process.cpu_percent(interval=0.5)})
    except Exception as exc:                      # noqa: BLE001
        found.append({"error": str(exc)})
    return found


def pytest_running() -> int:
    """Test runs going on this machine (another worktree's suite loads it)."""
    try:
        import psutil

        return sum(1 for p in psutil.process_iter(["cmdline"])
                   if "pytest" in " ".join(p.info.get("cmdline") or []))
    except Exception:                             # noqa: BLE001
        return -1


def _sample(folder: Path, exts: set[str], count: int) -> list[Path]:
    files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in exts)
    if count <= 0 or not files:
        return []
    if len(files) <= count:
        return files
    step = len(files) / count
    return [files[int(i * step)] for i in range(count)]


def _copy_sample(photos: list[Path], docs: list[Path], corpus: Path) -> None:
    for kind, items in (("photos", photos), ("docs", docs)):
        target = corpus / kind
        target.mkdir(parents=True, exist_ok=True)
        for n, source in enumerate(items):
            dest = target / f"{n:03d}_{source.name}"
            if not dest.exists():
                shutil.copy2(source, dest)
                # An hour old, so no run takes it for a file still being written.
                stamp = time.time() - 3600
                os.utime(dest, (stamp, stamp))


def _copy_models(cache: Path, target: Path) -> list[str]:
    copied = []
    target.mkdir(parents=True, exist_ok=True)
    for entry in sorted(cache.iterdir()):
        if entry.is_dir() and any(part in entry.name for part in MODEL_PARTS):
            dest = target / entry.name
            if not dest.exists():
                shutil.copytree(entry, dest)
            copied.append(entry.name)
    return copied


def _phase_times(stats: Any, started_wall: float, ended_wall: float) -> dict[str, Any]:
    entries = [e for e in stats.activity.since(0) if e.kind == "phase"]
    timeline = [(float(e.at), str(e.text)) for e in entries]
    seconds: dict[str, float] = {}
    for (at, phase), following in zip(timeline, timeline[1:] + [(ended_wall, "")]):
        seconds[phase] = seconds.get(phase, 0.0) + max(0.0, following[0] - at)
    tail_start = next((at for at, phase in timeline if phase in TAIL_PHASES), ended_wall)
    reading_start = next((at for at, phase in timeline if phase in ("reading", "scanning")),
                         started_wall)
    return {
        "phases_s": {k: round(v, 1) for k, v in seconds.items()},
        "reading_and_meaning_s": round(tail_start - reading_start, 1),
        "run_end_s": round(ended_wall - tail_start, 1),
        "start_s": round(reading_start - started_wall, 1),
    }


def run_one(mode: str, work: Path, owner_env: Path) -> dict[str, Any]:
    """Index the sample once, in this process. `mode` "on" or "off"."""
    priority = _below_normal()
    import psutil

    from app.core.config import _read_env_file, load_settings
    from app.core.logging import setup_logging
    from app.index.clip_embedder import ClipImageEmbedder
    from app.index.embedder import Embedder
    from app.index.phash import default_phash_computer
    from app.index.pipeline import Pipeline
    from app.index.pipeline_bench import _LOCATION_KEYS, _write_throwaway_env
    from app.index.resolve import resolve_for_run
    from app.index.run_setup import build_pipeline_config
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import ImageVectorStore, VectorStore

    raw = dict(_read_env_file(owner_env))
    owner = load_settings(owner_env, create_dirs=False, check_writable=False)
    extra = {k: v for k, v in raw.items() if k not in _LOCATION_KEYS}
    extra.update({
        "PEOPLE_RECOGNITION_ENABLED": "true", "INDEX_OCR_PASS": "with-run",
        "INDEX_OCR_MODE": "both", "DOC_CARDS_ENABLED": "false",
        "CAPTION_TRICKLE_ENABLED": "false", "INDEX_LOW_PRIORITY": "true",
    })
    run_dir = work / f"run-{mode}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    env = _write_throwaway_env(run_dir, model_cache=work / "models",
                               model_name=owner.embed_model, dim=owner.embed_dim,
                               extra=extra)
    # The models read their own settings (`florence_tagger._settings` and the
    # like) from the project's `.env`; here that is the throwaway one, so they
    # find the copied models and never the person's folders.
    import app.core.config as config_module

    config_module.find_env_file = (
        lambda explicit=None: Path(explicit) if explicit is not None else env)
    settings = load_settings(env)
    setup_logging(settings.log_path, console_level="CRITICAL", force=True)
    corpus = work / "corpus"
    load_before = psutil.cpu_percent(interval=1.0)
    with SqliteStore(settings.fts_db) as store, \
            VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors, \
            ImageVectorStore(settings.vector_path) as images:
        tuned = resolve_for_run(settings, store)
        config = build_pipeline_config(settings, [corpus], tuned=tuned)
        config.conductor = mode == "on"
        embedder = Embedder.from_settings(settings, threads=tuned.onnx_threads)
        began = time.perf_counter()
        embedder.warm_up()
        load_s = time.perf_counter() - began
        pipeline = Pipeline(store, vectors, embedder, config,
                            image_embedder=ClipImageEmbedder.from_settings(settings),
                            image_vectors=images, phash_computer=default_phash_computer())
        if mode == "off":
            # Metering only: slots enough for everything, so nothing waits, and
            # the busy seconds per lane are counted the same way both runs.
            original = pipeline._start_conductor

            def metered() -> None:
                original()
                from app.index.conductor import Conductor

                pipeline._conductor = Conductor(1_000_000)

            pipeline._start_conductor = metered
        started_wall = time.time()
        began = time.perf_counter()
        stats = pipeline.run()
        wall = time.perf_counter() - began
        ended_wall = time.time()
        busy = pipeline._conductor.busy_seconds()
        faces = store.conn.execute("SELECT count(*) FROM faces").fetchone()[0]
        scanned = store.conn.execute("SELECT count(*) FROM face_scans").fetchone()[0] \
            if store.conn.execute("SELECT 1 FROM sqlite_master WHERE name='face_scans'"
                                  ).fetchone() else None
        held = dict(store.conn.execute(
            "SELECT coalesce(skip_code, status), count(*) FROM files GROUP BY 1").fetchall())
    from app.extract import face_detect, florence_tagger

    return {
        "mode": mode,
        "conditions": {
            "priority": priority, "cpu_percent_before": load_before,
            "workers": config.worker_count(), "onnx_threads": tuned.onnx_threads,
            "meaning_device": str(getattr(getattr(embedder, "choice", None), "device", "")),
            "faces_device": "graphics card" if face_detect._engine_is_gpu else "processor",
            "faces_threads": face_detect.session_threads(),
            "florence_on_gpu": florence_tagger.on_gpu(),
            "conductor": stats.resolved.get("conductor"),
            "embed_model_load_s": round(load_s, 1),
        },
        "wall_s": round(wall, 1),
        **_phase_times(stats, started_wall, ended_wall),
        "model_busy_s": {k: round(v, 1) for k, v in busy.items()},
        "stages_s": {k: round(v, 1) for k, v in (stats.stages or {}).items()},
        "counts": {"indexed": stats.indexed, "chunks": stats.chunks, "faces": faces,
                   "face_scans": scanned, "rows": held,
                   "enrichment": dict(stats.enrichment_counts)},
        "bypassed": stats.resolved.get("conductor_bypassed", {}),
    }


def faces_one(device: str, pool: int, threads: int, photos: list[Path]) -> dict[str, Any]:
    """Faces alone, in this process: the face pack on `device` ("cpu" or
    "gpu"), `pool` photos at a time, `threads` intra-op threads per session.
    The first photo loads the pack and is timed apart. The master thread's ask
    of 2026-10-14: which device should faces use on this laptop?"""
    priority = _below_normal()
    from concurrent.futures import ThreadPoolExecutor

    from app.core.compute_profile import detect
    from app.extract import face_detect
    from app.index import backends

    profile = detect()
    face_detect._choice = lambda: backends.choose(profile, device)
    face_detect.configure_threads(threads)
    began = time.perf_counter()
    face_detect.detect_faces(photos[0])
    load_s = time.perf_counter() - began
    rest = photos[1:]
    found = []
    began = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, pool)) as workers:
        for faces in workers.map(face_detect.detect_faces, rest):
            found.append(len(faces))
    seconds = time.perf_counter() - began
    return {"device": "graphics card" if face_detect._engine_is_gpu else "processor",
            "asked": device, "pool": pool, "threads": face_detect.session_threads(),
            "photos": len(rest), "seconds": round(seconds, 1),
            "photos_per_s": round(len(rest) / seconds, 3) if seconds else None,
            "faces": sum(found), "first_photo_and_load_s": round(load_s, 1),
            "priority": priority}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--photos", default=r"D:\Data\_Media\PhotosMaster")
    parser.add_argument("--count", type=int, default=60)
    parser.add_argument("--docs", default="")
    parser.add_argument("--doc-count", type=int, default=6)
    parser.add_argument("--env", default=str(ROOT.parents[2] / ".env")
                        if (ROOT.parents[2] / ".env").exists() else str(ROOT / ".env"))
    parser.add_argument("--work", default="")
    parser.add_argument("--modes", default="off,on")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--out", default="")
    parser.add_argument("--one", default="", help=argparse.SUPPRESS)
    parser.add_argument("--faces", default="",
                        help="faces alone: configs like gpu:1:4,cpu:1:4,cpu:2:3,cpu:4:1 "
                             "(device:pool:threads), each in its own process")
    parser.add_argument("--faces-one", default="", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.faces_one:
        device, pool, threads = args.faces_one.split(":")
        photos = sorted((Path(args.work) / "corpus" / "photos").iterdir())
        print("REPORT " + json.dumps(faces_one(device, int(pool), int(threads), photos)))
        return 0
    if args.faces:
        work = Path(args.work)
        photos = _sample(Path(args.photos), PHOTO_EXTS, args.count)
        _copy_sample(photos, [], work / "corpus")
        results = []
        for config in args.faces.split(","):
            done = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), "--faces-one", config,
                 "--work", str(work)], capture_output=True, text=True, cwd=str(ROOT))
            line = next((ln for ln in done.stdout.splitlines() if ln.startswith("REPORT ")), "")
            report = json.loads(line[7:]) if line else {"config": config,
                                                        "error": done.stderr[-1500:]}
            report["other_leasha_processes"] = other_index_runs()
            report["pytest_processes"] = pytest_running()
            results.append(report)
            print(json.dumps(report), flush=True)
        if args.out:
            Path(args.out).write_text(json.dumps(results, indent=1), encoding="utf-8")
        return 0

    if args.one:
        report = run_one(args.one, Path(args.work), Path(args.env))
        print("REPORT " + json.dumps(report))
        return 0

    _below_normal()
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="leasha-conductor-"))
    work.mkdir(parents=True, exist_ok=True)
    from app.core.config import load_settings

    owner = load_settings(Path(args.env), create_dirs=False, check_writable=False)
    photos = _sample(Path(args.photos), PHOTO_EXTS, args.count)
    docs = _sample(Path(args.docs), DOC_EXTS, args.doc_count) if args.docs else []
    print(f"sample: {len(photos)} photos, {len(docs)} documents -> {work}", flush=True)
    _copy_sample(photos, docs, work / "corpus")
    models = _copy_models(Path(owner.model_cache), work / "models")
    print(f"models copied: {', '.join(models)}", flush=True)

    results = []
    for mode in [m.strip() for m in args.modes.split(",") if m.strip()]:
        others = other_index_runs()
        print(f"run {mode}: other Leasha processes {others}", flush=True)
        done = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--one", mode,
             "--work", str(work), "--env", args.env],
            capture_output=True, text=True, cwd=str(ROOT))
        line = next((ln for ln in done.stdout.splitlines() if ln.startswith("REPORT ")), "")
        if not line:
            print(done.stdout[-3000:], done.stderr[-3000:], sep="\n")
            return 1
        report = json.loads(line[len("REPORT "):])
        report["conditions"]["other_leasha_processes"] = others
        report["conditions"]["pytest_processes_after"] = pytest_running()
        results.append(report)
        print(json.dumps(report, indent=1), flush=True)

    summary = {"sample": {"photos": len(photos), "documents": len(docs),
                          "from": [args.photos, args.docs]},
               "machine": _machine(), "runs": results}
    if args.out:
        Path(args.out).write_text(json.dumps(summary, indent=1), encoding="utf-8")
    if not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    return 0


def _machine() -> dict[str, Any]:
    from app.core.compute_profile import cores_only

    profile = cores_only()
    return {"logical": profile.logical_processors, "physical": profile.physical_cores,
            "p_cores": profile.performance_cores, "e_cores": profile.efficiency_cores}


if __name__ == "__main__":
    raise SystemExit(main())
