r"""The nightly system loop - order 0m section 5a.

Layer: tooling, not app code - it drives the real CLI as a subprocess, the
same way a person would, rather than importing application internals.

    venv\Scripts\python.exe tools\nightly.py
    venv\Scripts\python.exe tools\nightly.py --quick   # skip the kill-resume check

**On the owner's machine - the target hardware**, per the work order's own
heading. This script runs the sequence; it does not invent the numbers a
real pass has not yet produced. `PERF_FLOORS` below is `None` for every
metric until one real run on the target machine fills it in - asserting an
unmeasured number would be exactly the guess this project's "measure, do
not assume" rule exists to stop. Until then this script *records* what it
measures (one line per run, appended to `logs/nightly.log`), which is
itself the first thing a floor needs.

Five stages, each best-effort and each recorded even when a later one
fails, so one broken stage does not hide whether the others were fine:

  1. Refresh the fixture corpus (`tests/fixtures/generate.py` - the same one
     the test suite uses; a genuinely GB-scale corpus is a separate, larger
     asset this script does not generate itself, matching the honest gap
     named in the work order note - see `docs/WORKORDER-202626270547-test-
     automation.md`'s dated note).
  2. A full index run via the CLI, timed.
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
PYTHON = ROOT / "venv" / "Scripts" / "python.exe"
LOG_PATH = ROOT / "logs" / "nightly.log"

#: Pinned floors, one real measurement each, before any of these can gate a
#: run. `None` means "not yet measured on the target machine" - see the
#: module docstring. Fill each in from `logs/nightly.log`'s own history
#: once a few real nights exist to read it from.
PERF_FLOORS: dict[str, Optional[float]] = {
    "index_files_per_second": None,
    "search_p95_ms": None,
    "recall_at_10": None,
}

ENV_TEMPLATE = """\
DATA_PATH={d}
VECTOR_PATH={d}/vectors
FTS_DB={d}/fts/knowledge.db
CACHE_PATH={d}/cache
MODEL_CACHE={d}/models
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


def _run_cli(args: list, *, env_file: Path, timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    """`cwd` stays the project root - `-m app.cli` needs `app/` importable,
    which an isolated temp `.env` directory does not have - and `--env`
    (`app/cli.py`'s own `_load`) is how every command points at a different
    `.env` without moving where it runs from."""
    return subprocess.run(
        [str(PYTHON), "-m", "app.cli", *args, "--env", str(env_file)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=timeout, check=False)


def stage_refresh_fixture() -> dict:
    """Stage 1. The small corpus `tests/fixtures/generate.py` builds - see
    the module docstring for why this is not yet the GB-scale one."""
    started = time.monotonic()
    try:
        sys.path.insert(0, str(ROOT))
        from tests.fixtures.generate import ensure_fixtures
        root = ensure_fixtures()
        return {"ok": True, "seconds": time.monotonic() - started, "path": str(root)}
    except Exception as exc:                              # noqa: BLE001
        return {"ok": False, "seconds": time.monotonic() - started, "error": str(exc)}


def stage_index_run(env_file: Path, fixture_root: Path) -> dict:
    """Stage 2. A full index run over the fixture corpus, timed."""
    started = time.monotonic()
    result = _run_cli(["index", str(fixture_root)], env_file=env_file, timeout=1800)
    elapsed = time.monotonic() - started
    return {
        "ok": result.returncode == 0, "seconds": elapsed, "returncode": result.returncode,
        "stderr_tail": result.stderr[-2000:] if result.returncode != 0 else "",
    }


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


def stage_kill_resume(env_file: Path, fixture_root: Path, *, kill_after_s: float = 3.0) -> dict:
    """Stage 4. Kill an index run mid-flight; a second run must resume, not
    restart - the cursor is the thing under test, not the wall clock."""
    started = time.monotonic()
    proc = subprocess.Popen(
        [str(PYTHON), "-m", "app.cli", "index", str(fixture_root), "--env", str(env_file)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(kill_after_s)
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)

    resumed = _run_cli(["index", str(fixture_root)], env_file=env_file, timeout=1800)
    elapsed = time.monotonic() - started
    return {"ok": resumed.returncode == 0, "seconds": elapsed, "returncode": resumed.returncode}


def _check_floor(name: str, value: Optional[float], *, higher_is_better: bool) -> Optional[str]:
    floor = PERF_FLOORS.get(name)
    if floor is None or value is None:
        return None
    breached = value < floor if higher_is_better else value > floor
    if breached:
        return f"{name} {value} breached the pinned floor {floor}"
    return None


def run(*, quick: bool = False) -> dict:
    with tempfile.TemporaryDirectory(prefix="leasha-nightly-") as tmp:
        root = Path(tmp)
        env_file = root / ".env"
        env_file.write_text(ENV_TEMPLATE.format(d=root.as_posix()), encoding="utf-8")

        report: dict[str, Any] = {"started_at": datetime.now(timezone.utc).isoformat()}
        fixture = stage_refresh_fixture()
        report["fixture"] = fixture
        if not fixture["ok"]:
            return _finish(report, ok=False)

        fixture_root = Path(fixture["path"])
        report["index"] = stage_index_run(env_file, fixture_root)
        report["evaluate"] = stage_evaluate(env_file)
        if not quick:
            report["kill_resume"] = stage_kill_resume(env_file, fixture_root)

        breaches = []
        if report["index"]["ok"] and fixture_root.exists():
            file_count = sum(1 for _ in fixture_root.rglob("*") if _.is_file())
            rate = file_count / max(report["index"]["seconds"], 0.001)
            report["index"]["files_per_second"] = rate
            msg = _check_floor("index_files_per_second", rate, higher_is_better=True)
            if msg:
                breaches.append(msg)
        recall = report.get("evaluate", {}).get("recall")
        msg = _check_floor("recall_at_10", recall, higher_is_better=True)
        if msg:
            breaches.append(msg)
        report["breaches"] = breaches

        ok = (
            report["fixture"]["ok"] and report["index"]["ok"] and report["evaluate"]["ok"]
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
    line = (
        f"{report['finished_at']} {status} "
        f"index={index_s if index_s is None else round(index_s, 1)}s "
        f"recall={recall} breaches={len(report.get('breaches', []))}\n"
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
    args = parser.parse_args(argv)

    if not PYTHON.exists():
        print("Leasha is not installed yet. Run run-install.cmd first.")
        return 1

    report = run(quick=args.quick)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
