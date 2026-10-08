r"""The repository tree beside the Code list.

Layer: L6 (UI), driving L4

Asked for: *"add a git view option by which the view changes to a two pane view
git on the left and the file list on the right"* - with the invitation to
propose something better than a folder tree, which this takes.

**A folder tree would duplicate Explorer.** What only this application can offer
is scoping by *git*: a branch, a commit, the working tree. So the left pane is

    ▾ leasha
      ▾ Branches          main, feature/x …
      ▾ Recent commits    3b29b7f  389 source and code types …
        Working tree

and selecting a node narrows the list on the right. Two things follow from that
which a folder tree would not have given:

* **The index cannot answer a branch.** It holds the working tree - a file
  deleted on `main` but alive on a feature branch has no row at all. Selecting a
  branch reads `git ls-tree`, which is the only thing that can answer it.
* **It makes `/branch`, `/history` and `/commit` discoverable.** Those switches
  have worked since the git backend landed and are reachable only by knowing to
  type them. This codebase keeps finding features that were built, shipped and
  invisible; a tree of branches is the same capability with a way in.

**Everything git is loaded lazily and off the interface thread.** A repository's
branches and commits are two subprocesses; doing that for every repository when
the pane opens would freeze the window on a machine with twenty checkouts. The
repositories themselves come from the index and are instant.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem, QWidget

from app.core.logging import logger
from app.ui.presenter import GitScope
from app.ui.view_options import weak_slot
from app.ui.workers import CallableWorker, _emit, run

__all__ = ["GitTree", "attach_git_tree", "scope_rows", "GIT_VIEW_HINT",
           "ROLE_SCOPE", "paint_repo_state", "draw_git_progress",
           "fill_keeping_selection"]

_log = logger.bind(component="ui.gittree")

#: Where a node keeps its `GitScope`. `UserRole + 100`, matching
#: `result_table.ROLE_ROW` - the collision that broke Mail sorting was two
#: files independently choosing `UserRole + 1`.
ROLE_SCOPE = int(Qt.ItemDataRole.UserRole) + 100

#: Branches and commits offered per repository. Enough to find what you want,
#: few enough that the subprocess stays under a second and the tree stays a
#: tree rather than a log viewer.
BRANCH_LIMIT = 60
COMMIT_LIMIT = 40

#: On the button that reveals the pane. It names the thing the index genuinely
#: cannot do, which is the reason the pane exists at all.
GIT_VIEW_HINT = (
    "Show the repositories, their branches and their recent commits.\n\n"
    "Selecting a branch lists the files as of that branch — which the index "
    "cannot answer, because it holds the working tree. The switches still "
    "apply on top of whatever is selected."
)


class GitTree(QTreeWidget):
    """Repositories, their branches and their recent commits."""

    #: `(GitScope, rows or None)`. **One signal carrying both**, because a
    #: branch has to be *read* before it can be listed and the read is a
    #: subprocess: the view would otherwise have to own a fetch, a cache and
    #: the rule about which scopes need one. `None` means "ask the index".
    scoped = Signal(object, object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setAccessibleName("Repositories, branches and commits")
        self.setUniformRowHeights(True)
        #: Repositories whose git has already been read, so expanding twice
        #: does not run four subprocesses.
        self._loaded: set[str] = set()
        #: What git said about each repository, by root, so a redraw can reopen
        #: one without reading it again (2026-10-05, see `show_repos`).
        self._read: dict[str, dict] = {}
        self.itemExpanded.connect(self._expanded)
        # `currentItemChanged`, not `itemClicked`: the keyboard moves the current
        # row too, and a branch chosen by arrow key must scope the list as well.
        self.currentItemChanged.connect(
            lambda item, _previous: self._chosen(item))

    # -- filling it in -------------------------------------------------------

    def show_repos(self, repos: Any, matching: Any = None) -> None:
        r"""Draw the repositories. Instant: they come from the index.

        `matching` is the set of names holding a match, or `None` for *"nothing
        was asked, show everything"*. The tree used to be given the full list
        and draw it whatever was typed, so a term matching files in one checkout
        left the other three sitting there as though they matched too - and a
        tree is read as *"these are the repositories that have what you asked
        for"*.

        **Hidden, not greyed.** A tree of four with three inert rows is the
        noise being removed, not a different arrangement of it. The count above
        it says how many were left out, so a shrunken tree is never silent.
        """
        # 2026-10-05: **a redraw keeps what was opened and chosen, and asks
        # for no search.** Every search result redraws the tree (`draw_matches`).
        # This used to clear it, which collapsed every open repository, then
        # select "All repositories", which told the view to search again - so a
        # repository that was opened was closed again before anybody saw it
        # ("the git is not expanding"). What git said about an open repository
        # is kept in `_read`, so opening it again costs no subprocess.
        current = self.currentItem()
        chosen = current.data(0, ROLE_SCOPE) if current is not None else None
        opened = {item.data(0, ROLE_SCOPE).root
                  for item in self._repo_items() if item.isExpanded()}
        self.blockSignals(True)
        try:
            self._redraw(repos, matching, opened)
            # The new item for what was chosen, if it is still there.
            kept = self._item_for(chosen) if isinstance(chosen, GitScope) else None
            self.setCurrentItem(kept or self.topLevelItem(0))
        finally:
            self.blockSignals(False)
        # Only a choice that has gone from the tree is a change worth a search.
        if isinstance(chosen, GitScope) and chosen != GitScope() and kept is None:
            self.scoped.emit(GitScope(), None)

    def _redraw(self, repos: Any, matching: Any, opened: set[str]) -> None:
        """Clear and draw the rows; reopen `opened` from what git already said."""
        self.clear()
        self._loaded.intersection_update(opened)

        everything = QTreeWidgetItem(["All repositories"])
        everything.setData(0, ROLE_SCOPE, GitScope())
        self.addTopLevelItem(everything)

        for repo in repos or ():
            name = str(_get(repo, "name") or "")
            root = str(_get(repo, "root_path") or "")
            if not name:
                continue
            if matching is not None and name not in matching:
                continue
            count = int(_get(repo, "files") or 0)
            item = QTreeWidgetItem([f"{name}   ({count:,} files)"])
            item.setToolTip(0, root)
            item.setData(0, ROLE_SCOPE,
                         GitScope(kind="repo", repo=name, root=root))
            # **A placeholder, so the arrow appears before anything is read.**
            # Without one the repository looks like a leaf and nobody expands
            # it, which is the whole feature going undiscovered.
            item.addChild(QTreeWidgetItem(["Reading…"]))
            self.addTopLevelItem(item)
            if root in opened:
                # Still being read: it stays open on "Reading…", and
                # `_read_done` fills it when git answers.
                if root in self._read:
                    self._fill(item, self._read[root])
                item.setExpanded(True)

    def _repo_items(self) -> list:
        """The repository rows, top level, without "All repositories"."""
        items = (self.topLevelItem(i) for i in range(self.topLevelItemCount()))
        return [item for item in items
                if getattr(item.data(0, ROLE_SCOPE), "kind", "") == "repo"]

    def _item_for(self, scope: GitScope) -> Any:
        """The row holding `scope`, at any depth, or None."""
        stack = [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
        while stack:
            item = stack.pop()
            if item.data(0, ROLE_SCOPE) == scope:
                return item
            stack.extend(item.child(i) for i in range(item.childCount()))
        return None

    def _expanded(self, item: Any) -> None:
        """Load one repository's branches and commits, once, on a worker."""
        scope = item.data(0, ROLE_SCOPE)
        if not isinstance(scope, GitScope) or scope.kind != "repo":
            return
        if scope.root in self._loaded:
            return
        self._loaded.add(scope.root)
        if scope.root in self._read:
            self._fill(item, self._read[scope.root])
            return

        worker = CallableWorker(_read_repo, scope.root, component="ui.gittree")
        worker.signals.finished.connect(
            lambda payload, root=scope.root: self._read_done(root, payload))
        # A repository that cannot be read keeps its "Reading…" row replaced by
        # a sentence rather than sitting there for ever - see `_fill`.
        worker.signals.failed.connect(
            lambda _error, root=scope.root: self._read_done(root, {}))
        run(QThreadPool.globalInstance(), worker)

    def _read_done(self, root: str, payload: Any) -> None:
        """Git has answered for `root`: fill the row that holds it *now*.

        Found by its root rather than held from when the read began, because
        a redraw while git was reading replaces every row - the old one is
        gone, and filling it would leave the new one on "Reading…" for ever.
        A read that found nothing is not kept, so opening the repository again
        asks git again.
        """
        if payload:
            self._read[root] = payload
        for item in self._repo_items():
            if item.data(0, ROLE_SCOPE).root == root:
                self._fill(item, payload)

    def _fill(self, item: Any, payload: Any) -> None:
        """Replace the placeholder with what git said. Interface thread."""
        try:
            scope = item.data(0, ROLE_SCOPE)
            item.takeChildren()
        except RuntimeError:                     # the pane closed mid-read
            return

        branches = list((payload or {}).get("branches") or ())
        commits = list((payload or {}).get("commits") or ())

        work = QTreeWidgetItem(["Working tree"])
        work.setData(0, ROLE_SCOPE, GitScope(
            kind="worktree", repo=scope.repo, root=scope.root))
        item.addChild(work)

        if branches:
            head = QTreeWidgetItem([f"Branches ({len(branches)})"])
            item.addChild(head)
            for name in branches:
                node = QTreeWidgetItem([name])
                node.setData(0, ROLE_SCOPE, GitScope(
                    kind="branch", repo=scope.repo, root=scope.root, ref=name))
                head.addChild(node)

        if commits:
            head = QTreeWidgetItem([f"Recent commits ({len(commits)})"])
            item.addChild(head)
            for line in commits:
                # `repo_values` returns "3b29b7f  389 source and code types".
                sha, _sep, subject = str(line).partition("  ")
                node = QTreeWidgetItem([str(line)])
                node.setToolTip(0, subject.strip())
                node.setData(0, ROLE_SCOPE, GitScope(
                    kind="commit", repo=scope.repo, root=scope.root,
                    ref=sha.strip(), subject=subject.strip()))
                head.addChild(node)

        if not branches and not commits:
            # **Said, not left empty.** A repository whose git cannot be read -
            # git missing, the folder moved, a repository being rewritten -
            # would otherwise expand to nothing, which reads as a broken pane.
            note = QTreeWidgetItem(["No branches or commits could be read"])
            note.setDisabled(True)
            item.addChild(note)

    # -- selection -----------------------------------------------------------

    def _chosen(self, item: Any) -> None:
        """A selection: the index answers at once, a branch is read first.

        The read happens here rather than on the typing path - `ls-tree` is a
        subprocess, and once per selection is the whole difference. See
        `presenter.code_rows_for` for the guard that insisted on it.
        """
        if item is None:
            return
        scope = item.data(0, ROLE_SCOPE)
        if not isinstance(scope, GitScope):
            return
        if not scope.from_git:
            self.scoped.emit(scope, None)
            return

        worker = CallableWorker(scope_rows, scope, component="ui.gittree")
        worker.signals.finished.connect(
            lambda rows, chosen=scope: self.scoped.emit(chosen, list(rows or [])))
        # A ref that cannot be read lists nothing rather than leaving whatever
        # the last selection showed, which would be the wrong files under the
        # right heading.
        worker.signals.failed.connect(
            lambda _error, chosen=scope: self.scoped.emit(chosen, []))
        run(QThreadPool.globalInstance(), worker)


