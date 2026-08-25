r"""One box, two engines: which one answers, and what it is given.

Layer: L5 (the decision) — no Qt, so it runs anywhere

**Corrected by the owner**: *"the code search page is all wrong it should be a
combined one search box with the git code files in the list"*. It had been a
tree of repositories above a separate git search box, which made somebody
choose an engine before they had a question.

So the grammar chooses. Everything about that choice is here rather than in the
widget, for the reason every UI decision in this project ends up in
`presenter.py`: a decision made inside a Qt view is one nobody can test without
a display, and this project has already shipped UI logic that was verified by
reading and crashed on the first run.

**The direction of the mistake is not symmetric.** Routing a git query to the
index costs an empty list and a confused person. Routing an index query to git
costs a subprocess that diffs every commit in the repository - two seconds and a
spinning window - for a question that had a 3ms answer. So the router only
reaches for git when a switch *only git has* is present, and `/type`, `/repo`,
`/path` and `/file` - which both catalogues share and which are the commonest
things anybody types here - stay on the fast path.
"""

from __future__ import annotations

import pytest

from app.ui.presenter import (
    GIT_ONLY,
    code_route,
    code_summary,
    git_result_row,
    git_summary,
    repo_root_for,
)

REPOS = [
    {"name": "leasha", "root_path": r"D:\GIT\leasha", "files": 480},
    {"name": "tools", "root_path": r"D:\GIT\tools", "files": 20},
]


# --- which engine -----------------------------------------------------------

def test_an_empty_box_is_the_index_showing_everything():
    """Not "nothing". The list is what you have; it is the starting state."""
    route = code_route("")

    assert route.engine == "index"
    assert route.text == ""


def test_a_plain_word_is_an_index_lookup():
    assert code_route("OrderService").engine == "index"


@pytest.mark.parametrize("text", [
    "x /history",
    "x /branch develop",
    "x /introduced",
    "x /author dave",
    "/class OrderService",
    "x /commit a1b2c3d",
    "x /file-history src/a.py",
])
def test_a_git_only_switch_routes_to_git(text):
    assert code_route(text).engine == "git", text


@pytest.mark.parametrize("text", [
    "order /type cs",
    "order type:cs",
    "/repo leasha",
    "order /path src",
    "order /name Service",
])
def test_a_shared_switch_stays_on_the_fast_path(text):
    """**The one that matters.** `/type` is the commonest thing typed here, and
    both catalogues have it. Routing it to git would turn a 3ms lookup into a
    subprocess that diffs every commit."""
    assert code_route(text).engine == "index", text


def test_an_alias_counts_as_its_switch():
    """`/hist` and `/by` are `history` and `author`. A spelling that works in
    the box and not in the router is the drift a shared catalogue prevents."""
    assert code_route("x /hist").engine == "git"
    assert code_route("x /by dave").engine == "git"


def test_it_says_which_switch_made_it_slow():
    """The line above the results has to name it, or "press Enter" reads as the
    application refusing to search."""
    assert "/history" in code_route("x /history").because


def test_several_git_switches_are_all_named():
    because = code_route("x /history /removed-only").because

    assert "/history" in because and "/removed-only" in because


def test_the_index_route_says_nothing_about_why():
    """There is nothing to explain: it already ran."""
    assert code_route("order").because == ""


def test_an_unknown_switch_is_text_not_an_engine_change():
    """`/api/orders` is a route somebody is searching for. The index answers
    it; sending it to git because it starts with a slash would be the search
    box rewriting what was typed."""
    route = code_route("/api/orders")

    assert route.engine == "index"


# --- what the index is given ------------------------------------------------

def test_the_free_text_is_separated_from_the_switches():
    route = code_route("/repo leasha OrderService /type cs")

    assert route.text == "OrderService"
    assert route.repo == "leasha"
    assert route.extensions == ("cs",)


def test_several_types_come_through():
    assert code_route("x type:cs,ts").extensions == ("cs", "ts")


def test_a_half_typed_line_does_not_raise():
    """Parsed on every keystroke."""
    for text in ("/", "/re", "/repo", 'x /author "unclosed', "//", "x /"):
        assert code_route(text) is not None


# --- the catalogue split ----------------------------------------------------

