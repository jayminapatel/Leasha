r"""The repository tree on the Code tab keeps what was opened when it is redrawn.

Layer: L5

2026-10-05, the owner: *"on the code page the git is not expanding"*.
Reproduced on the real window: clicking the arrow beside a repository, or
double-clicking it, did nothing visible.

The cause was a loop. Every search result redraws the tree, to show only the
repositories that match (`draw_matches` -> `show_repos`). The redraw cleared
the tree, which collapsed it, and then selected "All repositories". That
selection told the view to search again, and the new result redrew the tree
again. A repository that was opened was closed again before anybody saw it,
and a branch that was chosen went back to "All repositories".

What these pin: a redraw keeps the open repositories open with what git
already said about them, keeps the selection, and starts no search. The
selection falls back to "All repositories", and says so, only when the item
that was chosen is no longer in the tree.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PyQt6")

from app.ui.presenter import GitScope  # noqa: E402
from app.ui.widgets.git_tree import ROLE_SCOPE, GitTree  # noqa: E402

pytestmark = pytest.mark.gui

REPOS = [{"name": "JT_Template", "root_path": r"D:\JEFF\JT_Template", "files": 52},
         {"name": "Leasha", "root_path": r"D:\Local\GitHub\SearchProject", "files": 900}]
PAYLOAD = {"branches": ["main", "dev"], "commits": ["3b29b7f  first", "a1b2c3d  second"]}


def _tree(qtbot, monkeypatch) -> tuple[GitTree, list]:
    from app.ui.widgets import git_tree

    reads: list[str] = []

    # The git read is a subprocess on a worker; here it answers at once, on
    # this thread, through the tree's own `_expanded` and `_read_done`.
    def read_repo(root: str) -> dict:
        reads.append(root)
        return PAYLOAD

    monkeypatch.setattr(git_tree, "_read_repo", read_repo)
    monkeypatch.setattr(git_tree, "run", lambda _pool, worker: worker.run())
    tree = GitTree()
    qtbot.addWidget(tree)
    tree.show_repos(REPOS)
    return tree, reads


def _repo_item(tree: GitTree, name: str):
    for index in range(tree.topLevelItemCount()):
        item = tree.topLevelItem(index)
        if item.data(0, ROLE_SCOPE).repo == name:
            return item
    return None


def _find(tree: GitTree, scope: GitScope):
    stack = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    while stack:
        item = stack.pop()
        if item.data(0, ROLE_SCOPE) == scope:
            return item
        stack.extend(item.child(i) for i in range(item.childCount()))
    return None


def test_a_redraw_keeps_an_open_repository_open(qtbot, monkeypatch) -> None:
    tree, reads = _tree(qtbot, monkeypatch)
    _repo_item(tree, "JT_Template").setExpanded(True)
    assert reads == [r"D:\JEFF\JT_Template"]

    tree.show_repos(REPOS, {"JT_Template", "Leasha"})

    item = _repo_item(tree, "JT_Template")
    assert item.isExpanded()
    labels = [item.child(i).text(0) for i in range(item.childCount())]
    assert labels == ["Working tree", "Branches (2)", "Recent commits (2)"]
    # Refilled from what git already said, not read a second time.
    assert reads == [r"D:\JEFF\JT_Template"]


def test_a_redraw_keeps_the_choice_and_starts_no_search(qtbot, monkeypatch) -> None:
    tree, _reads = _tree(qtbot, monkeypatch)
    _repo_item(tree, "JT_Template").setExpanded(True)
    branch = GitScope(kind="branch", repo="JT_Template",
                      root=r"D:\JEFF\JT_Template", ref="dev")
    tree.setCurrentItem(_find(tree, branch))
    emitted: list = []
    tree.scoped.connect(lambda scope, rows: emitted.append(scope))

    tree.show_repos(REPOS, {"JT_Template"})

    assert tree.currentItem().data(0, ROLE_SCOPE) == branch
    assert emitted == []


def test_a_redraw_that_hides_the_choice_falls_back_and_says_so(qtbot, monkeypatch) -> None:
    tree, _reads = _tree(qtbot, monkeypatch)
    tree.setCurrentItem(_repo_item(tree, "Leasha"))
    emitted: list = []
    tree.scoped.connect(lambda scope, rows: emitted.append(scope))

    tree.show_repos(REPOS, {"JT_Template"})

    assert tree.currentItem().data(0, ROLE_SCOPE) == GitScope()
    assert emitted == [GitScope()]


def test_the_first_draw_starts_no_search(qtbot) -> None:
    """The view searches straight after it shows the tree; a second search
    from the tree's own first selection is the loop's first turn."""
    tree = GitTree()
    qtbot.addWidget(tree)
    emitted: list = []
    tree.scoped.connect(lambda scope, rows: emitted.append(scope))
    tree.show_repos(REPOS)
    assert tree.currentItem().data(0, ROLE_SCOPE) == GitScope()
    assert emitted == []


def test_a_read_that_lands_after_a_redraw_fills_the_new_row(qtbot, monkeypatch) -> None:
    """The read held the row it started from; a redraw replaced that row, and
    the new one sat on "Reading…" for ever."""
    from app.ui.widgets import git_tree

    held: list = []
    monkeypatch.setattr(git_tree, "_read_repo", lambda root: PAYLOAD)
    monkeypatch.setattr(git_tree, "run", lambda _pool, worker: held.append(worker))
    tree = GitTree()
    qtbot.addWidget(tree)
    tree.show_repos(REPOS)
    _repo_item(tree, "JT_Template").setExpanded(True)

    tree.show_repos(REPOS, {"JT_Template"})
    held.pop().run()

    item = _repo_item(tree, "JT_Template")
    assert item.isExpanded()
    assert item.child(0).text(0) == "Working tree"