def _get(row: Any, key: str) -> Any:
    """A field from a row that may be a mapping or an object."""
    if isinstance(row, dict):
        return row.get(key)
    return getattr(row, key, None)


def _read_repo(root: str) -> dict:
    """Branches and recent commits for one repository. **Worker thread.**

    Never raises: `repo_values` already swallows a missing git, a folder that
    is not a repository and a timeout, and returns an empty list for each. An
    empty tree node is a sentence on screen; an exception here would be a
    dialog for expanding a row.
    """
    from pathlib import Path

    from app.search.gitsearch import repo_values

    try:
        return {
            "branches": repo_values(Path(root), "branch", limit=BRANCH_LIMIT),
            "commits": repo_values(Path(root), "commit", limit=COMMIT_LIMIT),
        }
    except Exception as exc:                     # noqa: BLE001 - see the docstring
        _log.debug("no git detail for {}: {}", root, exc)
        return {}


def paint_repo_state(view: Any, *, has_repos: bool, message: str) -> None:
    r"""Show the Code page with or without a list - **but always with its box.**

    The version of this that lived in `code_view._show_state` called
    `self.input.setVisible(False)` when no repositories were found, and the tab
    was reported as *"screwed, it does not have a search box or nothing"*.

    Hiding the one control a page exists for is never the right answer to having
    nothing to show. It removes the only affordance, and it explains nothing:
    somebody looking at the result cannot tell whether the tab is broken, still
    loading, or working perfectly and simply empty. It is worse than cosmetic
    here, because `_repos` is *also* empty when `repos_list` fails - that
    worker's failure is swallowed deliberately, a heading being no reason for an
    error dialog - so a database problem presented as a page with nothing on it.

    So the box stays, always. What changes is the list: hidden, with `message`
    in its place saying why there is nothing in it. The two buttons that need
    repositories are disabled rather than removed, and say which of the two
    things has happened - nothing to search, as against having stopped working.

    Here rather than in the view because `code_view.py` sits on the 250-line
    guard, and this is a widget arrangement rather than a decision: what to say
    is `presenter.repo_empty_state`, and it is passed in already decided.
    """
    view.input.setVisible(True)
    view.results.setVisible(True)
    view.git_split.setVisible(has_repos)
    view.empty.setVisible(not has_repos)

    view.git_button.setEnabled(has_repos)
    view.run_button.setEnabled(has_repos)
    view.git_button.setToolTip(
        GIT_VIEW_HINT if has_repos else
        "No repositories are indexed yet, so there is no tree to show.")
    view.run_button.setToolTip(
        view._run_hint if has_repos else
        "No repositories are indexed yet, so there is no history to search.")

    if has_repos:
        return
    view.summary.setText("")
    view.empty.setText(message)


