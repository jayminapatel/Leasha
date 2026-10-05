r"""Order 0y §3a and §3b: what the Code tab does with a history search.

Layer: L5

* the words: which repositories a search covers, the live line, the summary
  that names a repository that could not be searched (`presenter`, no Qt);
* the window: no `/repo` starts one search over every repository instead of
  asking for a name; rows are drawn as they are reported, newest first; the row
  somebody has selected stays selected while more arrive; Stop keeps what was
  found;
* the command line, which has it before the window does (non-negotiable 8).

Nothing here starts git: the worker is held, and its reports are handed to the
window by the test.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from app.search.gitquery import parse_git_query
from app.search.gitsearch import GitProgress, GitRow, GitSearchResult
from app.ui.presenter import (
    git_live_line, git_live_rows, git_needle, git_result_row, git_stopped_summary,
    git_summary, repo_targets,
)

REPOS = [
    {"id": 1, "name": "leasha", "root_path": "D:/SearchProject", "files": 9},
    {"id": 2, "name": "my tools", "root_path": "D:/code/my tools", "files": 10},
    {"id": 3, "name": "ghost", "root_path": "", "files": 0},
]


def commit(sha: str, date: str, subject: str, repo: str = "", root: str = "") -> GitRow:
    return GitRow(kind="commit", commit=sha * 8, date=date, author="Ada", subject=subject,
                  repo=repo, root=root)


# --- the words -------------------------------------------------------------------


def test_no_name_means_every_repository() -> None:
    assert repo_targets(REPOS, "") == [("leasha", "D:/SearchProject"),
                                       ("my tools", "D:/code/my tools")]


def test_a_name_means_that_repository_and_a_wrong_name_means_none() -> None:
    assert repo_targets(REPOS, "tools") == [("my tools", "D:/code/my tools")]
    assert repo_targets(REPOS, "nothing-like-it") == []
    assert repo_targets([], "") == []


def test_the_live_line_is_the_orders_own_words() -> None:
    assert git_live_line(12) == "Searching history… 12 found so far"
    assert git_live_line(12, 1, 3) == (
        "Searching history… 12 found so far  ·  1 of 3 repositories searched")
    assert git_live_line(0, history=False) == "Searching… 0 found so far"


def test_rows_from_several_repositories_are_kept_newest_first() -> None:
    shown = [git_result_row(commit("a", "2026-09-10", "alpha new", "alpha", "D:/a"), "")]
    new = [git_result_row(commit("b", "2026-09-20", "beta new", "beta", "D:/b"), ""),
           git_result_row(commit("c", "2026-01-01", "beta old", "beta", "D:/b"), "")]
    assert [row.name for row in git_live_rows(shown, new, many=True)] == [
        "beta new", "alpha new", "beta old"]
    assert [row.name for row in git_live_rows(shown, new, many=False)] == [
        "alpha new", "beta new", "beta old"], "one repository keeps git's own order"


def test_a_row_says_which_repository_it_came_from() -> None:
    row = git_result_row(commit("a", "2026-09-10", "Fix it", "leasha", "D:/SearchProject"),
                         "", "CustomerId")
    assert row.repo == "leasha", "the Repository column holds the repository"
    assert row.path == "aaaaaaaa  ·  Ada", "the commit and its author are still on the row"
    assert (row.commit, row.repo_root, row.needle) == ("a" * 8, "D:/SearchProject", "CustomerId")
    assert row.full_path == "" and row.seen == "2026-09-10"


def test_a_row_with_no_repository_name_reads_as_it_always_did() -> None:
    row = git_result_row(commit("a", "2026-09-10", "Fix it"), "D:/x")
    assert row.repo == "aaaaaaaa" and row.path == ""


def test_a_checkout_hit_in_a_named_repository_opens_under_that_repository() -> None:
    hit = GitRow(kind="content", path="src/a.cs", line_no=4, text="x", repo="my tools",
                 root="D:/code/my tools")
    row = git_result_row(hit, "D:/somewhere-else")
    assert row.full_path.replace("\\", "/") == "D:/code/my tools/src/a.cs"
    assert row.commit == "" and row.line_no == 4


def test_a_changed_line_from_history_is_a_commit_not_a_file_on_disk() -> None:
    """It used to preview today's file under a line that may no longer be in it."""
    removed = GitRow(kind="content", commit="b" * 40, path="src/a.cs", status="-",
                     text="var ApiKey = 1;", date="2026-09-01", repo="leasha", root="D:/x")
    row = git_result_row(removed, "", "ApiKey")
    assert row.full_path == "" and row.commit == "b" * 40 and row.line_no == 0


