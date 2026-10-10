"""A private pytest base temp directory for every run (2026-10-10).

Layer: L0 (test support only - nothing in `app` imports this)

**Why this exists.** Until 2026-10-10 `pyproject.toml` passed
`--basetemp=.pytest_tmp` to every run. pytest *deletes* a given basetemp at the
start of a run (`TempPathFactory.getbasetemp` calls `rm_rf` on it), so two
sessions testing in one checkout at the same time - the owner's VS Code Test
Explorer and a Claude session, or two Claude sessions - wiped each other's
temporary folders mid-run. What that looked like was never "the other run
deleted my files": it was a hang, a `PermissionError` on a folder that had just
been created, or a stray failure in a test that has nothing wrong with it
(HANDOFF.md, 2026-09-29: "two pytest runs at once break each other").
`scripts/run_suite.py` already gave each of its parts a `--basetemp` of its own;
a plain `pytest` did not.

**Why the basetemp still lives inside the project.** The long comment in
`pyproject.toml` has it: pytest's default temp root keeps a `pytest-current`
junction, and on Windows creating or resolving a junction needs a privilege a
normal account may not have; when it fails, `cleanup_dead_symlinks` raises
`PermissionError: [WinError 5]` from `pytest_sessionfinish` *after every test
passed*. A basetemp pytest is *given* never gets that junction. So this module
still gives pytest an explicit, dedicated directory - one per run, under the
project's `.pytest_tmp` - rather than falling back to pytest's own default.

The layout:

    <project>/.pytest_tmp/            the parent: never deleted by pytest, gitignored
    <project>/.pytest_tmp/run-<pid>/  this run's basetemp: pytest's to wipe and fill

The process id makes the name unique among runs alive at the same moment, which
is the only uniqueness that matters: two live processes never share a pid. A pid
is reused only after its process has died, and a dead run's folder is exactly
what `tidy_stale` is allowed to take.

An explicit `--basetemp` (on the command line, in `PYTEST_ADDOPTS`, or from
`run_suite.py`) always wins: `choose` returns None when one was given, and the
caller then leaves pytest's option alone.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

#: The dedicated parent folder, relative to the project root. Its name starts with
#: `.pytest_tmp` on purpose: `norecursedirs` in `pyproject.toml`, the walker's
#: excluded names (`app/index/walker.py`), `.gitignore` (`.pytest_tmp*/`) and
#: `tests/unit/test_docs_versioned.py`'s prefix prune all already know it.
PARENT_NAME = ".pytest_tmp"

#: A run's own folder is `run-<pid>`. Anything else in the parent - in particular
#: the files the old fixed basetemp left there - is not this module's to judge,
#: except the old layout's numbered `pytest-N` trees, which `tidy_stale` also
#: takes because they belong to no live run by construction.
RUN_PREFIX = "run-"


def run_dir_name(pid: int | None = None) -> str:
    """`run-<pid>` for the given process (this one by default)."""
    return f"{RUN_PREFIX}{os.getpid() if pid is None else pid}"


def choose(project_root: Path, given: str | os.PathLike | None,
           pid: int | None = None) -> Path | None:
    """The basetemp this run should use, or None to leave pytest's option alone.

    `given` is `config.option.basetemp` - set only when somebody passed
    `--basetemp` explicitly, because `pyproject.toml` no longer does. That
    explicit choice wins, always: `run_suite.py` relies on it to give each part
    its own folder, and a developer who types one means it.
    """
    if given:
        return None
    return Path(project_root) / PARENT_NAME / run_dir_name(pid)


def pid_alive(pid: int) -> bool:
    """True when a process with this id is running now.

    psutil is in the venv (the resource governor uses it) and is the one answer
    that is right on Windows, where `os.kill(pid, 0)` is not a probe but a call
    to `TerminateProcess`. Without psutil, on Windows, the answer is a
    conservative True - a folder that might belong to a live run is left alone;
    a stale folder costs disk, a deleted live one costs somebody's run.
    """
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        import psutil
    except Exception:                            # noqa: BLE001 - no psutil, fall back
        psutil = None
    if psutil is not None:
        try:
            return bool(psutil.pid_exists(pid))
        except Exception:                        # noqa: BLE001 - unanswerable: assume alive
            return True
    if sys.platform == "win32":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def _pid_of(name: str) -> int | None:
    if not name.startswith(RUN_PREFIX):
        return None
    try:
        return int(name[len(RUN_PREFIX):])
    except ValueError:
        return None


def tidy_stale(parent: Path, *, keep: int = 3, alive=pid_alive) -> list[Path]:
    """Remove the run folders of runs that are no longer running.

    The newest `keep` dead runs' folders are kept, by modification time: a run
    that failed leaves its folder behind on purpose (`finish` removes it only on
    a green run), so the failed tests' temporary files can still be looked at -
    the same promise `tmp_path_retention_count = 3` makes in `pyproject.toml`.
    A folder whose process is alive is never touched, whatever its age.

    Best effort, every step: a folder Windows still holds open (an antivirus
    scan, an Explorer window, a leaked handle in the dead run) is skipped, not
    raised - this runs before the first test and must never be why a run fails.
    Returns the folders it removed.
    """
    try:
        entries = list(Path(parent).iterdir())
    except OSError:
        return []
    dead: list[tuple[float, Path]] = []
    for entry in entries:
        pid = _pid_of(entry.name)
        if pid is None:
            continue
        try:
            if not entry.is_dir():
                continue
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if alive(pid):
            continue
        dead.append((mtime, entry))
    dead.sort(key=lambda pair: pair[0], reverse=True)
    removed: list[Path] = []
    for _mtime, entry in dead[max(keep, 0):]:
        shutil.rmtree(entry, ignore_errors=True)
        if not entry.exists():
            removed.append(entry)
    return removed


def finish(run_dir: Path, *, passed: bool) -> bool:
    """At the end of a run: remove this run's folder when every test passed.

    pytest itself removes a basetemp after a green run only when it chose the
    folder (`pytest_sessionfinish`: `_given_basetemp is None`). This run's folder
    is one we gave it, so pytest leaves it - and without this, every green run
    would leave a `run-<pid>` behind for `tidy_stale` to find later. A red run
    keeps its folder for inspection. Best effort; returns whether it is gone.
    """
    if not passed:
        return False
    shutil.rmtree(run_dir, ignore_errors=True)
    return not Path(run_dir).exists()
