r"""A GitHub run's pytest step ends when pytest has - `scripts/ci_pytest.py`.

Layer: L0

2026-10-25: Windows Run on 9e45ff3 printed its last failure at 13:27 and was cancelled by
the 90-minute ceiling at 13:55: pytest did not end after its session, and a process left
behind held the step's output open.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import psutil
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci_pytest.py"


def _module():
    spec = importlib.util.spec_from_file_location("ci_pytest", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ci_pytest = _module()


def test_the_finished_status_is_read_only_once_written(tmp_path):
    done = tmp_path / "done.txt"
    assert ci_pytest.finished_status(done) is None
    done.write_text("1", encoding="ascii")
    assert ci_pytest.finished_status(done) == 1


def test_the_plugin_writes_the_status_only_when_asked(tmp_path, monkeypatch):
    done = tmp_path / "done.txt"
    monkeypatch.delenv(ci_pytest.DONE_ENV, raising=False)
    ci_pytest.pytest_sessionfinish(None, 0)
    assert not done.exists()
    monkeypatch.setenv(ci_pytest.DONE_ENV, str(done))
    ci_pytest.pytest_sessionfinish(None, 1)
    assert done.read_text(encoding="ascii") == "1"


def _run(folder: Path, grace: str = "60") -> tuple[subprocess.CompletedProcess, float]:
    began = time.monotonic()
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--", "-q", "-p", "no:cacheprovider", "."],
        cwd=folder, capture_output=True, text=True, timeout=90,
        env=dict(os.environ, LEASHA_CI_PYTEST_GRACE=grace))
    return done, time.monotonic() - began


@pytest.mark.slow
def test_the_step_ends_with_pytest_though_a_process_it_left_holds_the_output(tmp_path):
    """A process left behind sleeps for a minute with pytest's output open; the wrapper
    still returns pytest's own exit code within seconds, with pytest's output copied."""
    marker = tmp_path / "left.pid"
    (tmp_path / "test_leaves_one.py").write_text(textwrap.dedent(f"""
        import subprocess, sys
        def test_leaves_a_process_behind():
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            open({str(marker)!r}, "w").write(str(child.pid))
    """), encoding="utf-8")
    done, took = _run(tmp_path)
    try:
        assert done.returncode == 0, done.stdout + done.stderr
        assert "[100%]" in done.stdout, "pytest's output is copied to the step's log"
        assert took < 40, f"the wrapper waited {took:.0f} s for the process left behind"
    finally:
        if marker.exists():
            try:
                psutil.Process(int(marker.read_text())).kill()
            except psutil.NoSuchProcess:
                pass


@pytest.mark.slow
def test_a_pytest_that_does_not_end_after_its_session_is_ended_with_its_status(tmp_path):
    """The 9e45ff3 shape: the session finished red, then the process would not end."""
    (tmp_path / "conftest.py").write_text(textwrap.dedent("""
        import threading, time
        def pytest_unconfigure(config):
            threading.Thread(target=time.sleep, args=(600,)).start()   # not a daemon
    """), encoding="utf-8")
    (tmp_path / "test_red.py").write_text("def test_red():\n    assert False\n",
                                          encoding="utf-8")
    done, took = _run(tmp_path, grace="3")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "ending it" in done.stdout
    assert took < 40, f"took {took:.0f} s"
