r"""Order 0y §3: history that answers as it goes - the part below the window.

Layer: L0 / L4

* **3a** - rows are handed over as git prints them; the first is on its way
  before git has finished; Stop still ends git within half a second.
* **3b** - every repository at once: at most two git processes at a time, the
  rows merged newest first, and one repository that fails does not stop the
  others.
* **3c** - `show_commit` reads one commit: message, author, date, the files
  changed and the diff.

**The fake git is a real process** (this Python, running a short script), so
the pipe, the reader threads and the kill are the real ones. Only the program
at the other end of the pipe is invented.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from app.core.osbridge import programs
from app.search import gitsearch
from app.search.gitquery import build, parse_git_query
from app.search.gitsearch import (
    STOPPED_EXIT, GitProgress, StopFlag, search_repositories, show_commit, stream_query,
)

HISTORY = parse_git_query("CustomerId /history")

#: The interpreter itself. In a Windows virtual environment `sys.executable` is a
#: launcher that starts the real Python as a child - the same shape as git's own
#: launcher - and a fake git must be one process for "it was ended" to be true.
PYTHON = getattr(sys, "_base_executable", None) or sys.executable


def header(sha: str, date: str, subject: str, author: str = "Ada") -> str:
    return f"{sha * 8}\t{date}\t{author}\t{subject}"


def fake_git(script: str):
    """A `streamer` that runs `script` in place of git, through the real `_stream`."""
    def streamer(_args, _cwd, timeout, on_line, stop):
        return gitsearch._stream([PYTHON, "-u", "-c", script], None, timeout,
                                 on_line, stop)
    return streamer


def printing(lines: list[str], *, pause_after_first: float = 0.0, then_hang: bool = False) -> str:
    """A script that prints `lines`, slowly or for ever."""
    body = ["import sys, time"]
    for index, line in enumerate(lines):
        body.append(f"sys.stdout.write({line + chr(10)!r}); sys.stdout.flush()")
        if index == 0 and pause_after_first:
            body.append(f"time.sleep({pause_after_first})")
    if then_hang:
        body.append("time.sleep(60)")
    return "\n".join(body)


# --- 3a: line by line ------------------------------------------------------------


def test_the_first_row_is_shown_before_git_finishes() -> None:
    """The §5 budget, with a fake git that prints one row and then takes a second."""
    script = printing([header("a", "2026-09-03", "first"), header("b", "2026-09-02", "second")],
                      pause_after_first=1.0)
    seen: list[tuple[float, str]] = []
    started = time.monotonic()
    found = stream_query(Path("."), HISTORY, streamer=fake_git(script),
                         on_rows=lambda rows: seen.extend(
                             (time.monotonic() - started, row.subject) for row in rows))
    finished = time.monotonic() - started

    assert [subject for _when, subject in seen] == ["first", "second"]
    assert finished >= 1.0, "the fake git takes a second to finish"
    assert finished - seen[0][0] >= 0.7, (
        f"the first row arrived at {seen[0][0]:.2f}s of {finished:.2f}s - not before git finished")
    assert found.ok and [row.subject for row in found.rows] == ["first", "second"]
    assert all(row.kind == "commit" for row in found.rows)


def test_stop_ends_a_git_that_has_printed_nothing_within_half_a_second() -> None:
    stop = StopFlag()
    threading.Timer(0.2, stop.stop).start()
    started = time.monotonic()
    code, err = gitsearch._stream([PYTHON, "-c", "import time; time.sleep(60)"],
                                  None, 120.0, lambda _line: None, stop)
    assert (code, err) == (STOPPED_EXIT, "stopped")
    assert time.monotonic() - started < 0.2 + 0.5


def test_stop_ends_a_git_that_never_stops_printing_within_half_a_second() -> None:
    """Rows arriving faster than they are read must not starve the Stop."""
    script = "import sys\nwhile True:\n    sys.stdout.write('x' * 40 + chr(10))"
    stop = StopFlag()
    threading.Timer(0.2, stop.stop).start()
    started = time.monotonic()
    code, _err = gitsearch._stream([PYTHON, "-c", script], None, 120.0,
                                   lambda _line: None, stop)
    assert code == STOPPED_EXIT
    assert time.monotonic() - started < 0.2 + 0.5


def test_stop_does_not_wait_for_a_program_that_outlives_its_launcher() -> None:
    """A launcher that is ended leaves its child running (git's does, and so does a
    virtual environment's Python). Stop must come back anyway, within the budget."""
    stop = StopFlag()
    threading.Timer(0.2, stop.stop).start()
    started = time.monotonic()
    code, _err = gitsearch._stream([sys.executable, "-c", "import time; time.sleep(8)"],
                                   None, 120.0, lambda _line: None, stop)
    assert code == STOPPED_EXIT
    assert time.monotonic() - started < 0.2 + 0.5


def test_a_stopped_search_keeps_the_rows_it_had_found() -> None:
    script = printing([header("a", "2026-09-03", "first")], then_hang=True)
    stop = StopFlag()
    started = time.monotonic()
    found = stream_query(Path("."), HISTORY, streamer=fake_git(script), stop=stop,
                         on_rows=lambda _rows: stop.stop())
    assert time.monotonic() - started < 5.0
    assert found.stopped and not found.ok
    assert [row.subject for row in found.rows] == ["first"]


def test_a_search_already_stopped_never_starts_git() -> None:
    stop = StopFlag()
    stop.stop()
    started: list = []
    found = stream_query(Path("."), HISTORY, stop=stop,
                         streamer=lambda *args: started.append(args) or (0, ""))
    assert found.stopped and started == []


def test_git_is_ended_at_the_row_limit_rather_than_read_to_the_end() -> None:
    lines = [header(letter, "2026-09-01", f"row {letter}") for letter in "abcde"]
    started = time.monotonic()
    found = stream_query(Path("."), HISTORY, limit=2,
                         streamer=fake_git(printing(lines, then_hang=True)))
    assert time.monotonic() - started < 5.0, "it waited for a git that was never going to end"
    assert found.ok and found.truncated and len(found.rows) == 2


def test_the_time_limit_is_a_result_and_keeps_its_rows() -> None:
    script = printing([header("a", "2026-09-03", "first")], then_hang=True)
    found = stream_query(Path("."), HISTORY, streamer=fake_git(script), timeout=0.6)
    assert not found.ok and not found.stopped and "timed out" in found.error
    assert len(found.rows) == 1


def test_a_git_that_fails_says_why() -> None:
    script = "import sys; sys.stderr.write('fatal: bad revision'); sys.exit(128)"
    found = stream_query(Path("."), HISTORY, streamer=fake_git(script))
    assert not found.ok and found.error == "fatal: bad revision"
    assert found.command[:2] == ("git", "log"), "the command is kept, to reproduce it by hand"


def test_a_git_that_cannot_be_started_is_a_result(tmp_path) -> None:
    code, err = gitsearch._stream(["git", "log"], tmp_path / "not-here", 5.0,
                                  lambda _line: None)
    assert code == 127 and err


def test_the_streamed_git_gets_no_console_window(monkeypatch) -> None:
    seen: dict = {}
    real = subprocess.Popen

    def popen(args, **kwargs):
        seen.update(kwargs)
        kwargs["creationflags"] = 0
        return real(args, **kwargs)

    monkeypatch.setattr(gitsearch.subprocess, "Popen", popen)
    monkeypatch.setattr(gitsearch, "hidden_console_flags", lambda: 0x08000000)
    gitsearch._stream([PYTHON, "-c", "print('x')"], None, 30.0, lambda _line: None)
    assert seen["creationflags"] == 0x08000000


def test_every_history_row_ends_its_own_line() -> None:
    """`format:` puts the newline between commits, so the last row of a search
    only arrived when git ended. `tformat:` ends each row - measured on real git."""
    argv = build(HISTORY).argv
    assert any(arg.startswith("--pretty=tformat:") for arg in argv)
    assert not any(arg.startswith("--pretty=format:") for arg in argv)


PATCH = [
    header("a", "2026-09-03", "remove the key"),
    "",
    "diff --git a/src/Order.cs b/src/Order.cs",
    "index 111..222 100644",
    "--- a/src/Order.cs",
    "+++ b/src/Order.cs",
    "@@ -1 +1 @@",
    "-    var ApiKey = 1;",
    "+    var Key = 1;",
    header("b", "2026-09-01", "add the key"),
    "",
    "diff --git a/src/Pay.cs b/src/Pay.cs",
    "--- /dev/null",
    "+++ b/src/Pay.cs",
    "+    var ApiKey = 2;",
]

NAMES = [
    header("a", "2026-09-03", "rename"), "", "R100\tsrc/Old.cs\tsrc/New.cs",
    header("b", "2026-09-01", "add"), "", "A\tsrc/Old.cs",
]


@pytest.mark.parametrize("text,lines", [
    ("ApiKey /history /removed-only", PATCH),
    ("ApiKey /history /added-only", PATCH),
    ("/file-history Old.cs", NAMES),
    ("CustomerId /history", [header("a", "2026-09-03", "one"), header("b", "2026-09-01", "two")]),
    ("/class OrderService", ["src/Order.cs:12:public class OrderService", "a:b.cs:3:class X"]),
])
def test_line_by_line_reads_exactly_what_the_whole_output_reads(text, lines) -> None:
    """The streamed reader calls the batch readers, so the two cannot disagree."""
    query = parse_git_query(text)
    whole = gitsearch.run_query(Path("."), query,
                                runner=lambda *_a: (0, "\n".join(lines), ""))
    streamed = stream_query(
        Path("."), query, runner=lambda *_a: (0, "", ""),
        streamer=lambda _args, _cwd, _timeout, on_line, _stop:
            ([on_line(line) for line in lines] and None) or (0, ""))
    assert streamed.rows == whole.rows and whole.rows, text


# --- the real git behind the launcher ----------------------------------------------


def test_on_windows_the_real_git_is_started_not_its_launcher(monkeypatch, tmp_path) -> None:
    """Ending the launcher left the real git running: Stop came back only when
    git had finished (measured 0.8-1.5 s late). The real one ends at once."""
    launcher = tmp_path / "Git" / "cmd" / "git.exe"
    real = tmp_path / "Git" / "mingw64" / "bin" / "git.exe"
    for path in (launcher, real):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"")
    monkeypatch.setattr(programs, "_git_program_found", None)
    monkeypatch.setattr(programs.shutil, "which", lambda _name: str(launcher))
    monkeypatch.setattr(programs, "is_windows", lambda: True)
    assert programs.git_program() == str(real)

    monkeypatch.setattr(programs, "_git_program_found", None)
    monkeypatch.setattr(programs, "is_windows", lambda: False)
    assert programs.git_program() == str(launcher)

    monkeypatch.setattr(programs, "_git_program_found", None)
    monkeypatch.setattr(programs.shutil, "which", lambda _name: None)
    assert programs.git_program() == "git"


