r"""The main search box reaches both engines, not just the index.

Layer: L4/L5

The owner, twice, and the second time to correct the first reading:

> *"the main search searches every thing no matter what… all the switches in the
> files mail and code should be available in the main search"*
> *"it is not just a switch union it is union of all data as source too"*

The switch half is `app/storage/filters.py`. This is the data half, and it is a
different kind of problem: **a repository's history is not in the index and
cannot be.** It is thousands of versions of files that no longer exist, and
reaching it means running `git`.

Three constraints shape the answer, and each already had a test guarding it
before this feature existed. They are the reason federation lives in
`app/search/federate.py` rather than anywhere more obvious:

* `SearchEngine` must stay git-free - it is what answers a keystroke;
* nothing on the typing path may so much as import `gitsearch`;
* a repository nobody named must not cost a subprocess each.

Every test below is one of those, or one of the shape problems that come from
putting rows into a list that was built for a different kind of row.
"""

from __future__ import annotations

import pytest

from app.search.engine import SearchResult
from app.search.federate import MAX_REPOS, git_hits
from app.search.gitquery import wants_git
from app.search.gitsearch import GitRow, GitSearchResult

REPOS = [
    {"name": "leasha", "root_path": r"D:\code\leasha"},
    {"name": "tools", "root_path": r"D:\code\tools"},
]

CONTENT = GitRow(kind="content", commit="a1b2c3d4e5f6", date="2024-06-01",
                 author="Dave", path="src/OrderService.cs", line_no=42,
                 text="var customerId = order.CustomerId;")
COMMIT = GitRow(kind="commit", commit="ffff1111", date="2023-01-09",
                author="Priya", subject="Remove the legacy CustomerId column")
CHANGE = GitRow(kind="change", commit="bbbb2222", path="src/Old.cs", status="D")


@pytest.fixture()
def ran(monkeypatch):
    """Records what git was asked, and answers without running it."""
    calls: list[tuple] = []

    def fake(root, query, **kwargs):
        calls.append((str(root), query))
        return GitSearchResult(ok=True, rows=[CONTENT, COMMIT, CHANGE])

    monkeypatch.setattr("app.search.gitsearch.run_query", fake)
    return calls


# --- when it runs at all ----------------------------------------------------

@pytest.mark.parametrize("query", [
    "pump station",
    # `after` is an alias of git's `/since`, so this is the case that used to
    # fork a subprocess for an ordinary date filter.
    "type:pdf after:2024",
    "x before:2025",
    "invoice /name invoice /path leeds",
    r"12/03 and D:/docs",                          # not switches, and never were
])
def test_an_ordinary_search_never_reaches_git(query, ran):
    r"""**The whole cost argument.**

    `git log -S` takes seconds and almost every search is not a git search, so
    the question "is this one?" has to be answerable without paying anything.
    `wants_git` is a regex and a dictionary lookup - no parse, no plan, no
    subprocess - and this asserts the consequence rather than the mechanism.
    """
    assert git_hits(REPOS, query) == []
    assert ran == []


@pytest.mark.parametrize("query,expected", [
    ("CustomerId /history", ("history",)),
    ("/class OrderService", ("class",)),
    # `author:` is git's alone and triggers it. `since:` is *not*: the index
    # catalogue claims that spelling as an alias of `after`, so it narrows dates
    # rather than deciding the engine. Once git is running for another reason,
    # `parse_git_query` reads it as `--since` - so it still applies, it simply
    # does not get a vote on whether a subprocess is worth forking.
    ("x author:dave since:2024", ("author",)),
    ("x /branch develop", ("branch",)),
    ("x /committer priya", ("committer",)),
    ("x /introduced", ("introduced",)),
])
def test_a_repository_switch_is_recognised_by_any_spelling(query, expected):
    """Aliases resolve through the git catalogue, except where the index
    catalogue already claims the spelling. Returned as names rather than a bool
    so the window can say which switch is about to make it slow."""
    assert wants_git(query) == expected