def test_the_shared_switches_are_deliberately_absent_from_the_git_only_set():
    """If one of these ever appears in `GIT_ONLY`, the fast path is gone and
    nothing else in the suite would notice."""
    for name in ("repo", "type", "path", "file", "name", "extension"):
        assert name not in GIT_ONLY, (
            f"'{name}' is in both catalogues; routing on it sends an index "
            f"lookup into a git subprocess")


def test_every_git_only_switch_is_a_switch_git_actually_has():
    """A name here that git's catalogue does not carry is a route to nowhere:
    the router would send the query to git and git would ignore it."""
    from app.search.gitquery import git_command_for

    unknown = sorted(name for name in GIT_ONLY if git_command_for(name) is None)

    assert unknown == []


# --- pointing git at a repository -------------------------------------------

def test_the_named_repository_wins():
    assert repo_root_for(REPOS, "tools") == r"D:\GIT\tools"


def test_a_partial_name_is_enough():
    """People type part of it, the same way `/from dave` matches an address."""
    assert repo_root_for(REPOS, "leash") == r"D:\GIT\leasha"


def test_one_repository_and_no_name_is_unambiguous():
    """With exactly one, that is plainly the one meant, and asking would be
    asking a question with one possible answer."""
    assert repo_root_for(REPOS[:1], "") == r"D:\GIT\leasha"


def test_several_repositories_and_no_name_is_a_question_not_a_guess():
    """**Picking the first would be a guess presented as an answer.** The
    caller shows "name a repository" instead, which is true and actionable."""
    assert repo_root_for(REPOS, "") == ""


def test_no_repositories_at_all():
    assert repo_root_for([], "") == ""
    assert repo_root_for([], "leasha") == ""


# --- git results as rows ----------------------------------------------------

def _row(**fields):
    from app.search.gitsearch import GitRow

    return GitRow(**fields)


def test_a_hit_in_the_checkout_can_be_opened_and_previewed():
    row = git_result_row(
        _row(kind="content", path="src/a.cs", line_no=4, text="x = 1"),
        r"D:\GIT\leasha")

    assert row.name == "a.cs"
    assert row.path == "src/a.cs:4"
    assert row.full_path.endswith("a.cs")


def test_a_historical_hit_has_no_file_to_open():
    """**The version that matched no longer exists on disk.** Handing the
    preview a path that is not there shows "file missing" for every history
    result, which reads as a broken preview rather than as a file that is
    genuinely gone."""
    row = git_result_row(
        _row(kind="commit", commit="abcdef1234", date="2025-01-01",
             author="Dave", subject="Fix the lookup"),
        r"D:\GIT\leasha")

    assert row.full_path == ""
    assert row.name == "Fix the lookup"


def test_a_commit_row_carries_its_text_for_the_preview():
    row = git_result_row(
        _row(kind="commit", commit="abcdef1234", date="2025-01-01",
             author="Dave", subject="Fix the lookup"), "")

    assert "Fix the lookup" in row.preview_text
    assert "Dave" in row.preview_text


def test_a_history_row_has_no_size():
    """A number invented for the column is a number somebody believes."""
    assert git_result_row(_row(kind="commit", commit="abc1234"), "").size == ""


# --- the line above the results ---------------------------------------------

def test_the_file_summary_gives_both_numbers():
    """"40 files" over a corpus of 48,000 and over one of 40 mean different
    things, and only one of them means "narrow it"."""
    summary = code_summary([object()] * 40, REPOS)

    assert "40 files" in summary
    assert "2 repositories" in summary
    assert "500" in summary


def test_one_repository_reads_as_one():
    assert "1 repository" in code_summary([], REPOS[:1])


class _Found:
    ok = True
    explain = "in the last 2,000 commits"
    elapsed_s = 2.26
    command = ("git", "log", "-Sx")

    def __init__(self, rows, truncated=False):
        self.rows = rows
        self.truncated = truncated


def test_the_git_summary_says_what_was_searched_and_what_it_cost():
    summary = git_summary(_Found([object(), object()]))

    assert "2 results" in summary
    assert "2,000 commits" in summary
    assert "2.26s" in summary


def test_a_truncated_git_result_says_so():
    """A capped list that does not say it was capped is a wrong answer."""
    assert "limit" in git_summary(_Found([object()], truncated=True))


def test_no_matches_shows_the_command():
    """So a surprising nothing can be reproduced by hand - and so "no matches"
    is attributable to a search rather than to the repository."""
    assert "git log -Sx" in git_summary(_Found([]))