def test_an_install_laid_out_differently_keeps_the_git_on_path(monkeypatch, tmp_path) -> None:
    other = tmp_path / "scoop" / "shims" / "git.exe"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"")
    monkeypatch.setattr(programs, "_git_program_found", None)
    monkeypatch.setattr(programs.shutil, "which", lambda _name: str(other))
    monkeypatch.setattr(programs, "is_windows", lambda: True)
    assert programs.git_program() == str(other)


def test_both_runners_start_that_program_and_record_plain_git(monkeypatch) -> None:
    monkeypatch.setattr(gitsearch, "git_program", lambda: sys.executable)
    assert gitsearch._real(["git", "log"]) == [sys.executable, "log"]
    assert gitsearch._real(["other", "git"]) == ["other", "git"]
    assert gitsearch._run(["git", "-c", "print('ran')"], None, 30.0)[:2] == (0, "ran\n")
    lines: list = []
    assert gitsearch._stream(["git", "-c", "print('ran')"], None, 30.0, lines.append)[0] == 0
    assert lines == ["ran"]


# --- 3b: every repository at once ----------------------------------------------------

THREE = [("alpha", "D:/alpha"), ("beta", "D:/beta"), ("gamma", "D:/gamma")]


def by_folder(replies: dict, *, delay: float = 0.0, record: dict | None = None):
    """A streamer answering per repository folder: lines, or `(code, err)`, or an error."""
    lock = threading.Lock()

    def streamer(_args, cwd, _timeout, on_line, stop):
        if record is not None:
            with lock:
                record["now"] = record.get("now", 0) + 1
                record["most"] = max(record.get("most", 0), record["now"])
                record.setdefault("order", []).append(Path(cwd).name)
        try:
            if delay:
                time.sleep(delay)
            reply = replies[Path(cwd).name]
            if isinstance(reply, Exception):
                raise reply
            if isinstance(reply, tuple):
                return reply
            for line in reply:
                if stop is not None and stop.stopped:
                    return STOPPED_EXIT, "stopped"
                if on_line(line):
                    break
            return 0, ""
        finally:
            if record is not None:
                with lock:
                    record["now"] -= 1
    return streamer


