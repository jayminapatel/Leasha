r"""The repository pane: a branch is a place the index cannot go.

Layer: L4 / L5

Asked for as *"a git view option by which the view changes to a two pane view
git on the left and the file list on the right"*, with the invitation to
propose something better than a folder tree - which this takes, because a
folder tree duplicates Explorer.

**The reason the pane earns its place is one fact**: the index holds the
*working tree*. A file deleted on `main` but alive on a feature branch has no
row in `files`, and one that only ever existed on a branch never had one. So
"list the files as of this branch" is a question only git can answer, and the
tests below are mostly about the two engines producing one shape and honouring
the same switches.

*"dont forget the code switches apply there too"* - the tree says where to
look, the box says what to look for, and neither overrides the other.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.search.gitsearch import GitRow, commit_files, tree_files
from app.ui.presenter import GitScope, code_rows_for, code_summary, git_rows_matching


def runner(**replies):
    """A fake git. The seam every reader in `gitsearch` already takes."""
    def run(args, cwd, timeout):
        for needle, out in replies.items():
            if needle in args:
                return 0, out, ""
        return 0, "", ""
    return run


# --- the two new readers ----------------------------------------------------

def test_a_branch_lists_the_files_as_of_that_branch():
    rows = tree_files(Path("/repo"), "main", runner=runner(**{
        "ls-tree": "src/Order.cs\nREADME.md\n"}))

    assert [row.path for row in rows] == ["src/Order.cs", "README.md"]
    assert all(row.kind == "tree" and row.commit == "main" for row in rows)


def test_a_commit_lists_what_it_did_to_each_file():
    """`--name-status` through the reader `/changed` already uses, so a commit
    picked in the tree and one named with a switch produce identical rows."""
    rows = commit_files(Path("/repo"), "3b29b7f", runner=runner(**{
        "show": "M\tsrc/Order.cs\nA\tsrc/New.cs\nD\told.py\n"}))

    assert [(row.status, row.path) for row in rows] == [
        ("M", "src/Order.cs"), ("A", "src/New.cs"), ("D", "old.py")]
    assert all(row.commit == "3b29b7f" for row in rows)


def test_an_empty_ref_asks_git_nothing():
    """A tree drawn before a selection would otherwise run `ls-tree ''`."""
    calls: list = []

    def watching(args, cwd, timeout):
        calls.append(args)
        return 0, "", ""

    assert tree_files(Path("/repo"), "  ", runner=watching) == []
    assert commit_files(Path("/repo"), "", runner=watching) == []
    assert calls == []


def test_git_that_is_not_there_is_an_empty_list_not_a_crash():
    """This runs from a selection change. A dialog for arrowing onto a row of
    a repository that has been moved would be absurd."""
    def broken(args, cwd, timeout):
        raise OSError("git is not installed")

    assert tree_files(Path("/repo"), "main", runner=broken) == []
    assert commit_files(Path("/repo"), "abc", runner=broken) == []


def test_a_ref_that_no_longer_exists_is_empty_rather_than_an_error():
    def refused(args, cwd, timeout):
        return 128, "", "fatal: not a valid object name"

    assert tree_files(Path("/repo"), "deleted-branch", runner=refused) == []


# --- the switches, on top of the scope --------------------------------------

ROWS = [GitRow(path=p) for p in (
    "src/OrderService.cs", "README.md", "Makefile", "build/main.tf",
    "docs/order.pdf")]


def test_free_text_narrows_a_branch_listing():
    """`ls-tree` cannot filter, so this is a pass over the listing - which is
    why it is capped before it arrives."""
    kept = git_rows_matching(ROWS, text="order")

    assert [r.path for r in kept] == ["src/OrderService.cs", "docs/order.pdf"]


def test_a_typed_type_beats_the_configured_code_types():
    """The same precedence as the index path, so a branch and the working tree
    filter identically. Naming a type is an instruction."""
    kept = git_rows_matching(ROWS, extensions=["pdf"], types=["cs"])

    assert [r.path for r in kept] == ["docs/order.pdf"]


def test_the_configured_types_apply_when_nothing_was_typed():
    kept = git_rows_matching(ROWS, types=["cs", "tf", "makefile"])

    assert [r.path for r in kept] == [
        "src/OrderService.cs", "Makefile", "build/main.tf"]


def test_a_named_file_is_matched_by_its_name_not_a_suffix():
    """`Makefile` has no extension and is a type - the same rule
    `source_types.indexed_ext` applies when the file is indexed."""
    assert [r.path for r in git_rows_matching(ROWS, types=["makefile"])] == ["Makefile"]


def test_no_filter_at_all_keeps_everything():
    assert len(git_rows_matching(ROWS)) == len(ROWS)


# --- one shape from two engines ---------------------------------------------

class FakeStore:
    """Only what `code_rows_for` reaches for."""

    def __init__(self):
        self.asked: list = []

    def get_state(self, key, default=""):
        return ""

    def code_files(self, text, *, repo="", ext=None, limit=500):
        self.asked.append({"text": text, "repo": repo, "ext": ext})
        return [{"path": r"D:\Repo\leasha\Indexed.cs", "ext": "cs",
                 "repo": "leasha", "size_bytes": 10, "mtime_ns": 1,
                 "status": "INDEXED"}]


class Route:
    def __init__(self, text="", extensions=(), repo=""):
        self.text, self.extensions, self.repo = text, extensions, repo


def test_a_repository_scope_uses_the_index(monkeypatch):
    """Instant, and the only thing that should ever run behind a keystroke."""
    store = FakeStore()

    rows = code_rows_for(store, GitScope(kind="repo", repo="leasha"), Route())

    assert store.asked and store.asked[0]["repo"] == "leasha"
    assert rows[0]["status"] == "INDEXED"


def test_a_branch_scope_filters_what_the_pane_already_read():
    r"""**The guard that reshaped this feature.**

    The first version fetched the branch inside `code_rows_for`, which runs on
    the typing debounce - so every keystroke shelled out to `git ls-tree`.
    `test_nothing_that_runs_on_a_keystroke_imports_this` refused it, and was
    right: the listing does not change while somebody types.

    So the pane reads it once per selection and hands it in. This asserts the
    half that matters - given a listing, the store is never asked at all.
    """
    store = FakeStore()
    cached = [{"path": r"D:\Repo\leasha\src\Order.cs", "ext": "cs"},
              {"path": r"D:\Repo\leasha\notes.pdf", "ext": "pdf"}]

    rows = code_rows_for(
        store, GitScope(kind="branch", repo="leasha", ref="main"),
        Route(extensions=["cs"]), cached=cached)

    assert store.asked == [], "a cached scope must not query the index"
    assert [row["path"] for row in rows] == [r"D:\Repo\leasha\src\Order.cs"]


def test_the_cached_rows_may_be_mappings_or_objects():
    r"""**Both shapes, because reading only attributes filtered every mapping
    out** - which looked exactly like a branch with no files in it, and did.

    `repo_file_rows` takes either for the same reason: which one arrives is an
    implementation detail of whichever reader ran.
    """
    from app.search.gitsearch import GitRow

    as_objects = git_rows_matching([GitRow(path="src/Order.cs")], text="order")
    as_mappings = git_rows_matching([{"path": "src/Order.cs"}], text="order")

    assert len(as_objects) == len(as_mappings) == 1


def test_the_pane_shapes_a_branch_into_the_tables_own_rows(monkeypatch):
    """One shape from two engines, so the table, the preview and the row menu
    stay ignorant of which one answered."""
    from app.search.gitsearch import GitRow
    from app.ui.widgets import git_tree

    monkeypatch.setattr("app.search.gitsearch.tree_files",
                        lambda root, ref, **kw: [GitRow(path="src/Order.cs"),
                                                 GitRow(path="Makefile")])

    rows = git_tree.scope_rows(
        GitScope(kind="branch", repo="leasha", root="/repo", ref="main"))

    assert [row["ext"] for row in rows] == ["cs", "makefile"]
    # Joined to the repository root: `ls-tree` reports a relative path, and the
    # preview pane would report every row as missing.
    assert all(row["path"].startswith("/repo") for row in rows)
    # A branch listing has nothing to say about index status, and "INDEXED"
    # would be a claim.
    assert all(row["status"] == "" for row in rows)


def test_a_commit_keeps_its_letters_through_the_pane(monkeypatch):
    from app.search.gitsearch import GitRow
    from app.ui.widgets import git_tree

    monkeypatch.setattr("app.search.gitsearch.commit_files",
                        lambda root, sha, **kw: [GitRow(path="a.cs", status="M"),
                                                 GitRow(path="b.cs", status="A")])

    rows = git_tree.scope_rows(
        GitScope(kind="commit", repo="leasha", root="/repo", ref="abc"))

    assert [row["status"] for row in rows] == ["M", "A"]


def test_the_pane_reads_nothing_for_an_index_scope():
    """A repository is answered in milliseconds by the index; sending it to a
    subprocess is the routing mistake `code_route` exists to avoid."""
    from app.ui.widgets import git_tree

    assert git_tree.scope_rows(GitScope(kind="repo", repo="leasha")) == []
    assert git_tree.scope_rows(GitScope()) == []


# --- saying what is on screen ------------------------------------------------

@pytest.mark.parametrize("scope,expected", [
    (GitScope(), "every repository"),
    (GitScope(kind="repo", repo="leasha"), "leasha · working tree"),
    (GitScope(kind="worktree", repo="leasha"), "leasha · working tree"),
    (GitScope(kind="branch", repo="leasha", ref="main"), "leasha · branch main"),
    (GitScope(kind="commit", repo="leasha", ref="3b29b7f", subject="types"),
     "leasha · commit 3b29b7f · types"),
])
def test_a_scope_says_what_it_is(scope, expected):
    assert scope.describe() == expected


def test_the_summary_names_the_scope():
    r"""**A list narrowed to a branch with nothing saying so is the same
    failure as an archive skipped in silence**: the numbers look ordinary and
    mean something else. Worse here, because git answers from the repository
    while "48,000 indexed" beside it comes from the index.
    """
    line = code_summary(
        [{}, {}], [{"files": 48_000}],
        GitScope(kind="branch", repo="leasha", ref="main"))

    assert "branch main" in line
    assert "2 files" in line


def test_the_summary_is_unchanged_without_a_scope():
    """The flat list is what the tab was, and must read as it did."""
    line = code_summary([{}], [{"files": 10}])

    assert "branch" not in line
    assert line.startswith("1 file")


def test_only_a_branch_or_a_commit_needs_git():
    """The index answers a repository in milliseconds; sending that to a
    subprocess would be the routing mistake `code_route` exists to avoid."""
    assert not GitScope(kind="repo").from_git
    assert not GitScope(kind="worktree").from_git
    assert not GitScope().from_git
    assert GitScope(kind="branch").from_git
    assert GitScope(kind="commit").from_git
