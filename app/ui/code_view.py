"""Code: one box, one list, two engines.

Layer: L5, driving L1 and L4

**Corrected by the owner**: *"the code search page is all wrong it should be a
combined one search box with the git code files in the list"*. A tree above a
separate git box made somebody choose an engine before they had a question -
and the question is nearly always *"where is that file"*, which the index
answers in milliseconds, so that is what typing does.

**The grammar picks the engine, not the person.** A line with no git switch
searches the indexed files of every repository at once, as you type. One
carrying `/history` or `/introduced` runs git, on Enter, because `git log -S`
diffs every commit it walks - 2.26s for 200 commits, measured here.

That decision lives in `presenter.code_route`, which imports no Qt: it is what
the whole tab turns on, it cannot be tested inside a widget, and it must never
go wrong *towards* git - an index lookup taken as a repository search costs two
seconds and a subprocess for a question that had a 3ms answer.

**The repository is a column, and now optionally a tree as well.** The flat list
says where each file came from and `/repo` narrows it. "Git view" adds a pane on
the left - repositories, branches, recent commits - because a branch is the one
thing the index cannot answer: it holds the working tree, so a file deleted on
`main` but alive on a feature branch has no row. See `widgets/git_tree.py`. The
switches still apply on top of whatever the tree has selected.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from app.core.logging import logger
from app.ui.presenter import (
    code_preset,
    REPO_FILE_LIMIT, GitScope, code_route, code_rows_for, code_summary,
    code_rows_and_repos,
    repo_empty_state, repo_file_rows, repo_root_for,
)
from app.ui.view_options import button as view_button
from app.ui.widgets.code_commands import (
    CODE_CATALOGUE, code_command_for, code_matching, git_values,
)
from app.ui.widgets.code_results import COLUMNS, CodeResults
from app.ui.widgets.command_popup import attach_to
from app.ui.widgets.git_tree import (
    GIT_VIEW_HINT, attach_git_tree, draw_git_result, draw_matches,
    paint_repo_state, start_git_search,
)
from app.ui.workers import CallableWorker, run, stop_timers

__all__ = ["CodeView", "COLUMNS", "PREFS_KEY"]

PREFS_KEY = "ui:code"

_log = logger.bind(component="ui.code")

#: One indexed lookup over an indexed column, so this only needs to be long
#: enough to avoid a query per keystroke on a fast typist.
CODE_DEBOUNCE_MS = 90



class CodeView(QWidget):
    """Search the code: indexed files instantly, git history on Enter."""

    error = pyqtSignal(object)
    #: A repository to search the *contents* of, handed to the search tab.
    search_repo_requested = pyqtSignal(str)
    open_requested = pyqtSignal(str)
    reveal_requested = pyqtSignal(str)
    indexing_requested = pyqtSignal()

    def __init__(self, store: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._generation = 0
        self._repos: list[Any] = []

        self.input = QLineEdit()
        self.input.setPlaceholderText(
            "Search your repositories — a file name, or / for switches: "
            "/repo  /type  /history  /class  /introduced")
        self.input.setClearButtonEnabled(True)
        self.input.setAccessibleName("Search code and repository history")
        self.input.textChanged.connect(lambda _t: self._timer.start())
        self.input.returnPressed.connect(self.start)
        # The two catalogues merged - see `widgets/code_commands.py`. Values
        # come from the index for `/repo` and `/type`, and from git for
        # `/branch`, `/tag` and `/author`.
        self._popup = attach_to(
            self.input, catalogue=CODE_CATALOGUE, matcher=code_matching,
            resolve=code_command_for, store=store,
            lookup=lambda kind, prefix, limit: git_values(
                self._repos, self.input.text(), kind, prefix, limit),
        )

        self.run_button = QPushButton("Search history")
        # Kept, because `_show_state` swaps in a different sentence while there
        # is nothing to search and has to be able to put this one back. Reading
        # the tooltip back off the widget would work exactly once.
        self._run_hint = (
            "Run the repository search now.\n\n"
            "Typing already searches the indexed files. Switches that read "
            "history - /history, /introduced, /branch - need this, or Enter: "
            "git diffs every commit it walks, so they take seconds.")
        self.run_button.setToolTip(self._run_hint)
        self.run_button.clicked.connect(self.start)

        self.summary = QLabel("", objectName="resultsSummary", wordWrap=True)

        self.empty = QLabel("", wordWrap=True, openExternalLinks=False,
                            visible=False)
        self.empty.linkActivated.connect(lambda _l: self.indexing_requested.emit())

        # The table, the preview and the row menu are in `code_results.py`;
        # the tree and its scope are in `git_tree.py`. Both were split out when
        # this file hit the 250-line guard - the guard working, not a nuisance.
        #: Empty means every repository, which is what the tab did before.
        self._scope = GitScope()
        #: The branch or commit listing, fetched once when the scope changes.
        #: None for an index scope, which is queried per keystroke instead.
        self._scoped_rows: Any = None

        self.results = CodeResults(self)
        self.results.error.connect(self.error)
        self.results.open_requested.connect(self.open_requested)
        self.results.reveal_requested.connect(self.reveal_requested)
        self.results.search_repo_requested.connect(self.search_repo_requested)
        self.results.view_menu_requested.connect(
            lambda at: self.view_button.show_menu(at))
        self.preview = self.results.preview        # re-exposed for callers

        # The repository tree, and the button that reveals it. Off by default:
        # it costs a subprocess per repository expanded, and somebody who wants
        # to browse branches asks for it - the same posture as the preview pane.
        (self.git_tree, self.git_split,
         self.tree_note) = attach_git_tree(self.results)
        self.git_tree.scoped.connect(self._scoped)
        self.git_button = QPushButton("Git view")
        self.git_button.setCheckable(True)
        self.git_button.setToolTip(GIT_VIEW_HINT)
        self.git_button.toggled.connect(self._git_view_toggled)

        self.view_button = view_button(
            self, store, PREFS_KEY,
            columns=[(key, heading) for key, heading, _a, _r in COLUMNS],
            on_change=self._prefs_changed,
            table=self.results.table,
        )

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(CODE_DEBOUNCE_MS)
        self._timer.timeout.connect(self._typed)

        top = QHBoxLayout()
        top.addWidget(self.input, stretch=1)
        top.addWidget(self.git_button)
        top.addWidget(self.run_button)
        top.addWidget(self.view_button)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.summary)
        # Given the list's stretch and pinned to the top, so the explanation
        # sits where the first row would be rather than floating in the middle
        # of the page. Only one of these two is ever visible, so the two stretch
        # factors never compete - a hidden widget is skipped by the layout.
        layout.addWidget(self.empty, 1, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(self.git_split, 1)
        self._apply_prefs()

    # -- lifecycle -----------------------------------------------------------

    def shutdown(self) -> None:
        stop_timers(self)
        self.results.shutdown()

    def focus(self) -> None:
        self.input.setFocus()
        self.input.selectAll()

    def refresh(self) -> None:
        """Re-read the repositories and the list. Runs on every tab switch."""
        worker = CallableWorker(self._store.repos_list, component="ui.code")
        worker.signals.finished.connect(self._repos_read)
        worker.signals.failed.connect(lambda _e: None)   # a heading, not a search
        run(QThreadPool.globalInstance(), worker)
        self._typed()

    def _repos_read(self, rows: Any) -> None:
        self._repos = list(rows or [])
        self._show_state()

    # -- running -------------------------------------------------------------

    def _typed(self) -> None:
        """As-you-type, and **only ever the index.**

        A git run behind a keystroke is the first non-negotiable broken: `git
        log -S` diffs every commit it walks. When the line asks for history the
        list holds still and the summary says which key runs it.
        """
        route = code_route(self.input.text())
        if route.engine == "git":
            self.summary.setText(
                f"{route.because} searches history — press Enter or "
                f"“Search history”. git diffs every commit it walks, so this "
                f"one takes seconds rather than milliseconds.")
            return
        self._search_index(route)

    def _search_index(self, route: Any) -> None:
        self._generation += 1
        generation = self._generation
        # One call for both engines - see `presenter.code_rows_for`. The tree
        # says where to look, the box says what to look for, and a typed
        # `/type` still beats the configured code types on either path.
        # **Both halves in one worker**, so the tree and the list are answered
        # by the same query and neither reaches the store on the UI thread.
        worker = CallableWorker(
            code_rows_and_repos, self._store, self._scope, route,
            cached=self._scoped_rows, repos=self._repos, limit=REPO_FILE_LIMIT,
            component="ui.code",
        )
        worker.signals.finished.connect(
            lambda rows, g=generation: self._show_files(rows, g))
        worker.signals.failed.connect(self.error.emit)
        run(QThreadPool.globalInstance(), worker)

    def _git_view_toggled(self, on: bool) -> None:
        """Reveal the tree and fill it; leaving drops the scope with it, since
        a list still narrowed to a branch with nothing saying so is the worst
        of both panes."""
        self.git_tree.setVisible(on)
        self.git_tree.parentWidget().setVisible(on)
        self.tree_note.setVisible(False)
        if on:
            self.git_tree.show_repos(self._repos)
            self._typed()                # narrows it, on a worker
        else:
            self._scoped(GitScope())

    def _scoped(self, scope: Any, rows: Any = None) -> None:
        """The tree has chosen, and has already read a branch if it needed to."""
        self._scope = scope if scope is not None else GitScope()
        self._scoped_rows = rows
        self._typed()

    def start(self) -> None:
        """Enter: run whatever the line asks for, including the slow one."""
        route = code_route(self.input.text())
        if route.engine == "git":
            start_git_search(self, route)
        else:
            self._search_index(route)

    # -- drawing -------------------------------------------------------------

    def _fill(self, rows: list[Any]) -> None:
        self.results.show_rows(rows, self.view_button.prefs)
        self.view_button.available = self.results.available

    def _show_files(self, payload: Any, generation: int) -> None:
        if generation != self._generation:
            return
        records = payload.get("rows") if isinstance(payload, dict) else payload
        rows = repo_file_rows(list(records or [])[:REPO_FILE_LIMIT])
        self._fill(rows)
        self._show_state()
        if isinstance(payload, dict):
            draw_matches(self, payload.get("matching"))
        if self._repos:
            self.summary.setText(code_summary(rows, self._repos, self._scope,
                                              preset=code_preset(self._store)))

    def _show_git(self, found: Any, generation: int) -> None:
        """Draw a git result. See `widgets.git_tree.draw_git_result`."""
        if generation == self._generation:
            draw_git_result(self, found)

    def _show_state(self) -> None:
        """Two questions, two sentences - and **the search box never goes away.**

        This used to call `self.input.setVisible(False)` when no repositories
        were found, which is how the tab came to be reported as *"screwed, it
        does not have a search box or nothing"*. Hiding the one control a page
        exists for removes the only affordance and explains nothing: somebody
        looking at it cannot tell whether the tab is broken, still loading, or
        working perfectly and simply empty.

        It is worse than cosmetic, because `_repos` is *also* empty when
        `repos_list` fails - that worker's failure is deliberately swallowed,
        since a heading is not worth an error dialog - so a database problem
        presented itself as a page with nothing on it at all.

        The box stays, always. What changes is the list: hidden, with a sentence
        in its place saying why there is nothing in it. The wording is on
        `repo_empty_state`, which is where it can be read without a display.
        """
        has_repos = bool(self._repos)
        paint_repo_state(
            self, has_repos=has_repos,
            message="" if has_repos else repo_empty_state(self._anything_indexed()))

    def _anything_indexed(self) -> bool:
        """Cheap and guarded. Only decides which of two sentences to show.

        **It used to say that and not be true.** `stats()` is three `COUNT(*)`,
        two of them scans of `chunks` - 93ms at two million, around 460ms at
        ten - and this runs while the tab is being drawn. `has_any_files()` is
        one row with a `LIMIT 1`, which is what "is there anything" needs.
        """
        try:
            return self._store.has_any_files()
        except Exception as exc:                 # noqa: BLE001
            _log.debug("could not read the index size: {}", exc)
            return True                          # the less alarming of the two

    def _apply_prefs(self) -> None:
        self.results.apply_prefs(self.view_button.prefs)

    def _prefs_changed(self, _prefs: Any) -> None:
        self._apply_prefs()

    # -- acting on a row -----------------------------------------------------
