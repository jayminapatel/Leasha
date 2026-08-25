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
    "DEFAULT_ROW_LIMIT",
    "GitRow",
    "GitSearchResult",
    "SEARCH_TIMEOUT_S",
    "run_query",
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


# ---------------------------------------------------------------------------
# The search itself
#
# Everything above this line is the measurement that decided whether this could
# exist. Everything below is what it decided: a repository search that runs one
# git command, reads its output, and is bounded at both ends.
# ---------------------------------------------------------------------------

#: A row is capped so a pattern matching half the repository cannot fill memory
#: or the window. The number is "more than anybody reads, fewer than hurts".
DEFAULT_ROW_LIMIT = 2000

#: What a history search is allowed to take before it is cut off. Far shorter
#: than the measurement's ten minutes: this one has somebody waiting.
SEARCH_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class GitRow:
    """One hit. Which fields are filled depends on what was asked."""

    #: "content" for a line of a file, "commit" for a commit, "change" for a
    #: file's status in a commit.
    kind: str = "content"
    commit: str = ""
    date: str = ""
    author: str = ""
    subject: str = ""
    path: str = ""
    line_no: int = 0
    text: str = ""
    #: "A" added, "M" modified, "D" deleted, "R" renamed - from --name-status,
    #: and from the +/- of a patch.
    status: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind, "commit": self.commit, "date": self.date,
            "author": self.author, "subject": self.subject, "path": self.path,
            "line": self.line_no, "text": self.text, "status": self.status,
        }


@dataclass
class GitSearchResult:
    """What a repository search found, and what it was allowed to look at."""

    ok: bool = True
    rows: list[GitRow] = field(default_factory=list)
    elapsed_s: float = 0.0
    #: The sentence from `GitPlan.explain`. **Carried into the result, not left
    #: at the call site**: "no matches" means nothing until you know whether it
    #: looked at one commit or nine thousand, and the answer has to travel with
    #: the answer.
    explain: str = ""
    command: tuple[str, ...] = ()
    error: str = ""
    #: True when the row cap cut the output. Said out loud, because a truncated
    #: list that does not say so is a wrong answer.
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok, "matches": len(self.rows),
            "elapsed_s": round(self.elapsed_s, 3), "explain": self.explain,
            "command": " ".join(self.command), "error": self.error,
            "truncated": self.truncated,
            "rows": [row.as_dict() for row in self.rows],
        }


def _header(line: str) -> Optional[tuple[str, str, str, str]]:
    """A `%H\\t%ad\\t%an\\t%s` line, or None.

    Tab-separated because a commit subject may contain anything except a
    newline - a separator that can appear in the data is a parser that is right
    on your repository and wrong on somebody else's.
    """
    parts = line.split("\t")
    if len(parts) >= 4 and len(parts[0]) >= 7 and " " not in parts[0]:
        return parts[0], parts[1], parts[2], "\t".join(parts[3:])
    return None


def _read_grep(lines: Sequence[str], limit: int) -> list[GitRow]:
    """`[rev:]path:line:text`.

    **The revision prefix is present only when one was named**, and splitting
    on a fixed field count gets it wrong the other way round. Counting from the
    right instead: the last field is the text, the one before it the line
    number - and a path containing a colon then costs nothing.
    """
    rows: list[GitRow] = []
    for line in lines:
        if len(rows) >= limit:
            break
        head, _sep, text = line.partition(":")
        if not _sep:
            continue
        # `head` is now `path` or `rev:path`, and what follows may still start
        # with the line number.
        middle, _sep2, rest = text.partition(":")
        if middle.isdigit():
            rows.append(GitRow(kind="content", path=head,
                               line_no=int(middle), text=rest))
            continue
        # Two colons: the first was a revision.
        number, _sep3, body = rest.partition(":")
        if number.isdigit():
            rows.append(GitRow(kind="content", commit=head, path=middle,
                               line_no=int(number), text=body))
    return rows


def _read_log(lines: Sequence[str], limit: int) -> list[GitRow]:
    """One row per commit."""
    rows: list[GitRow] = []
    for line in lines:
        if len(rows) >= limit:
            break
        found = _header(line)
        if found is not None:
            commit, date, author, subject = found
            rows.append(GitRow(kind="commit", commit=commit, date=date,
                               author=author, subject=subject))
    return rows


def _read_name_status(lines: Sequence[str], limit: int) -> list[GitRow]:
    """A file's life: one row per change, carrying the commit it happened in."""
    rows: list[GitRow] = []
    commit = date = author = subject = ""
    for line in lines:
        if len(rows) >= limit:
            break
        found = _header(line)
        if found is not None:
            commit, date, author, subject = found
            continue
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0][:1] in "AMDRCT":
            # `R100  old  new` - the new name is the one worth showing, and the
            # old one is the whole point of tracking renames, so both go in.
            path = parts[-1]
            text = f"renamed from {parts[1]}" if parts[0].startswith("R") else ""
            rows.append(GitRow(kind="change", commit=commit, date=date,
                               author=author, subject=subject, path=path,
                               status=parts[0][:1], text=text))
    return rows