def attach_git_tree(results: Any):
    """Build the tree, put it left of `results`, return `(tree, splitter, note)`.

    The same shape as `attach_preview`, and for the same reasons: the splitter
    exists whether or not the tree is shown, so toggling is a repaint rather
    than a relayout, and the divider somebody dragged is still where they left
    it when the pane comes back.

    Here rather than in the view because `code_view.py` is at the 250-line
    guard, and a splitter assembled in three places would drift.
    """
    from PySide6.QtWidgets import QLabel, QSplitter, QVBoxLayout

    tree = GitTree()

    # **The note lives with the tree, in the tree's own column.** A tree that
    # has silently shrunk is as bad as one that shows everything: without this
    # line somebody cannot tell whether three repositories are missing or were
    # never there. Assembled here rather than in the view for the same reason
    # the splitter is - `code_view.py` is at the 250-line guard.
    note = QLabel("")
    note.setObjectName("settingsHint")
    note.setWordWrap(True)
    note.setVisible(False)

    left = QWidget()
    column = QVBoxLayout(left)
    column.setContentsMargins(0, 0, 0, 0)
    column.setSpacing(4)
    column.addWidget(note)
    column.addWidget(tree, 1)

    split = QSplitter(Qt.Orientation.Horizontal)
    split.addWidget(left)
    split.addWidget(results)
    split.setStretchFactor(0, 1)
    split.setStretchFactor(1, 3)
    split.setChildrenCollapsible(False)
    left.setVisible(False)
    tree.setVisible(False)
    return tree, split, note