def test_what_was_searched_for_is_what_gets_highlighted() -> None:
    assert git_needle(parse_git_query("CustomerId /history")) == "CustomerId"
    assert git_needle(parse_git_query("/class OrderService /history")) == "OrderService"
    assert git_needle(parse_git_query("/history /message licence")) == "licence"


def test_the_summary_names_a_repository_that_could_not_be_searched() -> None:
    found = GitSearchResult(ok=True, rows=[commit("a", "2026-09-10", "x")], elapsed_s=1.5,
                            explain="in the last 2,000 commits, across 3 repositories",
                            repos=3, failed=[("ghost", "fatal: not a git\nrepository")])
    line = git_summary(found)
    assert "across 3 repositories" in line
    assert "1 repository could not be searched: ghost (fatal: not a git repository)" in line


def test_a_stopped_search_says_what_it_had_found_and_keeps_the_released_sentence() -> None:
    released = "History search stopped. Press Enter or “Search history” to run it again."
    assert git_stopped_summary(GitSearchResult(ok=False, stopped=True)) == released
    some = GitSearchResult(ok=False, stopped=True, rows=[commit("a", "2026-09-10", "x")] * 12)
    assert git_stopped_summary(some) == f"12 found before it was stopped.  {released}"


# --- the command line ---------------------------------------------------------------


def _cli(tmp_path, *argv):
    from app import cli
    from tests.unit.test_cli_wiring import env_file, parser_for

    env = env_file(tmp_path)
    cli.cmd_init(parser_for(["init", "--env", env]))
    return cli, parser_for(["gitsearch", *argv, "--env", env])


def test_the_command_line_needs_a_repository_or_every_repository(tmp_path, capsys) -> None:
    cli, args = _cli(tmp_path, "CustomerId /history")
    capsys.readouterr()
    assert cli.cmd_gitsearch(args) == cli.EXIT_ERROR
    printed = capsys.readouterr()
    assert "--every-repo" in (printed.out + printed.err)


def test_every_repo_with_an_empty_index_says_so(tmp_path, capsys) -> None:
    cli, args = _cli(tmp_path, "CustomerId /history", "--every-repo")
    capsys.readouterr()
    assert cli.cmd_gitsearch(args) == cli.EXIT_ERROR
    printed = capsys.readouterr()
    assert "no repositories" in (printed.out + printed.err)


def test_every_repo_searches_them_all_and_names_the_one_that_failed(
        tmp_path, capsys, monkeypatch) -> None:
    from app.search import gitsearch
    from app.storage.sqlite_store import SqliteStore

    cli, args = _cli(tmp_path, "CustomerId /history", "--every-repo", "--json")
    capsys.readouterr()
    seen: dict = {}

    def fake(targets, query, **kwargs):
        seen.update(targets=list(targets), text=query.text)
        kwargs["on_progress"](GitProgress(rows=(commit("a", "2026-09-10", "x", "one", "D:/one"),),
                                          found=1, repos_done=2, repos_total=2))
        return GitSearchResult(ok=True, rows=[commit("a", "2026-09-10", "x", "one", "D:/one")],
                               repos=2, failed=[("two", "fatal: gone")],
                               explain="in the last 2,000 commits, across 2 repositories")

    monkeypatch.setattr(gitsearch, "search_repositories", fake)
    monkeypatch.setattr(gitsearch, "git_version", lambda: "git version 2")
    monkeypatch.setattr(SqliteStore, "repos_list", lambda self: [
        {"name": "one", "root_path": "D:/one"}, {"name": "two", "root_path": "D:/two"}])
    assert cli.cmd_gitsearch(args) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert seen["targets"] == [("one", "D:/one"), ("two", "D:/two")] and seen["text"] == "CustomerId"
    assert payload["repos"] == 2 and payload["failed"] == [{"repo": "two", "error": "fatal: gone"}]
    assert payload["rows"][0]["commit"] == "a" * 8


def test_show_prints_a_commit(tmp_path, capsys, monkeypatch) -> None:
    from app.search import gitsearch

    cli, args = _cli(tmp_path, "CustomerId", "--repo", str(tmp_path), "--show", "abc1234")
    capsys.readouterr()
    monkeypatch.setattr(gitsearch, "show_commit", lambda repo, sha: gitsearch.CommitDetail(
        commit="abc1234" + "0" * 33, author="Ada", email="ada@example.com",
        date="2024-01-02T09:00:00+00:00", subject="Rename the key", body="Why.",
        files=(("M", "src/Order.cs", ""),), diff="-var CustomerId = 1;\n+var Id = 1;"))
    assert cli.cmd_gitsearch(args) == cli.EXIT_OK
    out = capsys.readouterr().out
    for piece in ("Rename the key", "Why.", "Ada <ada@example.com>", "1 file changed",
                  "M  src/Order.cs", "+var Id = 1;", '"CustomerId" is on 1 line'):
        assert piece in out, piece


