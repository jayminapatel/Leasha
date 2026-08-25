"""The measurement that decides whether history search can exist.

Layer: L4 (diagnostic)

`WORKORDER-git-search-backend.md` §14 and `HANDOFF-ui-to-backend.md` B4 ask the
same question from opposite sides: can searching git history live behind the
Enter key, against a contract of p95 under 300ms warm?

It is answered by numbers, so this module's job is to produce numbers that can
be trusted - which means every row carries the conditions that produced it. A
"50,000 commits" figure taken against a repository with 800 commits is not a
measurement of anything, and unqualified it is exactly the sort of number that
ends up in a decision document.

Every subprocess goes through `runner`, so all of this runs on a machine with
no git and no repository.
"""

from __future__ import annotations

from app.search.gitsearch import (
    DEFAULT_DEPTHS,
    GitResult,
    count_commits,
    is_repository,
    measure,
    search_history,
)


def runner_for(script):
    """A fake subprocess. `script` maps a command fragment to (code, out, err)."""
    calls = []

    def run(args, cwd, timeout):
        calls.append(list(args))
        joined = " ".join(args)
        for fragment, reply in script.items():
            if fragment in joined:
                return reply
        return (0, "", "")

    run.calls = calls
    return run


GIT_PRESENT = {"--version": (0, "git version 2.43.0\n", ""),
               "rev-parse": (0, "true\n", ""),
               "rev-list": (0, "12000\n", "")}


# --- reading the repository -------------------------------------------------

def test_a_folder_that_is_not_a_repository_is_reported_not_crashed():
    run = runner_for({"rev-parse": (128, "", "not a git repository")})

    assert is_repository("/tmp/nope", runner=run) is False


def test_the_commit_count_is_read_rather_than_assumed():
    run = runner_for({"rev-list": (0, "  48213 \n", "")})

    assert count_commits("/repo", runner=run) == 48213


def test_an_unreadable_commit_count_is_zero_not_an_exception():
    """A measurement must not fail on the way to being taken."""
    run = runner_for({"rev-list": (128, "", "fatal: bad revision")})

    assert count_commits("/repo", runner=run) == 0


# --- what "failed" means ----------------------------------------------------

def test_no_matches_is_not_a_failure():
    """**Exit code 1 means "found nothing".**

    Both `git log -S` and `git grep` use it. Treating it as an error reports
    every unsuccessful search as a broken tool, which is how a measurement
    turns into a bug report.
    """
    run = runner_for({"log": (1, "", "")})

    result = search_history("/repo", "nothing", runner=run)

    assert result.ok is True
    assert result.count == 0
    assert result.error == ""


def test_a_real_failure_is_reported_as_one():
    run = runner_for({"log": (128, "", "fatal: bad revision 'nope'")})

    result = search_history("/repo", "x", rev="nope", runner=run)

    assert result.ok is False
    assert "bad revision" in result.error


def test_a_timeout_is_a_result_not_an_exception():
    """A search that does not finish has answered the question."""
    run = runner_for({"log": (124, "", "timed out after 600s")})

    result = search_history("/repo", "x", runner=run)

    assert result.ok is False
    assert "timed out" in result.error


def test_the_command_is_recorded_so_a_surprising_number_can_be_reproduced():
    """A measurement without its conditions is not a measurement."""
    run = runner_for({"log": (0, "abc123 subject\n", "")})

    result = search_history("/repo", "widget", depth=500, runner=run)

    assert "-Swidget" in result.command
    assert "-n500" in result.command


def test_grep_and_log_run_different_commands():
    """The difference between them is the entire cost question: one is
    proportional to history, the other is not."""
    run = runner_for({})

    search_history("/repo", "x", mode="log-s", runner=run)
    search_history("/repo", "x", mode="grep", runner=run)

    assert run.calls[0][:2] == ["git", "log"]
    assert run.calls[1][:2] == ["git", "grep"]


# --- the measurement --------------------------------------------------------

def test_no_git_is_stated_rather_than_crashing(monkeypatch):
    """`shutil.which` decides, so this is what a machine without git sees."""
    import app.search.gitsearch as module

    monkeypatch.setattr(module.shutil, "which", lambda _name: None)

    found = measure("/repo", "x", runner=runner_for(GIT_PRESENT))

    assert found.git == ""
    assert found.rows == []
    assert any("not found" in note for note in found.notes)


def test_every_depth_is_timed_plus_one_grep(monkeypatch):
    import app.search.gitsearch as module

    monkeypatch.setattr(module.shutil, "which", lambda _name: "/usr/bin/git")
    run = runner_for(GIT_PRESENT)

    found = measure("/repo", "x", depths=(100, 1000), runner=run)

    assert [row["mode"] for row in found.rows] == ["log-s", "log-s", "grep"]
    assert [row["depth_asked"] for row in found.rows[:2]] == [100, 1000]


def test_a_depth_deeper_than_the_repository_is_flagged_not_reported_as_fact(
        monkeypatch):
    """**The number that would otherwise mislead.**

    A row asking for 50,000 commits against a repository holding 12,000
    searched the whole history. Reported unqualified it reads as "50k commits
    cost 2 seconds", which is false and would be used to justify building the
    wrong thing.
    """
    import app.search.gitsearch as module

    monkeypatch.setattr(module.shutil, "which", lambda _name: "/usr/bin/git")

    found = measure("/repo", "x", depths=(1000, 50_000),
                    runner=runner_for(GIT_PRESENT))

    shallow, deep = found.rows[0], found.rows[1]
    assert shallow["representative"] is True
    assert deep["representative"] is False
    assert deep["commits_searched"] == 12000
    assert any("not representative" in note for note in found.notes)


def test_the_peak_memory_note_says_whose_memory_it_is(monkeypatch):
    """It is this process, not git's. A number nobody can attribute is worse
    than an absent one."""
    import app.search.gitsearch as module

    monkeypatch.setattr(module.shutil, "which", lambda _name: "/usr/bin/git")

    found = measure("/repo", "x", depths=(10,), runner=runner_for(GIT_PRESENT))

    assert any("not git's" in note for note in found.notes)


def test_the_default_depths_are_the_three_the_work_order_asks_for():
    assert DEFAULT_DEPTHS == (1_000, 10_000, 50_000)


def test_a_result_serialises_for_json():
    """`--json` is how these numbers get pasted into HANDOFF.md."""
    payload = GitResult(ok=True, lines=("a", "b"), elapsed_s=1.23456,
                        command=("git", "log")).as_dict()

    assert payload["matches"] == 2
    assert payload["elapsed_s"] == 1.235
    assert payload["command"] == "git log"


# --- the guarantee that matters ---------------------------------------------

def test_nothing_in_the_search_path_imports_this():
    """**A measurement must never become a search.**

    The first non-negotiable is that no unbounded work sits behind the Enter
    key, and this module shells out to a command that takes minutes. It is
    reachable from `app.cli` and from nowhere else.
    """
    import ast
    import pathlib

    offenders = []
    for path in pathlib.Path("app").rglob("*.py"):
        if path.name in ("gitsearch.py", "cli.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and \
                    "gitsearch" in ast.unparse(node):
                offenders.append(str(path))

    assert not offenders, (
        f"gitsearch is reachable from {offenders} - it runs git as a "
        f"subprocess and can take minutes; it must not be on any search path")