def scope_rows(scope: Any) -> list:
    r"""Every file a branch or a commit holds, as the table's own row shape.

    **Worker thread, and once per selection rather than once per keystroke.**
    This shells out to git; `test_nothing_that_runs_on_a_keystroke_imports_this`
    refused the first version of this feature for putting it on the typing
    path, and it was right to - `ls-tree` on a large repository is a subprocess
    and tens of milliseconds. The listing does not change while somebody types,
    so the pane fetches it here and `presenter.code_rows_for` filters it in
    memory afterwards.

    The rows come back as mappings rather than `GitRow`s so that a branch and
    the index produce one shape, and the table, the preview and the row menu
    stay ignorant of which engine answered.

    Never raises: `tree_files` and `commit_files` already swallow a missing
    git, a moved repository and a deleted ref, and return an empty list.
    """
    import os
    from pathlib import Path

    from app.search.gitsearch import commit_files, tree_files

    kind = str(getattr(scope, "kind", "") or "")
    if kind not in ("branch", "commit"):
        return []
    root = Path(str(getattr(scope, "root", "") or ""))
    ref = str(getattr(scope, "ref", "") or "")
    rows = commit_files(root, ref) if kind == "commit" else tree_files(root, ref)

    shaped = []
    for row in rows:
        relative = str(getattr(row, "path", "") or "")
        if not relative:
            continue
        name = relative.replace("\\", "/").rpartition("/")[2].lower()
        stem, dot, suffix = name.rpartition(".")
        shaped.append({
            # **Joined to the repository root.** `ls-tree` reports a path
            # relative to the repository; the preview pane and "open" need one
            # that exists, and a relative path previews as missing for every
            # single row.
            "path": str(root / relative.replace("/", os.sep)),
            "ext": suffix if (dot and stem) else name.lstrip("."),
            "repo": str(getattr(scope, "repo", "") or ""),
            "repo_root": str(root),
            # A commit says what it did to the file; a branch listing has
            # nothing to say, and an invented "INDEXED" would be a claim.
            "status": str(getattr(row, "status", "") or ""),
            "size_bytes": 0,
            "mtime_ns": 0,
        })
    return shaped


