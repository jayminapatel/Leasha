r"""A native crash in a full run always leaves its stack in the run's log.

Layer: L0 - repo tooling.

2026-10-15: one part of `scripts/run_suite.py` died with 0xC0000005 and its log held
no stack. `faulthandler` is process-wide, and the tests of the app's own crash reporting
end by disabling it (`test_crash_reporting.py`, `test_crash_guard.py`), so every later
crash in that process was silent. `tests/conftest.py`'s
`_native_crashes_are_always_reported` puts it back after every test; this proves it the
only honest way - a real access violation, in a process of its own, after a test that
switched the handler off.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.slow
def test_a_crash_after_a_test_that_disabled_faulthandler_still_prints_its_stack(tmp_path):
    (tmp_path / "test_a_disables.py").write_text(
        "import faulthandler\n\n"
        "def test_switches_it_off():\n"
        "    faulthandler.disable()\n", encoding="utf-8")
    (tmp_path / "test_b_crashes.py").write_text(
        "import faulthandler\n\n"
        "def test_dies_natively():\n"
        "    faulthandler._sigsegv()\n", encoding="utf-8")
    crash_log = tmp_path / "crash0.txt"
    env = dict(os.environ, LEASHA_SUITE_CRASH_LOG=str(crash_log))
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "tests.conftest", "-p", "no:cacheprovider",
         "-q", "--rootdir", str(tmp_path), str(tmp_path)],
        cwd=ROOT, capture_output=True, text=True, timeout=300, env=env)
    said = done.stdout + done.stderr
    assert done.returncode != 0, said
    stacks = crash_log.read_text(encoding="utf-8", errors="replace") if crash_log.exists() else ""
    assert "test_dies_natively" in stacks, (
        "the crash left no stack naming the test that died:\n" + stacks[-2000:] + said[-1000:])


def test_the_runner_prints_a_dead_parts_stacks_and_says_nothing_when_there_are_none(tmp_path):
    spec = importlib.util.spec_from_file_location("run_suite", ROOT / "scripts" / "run_suite.py")
    run_suite = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run_suite)
    crash = tmp_path / "crash1.txt"
    crash.write_text("Fatal Python error: Segmentation fault\n\nCurrent thread 0x1:\n"
                     '  File "x.py", line 3 in test_dies_natively\n', encoding="utf-8")
    shown = run_suite._crash_stacks(crash)
    assert "test_dies_natively" in shown and shown.startswith("      Fatal Python error")
    assert run_suite._crash_stacks(tmp_path / "none.txt") == ""
