r"""The nightly system loop - order 0m section 5a.

Layer: tooling, not app code - it drives the real CLI as a subprocess, the
same way a person would, rather than importing application internals.

    venv\Scripts\python.exe tools\nightly.py
    venv\Scripts\python.exe tools\nightly.py --quick   # skip the kill-resume check

**On the owner's machine - the target hardware**, per the work order's own
heading. This script runs the sequence; it does not invent the numbers a
real pass has not yet produced. `PERF_FLOORS` below holds only numbers a real
run produced, each with what it was measured under written beside it; a
metric that could not be measured stays `None` rather than a guess. A floor is
a regression tripwire, not a target - it is set well below (or above, for a
latency) what was measured, so a busy machine does not trip it and a genuine
several-fold regression does.

Stages, each best-effort and each recorded even when a later one fails, so
one broken stage does not hide whether the others were fine:

  1. Build or refresh the fixture corpora: `tests/fixtures/generate.py` (the
     ~35 real-format files the test suite uses) and the *scale* corpus,
     `tests/fixtures/scale_corpus.py` (a few hundred unique text documents in
     a persistent folder, `logs/nightly-fixture/scale`, so an index run is
     throughput and not process start-up). Refreshing is free when the
     manifest already matches. A genuinely GB-scale corpus is still the
     owner's, not something this script invents - the count is a knob
     (`--scale-files`, `LEASHA_NIGHTLY_SCALE_FILES`).
  1b. Warm the embedding model into a *persistent* cache (`logs/nightly-models`,
     or `LEASHA_NIGHTLY_MODEL_CACHE`) so a first-ever 65MB download is never
     inside a timed stage - it was, when the cache lived in the temp dir.
  2. A full index run via the CLI, timed.
  2b. `tools/nightly_probe.py measure`: chunker rate, embed rate, the OCR
     ladder's rungs 0-1 rate and rung 2 (detection probe) time, and a search
     p95 over the index stage 2 just built.
  3. `leasha evaluate --builtin`, parsed for its recall number.
  4. A kill-resume spot check: start a second index run, kill it after a few
     seconds, run it again, and confirm the cursor advanced rather than
     restarting from zero (`--quick` skips this - it is the slowest stage).
  5. One line appended to `logs/nightly.log`, loud only on failure.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]


def _find_python() -> Path:
    """`LEASHA_PYTHON` first (a git worktree has no `venv/` of its own), then
    the checkout's own venv - the normal case, and the path `main()`'s "not
    installed yet" message is about."""
    override = os.environ.get("LEASHA_PYTHON")
    return Path(override) if override else ROOT / "venv" / "Scripts" / "python.exe"


PYTHON = _find_python()
LOG_PATH = ROOT / "logs" / "nightly.log"

