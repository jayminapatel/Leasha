"""Every pytest run gets a basetemp of its own (2026-10-10).

Layer: L0

`tests/basetemp.py` and the `pytest_configure` hook in `tests/conftest.py`
replaced the fixed `--basetemp=.pytest_tmp` that let two concurrent runs in one
checkout wipe each other's temporary folders.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from tests import basetemp


def test_a_run_without_an_explicit_basetemp_gets_one_named_for_its_process(tmp_path) -> None:
    chosen = basetemp.choose(tmp_path, None, pid=4242)
    assert chosen == tmp_path / ".pytest_tmp" / "run-4242"


def test_two_live_processes_never_share_a_basetemp(tmp_path) -> None:
    assert basetemp.choose(tmp_path, None, pid=1) != basetemp.choose(tmp_path, None, pid=2)


def test_an_explicit_basetemp_wins(tmp_path) -> None:
    """`scripts/run_suite.py` passes its own `--basetemp` per part; that must stand."""
    assert basetemp.choose(tmp_path, str(tmp_path / "mine")) is None


def test_the_parent_is_one_every_exclusion_already_knows() -> None:
    """`norecursedirs`, the walker, `.gitignore` and the docs-version prune all match
    `.pytest_tmp` (or its prefix); a different name would be collected and indexed."""
    assert basetemp.PARENT_NAME == ".pytest_tmp"


def test_pyproject_no_longer_fixes_the_basetemp() -> None:
    """A fixed `--basetemp` in addopts would make every run look explicit, and the
    conftest would leave the shared folder in place - the bug this replaced."""
    root = Path(__file__).resolve().parents[2]
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert "--basetemp" not in config["tool"]["pytest"]["ini_options"]["addopts"]


def test_this_session_uses_its_own_run_folder(request, tmp_path_factory) -> None:
    """Either somebody passed `--basetemp` (run_suite does), or this very run is in
    `.pytest_tmp/run-<this pid>` - never the shared `.pytest_tmp` itself."""
    base = Path(tmp_path_factory.getbasetemp()).resolve()
    assert base.name != ".pytest_tmp"
    if request.config.invocation_params.args and any(
            str(arg).startswith("--basetemp") for arg in request.config.invocation_params.args):
        return
    if os.environ.get("PYTEST_ADDOPTS", "").find("--basetemp") >= 0:
        return
    assert base.name == f"run-{os.getpid()}"
    assert base.parent.name == ".pytest_tmp"


def _make_run(parent: Path, pid: int, mtime: float) -> Path:
    folder = parent / f"run-{pid}"
    (folder / "test_something0").mkdir(parents=True)
    (folder / "test_something0" / "file.txt").write_text("x", encoding="utf-8")
    os.utime(folder, (mtime, mtime))
    return folder


def test_tidy_removes_dead_runs_beyond_the_kept_few(tmp_path) -> None:
    dead = [_make_run(tmp_path, pid, 1_000_000 + pid) for pid in (11, 12, 13, 14, 15)]
    removed = basetemp.tidy_stale(tmp_path, keep=2, alive=lambda pid: False)
    # The two newest dead runs (15 and 14) are kept for inspection.
    assert sorted(removed) == sorted(dead[:3])
    assert dead[3].exists() and dead[4].exists()


def test_tidy_never_touches_a_live_run_however_old(tmp_path) -> None:
    live = _make_run(tmp_path, 21, 1.0)
    dead = _make_run(tmp_path, 22, 2.0)
    removed = basetemp.tidy_stale(tmp_path, keep=0, alive=lambda pid: pid == 21)
    assert removed == [dead]
    assert live.exists()


def test_tidy_leaves_anything_that_is_not_a_run_folder(tmp_path) -> None:
    other = tmp_path / "test_from_the_old_layout0"
    other.mkdir()
    odd = tmp_path / "run-notapid"
    odd.mkdir()
    (tmp_path / "run-31").write_text("a file, not a folder", encoding="utf-8")
    assert basetemp.tidy_stale(tmp_path, keep=0, alive=lambda pid: False) == []
    assert other.exists() and odd.exists()


def test_tidy_of_a_missing_parent_is_a_quiet_nothing(tmp_path) -> None:
    assert basetemp.tidy_stale(tmp_path / "absent", keep=0, alive=lambda pid: False) == []


def test_a_green_run_removes_its_folder_and_a_red_one_keeps_it(tmp_path) -> None:
    green = _make_run(tmp_path, 41, 1.0)
    red = _make_run(tmp_path, 42, 1.0)
    assert basetemp.finish(green, passed=True) is True
    assert not green.exists()
    assert basetemp.finish(red, passed=False) is False
    assert red.exists()


def test_this_process_is_alive_and_pid_zero_is_not() -> None:
    assert basetemp.pid_alive(os.getpid()) is True
    assert basetemp.pid_alive(0) is False
    assert basetemp.pid_alive(-5) is False