def test_rows_from_every_repository_are_merged_newest_first() -> None:
    found = search_repositories(THREE, HISTORY, streamer=by_folder({
        "alpha": [header("a", "2026-09-10", "alpha new"), header("b", "2026-01-01", "alpha old")],
        "beta": [header("c", "2026-09-20", "beta new")],
        "gamma": [header("d", "2026-05-05", "gamma mid")],
    }))
    assert found.ok and found.repos == 3 and found.failed == []
    assert [row.subject for row in found.rows] == [
        "beta new", "alpha new", "gamma mid", "alpha old"]
    assert [row.repo for row in found.rows] == ["beta", "alpha", "gamma", "alpha"]
    assert found.rows[0].root == "D:/beta"
    assert "across 3 repositories" in found.explain


def test_at_most_two_repositories_are_searched_at_a_time() -> None:
    record: dict = {}
    five = [(f"r{n}", f"D:/r{n}") for n in range(5)]
    found = search_repositories(five, HISTORY, streamer=by_folder(
        {f"r{n}": [header(str(n), "2026-09-01", f"from {n}")] for n in range(5)},
        delay=0.15, record=record))
    assert record["most"] == 2, f"{record['most']} git processes ran at once"
    assert len(record["order"]) == 5 and len(found.rows) == 5