#: Pinned floors - regression tripwires, not targets. `None` means "not
#: measured on the target machine", and never breaches.
#:
#: **How the margins were chosen.** Every floor sits 2.5x to 3x beyond the WORST
#: value ever observed for that metric (a rate is divided by ~3, a latency
#: multiplied by ~3) - so ordinary machine noise cannot trip it and a genuine
#: several-fold regression does. The machine is noisy and was noisy for every run:
#: the owner's machine, Windows 11, running a large index of its own, and (for the
#: 2026-09-20 runs) other people's test processes and a GUI journey on the same
#: cores. That makes every observation pessimistic, and the spread between
#: back-to-back runs is itself large (2x-4x). No floor is taken from a single
#: run; the worst of at least three is the anchor, and a floor is tightened only
#: from a quiet night's history in `logs/nightly.log`, never from a hunch.
#:
#: Run history (all PASS):
#:   2026-09-19  five runs on the small (~35 file) corpus, two full.
#:   2026-09-20  three FULL runs (with kill-resume) on the scale corpus - 300
#:               generated text files plus the ~35 real-format fixtures = 344
#:               files, 326 indexed - back-to-back, on a loaded machine.
#:
#:   index_files_per_second   2026-09-20: 1.62, 2.07, 1.02 (worst 1.02) -> 0.35.
#:       Wall clock of the whole `app.cli index` call over BOTH corpora divided
#:       by every file under them, so it includes process start and model load.
#:       (The floor on the small corpus alone was 0.2; the corpus changed, so
#:       the metric did, and this replaces it.)
#:   chunker_chunks_per_second   all eight runs 523.7 - 2251.8 (worst 523.7) -> 175.
#:       `chunk_text` over 120 synthetic 400-word documents, single thread,
#:       model-free.
#:   embed_chunks_per_second   all eight runs 6.9 - 11.4 (worst 6.9) -> 2.5.
#:       bge-small int8 on CPU, 48 chunks after an untimed warm-up batch.
#:   ladder_rung01_images_per_second   2026-09-20: 56.9, 37.9, 18.5 (worst 18.5) -> 6.0.
#:       The OCR ladder's free rungs (filename, then the 256px thumbnail
#:       histogram) over 60 generated images (1240x1754 pages and 1600x1200
#:       photos), model-free - decoding the image dominates it.
#:   ladder_probe_ms   2026-09-20: 761, 829, 1252 (worst 1252) -> 3500.
#:       Rung 2, the detection-only probe, per photo, on the graphics card via
#:       DirectML (the machine's real configuration). The module docstring of
#:       `ocr_ladder.py` says ~50-150 ms; on this integrated adapter it is
#:       several times that, which is a measurement, not a defect - the floor
#:       guards "the probe did not become several times slower again".
#:   search_p95_ms   all eight runs 171 - 396 (worst 396) -> 1000. 96 searches
#:       (8 queries x 12) over the index the run just built (now ~326 files,
#:       ~500 chunks), reranker OFF, result cache off, models pre-warmed. It is a
#:       small index, so this guards the pipeline's fixed cost, not scaling; a
#:       p95 over the owner's real index is a different (larger) number nobody
#:       has measured here.
#:   recall_at_10   0.7 in every run (deterministic - no noise margin needed);
#:       floored at 0.6, one built-in question's worth.
PERF_FLOORS: dict[str, Optional[float]] = {
    "index_files_per_second": 0.35,
    "chunker_chunks_per_second": 175.0,
    "embed_chunks_per_second": 2.5,
    "ladder_rung01_images_per_second": 6.0,
    "ladder_probe_ms": 3500.0,
    "search_p95_ms": 1000.0,
    "recall_at_10": 0.6,
}

ENV_TEMPLATE = """\
DATA_PATH={d}
VECTOR_PATH={d}/vectors
FTS_DB={d}/fts/knowledge.db
CACHE_PATH={d}/cache
MODEL_CACHE={m}
STATE_PATH={d}/state
PROJECT_PATH={d}
LOG_PATH={d}/logs

EMBED_MODEL=BAAI/bge-small-en-v1.5
EMBED_DIM=384
RERANK_MODEL=BAAI/bge-reranker-base
RERANK_ENABLED=true

OLLAMA_URL=http://127.0.0.1:11434
OLLAMA_MODEL=mistral

MIN_FREE_GB=1
REQUIRED_FREE_GB=1
"""