# --- the window -----------------------------------------------------------------------

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.code_view import CodeView  # noqa: E402
from app.ui.widgets import git_tree  # noqa: E402
from tests.unit.test_code_view import FakeStore  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture()
def view(qapp, monkeypatch):
    monkeypatch.setattr("app.ui.code_view.run", lambda _pool, worker: worker.run())
    held: dict = {}
    monkeypatch.setattr("app.ui.widgets.git_tree.run",
                        lambda _pool, worker: held.update(worker=worker))
    widget = CodeView(FakeStore())
    widget.show()
    widget._repos_read(widget._store.repos_list())
    widget.held = held
    return widget


def names(view) -> list[str]:
    table = view.results.table
    return [table.item(row, 0).text() for row in range(table.rowCount())]


def progress(*rows, found=None, done=0, total=2) -> GitProgress:
    return GitProgress(rows=tuple(rows), found=len(rows) if found is None else found,
                       repos_done=done, repos_total=total)


def test_no_repository_named_searches_them_all(view) -> None:
    """A6. It used to answer "Name a repository first"."""
    view.input.setText("CustomerId /history")
    view.start()
    worker = view.held["worker"]
    assert "Name a repository" not in view.summary.text()
    assert worker._work.__name__ == "search_repositories"
    assert worker._args[0] == [("leasha", "D:/SearchProject"), ("my tools", "D:/code/my tools")]
    assert callable(worker._kwargs["on_progress"]), "rows must be reported as they come"
    seen: list = []
    worker.signals.progress.connect(seen.append)
    report = GitProgress(repos_total=2)
    worker._kwargs["on_progress"](report)
    assert seen == [report], "a report from the worker reaches the window"
    assert view.run_button.text() == "Stop"


def test_a_name_that_matches_nothing_still_asks_for_one(view) -> None:
    view.input.setText("/repo nothing-like-it CustomerId /history")
    view.start()
    assert "worker" not in view.held
    assert view.summary.text().startswith("Name a repository first")


def test_rows_are_drawn_as_they_are_reported_newest_first(view) -> None:
    view.input.setText("CustomerId /history")
    view.start()
    live = view._generation
    git_tree.draw_git_progress(view, progress(
        commit("a", "2026-09-10", "leasha new", "leasha", "D:/SearchProject")), live)
    assert names(view) == ["leasha new"]
    assert view.summary.text() == (
        "Searching history… 1 found so far  ·  0 of 2 repositories searched")

    git_tree.draw_git_progress(view, progress(
        commit("b", "2026-09-20", "tools newer", "my tools", "D:/code/my tools"),
        commit("c", "2026-01-01", "tools old", "my tools", "D:/code/my tools"),
        found=3, done=1), live)
    assert names(view) == ["tools newer", "leasha new", "tools old"]
    assert "3 found so far" in view.summary.text() and "1 of 2" in view.summary.text()
    assert view.run_button.text() == "Stop", "still running until the result arrives"


def test_the_selected_row_stays_selected_and_is_not_announced_again(view) -> None:
    view.input.setText("CustomerId /history")
    view.start()
    live = view._generation
    git_tree.draw_git_progress(view, progress(
        commit("a", "2026-09-10", "leasha new", "leasha", "D:/SearchProject")), live)
    table = view.results.table
    table.selectRow(0)
    chosen = table.current_row()
    announced: list = []
    table.selected.connect(announced.append)

    git_tree.draw_git_progress(view, progress(
        commit("b", "2026-09-20", "tools newer", "my tools", "D:/code/my tools"),
        found=2, done=1), live)
    assert names(view) == ["tools newer", "leasha new"]
    assert table.current_row() == chosen, "the row being read moved with its row"
    assert announced == [], "the preview was told to start over for a row that had not changed"


def test_a_report_from_a_search_that_was_replaced_is_dropped(view) -> None:
    view.input.setText("CustomerId /history")
    view.start()
    old = view._generation
    first_stop = view._git_stop
    view.input.setText("Other /history")
    view.start()                       # pressing the button while it runs stops it...
    view.start()                       # ...and the next press starts the new one
    assert first_stop.stopped and view._generation != old
    before = names(view)
    git_tree.draw_git_progress(view, progress(commit("a", "2026-09-10", "stale")), old)
    assert names(view) == before and "stale" not in names(view)