def test_one_repository_that_fails_does_not_stop_the_others() -> None:
    found = search_repositories(THREE, HISTORY, streamer=by_folder({
        "alpha": (128, "fatal: not a git repository"),
        "beta": [header("c", "2026-09-20", "beta new")],
        "gamma": RuntimeError("the drive went away"),
    }))
    assert found.ok, "two of three failing is still an answer"
    assert [row.subject for row in found.rows] == ["beta new"]
    assert dict(found.failed)["alpha"] == "fatal: not a git repository"
    assert "the drive went away" in dict(found.failed)["gamma"]


def test_every_repository_failing_is_a_failure_that_names_them() -> None:
    found = search_repositories(THREE[:2], HISTORY, streamer=by_folder({
        "alpha": (128, "fatal: a"), "beta": (128, "fatal: b")}))
    assert not found.ok and not found.stopped
    assert "alpha: fatal: a" in found.error and "beta: fatal: b" in found.error


def test_one_repository_is_not_re_ordered_and_reads_as_it_always_did() -> None:
    lines = [header("a", "2026-01-01", "older date first"), header("b", "2026-09-09", "newer")]
    found = search_repositories([("alpha", "D:/alpha")], HISTORY,
                                streamer=by_folder({"alpha": lines}))
    assert [row.subject for row in found.rows] == ["older date first", "newer"]
    assert found.repos == 1 and "across" not in found.explain
    failed = search_repositories([("alpha", "D:/alpha")], HISTORY,
                                 streamer=by_folder({"alpha": (128, "fatal: bad revision")}))
    assert not failed.ok and failed.error == "fatal: bad revision"


def test_the_same_commit_in_two_checkouts_is_listed_once() -> None:
    same = header("a", "2026-09-10", "shared commit")
    found = search_repositories(THREE[:2], HISTORY, streamer=by_folder({
        "alpha": [same], "beta": [same, header("b", "2026-09-01", "beta only")]}))
    assert sorted(row.subject for row in found.rows) == ["beta only", "shared commit"]


def test_progress_arrives_before_the_search_ends_and_counts_as_it_goes() -> None:
    reports: list[tuple[float, GitProgress]] = []
    started = time.monotonic()
    found = search_repositories(
        THREE, HISTORY,
        on_progress=lambda progress: reports.append((time.monotonic() - started, progress)),
        streamer=by_folder({
            "alpha": [header("a", "2026-09-10", "alpha")],
            "beta": [header("b", "2026-09-20", "beta")],
            "gamma": [header("c", "2026-05-05", "gamma")]}, delay=0.2))
    finished = time.monotonic() - started
    assert reports[0][0] < finished - 0.1, "the first report came with the end, not before it"
    assert sum(len(progress.rows) for _when, progress in reports) == 3, "each row reported once"
    last = reports[-1][1]
    assert (last.found, last.repos_done, last.repos_total) == (3, 3, 3)
    assert len(found.rows) == 3


