"""Timing git history search, so the design is chosen on numbers.

Layer: L4 (diagnostic only - **never reachable from a search**)

This is not a feature. It is the measurement that `WORKORDER-git-search-backend.md`
§14 makes phase 2 conditional on, and it exists to produce four numbers:
elapsed at 1k, 10k and 50k commits, and peak memory.

**Why the numbers decide the architecture.** Searching a repository's full
history is O(commits x changed files). This application's contract is a p95
under 300ms warm, and its first non-negotiable is that no unbounded work sits
in the search hot path. There are only two outcomes and they lead to completely
different products:

* Fast enough to be interactive - history becomes a mode of the search box.
* Not fast enough, which is the likely answer - a separate, explicitly slow,
  cancellable job with a progress bar, on its own button, never on Enter.

Building the UI before knowing which is how a 300ms budget is lost by accident.

**Shelling out, deliberately.** `git log -S` and `git grep` are the two
commands worth timing, and both are already tuned far beyond anything worth
reimplementing. A library wrapper around the same subprocess would add a
dependency and remove the control that matters here - streaming, a hard
timeout, and killing the process on cancellation. If phase 2 ever traverses
commits and diffs as objects rather than grepping, that is when a library earns
its place, and this measurement is what says whether it will.

**The seam is `runner`.** Every subprocess goes through one callable, so the
whole module is tested with invented output on a machine with no repository -
the same pattern used for COM, Qt and the resource probe.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.logging import logger

__all__ = [
    "DEFAULT_DEPTHS",
    "DEFAULT_TIMEOUT_S",
    "GitResult",
    "Measurement",
    "count_commits",
    "git_version",
    "is_repository",
    "measure",
    "search_history",
]

_log = logger.bind(component="search.gitsearch")

#: Commit depths to time at. From §14 - the three points that show whether the
#: cost is flat, linear or worse, which is the whole question.
DEFAULT_DEPTHS = (1_000, 10_000, 50_000)

#: A measurement that has not finished in this long has answered the question.
#:
#: **Generous on purpose.** The point is to record how slow it is, not to
#: decide in advance that slow is a failure - a run that takes four minutes is
#: a *result*, and cutting it off at thirty seconds would throw away the very
#: number the design depends on.
DEFAULT_TIMEOUT_S = 600.0

#: What a `runner` must look like. Returns (exit code, stdout, stderr).
Runner = Callable[[Sequence[str], Path | None, float], "tuple[int, str, str]"]


def _run(args: Sequence[str], cwd: Path | None,
         timeout: float) -> tuple[int, str, str]:
    """The real subprocess. The only part of this module that is not testable."""
    try:
        completed = subprocess.run(
            list(args), cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout:.0f}s"
    except OSError as exc:
        return 127, "", str(exc)
    return completed.returncode, completed.stdout, completed.stderr


@dataclass(frozen=True)
class GitResult:
    """One git invocation: what it cost as much as what it said."""

    ok: bool
    lines: tuple[str, ...] = ()
    elapsed_s: float = 0.0
    exit_code: int = 0
    error: str = ""
    #: The command, so a surprising number can be reproduced by hand. This is a
    #: measurement, and a measurement without its conditions is not one.
    command: tuple[str, ...] = ()

    @property
    def count(self) -> int:
        return len(self.lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "matches": self.count,
            "elapsed_s": round(self.elapsed_s, 3),
            "exit_code": self.exit_code, "error": self.error,
            "command": " ".join(self.command),
        }


@dataclass
class Measurement:
    """The table §14 asks for, and the conditions it was taken under."""

    repo: str = ""
    pattern: str = ""
    commits_total: int = 0
    git: str = ""
    rows: list[dict[str, Any]] = field(default_factory=list)
    peak_rss_mb: float | None = None
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo, "pattern": self.pattern,
            "commits_total": self.commits_total, "git": self.git,
            "peak_rss_mb": (round(self.peak_rss_mb, 1)
                            if self.peak_rss_mb is not None else None),
            "rows": self.rows, "notes": self.notes,
        }


def git_version(*, runner: Runner = _run) -> str | None:
    """The installed git, or None. Checked before anything else is attempted."""
    if shutil.which("git") is None:
        return None
    code, out, _err = runner(["git", "--version"], None, 10.0)
    return out.strip() if code == 0 and out.strip() else None


def is_repository(repo: Path, *, runner: Runner = _run) -> bool:
    code, out, _err = runner(
        ["git", "rev-parse", "--is-inside-work-tree"], Path(repo), 10.0)
    return code == 0 and out.strip() == "true"


def count_commits(repo: Path, *, rev: str = "HEAD",
                  runner: Runner = _run) -> int:
    """How deep the history actually is.

    Reported because a "50,000 commit" row measured against a repository with
    800 commits is not a measurement of anything, and that is exactly the sort
    of number that ends up in a decision document unqualified.
    """
    code, out, _err = runner(
        ["git", "rev-list", "--count", rev], Path(repo), 60.0)
    if code != 0:
        return 0
    try:
        return int(out.strip())
    except ValueError:
        return 0


def search_history(
    repo: Path,
    pattern: str,
    *,
    mode: str = "log-s",
    rev: str = "HEAD",
    depth: int | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    runner: Runner = _run,
) -> GitResult:
    """One history search, timed.

    `log-s` is `git log -S`, the "when did this string appear or disappear"
    search - the genuinely valuable half of the original request, and the
    expensive one, because git has to diff every commit.

    `grep` is `git grep`, which searches *one* revision's tree. Included
    because the difference between the two is the entire cost question: one is
    proportional to history, the other is not.
    """
    if mode == "grep":
        command = ["git", "grep", "-I", "-n", "--fixed-strings", pattern, rev]
    else:
        command = ["git", "log", f"-S{pattern}", "--oneline", "--no-color"]
        if depth:
            command.append(f"-n{depth}")
        command.append(rev)

    started = time.perf_counter()
    code, out, err = runner(command, Path(repo), timeout)
    elapsed = time.perf_counter() - started

    # **Exit code 1 means "no matches", not "failed".** Both commands use it,
    # and treating it as an error would report every unsuccessful search as a
    # broken tool - which is how a measurement turns into a bug report.
    ok = code in (0, 1)
    lines = tuple(line for line in out.splitlines() if line.strip())
    if not ok:
        _log.warning("git {} failed ({}): {}", mode, code, err.strip()[:200])

    return GitResult(ok=ok, lines=lines, elapsed_s=elapsed, exit_code=code,
                     error="" if ok else err.strip(), command=tuple(command))


def _peak_rss_mb() -> float | None:
    """Peak memory of *this* process, which is not what git costs.

    Recorded anyway and labelled as such: the subprocess's own peak is not
    visible to us without polling it, and a number nobody can attribute is
    worse than an absent one. See the note attached to every measurement.
    """
    try:
        import psutil

        return psutil.Process().memory_info().rss / 1_048_576
    except Exception:                    # psutil is optional
        return None


def measure(
    repo: Path,
    pattern: str,
    *,
    depths: Sequence[int] = DEFAULT_DEPTHS,
    rev: str = "HEAD",
    timeout: float = DEFAULT_TIMEOUT_S,
    runner: Runner = _run,
) -> Measurement:
    """`git log -S` at each depth, plus one `git grep` for comparison.

    The output is the input to a design decision, so every row carries the
    conditions that produced it - depth asked for, commits actually available,
    the command, and whether it completed.
    """
    found = Measurement(repo=str(repo), pattern=pattern)
    found.git = git_version(runner=runner) or ""
    if not found.git:
        found.notes.append(
            "git was not found on PATH, so nothing could be measured. "
            "Install git, or add it to PATH, and run this again.")
        return found

    if not is_repository(Path(repo), runner=runner):
        found.notes.append(f"{repo} is not a git repository.")
        return found

    found.commits_total = count_commits(Path(repo), rev=rev, runner=runner)

    for depth in depths:
        result = search_history(Path(repo), pattern, mode="log-s", rev=rev,
                                depth=depth, timeout=timeout, runner=runner)
        # **Say when the repository is shallower than the row claims.** A "50k
        # commits" figure taken against 800 commits is not a measurement, and
        # unqualified it is exactly the kind of number that ends up in a
        # decision document.
        reached = min(depth, found.commits_total) if found.commits_total else depth
        found.rows.append({
            "mode": "log-s",
            "depth_asked": depth,
            "commits_searched": reached,
            "representative": found.commits_total >= depth,
            **result.as_dict(),
        })

    grep = search_history(Path(repo), pattern, mode="grep", rev=rev,
                          timeout=timeout, runner=runner)
    found.rows.append({
        "mode": "grep", "depth_asked": 1, "commits_searched": 1,
        "representative": True, **grep.as_dict(),
    })

    found.peak_rss_mb = _peak_rss_mb()
    found.notes.append(
        "peak_rss_mb is this process, not git's - git runs as a subprocess and "
        "its own peak is not visible without polling it.")
    if found.commits_total and found.commits_total < max(depths):
        found.notes.append(
            f"This repository has {found.commits_total:,} commits, so rows "
            f"asking for more than that searched the whole history and are "
            f"not representative of a deeper one.")
    return found