def _read_patch(lines: Sequence[str], limit: int, keep: str = "") -> list[GitRow]:
    """Changed lines, attributed to the commit and file they changed in.

    `keep` is "+", "-" or "": git has no switch for "only the deleted lines"
    (`--diff-filter` selects files, not lines), so it is done here - and it is
    done here rather than in the caller so that "removed only" cannot end up
    showing added lines under a heading that says removed.
    """
    rows: list[GitRow] = []
    commit = date = author = subject = ""
    path = ""
    for line in lines:
        if len(rows) >= limit:
            break
        found = _header(line)
        if found is not None:
            commit, date, author, subject = found
            path = ""
            continue
        if line.startswith("+++ b/"):
            path = line[6:]
            continue
        if line.startswith(("--- ", "+++ ", "diff --git", "index ", "@@",
                            "new file", "deleted file", "similarity ",
                            "rename ", "old mode", "new mode")):
            continue
        if line[:1] in ("+", "-"):
            if keep and line[0] != keep:
                continue
            rows.append(GitRow(kind="content", commit=commit, date=date,
                               author=author, subject=subject, path=path,
                               text=line[1:], status=line[0]))
    return rows


def run_query(
    repo: Path,
    query: Any,
    *,
    limit: int = DEFAULT_ROW_LIMIT,
    timeout: float = SEARCH_TIMEOUT_S,
    runner: Runner = _run,
) -> GitSearchResult:
    """Run one `GitQuery` against `repo` and read what came back.

    The plan comes from `gitquery.build`, which is where every decision about
    *what* to run lives; this runs it and parses it. Splitting them that way is
    what lets the whole switch catalogue be tested without git installed.

    **Never raises.** A repository that has been moved, a bad revision, a
    pattern git rejects - all of them are a result carrying an error, because
    this is reached from a button and a traceback there is a bug report about
    something that is not a bug.
    """
    from app.search.gitquery import (
        KIND_GREP, KIND_NAME_STATUS, KIND_PATCH, build,
    )

    plan = build(query)
    started = time.perf_counter()
    code, out, err = runner(plan.argv, Path(repo), timeout)
    elapsed = time.perf_counter() - started

    # Exit code 1 is "found nothing" for both grep and log. See the note on
    # `search_history`: treating it as failure reports every unsuccessful
    # search as a broken tool.
    if code not in (0, 1):
        _log.warning("git search failed ({}): {}", code, err.strip()[:200])
        return GitSearchResult(
            ok=False, elapsed_s=elapsed, explain=plan.explain,
            command=tuple(plan.argv), error=err.strip() or f"git exited {code}")

    lines = out.splitlines()
    wanted = 1 if plan.first_only else limit
    if plan.kind == KIND_GREP:
        rows = _read_grep(lines, wanted)
    elif plan.kind == KIND_PATCH:
        rows = _read_patch(lines, wanted, keep=plan.line_filter)
    elif plan.kind == KIND_NAME_STATUS:
        rows = _read_name_status(lines, wanted)
    else:
        rows = _read_log(lines, wanted)

    return GitSearchResult(
        ok=True, rows=rows, elapsed_s=elapsed, explain=plan.explain,
        command=tuple(plan.argv),
        # A single-row answer by design is not a truncated one, and saying it
        # was would put "showing the first 1 of many" under `/introduced`.
        truncated=not plan.first_only and len(rows) >= limit,
    )


#: What `/branch`, `/tag`, `/author` and `/commit` offer once they are chosen.
#: Read from the repository, for the same reason the index's menu reads the
#: index: a value typed from a guess returns nothing, and a filter that returns
#: nothing is indistinguishable from a filter that does not work.
#:
#: Every one is a single bounded command. `--count` and `-n` are not decoration
#: - this is reached from a keystroke, and a repository can have thousands of
#: refs.
_VALUE_COMMANDS: dict[str, list[str]] = {
    "branch": ["git", "for-each-ref", "--sort=-committerdate", "--count=200",
               "--format=%(refname:short)", "refs/heads", "refs/remotes"],
    "tag": ["git", "for-each-ref", "--sort=-creatordate", "--count=200",
            "--format=%(refname:short)", "refs/tags"],
    "author": ["git", "log", "-n", "500", "--pretty=format:%an"],
    "commit": ["git", "log", "-n", "50", "--pretty=format:%h  %s"],
}


def repo_values(repo: Path, kind: str, *, prefix: str = "", limit: int = 40,
                runner: Runner = _run) -> list[str]:
    """Branch, tag, author and commit names actually present in `repo`.

    The `lookup` the Code tab's `/` menu is given. **Never raises**: a folder
    that is not a repository, a git that is not installed, a repository being
    rewritten - all of them are an empty list, because a suggestion list is a
    convenience and must never be the reason a menu fails to open.

    Deduplicated in order, so `/author` shows each person once with the most
    recent first rather than five hundred rows of the same three names.
    """
    command = _VALUE_COMMANDS.get(str(kind))
    if command is None:
        return []
    try:
        code, out, _err = runner(command, Path(repo), 20.0)
    except Exception:                        # noqa: BLE001 - see the docstring
        return []
    if code != 0:
        return []

    wanted = str(prefix or "").strip().lower()
    found: list[str] = []
    for line in out.splitlines():
        value = line.strip()
        if not value or value in found:
            continue
        if wanted and wanted not in value.lower():
            continue
        found.append(value)
        if len(found) >= max(1, int(limit)):
            break
    return found