def test_a_row_that_arrives_just_after_a_report_does_not_wait_for_the_next_one() -> None:
    """Two rows at once, then git goes quiet for a second: both are on screen long
    before it ends. The second used to wait for a third row that never came."""
    script = "\n".join([
        "import sys, time",
        f"sys.stdout.write({header('a', '2026-09-03', 'first') + chr(10)!r})",
        f"sys.stdout.write({header('b', '2026-09-02', 'second') + chr(10)!r})",
        "sys.stdout.flush(); time.sleep(1.2)"])
    reports: list[tuple[float, int]] = []
    started = time.monotonic()
    search_repositories(
        [("alpha", ".")], HISTORY, streamer=fake_git(script),
        on_progress=lambda p: reports.append((time.monotonic() - started, p.found)))
    finished = time.monotonic() - started
    both = next(when for when, found in reports if found == 2)
    assert finished - both >= 0.7, f"both rows shown at {both:.2f}s of {finished:.2f}s"


def test_stop_ends_every_repository_and_the_waiting_ones_never_start() -> None:
    stop = StopFlag()
    record: dict = {}
    five = [(f"r{n}", f"D:/r{n}") for n in range(5)]

    def on_progress(_progress) -> None:
        stop.stop()

    found = search_repositories(five, HISTORY, stop=stop, on_progress=on_progress,
                                streamer=by_folder(
        {f"r{n}": [header(str(n), "2026-09-01", f"from {n}")] * 1 for n in range(5)},
        delay=0.1, record=record))
    assert found.stopped and not found.ok
    assert len(record["order"]) <= 2, "repositories waiting their turn were started after Stop"
    assert found.failed == [], "a stopped repository is not a failed one"


def test_the_merged_list_is_capped_and_says_so() -> None:
    lines = [header(f"{n:x}", f"2026-09-{n + 1:02d}", f"row {n}") for n in range(6)]
    found = search_repositories(THREE[:2], HISTORY, limit=4, streamer=by_folder({
        "alpha": lines[:3], "beta": lines[3:]}))
    assert len(found.rows) == 4 and found.truncated
    assert found.rows[0].subject == "row 5", "the newest are the ones kept"


def test_no_repositories_is_a_failure_not_a_crash() -> None:
    found = search_repositories([], HISTORY)
    assert not found.ok and found.rows == [] and found.error


# --- 3c: one commit ---------------------------------------------------------------------

SHOWN = [
    "a" * 40, "Ada Lovelace", "ada@example.com", "2024-01-02T09:00:00+00:00",
    "Rename the key", "\x1e", "Because ApiKey was misleading.", "", "Second paragraph.", "\x1e",
    "",
    ":100644 100644 1111111 2222222 M\tsrc/Order.cs",
    ":000000 100644 0000000 3333333 A\tsrc/New.cs",
    ":100644 100644 4444444 4444444 R100\tsrc/Old.cs\tsrc/Moved.cs",
    "",
    "diff --git a/src/Order.cs b/src/Order.cs",
    "--- a/src/Order.cs",
    "+++ b/src/Order.cs",
    "@@ -1 +1 @@",
    "-    var ApiKey = 1;",
    "+    var Key = 1;",
]


def showing(lines, code: int = 0, err: str = ""):
    seen: dict = {}

    def streamer(args, cwd, _timeout, on_line, _stop):
        seen.update(args=list(args), cwd=cwd)
        for line in lines:
            if on_line(line):
                break
        return code, err
    streamer.seen = seen
    return streamer


def test_a_commit_is_read_as_message_author_date_files_and_diff() -> None:
    streamer = showing(SHOWN)
    detail = show_commit(Path("D:/repo"), "a" * 40, streamer=streamer)
    assert detail.ok and detail.commit == "a" * 40
    assert (detail.author, detail.email) == ("Ada Lovelace", "ada@example.com")
    assert detail.date == "2024-01-02T09:00:00+00:00"
    assert detail.subject == "Rename the key"
    assert detail.body == "Because ApiKey was misleading.\n\nSecond paragraph."
    assert detail.files == (("M", "src/Order.cs", ""), ("A", "src/New.cs", ""),
                            ("R", "src/Moved.cs", "src/Old.cs"))
    assert detail.diff.splitlines()[0] == "diff --git a/src/Order.cs b/src/Order.cs"
    assert detail.diff.splitlines()[-1] == "+    var Key = 1;"
    assert not detail.truncated
    assert streamer.seen["args"][:2] == ["git", "show"] and "a" * 40 in streamer.seen["args"]