def test_a_spelling_both_catalogues_know_belongs_to_the_fast_engine():
    r"""**The collision that would have cost a subprocess per date filter.**

    `after` and `before` are aliases of git's `/since` and `/until`. Resolved
    through the git catalogue alone, `after:2024` - among the commonest things
    anybody types - became a repository switch and forked `git log` on every
    search carrying a date. `widgets/code_commands._merged` had already settled
    this for the menu; this is the same rule applied to the routing.
    """
    assert wants_git("pump after:2024 before:2025") == ()
    assert wants_git("pump type:cs path:src file:main.py repo:leasha") == ()


def test_the_switch_check_is_not_shadowed_by_the_other_token_pattern():
    r"""**A regression, and an instructive one.**

    The first version of `wants_git` compiled its pattern into `_TOKEN` - a name
    `gitquery` already used further down for a whole-word-anchored pattern. The
    later definition won, `findall` matched nothing, and the function answered
    "no git switches here" for every line ever typed. Every test passed; the
    feature simply never ran.
    """
    assert wants_git("CustomerId /history") == ("history",)


# --- what comes back --------------------------------------------------------

def test_rows_arrive_in_the_shape_the_results_list_draws(ran):
    """`SearchResult` or the list cannot draw them. Not a subclass, not a
    lookalike - the same class, so nothing downstream needs a branch."""
    hits = git_hits(REPOS, "CustomerId /history", limit=3)

    assert hits and all(isinstance(hit, SearchResult) for hit in hits)
    assert all(isinstance(hit.text, str) and hit.text for hit in hits)


def test_every_federated_row_has_its_own_identity(ran):
    r"""**`group_results` keys rows by `file_id`.**

    So a hundred history hits all carrying the default zero would collapse into
    a single group with ninety-nine hidden inside it - which reads as git having
    found one thing. Negative because every real file id is positive, which also
    makes a federated row recognisable anywhere downstream without a second
    field to consult.
    """
    hits = git_hits(REPOS, "CustomerId /history", limit=6)

    ids = [hit.file_id for hit in hits]
    assert len(set(ids)) == len(ids)
    assert all(one < 0 for one in ids)


def test_a_row_says_where_it_came_from(ran):
    r"""*"keyword match"* would be a claim about a retriever that never saw this
    row. The label carries the commit, author and date, which is the only thing
    that makes a historical hit identifiable at all."""
    first = git_hits(REPOS, "CustomerId /history", limit=1)[0]

    assert first.explain().startswith("repository history")
    assert "a1b2c3d4" in first.explain() and "Dave" in first.explain()


def test_an_index_result_still_explains_itself_as_before():
    """The label is empty for everything the engine produces, so nothing about
    an ordinary result changed."""
    ordinary = SearchResult(chunk_id=1, file_id=1, path="a.txt", text="x",
                            score=0.5, rank=1, sources=(0, 1))

    assert ordinary.explain() == "keyword and meaning both matched"


def test_a_commit_with_no_matching_line_still_has_something_to_show(ran):
    """A commit row carries only a subject and a change row carries neither -
    and `build_snippet` runs a regex over whatever this is, so an empty string
    is a row that cannot draw."""
    texts = [hit.text for hit in git_hits(REPOS, "x /history", limit=3)]

    assert "Remove the legacy CustomerId column" in texts
    assert any(text.startswith("D ") for text in texts)


def test_paths_are_joined_without_mixing_separators(ran):
    r"""**The seventh sighting of this trap in this project.**

    Git reports `src/OrderService.cs`; the rest of the application stores
    `D:\code\leasha`. `Path(root) / relative` on Linux produces
    `D:\code\leasha/src/OrderService.cs` - two separators in one path - because
    `pathlib` there has no idea a backslash divides anything.
    """
    first = git_hits(REPOS, "CustomerId /history", limit=1)[0]

    assert first.path == r"D:\code\leasha\src\OrderService.cs"
    assert "/" not in first.path


def test_ranks_continue_from_the_index_half(ran):
    """The two halves are drawn as one numbered list, so the git rows must not
    restart at 1 half way down it."""
    hits = git_hits(REPOS, "x /history", limit=3, start_rank=11)

    assert [hit.rank for hit in hits] == [11, 12, 13]


