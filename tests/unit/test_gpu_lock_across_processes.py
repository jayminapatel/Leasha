r"""The graphics-card gate and the "driver failed" latch reach every process.

Layer: L0

Order 1h section 1 (2026-10-10). Since 2026-10-09 card work runs in three
processes - the indexer, the OCR helper and the vision host - and the gate and the
latch were per process. These tests start real child processes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.core import gpu_serialize
from app.core.osbridge.filelock import try_lock, unlock

ROOT = Path(__file__).resolve().parents[2]


def _child(code: str, env: dict[str, str]) -> subprocess.Popen:
    script = f"import sys; sys.path.insert(0, {str(ROOT)!r})\n" + code
    return subprocess.Popen([sys.executable, "-c", script], env=env, cwd=str(ROOT))


@pytest.fixture
def shared(tmp_path, monkeypatch):
    """One lock folder and one session, as the window and its children have."""
    monkeypatch.setenv(gpu_serialize.DIR_ENV, str(tmp_path))
    monkeypatch.setenv(gpu_serialize.SESSION_ENV, "test-session")
    gpu_serialize._reset_for_tests()
    yield tmp_path, dict(os.environ)
    gpu_serialize._reset_for_tests()


_HOLD_IN_TURNS = """
import time
from app.core.gpu_serialize import gpu_exclusive
with open({out!r}, "w") as out:
    for _ in range(25):
        with gpu_exclusive(True):
            start = time.perf_counter()
            time.sleep(0.01)
            out.write(f"{{start}} {{time.perf_counter()}}\\n")
"""


def test_two_processes_never_hold_the_card_at_once(shared) -> None:
    folder, env = shared
    outs = [folder / "a.txt", folder / "b.txt"]
    children = [_child(_HOLD_IN_TURNS.format(out=str(out)), env) for out in outs]
    for child in children:
        assert child.wait(timeout=120) == 0
    spans = sorted(tuple(map(float, line.split()))
                   for out in outs for line in out.read_text().splitlines())
    assert len(spans) == 50
    for (_s1, end), (start, _e2) in zip(spans, spans[1:]):
        assert start >= end, "two processes were inside the gate at the same time"


def test_a_holder_that_is_killed_gives_the_card_back(shared) -> None:
    folder, env = shared
    flag = folder / "held"
    child = _child(
        "import time, pathlib\n"
        "from app.core.gpu_serialize import gpu_exclusive\n"
        "with gpu_exclusive(True):\n"
        f"    pathlib.Path({str(flag)!r}).write_text('yes')\n"
        "    time.sleep(120)\n", env)
    try:
        deadline = time.monotonic() + 60
        while not flag.exists():
            assert time.monotonic() < deadline, "the child never took the gate"
            time.sleep(0.05)
        with open(folder / "leasha-gpu.lock", "a+b") as handle:
            assert not try_lock(handle), "the child holds it"
            child.kill()
            child.wait(timeout=30)
            deadline = time.monotonic() + 10
            while not try_lock(handle):
                assert time.monotonic() < deadline, "a dead holder kept the card"
                time.sleep(0.05)
            unlock(handle)
    finally:
        if child.poll() is None:
            child.kill()


def test_a_driver_fault_in_one_process_moves_the_others_to_the_processor(shared) -> None:
    _folder, env = shared
    child = _child("from app.core.gpu_serialize import mark_gpu_unreliable\n"
                   "mark_gpu_unreliable('887A0020 seen by the OCR helper')\n", env)
    assert child.wait(timeout=60) == 0
    assert gpu_serialize.gpu_unreliable() == "887A0020 seen by the OCR helper"


def test_another_session_does_not_inherit_the_latch(shared, monkeypatch) -> None:
    _folder, env = shared
    child = _child("from app.core.gpu_serialize import mark_gpu_unreliable\n"
                   "mark_gpu_unreliable('887A0020')\n",
                   {**env, gpu_serialize.SESSION_ENV: "an-earlier-session"})
    assert child.wait(timeout=60) == 0
    assert gpu_serialize.gpu_unreliable() == "", \
        "a fault in an earlier run must not keep this one off the card"


def test_a_holder_that_never_lets_go_is_waited_for_only_so_long(shared, monkeypatch) -> None:
    folder, _env = shared
    monkeypatch.setattr(gpu_serialize, "MACHINE_WAIT_S", 0.3)
    with open(folder / "leasha-gpu.lock", "a+b") as other:
        assert try_lock(other)
        started = time.monotonic()
        with gpu_serialize.gpu_exclusive(True):
            waited = time.monotonic() - started
        unlock(other)
    assert 0.25 <= waited < 5, "it waited, then went on rather than hanging"


def test_the_processor_path_takes_no_lock_file(shared) -> None:
    folder, _env = shared
    with gpu_serialize.gpu_exclusive(False):
        pass
    assert not (folder / "leasha-gpu.lock").exists()