def test_a_very_long_diff_is_cut_and_says_so() -> None:
    long = SHOWN + [f"+line {n}" for n in range(500)]
    detail = show_commit(Path("D:/repo"), "abc1234", max_lines=50, streamer=showing(long))
    assert detail.ok and detail.truncated and len(detail.diff.splitlines()) == 50


def test_only_a_commit_id_reaches_the_command_line() -> None:
    streamer = showing(SHOWN)
    for bad in ("", "--output=x", "HEAD; rm", "main"):
        detail = show_commit(Path("D:/repo"), bad, streamer=streamer)
        assert not detail.ok and detail.error
    assert streamer.seen == {}, "git was started for something that is not a commit id"


def test_a_commit_git_cannot_show_is_a_result() -> None:
    detail = show_commit(Path("D:/repo"), "abc1234",
                         streamer=showing([], code=128, err="fatal: bad object abc1234"))
    assert not detail.ok and detail.error == "fatal: bad object abc1234"


# --- against the real git, when there is one --------------------------------------------

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    """One small real repository for the three tests below (building it is a
    dozen git calls, so it is built once; none of the tests changes it)."""
    folder = tmp_path_factory.mktemp("history") / "repo"
    folder.mkdir()

    def git(*args: str) -> str:
        done = subprocess.run(
            ["git", "-c", "user.name=Ada", "-c", "user.email=ada@example.com",
             "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", *args],
            cwd=folder, capture_output=True, text=True, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return done.stdout.strip()

    git("init", "-q", "-b", "main")
    (folder / "order.py").write_text("CustomerId = 1\n", encoding="utf-8")
    git("add", "."), git("commit", "-q", "-m", "Add the customer id")
    git("checkout", "-q", "-b", "side")
    (folder / "pay.py").write_text("def pay(CustomerId):\n    return CustomerId\n", encoding="utf-8")
    git("add", "."), git("commit", "-q", "-m", "Pay by customer\n\nThe body of the message.")
    git("checkout", "-q", "main")
    (folder / "order.py").write_text("CustomerId = 2\nOther = 3\n", encoding="utf-8")
    git("add", "."), git("commit", "-q", "-m", "Change the id")
    git("merge", "-q", "--no-ff", "side", "-m", "Merge side")
    return folder, git


@needs_git
def test_real_git_streams_a_history_search(repo) -> None:
    folder, _git = repo
    batches: list = []
    found = search_repositories([("repo", str(folder))], HISTORY, on_progress=batches.append)
    assert found.ok, found.error
    assert {row.subject for row in found.rows} == {"Add the customer id", "Pay by customer"}
    assert all(row.repo == "repo" and row.kind == "commit" for row in found.rows)
    assert sum(len(batch.rows) for batch in batches) == len(found.rows)


@needs_git
def test_real_git_shows_a_commit_and_a_merge(repo) -> None:
    folder, git = repo
    sha = git("log", "-1", "--format=%H", "side")
    detail = show_commit(folder, sha)
    assert detail.ok, detail.error
    assert detail.subject == "Pay by customer" and detail.body == "The body of the message."
    assert detail.author == "Ada" and detail.date[:2] == "20"
    assert detail.files == (("A", "pay.py", ""),)
    assert "+def pay(CustomerId):" in detail.diff

    merge = show_commit(folder, git("log", "-1", "--format=%H", "main"))
    assert merge.ok and merge.subject == "Merge side"
    assert merge.files == (("A", "pay.py", ""),), "a merge shows what it brought in"
    assert merge.diff.count("diff --git") == 1


@needs_git
def test_a_folder_that_is_not_a_repository_fails_alone(repo, tmp_path) -> None:
    folder, _git = repo
    plain = tmp_path / "plain"
    plain.mkdir()
    found = search_repositories([("plain", str(plain)), ("repo", str(folder)),
                                 ("gone", str(tmp_path / "gone"))], HISTORY)
    assert found.ok and len(found.rows) == 2
    assert {name for name, _why in found.failed} == {"plain", "gone"}