def test_the_result_replaces_the_live_rows_and_says_what_failed(view) -> None:
    view.input.setText("CustomerId /history")
    view.start()
    rows = [commit("b", "2026-09-20", "tools newer", "my tools", "D:/code/my tools"),
            commit("a", "2026-09-10", "leasha new", "leasha", "D:/SearchProject")]
    view._show_git(GitSearchResult(
        ok=True, rows=rows, elapsed_s=2.0, repos=2, failed=[("ghost", "fatal: gone")],
        explain="in the last 2,000 commits, across 2 repositories"), view._generation)
    assert names(view) == ["tools newer", "leasha new"]
    assert view.run_button.text() == "Search history"
    assert "2 results" in view.summary.text()
    assert "could not be searched: ghost" in view.summary.text()
    repo_column = [key for key, *_r in __import__(
        "app.ui.widgets.code_results", fromlist=["COLUMNS"]).COLUMNS].index("repo")
    assert view.results.table.item(0, repo_column).text() == "my tools"


def test_stop_keeps_what_had_been_found(view) -> None:
    """A2, with streaming: the list says it was stopped, and keeps its rows."""
    view.input.setText("CustomerId /history")
    view.start()
    view.start()
    assert view._git_stop.stopped
    view._show_git(GitSearchResult(
        ok=False, stopped=True, error="stopped",
        rows=[commit("a", "2026-09-10", "leasha new", "leasha", "D:/SearchProject")]),
        view._generation)
    assert names(view) == ["leasha new"]
    assert "History search stopped" in view.summary.text()
    assert view.summary.text().startswith("1 found before it was stopped")
    assert view.run_button.text() == "Search history"


def test_the_button_comes_back_when_the_search_ends_even_if_the_list_moved_on(view) -> None:
    """Typing a file search while git ran left the button reading Stop for good."""
    view.input.setText("CustomerId /history")
    view.start()
    worker = view.held["worker"]
    view.input.setText("engine")
    view._typed()                                   # an index search: the list moves on
    assert view.run_button.text() == "Stop"
    worker.signals.done.emit()
    assert view.run_button.text() == "Search history"


def test_git_is_never_run_on_the_interface_thread() -> None:
    """The window only ever hands the search to a worker; it never calls it."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(git_tree))
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not called & {"search_repositories", "stream_query", "run_query", "show_commit"}


def test_only_the_newest_are_listed_while_it_runs_and_the_line_says_so(view) -> None:
    from app.ui.presenter import LIVE_ROW_LIMIT

    view.input.setText("/repo leasha CustomerId /history")
    view.start()
    many = [commit(f"{n:08x}", "2026-09-10", f"row {n}", "leasha", "D:/SearchProject")
            for n in range(LIVE_ROW_LIMIT + 50)]
    git_tree.draw_git_progress(view, progress(*many, total=1), view._generation)
    assert view.results.table.rowCount() == LIVE_ROW_LIMIT
    assert names(view)[0] == "row 0", "git prints newest first, so the first are the newest"
    assert f"{LIVE_ROW_LIMIT + 50} found so far" in view.summary.text()
    assert f"the newest {LIVE_ROW_LIMIT} are listed until it finishes" in view.summary.text()

    drawn: list = []
    view._fill = lambda rows: drawn.append(len(rows))
    git_tree.draw_git_progress(view, progress(
        commit("ffffffff", "2026-01-01", "one more", "leasha", "D:/SearchProject"),
        found=LIVE_ROW_LIMIT + 51, total=1), view._generation)
    assert drawn == [], "a row below the ones listed must not redraw the list"
    assert f"{LIVE_ROW_LIMIT + 51} found so far" in view.summary.text()


def test_one_redraw_while_streaming_costs_a_fraction_of_a_second(view) -> None:
    """Measured, not assumed. The list costs about 0.6 ms a row to draw, which is
    why only `LIVE_ROW_LIMIT` rows are listed while git runs."""
    from app.ui.presenter import LIVE_ROW_LIMIT

    rows = [git_result_row(commit(f"{n:08x}", f"2026-{1 + n % 12:02d}-01", f"row {n}",
                                  "leasha", "D:/SearchProject"), "")
            for n in range(LIVE_ROW_LIMIT)]
    git_tree.fill_keeping_selection(view, rows[:1])          # the first draw sizes columns
    started = time.perf_counter()
    git_tree.fill_keeping_selection(view, rows)
    elapsed = time.perf_counter() - started
    print(f"\nredraw of {LIVE_ROW_LIMIT} history rows: {elapsed * 1000:.0f} ms")
    assert view.results.table.rowCount() == LIVE_ROW_LIMIT
    assert elapsed < 0.5
