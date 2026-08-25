"""Code: one box, one list, two engines.

Layer: L5, driving L1 and L4

**Corrected by the owner**: *"the code search page is all wrong it should be a
combined one search box with the git code files in the list"*. It was a tree of
repositories above a separate git search box, and that made somebody choose an
engine before they had a question. The question is nearly always *"where is that
file"*, and that is answered from the index in milliseconds - so it must be what
typing does.

**The grammar picks the engine, not the person.** A line with no git switch in
it searches the indexed files of every repository at once, as you type. A line
carrying `/history`, `/branch`, `/introduced` and the rest runs git, on Enter,
because `git log -S` diffs every commit it walks - 2.26s for 200 commits,
measured on this project. Both fill the same table, and the line above it says
which engine answered and what it looked at.

That decision lives in `presenter.code_route`, which imports no Qt, because
"which engine" is what the whole tab turns on and a decision made inside a
widget is one nobody can test. It is also the one that must never go wrong
*towards* git: an index lookup taken as a repository search costs two seconds
and a subprocess for a question that had a 3ms answer.

**The repository is a column, not a tree.** A flat list has to say where a file
came from, and `/repo` narrows it - offering the names actually found, like
every other value in the `/` menu.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QThreadPool, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget,
)

from app.core.logging import logger
from app.ui.presenter import (
    REPO_FILE_LIMIT, code_route, code_summary, code_type_filter,
    git_result_row, git_summary,
    repo_empty_state, repo_file_rows, repo_root_for,
)
from app.ui.view_options import button as view_button
from app.ui.widgets.code_commands import (
    CODE_CATALOGUE, code_command_for, code_matching,
)
from app.ui.widgets.code_results import COLUMNS, CodeResults
from app.ui.widgets.command_popup import attach_to
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
            resolve=code_command_for, store=store, lookup=self._values,
        )

        self.run_button = QPushButton("Search history")
        self.run_button.setToolTip(
            "Run the repository search now.\n\n"
            "Typing already searches the indexed files. Switches that read "
            "history - /history, /introduced, /branch - need this, or Enter: "
            "git diffs every commit it walks, so they take seconds.")
        self.run_button.clicked.connect(self.start)

        self.summary = QLabel("")
        self.summary.setObjectName("resultsSummary")
        self.summary.setWordWrap(True)

        self.empty = QLabel("")
        self.empty.setWordWrap(True)
        self.empty.setOpenExternalLinks(False)
        self.empty.linkActivated.connect(lambda _link: self.indexing_requested.emit())
        self.empty.setVisible(False)

        # The table, the preview beside it and the row menu - see
        # `widgets/code_results.py`. Split out when this file crossed the
        # 250-line guard, which is the guard working rather than a nuisance.
        self.results = CodeResults(self)
        self.results.error.connect(self.error)
        self.results.open_requested.connect(self.open_requested)
        self.results.reveal_requested.connect(self.reveal_requested)
        self.results.search_repo_requested.connect(self.search_repo_requested)
        self.results.view_menu_requested.connect(
            lambda at: self.view_button.show_menu(at))
        #: Re-exposed so callers and tests need not know where it moved to,
        #: the same way `settings_view` re-exposes the controls `SearchBox`
        #: took with it.
        self.preview = self.results.preview

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
        top.addWidget(self.run_button)
        top.addWidget(self.view_button)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.summary)
        layout.addWidget(self.empty)
        layout.addWidget(self.results, 1)
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

    def _values(self, kind: str, prefix: str, limit: int) -> list:
        """Branches, tags and authors, from whichever repository is in the box.

        The index answers `/repo` and `/type` through `store`; only git can
        answer these, and only for one repository at a time - so the value in
        `/repo` decides which. With none named and exactly one repository
        known, that is the one meant; with several it is a question nobody has
        answered yet, and offering the first would be a guess presented as a
        fact.
        """
        root = repo_root_for(self._repos, code_route(self.input.text()).repo)
        if not root:
            return []
        from app.search.gitsearch import repo_values

        return repo_values(root, kind, prefix=prefix, limit=limit)

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
        # **A typed `/type` wins over the configured default.** Naming a type is
        # an instruction; the setting is what to do when nobody has. Same rule
        # as `app.cli index`, where explicit roots beat the saved ones - a
        # command that quietly ignored what was typed in favour of a setting
        # would be the same bug pointing the other way.
        #
        # `None` means "no filter", which is what "Everything in the repository"
        # resolves to - not an empty list, which would show nothing.
        wanted = list(route.extensions) or code_type_filter(self._store)
        worker = CallableWorker(
            self._store.code_files, route.text, repo=route.repo,
            ext=wanted, limit=REPO_FILE_LIMIT,
            component="ui.code",
        )
        worker.signals.finished.connect(
            lambda rows, g=generation: self._show_files(rows, g))
        worker.signals.failed.connect(self.error.emit)
        run(QThreadPool.globalInstance(), worker)

    def start(self) -> None:
        """Enter: run whatever the line asks for, including the slow one."""
        route = code_route(self.input.text())
        if route.engine != "git":
            self._search_index(route)
            return

        root = repo_root_for(self._repos, route.repo)
        if not root:
            self.summary.setText(
                "Name a repository first — /repo <name> — so git knows which "
                "checkout to read. The Repository column lists them."
                if self._repos else
                "No repositories are indexed yet, so there is no history to "
                "search.")
            return

        from app.search.gitquery import build, parse_git_query

        # **The raw line, not `route.text`.** `parse_git_query` is the module
        # that knows what `/class OrderService` means; re-deriving half of it
        # here is how the two come to disagree.
        query = parse_git_query(self.input.text())
        plan = build(query)
        self._generation += 1
        generation = self._generation
        self.summary.setText(f"Searching {plan.explain}…")

        from app.search.gitsearch import run_query

        worker = CallableWorker(run_query, root, query, component="ui.code.git")
        worker.signals.finished.connect(
            lambda found, g=generation: self._show_git(found, g))
        worker.signals.failed.connect(self.error.emit)
        run(QThreadPool.globalInstance(), worker)

    # -- drawing -------------------------------------------------------------

    def _fill(self, rows: list[Any]) -> None:
        self.results.show_rows(rows, self.view_button.prefs)
        self.view_button.available = self.results.available

    def _show_files(self, records: Any, generation: int) -> None:
        if generation != self._generation:
            return
        rows = repo_file_rows(list(records or [])[:REPO_FILE_LIMIT])
        self._fill(rows)
        self._show_state()
        if self._repos:
            self.summary.setText(code_summary(rows, self._repos))

    def _show_git(self, found: Any, generation: int) -> None:
        if generation != self._generation:
            return
        if not found.ok:
            # Never silent: git's own message about a bad revision or pattern
            # is the useful one, and the command makes it reproducible.
            self.summary.setText(f"git could not run that: {found.error}\n"
                                 f"{' '.join(found.command)}")
            self._fill([])
            return
        root = repo_root_for(self._repos, code_route(self.input.text()).repo)
        rows = [git_result_row(row, root) for row in found.rows]
        self._fill(rows)
        self.summary.setText(git_summary(found))

    def _show_state(self) -> None:
        """Two different questions, so two different sentences - see
        `repo_empty_state`. A corpus with no repositories needs to be told what
        one is here and that adding its parent as an indexed root is what makes
        it appear."""
        if self._repos:
            self.empty.setVisible(False)
            self.results.setVisible(True)
            self.input.setVisible(True)
            return
        self.input.setVisible(False)
        self.results.setVisible(False)
        self.empty.setVisible(True)
        self.summary.setText("")
        self.empty.setText(repo_empty_state(self._anything_indexed()))

    def _anything_indexed(self) -> bool:
        """Cheap and guarded. Only decides which of two sentences to show."""
        try:
            return bool(self._store.stats().get("files_total", 0))
        except Exception as exc:                 # noqa: BLE001
            _log.debug("could not read the index size: {}", exc)
            return True                          # the less alarming of the two

    def _apply_prefs(self) -> None:
        self.results.apply_prefs(self.view_button.prefs)

    def _prefs_changed(self, _prefs: Any) -> None:
        self._apply_prefs()

    # -- acting on a row -----------------------------------------------------