def draw_matches(view: Any, matching: Any) -> None:
    r"""Show only the repositories holding a match, and say how many.

    **The tree used to list every repository whatever was typed.** A term
    matching files in one checkout left the other three sitting there as though
    they matched too - and a tree is read as *"these are the repositories that
    have what you asked for"*, which made it the most misleading pane here.

    `matching` is computed in the same worker that fetched the rows - see
    `presenter.code_rows_and_repos` - which is what stops the tree and the list
    answering differently, and keeps the aggregate off the UI thread. `None`
    means nothing was asked, so every repository is shown and no count claimed.

    Here rather than in `code_view.py` because that view is at its 250-line
    guard, and because every decision this draws is already in the presenter.
    """
    from app.ui.presenter import repo_tree_summary

    if not view.git_tree.isVisible() or not view._repos:
        return

    view.git_tree.show_repos(view._repos, matching)

    note = repo_tree_summary(matching, len(view._repos))
    if note:
        view.git_tree.setToolTip(note)
        view.tree_note.setText(note)
    view.tree_note.setVisible(bool(note))


def draw_git_result(view: Any, found: Any) -> None:
    """Draw what `git` returned, or say plainly why it could not run.

    Extracted from `code_view.py` under the 250-line guard, and it belongs
    beside the tree: everything it renders is a git row rather than an indexed
    one, and `git_result_row` is the shape both panes agree on.
    """
    from app.ui.presenter import (
        code_route, git_result_row, git_stopped_summary, git_summary, repo_root_for,
    )

    root = repo_root_for(view._repos, code_route(view.input.text()).repo)
    needle = str(getattr(view, "_git_needle", "") or "")
    rows = [git_result_row(row, root, needle) for row in found.rows]
    if getattr(found, "stopped", False):
        # Order 0y §3a: what was found before the Stop stays on screen.
        view.summary.setText(git_stopped_summary(found))
        fill_keeping_selection(view, rows)
        return
    if not found.ok:
        # Never silent: git's own message about a bad revision or pattern is the
        # useful one, and the command makes it reproducible.
        view.summary.setText(f"git could not run that: {found.error}\n"
                             f"{' '.join(found.command)}")
        view._fill([])
        return

    fill_keeping_selection(view, rows)
    view.summary.setText(git_summary(found))


