"""Layer 0 acceptance tests.

These are the four criteria from BUILD_SPEC_V2.md, verbatim:

  1. `python -m app.cli stats` runs, prints config, exits 0.
  2. Corrupting a path in .env produces a readable AppError, not a traceback.
  3. Launching the app twice: the second instance exits with the mutex message.
  4. A deliberately raised exception in a worker arrives as an AppError with a fix.

Layer 0 is not done until all four pass.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.core.errors import AppErrorException, guard
from app.core.logging import setup_logging
from app.core.single_instance import SingleInstance


def run_cli(project_root: Path, *args: str, env_file: Path | None = None,
            timeout: float = 60.0) -> subprocess.CompletedProcess:
    command = [sys.executable, "-m", "app.cli"]
    if env_file is not None:
        command += ["--env", str(env_file)]
    command += list(args)
    return subprocess.run(
        command, cwd=str(project_root), capture_output=True,
        text=True, timeout=timeout,
    )


# --- 1 ----------------------------------------------------------------------

def test_acceptance_1_stats_runs_and_exits_zero(project_root: Path, temp_env: Path) -> None:
    result = run_cli(project_root, "stats", env_file=temp_env)
    assert result.returncode == 0, result.stderr
    assert "data_path" in result.stdout
    assert "embed_model" in result.stdout


def test_acceptance_1_stats_json_is_valid(project_root: Path, temp_env: Path) -> None:
    result = run_cli(project_root, "--json", "stats", env_file=temp_env)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["settings"]["embed_dim"] == 384
    assert payload["version"]["version"]


# --- 2 ----------------------------------------------------------------------

def test_acceptance_2_bad_config_is_readable_not_a_traceback(
    project_root: Path, temp_env: Path, tmp_path: Path
) -> None:
    blocker = tmp_path / "not_a_directory"
    blocker.write_text("x", encoding="utf-8")

    text = "\n".join(
        f"DATA_PATH={blocker.as_posix()}" if line.startswith("DATA_PATH=") else line
        for line in temp_env.read_text(encoding="utf-8").splitlines()
    )
    temp_env.write_text(text, encoding="utf-8")

    result = run_cli(project_root, "stats", env_file=temp_env)

    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "ERR_CONFIG_INVALID" in combined
    assert "DATA_PATH" in combined
    assert "FIX:" in combined
    # The whole point: no raw traceback reaches the user.
    assert "Traceback (most recent call last)" not in combined


def test_acceptance_2_missing_env_is_readable(project_root: Path, tmp_path: Path) -> None:
    result = run_cli(project_root, "stats", env_file=tmp_path / "absent.env")
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "ERR_CONFIG_MISSING" in combined
    assert "Traceback (most recent call last)" not in combined


# --- 3 ----------------------------------------------------------------------

def test_acceptance_3_second_instance_refuses_to_start(
    project_root: Path, temp_env: Path
) -> None:
    """Two copies cannot share one index, so the second must refuse."""
    first = subprocess.Popen(
        [sys.executable, "-m", "app.cli", "--env", str(temp_env), "lock", "--hold", "8"],
        cwd=str(project_root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        # Wait for the first process to confirm it holds the lock.
        deadline = time.time() + 30
        line = ""
        while time.time() < deadline:
            line = first.stdout.readline() if first.stdout else ""
            if "LOCK ACQUIRED" in line:
                break
            if first.poll() is not None:
                pytest.fail(f"first instance exited early: {first.stderr.read() if first.stderr else ''}")
        assert "LOCK ACQUIRED" in line, "first instance never acquired the lock"

        second = run_cli(project_root, "lock", "--hold", "1", env_file=temp_env, timeout=30)

        assert second.returncode == 1, "the second instance should have refused to start"
        combined = second.stdout + second.stderr
        assert "ERR_DB_LOCKED" in combined
        assert "Another copy" in combined
        assert "Traceback (most recent call last)" not in combined
    finally:
        first.kill()
        first.wait(timeout=10)


def test_lock_is_reusable_after_release(tmp_path: Path) -> None:
    """Releasing must actually release, or the app cannot restart."""
    first = SingleInstance(name="test-reacquire", lock_dir=tmp_path)
    first.acquire()
    first.release()

    second = SingleInstance(name="test-reacquire", lock_dir=tmp_path)
    second.acquire()
    assert second.acquired
    second.release()


def test_lock_rejects_a_second_holder_in_process(tmp_path: Path) -> None:
    with SingleInstance(name="test-contended", lock_dir=tmp_path):
        other = SingleInstance(name="test-contended", lock_dir=tmp_path)
        with pytest.raises(AppErrorException) as caught:
            other.acquire()
        assert caught.value.error.code == "ERR_DB_LOCKED"
        assert caught.value.error.suggestion


# --- 4 ----------------------------------------------------------------------

def test_acceptance_4_worker_exception_arrives_as_app_error_with_a_fix(
    tmp_path: Path,
) -> None:
    """A library blowing up inside a worker becomes a structured, fixable error."""
    setup_logging(tmp_path / "logs", force=True)

    def worker_that_explodes() -> None:
        with guard("index.pipeline", path="D:/docs/report.pdf"):
            raise MemoryError("simulated allocation failure inside a parser")

    with pytest.raises(AppErrorException) as caught:
        worker_that_explodes()

    err = caught.value.error
    assert err.code == "ERR_UNEXPECTED"
    assert err.component == "index.pipeline"
    assert err.suggestion, "an AppError without a fix breaks the contract"
    assert err.action_type
    assert "MemoryError" in (err.details or "")
    assert err.context.get("path") == "D:/docs/report.pdf"


def test_logging_writes_a_file_and_survives_double_setup(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    setup_logging(log_dir, force=True)
    setup_logging(log_dir)  # idempotent: must not duplicate sinks

    from app.core.logging import log_app_error
    from app.core.errors import make_error

    log_app_error(make_error("ERR_FILE_CORRUPT", "indexer.pdf", path="broken.pdf"))

    from loguru import logger
    logger.complete()

    written = list(log_dir.glob("app_*.log"))
    assert written, "no log file was created"
    content = written[0].read_text(encoding="utf-8")
    assert "ERR_FILE_CORRUPT" in content
    assert content.count("broken.pdf") == 1, "sink was registered twice"