def test_federated_rows_do_not_claim_a_relevance_score(ran):
    """git returns matches, not ranks. Interleaving them among scored results on
    a made-up number would be a claim about relevance that nothing supports."""
    assert all(hit.score == 0.0 for hit in git_hits(REPOS, "x /history", limit=3))


# --- which repositories -----------------------------------------------------

def test_with_no_repository_named_every_known_one_is_searched(ran):
    git_hits(REPOS, "CustomerId /history", limit=99)

    assert [root for root, _query in ran] == [r"D:\code\leasha", r"D:\code\tools"]


def test_a_named_repository_narrows_it(ran):
    """Substring and case-insensitive, the way `repo_root_for` matches, because
    people type half a project name."""
    git_hits(REPOS, "CustomerId /history /repo tools", limit=99)

    assert [root for root, _query in ran] == [r"D:\code\tools"]


def test_the_number_of_repositories_is_capped(ran):
    r"""**A cap, not a preference.** Each repository is at least one subprocess
    and possibly several seconds; forty checkouts would turn one query into a
    minute of forking."""
    many = [{"name": f"r{n}", "root_path": rf"D:\code\r{n}"} for n in range(MAX_REPOS + 6)]

    git_hits(many, "x /history", limit=9_999)

    assert len(ran) == MAX_REPOS


def test_the_row_limit_is_honoured_across_repositories(ran):
    assert len(git_hits(REPOS, "x /history", limit=4)) == 4


# --- failure is one repository, never the search ----------------------------

def test_a_repository_that_fails_does_not_take_the_search_with_it(monkeypatch):
    def sometimes(root, query, **kwargs):
        if "leasha" in str(root):
            raise OSError("git is not installed")
        return GitSearchResult(ok=True, rows=[CONTENT])

    monkeypatch.setattr("app.search.gitsearch.run_query", sometimes)

    hits = git_hits(REPOS, "x /history", limit=9)

    assert len(hits) == 1
    assert "tools" in hits[0].path


def test_an_unsuccessful_run_contributes_nothing_and_says_so(monkeypatch):
    monkeypatch.setattr(
        "app.search.gitsearch.run_query",
        lambda root, query, **kwargs: GitSearchResult(ok=False, error="not a repository"))

    assert git_hits(REPOS, "x /history") == []


def test_a_repository_with_no_root_is_skipped(ran):
    """A detected-but-unindexed repository can have an empty root. Handing that
    to a subprocess as its working directory is how a search dies in a way
    nobody can read."""
    git_hits([{"name": "ghost", "root_path": ""}], "x /history")

    assert ran == []


# --- the guards that shaped the design --------------------------------------

def test_the_engine_still_cannot_reach_git():
    r"""`test_the_index_search_engine_cannot_reach_git_even_indirectly` says the
    same thing from the other side. Restated here because *this* is the feature
    that would have been tempted to break it: federating into `SearchEngine`
    would have been the obvious place, and it is the wrong one - the engine is
    what answers a keystroke.
    """
    import ast

    import app.search.engine as engine_module

    with open(engine_module.__file__ or "", encoding="utf-8") as handle:
        tree = ast.parse(handle.read())

    # Asserted on the *imports*, not on the text. The first version of this
    # searched the source for the word and failed on a comment explaining why
    # federation is elsewhere - a test that forbids describing the design is not
    # protecting it.
    imported = [
        ast.unparse(node) for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]

    assert not [line for line in imported if "gitsearch" in line or "federate" in line]


def test_importing_the_federator_does_not_import_git():
    r"""**Why `gitsearch` is imported inside the function.**

    `app/ui/search_view.py` runs on the typing path and now refers to this
    module. If importing it pulled `gitsearch` in, the guard listing the hot
    modules would be satisfied by the letter and defeated in substance. The
    import is deferred so that it is not.
    """
    import subprocess
    import sys

    probe = (
        "import sys; import app.search.federate; "
        "print('app.search.gitsearch' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, cwd=str(_project_root()))

    assert out.stdout.strip() == "False", out.stderr


def _project_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[2]