def fill_keeping_selection(view: Any, rows: list) -> None:
    """Redraw the list without moving what the person has selected. Order 0y §3a.

    Rows arrive while somebody may already be reading one. A plain refill
    re-announces the selection on every batch, so the preview pane would start
    over - and its `git show` with it - several times a second. The row that was
    selected is found again and kept, with the table's signals held while it is;
    only when it is no longer in the list is the new selection announced.
    """
    table = view.results.table
    keep = table.current_row()
    held = table.blockSignals(True)
    found = False
    try:
        view._fill(rows)
        if keep is not None:
            for index in range(table.rowCount()):
                if table.row_object(index) == keep:
                    table.setCurrentCell(index, 0)
                    found = True
                    break
    finally:
        table.blockSignals(held)
    if not found:
        table.selected.emit(table.current_row())


def draw_git_progress(view: Any, progress: Any, generation: int) -> None:
    """Rows git has found so far, and the live line above them. Order 0y §3a.

    Called on the interface thread for each report from the worker; it draws
    and nothing else. A report from a search that has been replaced or stopped
    (`_generation` moved on) is dropped.
    """
    from app.ui.presenter import (
        LIVE_ROW_LIMIT, git_live_line, git_live_rows, git_result_row,
    )

    if generation != view._generation:
        return
    needle = str(getattr(view, "_git_needle", "") or "")
    new = [git_result_row(row, row.root, needle) for row in progress.rows]
    first = not getattr(view, "_git_started", False)
    if new or first:
        view._git_started = True
        view._git_live = git_live_rows(getattr(view, "_git_live", []), new,
                                       many=progress.repos_total > 1)
        # Only the newest are listed while it runs, and the list is redrawn
        # only when those have changed - see `presenter.LIVE_ROW_LIMIT`.
        shown = view._git_live[:LIVE_ROW_LIMIT]
        if first or shown != getattr(view, "_git_shown", None):
            view._git_shown = shown
            fill_keeping_selection(view, shown)
    view.summary.setText(git_live_line(
        progress.found, progress.repos_done, progress.repos_total,
        history=getattr(view, "_git_history", True)))


#: The Stop button's tooltip while a history search runs (order 0y §1b).
STOP_HINT = ("Stop this history search now. Esc does the same.\n\n"
             "Nothing is lost: press Enter to run it again.")


def git_search_running(view: Any, running: bool) -> None:
    """The button reads Stop while git runs, and Esc stops it. Order 0y §1b.

    "Search history" and "Stop" are both labels the button system already
    knows, so `refresh_icon` gives each its own icon.
    """
    from PySide6.QtGui import QKeySequence, QShortcut

    from app.ui.widgets.buttons import refresh_icon

    view.run_button.setText("Stop" if running else "Search history")
    view.run_button.setToolTip(STOP_HINT if running else view._run_hint)
    refresh_icon(view.run_button)
    escape = getattr(view, "_git_escape", None)
    if escape is None:
        escape = QShortcut(QKeySequence(Qt.Key.Key_Escape), view)
        escape.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        # 2026-09-30: `weak_slot` - the view holds this shortcut, so a slot
        # closing over the view was a cycle. See `view_options.weak_slot`.
        escape.activated.connect(weak_slot(view, stop_git_search))
        view._git_escape = escape
    escape.setEnabled(running)