def _model_cache() -> Path:
    """One model cache shared by every night, so the model is downloaded once
    ever and never inside a timed stage."""
    override = os.environ.get("LEASHA_NIGHTLY_MODEL_CACHE")
    path = Path(override) if override else ROOT / "logs" / "nightly-models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run_cli(args: list, *, env_file: Path, timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    """`cwd` stays the project root - `-m app.cli` needs `app/` importable,
    which an isolated temp `.env` directory does not have - and `--env`
    (`app/cli.py`'s own `_load`) is how every command points at a different
    `.env` without moving where it runs from."""
    return subprocess.run(
        [str(PYTHON), "-m", "app.cli", *args, "--env", str(env_file)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=timeout, check=False)


DEFAULT_SCALE_FILES = 300


def _scale_files(requested: Optional[int]) -> int:
    if requested is not None:
        return max(1, int(requested))
    try:
        return max(1, int(os.environ.get("LEASHA_NIGHTLY_SCALE_FILES", DEFAULT_SCALE_FILES)))
    except ValueError:
        return DEFAULT_SCALE_FILES


def _scale_dir() -> Path:
    override = os.environ.get("LEASHA_NIGHTLY_FIXTURE_DIR")
    return (Path(override) if override else ROOT / "logs" / "nightly-fixture") / "scale"


def stage_refresh_fixture(scale_files: Optional[int] = None) -> dict:
    """Stage 1. The small real-format corpus `tests/fixtures/generate.py`
    builds, plus the scale corpus `tests/fixtures/scale_corpus.py` builds or
    refreshes in a persistent folder - see the module docstring."""
    started = time.monotonic()
    try:
        sys.path.insert(0, str(ROOT))
        from tests.fixtures.generate import ensure_fixtures
        from tests.fixtures.scale_corpus import ensure_scale_corpus
        root = ensure_fixtures()
        scale = ensure_scale_corpus(_scale_dir(), _scale_files(scale_files))
        return {"ok": True, "seconds": time.monotonic() - started, "path": str(root),
                "scale_path": str(scale["path"]), "scale_files": scale["files"],
                "scale_refreshed": scale["refreshed"]}
    except Exception as exc:                              # noqa: BLE001
        return {"ok": False, "seconds": time.monotonic() - started, "error": str(exc)}


def stage_index_run(env_file: Path, roots: list) -> dict:
    """Stage 2. A full index run over the fixture corpora, timed."""
    started = time.monotonic()
    result = _run_cli(["index", *[str(r) for r in roots]], env_file=env_file, timeout=3600)
    elapsed = time.monotonic() - started
    return {
        "ok": result.returncode == 0, "seconds": elapsed, "returncode": result.returncode,
        "stderr_tail": result.stderr[-2000:] if result.returncode != 0 else "",
    }


def stage_warm(env_file: Path) -> dict:
    """Stage 1b. Load the embedding model once, untimed by any later stage."""
    started = time.monotonic()
    result = subprocess.run(
        [str(PYTHON), str(ROOT / "tools" / "nightly_probe.py"), "warm", "--env", str(env_file)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=1800, check=False)
    return {"ok": result.returncode == 0, "seconds": time.monotonic() - started,
            "stderr_tail": result.stderr[-1000:] if result.returncode != 0 else ""}


def stage_probe(env_file: Path, fixture_root: Path) -> dict:
    """Stage 2b. Chunker rate, embed rate and search p95 - see
    `tools/nightly_probe.py`. A metric it could not measure comes back `null`
    with a note; it is recorded as such, never filled in."""
    started = time.monotonic()
    result = subprocess.run(
        [str(PYTHON), str(ROOT / "tools" / "nightly_probe.py"), "measure",
         "--env", str(env_file), "--fixture", str(fixture_root)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=1800, check=False)
    elapsed = time.monotonic() - started
    if result.returncode != 0:
        return {"ok": False, "seconds": elapsed, "stderr_tail": result.stderr[-1000:]}
    try:
        payload = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return {"ok": False, "seconds": elapsed, "error": "probe output did not parse"}
    payload.update(ok=True, seconds=elapsed)
    return payload


def stage_evaluate(env_file: Path) -> dict:
    """Stage 3. `evaluate --builtin`, parsed for its own reported recall.

    `Report.as_dict()` (`app/search/evaluate.py`) has no single "recall"
    field - it is split by whether a question carried a constraint, on
    purpose, because the two fail for different reasons. `overall` is the
    one number worth a floor; `topic_only`/`constrained` travel through too,
    for the log line a person reads by eye.
    """
    started = time.monotonic()
    result = _run_cli(["evaluate", "--builtin", "--json"], env_file=env_file, timeout=600)
    elapsed = time.monotonic() - started
    if result.returncode != 0:
        return {"ok": False, "seconds": elapsed, "returncode": result.returncode,
                "stderr_tail": result.stderr[-2000:]}
    # `--json` appends the JSON block after the human-readable report
    # (`cmd_evaluate` prints `report.lines()` unconditionally, then the JSON
    # - `app/cli.py`'s own `_evaluate_builtin`) rather than replacing it, so
    # this is never pure JSON on its own. The rendered text has no literal
    # `{` (`Report.lines()` builds plain padded strings, not braces), so the
    # first one marks where the JSON block starts.
    brace = result.stdout.find("{")
    if brace == -1:
        return {"ok": False, "seconds": elapsed, "error": "no JSON block in evaluate's output"}
    try:
        payload = json.loads(result.stdout[brace:])
    except json.JSONDecodeError:
        return {"ok": False, "seconds": elapsed, "error": "evaluate --json did not parse"}
    return {
        "ok": True, "seconds": elapsed,
        "recall": payload.get("overall"),
        "topic_only": payload.get("topic_only"),
        "constrained": payload.get("constrained"),
    }


def _indexed_count(env_file: Path) -> int:
    """`files` rows already INDEXED in the store `env_file` points at - read
    straight from SQLite, read-only, the authority (non-negotiable #6)."""
    import re
    import sqlite3

    match = re.search(r"^FTS_DB=(.+)$", env_file.read_text(encoding="utf-8"), re.M)
    if not match or not Path(match.group(1).strip()).exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{match.group(1).strip()}?mode=ro", uri=True, timeout=2)
        try:
            return int(conn.execute(
                "SELECT COUNT(*) FROM files WHERE status='INDEXED'").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def stage_kill_resume(env_file: Path, roots, *, reference_indexed: int,
                      wait_s: float = 240.0) -> dict:
    """Stage 4. Kill an index run **mid-flight**, then run it again: the second
    run must finish the job (same INDEXED count as the uninterrupted run in
    stage 2) - a crash costs seconds, not the run (non-negotiable #4).

    `env_file` must be a *fresh* store: killing a run over an index that is
    already complete tests nothing. The kill waits until the fresh store holds
    at least one INDEXED file and fewer than the reference count, i.e. it is
    genuinely part-way; `killed_at` records how far it had got."""
    started = time.monotonic()
    proc = subprocess.Popen(
        [str(PYTHON), "-m", "app.cli", "index", *[str(r) for r in _as_list(roots)],
         "--env", str(env_file)],
        cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    killed_at = 0
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline and proc.poll() is None:
        killed_at = _indexed_count(env_file)
        if killed_at > 0:
            break
        time.sleep(0.4)
    finished_before_kill = proc.poll() is not None
    if not finished_before_kill:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    resumed = _run_cli(["index", *[str(r) for r in _as_list(roots)]],
                       env_file=env_file, timeout=3600)
    after = _indexed_count(env_file)
    result = {
        "seconds": time.monotonic() - started, "returncode": resumed.returncode,
        "killed_at": killed_at, "indexed_after": after, "reference": reference_indexed,
        "finished_before_kill": finished_before_kill,
    }
    result["ok"] = (resumed.returncode == 0 and after >= reference_indexed > 0
                    and (finished_before_kill or 0 < killed_at))
    if not result["ok"]:
        result["error"] = ("the resumed run did not reach the uninterrupted run's INDEXED count"
                           if after < reference_indexed else "kill stage did not run mid-flight")
    return result


def _as_list(value) -> list:
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _check_floor(name: str, value: Optional[float], *, higher_is_better: bool) -> Optional[str]:
    floor = PERF_FLOORS.get(name)
    if floor is None or value is None:
        return None
    breached = value < floor if higher_is_better else value > floor
    if breached:
        return f"{name} {value} breached the pinned floor {floor}"
    return None


def run(*, quick: bool = False, scale_files: Optional[int] = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="leasha-nightly-") as tmp:
        root = Path(tmp)
        env_file = root / ".env"
        env_file.write_text(
            ENV_TEMPLATE.format(d=root.as_posix(), m=_model_cache().as_posix()),
            encoding="utf-8")

        report: dict[str, Any] = {"started_at": datetime.now(timezone.utc).isoformat()}
        fixture = stage_refresh_fixture(scale_files)
        report["fixture"] = fixture
        if not fixture["ok"]:
            return _finish(report, ok=False)

        fixture_root = Path(fixture["path"])
        index_roots = [fixture_root, Path(fixture["scale_path"])]
        report["warm"] = stage_warm(env_file)
        report["index"] = stage_index_run(env_file, index_roots)
        report["probe"] = stage_probe(env_file, fixture_root)
        report["evaluate"] = stage_evaluate(env_file)
        if not quick:
            fresh = root / "resume"
            fresh.mkdir()
            fresh_env = fresh / ".env"
            fresh_env.write_text(
                ENV_TEMPLATE.format(d=fresh.as_posix(), m=_model_cache().as_posix()),
                encoding="utf-8")
            report["kill_resume"] = stage_kill_resume(
                fresh_env, index_roots, reference_indexed=_indexed_count(env_file))

        breaches = []
        if report["index"]["ok"] and fixture_root.exists():
            file_count = sum(1 for r in index_roots for _ in r.rglob("*") if _.is_file())
            report["index"]["files"] = file_count
            rate = file_count / max(report["index"]["seconds"], 0.001)
            report["index"]["files_per_second"] = rate
            msg = _check_floor("index_files_per_second", rate, higher_is_better=True)
            if msg:
                breaches.append(msg)
        recall = report.get("evaluate", {}).get("recall")
        msg = _check_floor("recall_at_10", recall, higher_is_better=True)
        if msg:
            breaches.append(msg)
        probe = report.get("probe", {})
        for name, higher in (("chunker_chunks_per_second", True),
                             ("embed_chunks_per_second", True),
                             ("ladder_rung01_images_per_second", True),
                             ("ladder_probe_ms", False),
                             ("search_p95_ms", False)):
            msg = _check_floor(name, probe.get(name), higher_is_better=higher)
            if msg:
                breaches.append(msg)
        report["breaches"] = breaches

        ok = (
            report["fixture"]["ok"] and report["index"]["ok"] and report["evaluate"]["ok"]
            and report["probe"]["ok"]
            and (quick or report.get("kill_resume", {}).get("ok", False))
            and not breaches
        )
        return _finish(report, ok=ok)


def _finish(report: dict, *, ok: bool) -> dict:
    report["ok"] = ok
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    _append_log_line(report)
    return report


def _append_log_line(report: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    status = "PASS" if report["ok"] else "FAIL"
    recall = report.get("evaluate", {}).get("recall")
    index_s = report.get("index", {}).get("seconds")
    probe = report.get("probe", {})
    fps = report.get("index", {}).get("files_per_second")
    # The first three fields are what `doctor.check_nightly_status` and its
    # tests already read; the measurements ride after them.
    line = (
        f"{report['finished_at']} {status} "
        f"index={index_s if index_s is None else round(index_s, 1)}s "
        f"recall={recall} breaches={len(report.get('breaches', []))} "
        f"files_per_s={fps if fps is None else round(fps, 2)} "
        f"chunker_per_s={probe.get('chunker_chunks_per_second')} "
        f"embed_per_s={probe.get('embed_chunks_per_second')} "
        f"ladder01_per_s={probe.get('ladder_rung01_images_per_second')} "
        f"ladder_probe_ms={probe.get('ladder_probe_ms')} "
        f"search_p95_ms={probe.get('search_p95_ms')} "
        f"search_p50_ms={probe.get('search_p50_ms')}\n"
    )
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(line)
    # Loud only on failure - stdout stays quiet on a pass, which is what
    # makes a scheduled task's own output worth glancing at when it mails
    # one out.
    if not report["ok"]:
        print(line, end="")
        print(json.dumps(report, indent=2, default=str))


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--quick", action="store_true",
                        help="skip the kill-resume check - the slowest stage")
    parser.add_argument("--scale-files", type=int, default=None, metavar="N",
                        help=f"documents in the scale corpus (default {DEFAULT_SCALE_FILES}, "
                             "or LEASHA_NIGHTLY_SCALE_FILES)")
    parser.add_argument("--verbose", action="store_true",
                        help="print the full report even when the run passes")
    args = parser.parse_args(argv)

    if not PYTHON.exists():
        print("Leasha is not installed yet. Run run-install.cmd first.")
        return 1

    report = run(quick=args.quick, scale_files=args.scale_files)
    if args.verbose and report["ok"]:
        print(json.dumps(report, indent=2, default=str))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