def stop_git_search(view: Any) -> bool:
    """Stop the history search that is running, if there is one.

    True when one was stopped - the caller then does nothing else, which is
    what makes one button both start and stop. The git process ends within a
    tenth of a second (`gitsearch._STOP_POLL_S`); the worker then hands back a
    result marked `stopped`, which `draw_git_result` says in words.
    """
    stop = getattr(view, "_git_stop", None)
    if stop is None or stop.stopped:
        return False
    stop.stop()
    return True


def start_git_search(view: Any, route: Any) -> None:
    """Run the slow one: a real `git` invocation, on a worker.

    Extracted from `code_view.start` under the 250-line guard. It belongs here
    for the same reason `draw_git_result` does - none of it is about the list,
    all of it is about a checkout, and the view's job is to say Enter was
    pressed rather than to know what `/class OrderService` means.
    """
    # `CallableWorker`, `run` and `QThreadPool` come from this module's own
    # imports. Re-importing them locally shadowed the module attributes, which
    # made `monkeypatch.setattr("...git_tree.run", ...)` bind a name this
    # function never looked at - so the test saw nothing start, correctly.
    from app.search.gitquery import build, parse_git_query
    from app.search.gitsearch import StopFlag, search_repositories
    from app.ui.presenter import git_needle, repo_targets

    # Order 0y §3b: no repository named means every repository. Only a name
    # that matches none of them is still answered with the sentence below.
    targets = repo_targets(view._repos, route.repo)
    if not targets:
        view.summary.setText(
            "Name a repository first — /repo <name> — so git knows which "
            "checkout to read. The Repository column lists them."
            if view._repos else
            "No repositories are indexed yet, so there is no history to search.")
        return

    # **The raw line, not `route.text`.** `parse_git_query` is the module that
    # knows what `/class OrderService` means; re-deriving half of it in the view
    # is how the two come to disagree.
    query = parse_git_query(view.input.text())
    view._generation += 1
    generation = view._generation
    view.summary.setText(f"Searching {build(query).explain}…")

    # Order 0y §1b: a newer search ends the older one, and this one can be
    # stopped from the button or with Esc.
    stop_git_search(view)
    view._git_stop = StopFlag()
    git_search_running(view, True)
    # Order 0y §3a: rows are drawn as git prints them. The worker reports
    # through `progress`; `draw_git_progress` is the only thing that draws.
    view._git_live, view._git_started = [], False
    view._git_needle, view._git_history = git_needle(query), query.wants_history()
    # `relay` runs on the worker's thread and only emits; `_emit` is the
    # workers' own guarded emit, so a report after the window has closed is
    # dropped rather than raised.
    relay = lambda progress: _emit(worker.signals, "progress", progress)  # noqa: E731
    worker = CallableWorker(search_repositories, targets, query, stop=view._git_stop,
                            on_progress=relay, component="ui.code.git")
    # 2026-09-30: `weak_slot` throughout - a finished worker and its slots
    # wait for the collector, and would keep a view that was let go with them.
    worker.signals.progress.connect(weak_slot(
        view, lambda code, progress, g=generation: draw_git_progress(code, progress, g)))
    worker.signals.finished.connect(weak_slot(
        view, lambda code, found, g=generation: code._show_git(found, g)))
    worker.signals.failed.connect(
        weak_slot(view, lambda code, _e: git_search_running(code, False)))
    worker.signals.failed.connect(view.error.emit)
    # 2026-09-30: the button goes back to "Search history" when *this* search
    # ends, whatever the list is showing by then. Typing a plain file search
    # while git ran moved `_generation` on, the result was (rightly) dropped,
    # and the button read "Stop" until somebody pressed it.
    worker.signals.done.connect(weak_slot(
        view, lambda code, flag=view._git_stop: git_search_running(code, False)
        if code._git_stop is flag else None))
    run(QThreadPool.globalInstance(), worker)
