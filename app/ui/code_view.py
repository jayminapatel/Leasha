"""Code repositories, and the files inside them.

Layer: L5, driving L1

The search box answers "which file says this". This answers "what have I got,
and how much of it is indexed" - a question about the machine, asked before you
know what you are looking for. **Source code was already searchable**; what the
application could not do was say which repository a result came from. That gap
is the whole of what this fills.

**Repositories and their files, in one tree.** A list of repository names told
you a repository existed and nothing about what was in it, so the obvious next
click - open it and look - had no answer here. Children load when a row is
expanded and not before; see `widgets/repo_tree.py` for why that matters at
48,000 files, and `presenter.read_repo_files` for where they come from.

**Nothing new was invented.** `/repo` parses through `app/search/query.py` like
`/from` and `/type`, so it works in every tab with the same spelling. The
alternative proposal ran to fifty-six switches, and a tab whose filters work
nowhere else is how that begins.

The box at the top narrows this tree rather than searching - see
`widgets/table_filter.py`. Repositories are found by the walk, never registered,
so there is nothing here to add or configure.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, QThreadPool, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.core.logging import logger
from app.ui.presenter import (
    CODE_COMMANDS, REPO_FILE_LIMIT, read_repo_files, repo_empty_state,
    repo_file_rows, repo_filter, repo_filter_summary, repo_rows,
)
from app.ui.view_options import (
    apply_to_tree, available_columns, button as view_button,
)
from app.ui.widgets.command_popup import attach_to
from app.ui.widgets.file_menu import FileActions, show_for, viewport_point
from app.ui.widgets.git_search import GitSearchPanel, attach_below
from app.ui.widgets.preview import attach_preview
from app.ui.widgets.repo_tree import COLUMNS, RepoTree
from app.ui.widgets.table_filter import build_filter
from app.ui.workers import CallableWorker, run

__all__ = ["CodeView", "COLUMNS", "PREFS_KEY"]

PREFS_KEY = "ui:code"

_log = logger.bind(component="ui.code")

#: Offered whatever the rows say. A repository list with no names is not a list.
ALWAYS_OFFERED = ("name",)


class CodeView(QWidget):
    """A tree of every repository found under the indexed roots."""

    error = pyqtSignal(object)
    #: A repository to search the contents of. The bridge between the browser
    #: and the search box: found the repository, now find what is in it.
    search_repo_requested = pyqtSignal(str)
    #: A file to open, by full path. The window owns opening things.
    open_requested = pyqtSignal(str)
    #: A file to show in Explorer, by full path.
    reveal_requested = pyqtSignal(str)
    #: Somebody with nothing indexed wants to start. The window owns the tabs.
    indexing_requested = pyqtSignal()

    def __init__(self, store: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._rows: list[Any] = []
        self._generation = 0
        self._available: tuple[str, ...] = tuple(key for key, *_ in COLUMNS)

        # Narrows the tree; it does not search - see widgets/table_filter.py
        # for why that distinction is kept sharp. This is not the second search
        # box the work order refused: searching *inside* a repository is still
        # Enter, still the one grammar, still the search tab.
        self.input = build_filter(
            "Filter by name, kind or location",
            accessible_name="Filter repositories and files",
            tooltip=("Narrows this tree. Expand a repository to see its files."
                     "\n\nTo search what is *inside* a repository, select it "
                     "and press Enter - that hands it to the search box with "
                     "repo: already set."),
            on_change=self._apply_filter,
        )
        # `/` opens a menu here as it does in every other box - but only the
        # commands a repository tree can answer. See `command_popup`.
        self._popup = attach_to(self.input, only=CODE_COMMANDS, store=store)

        self.summary = QLabel("")
        self.summary.setObjectName("resultsSummary")
        self.summary.setWordWrap(True)

        self.empty = QLabel("")
        self.empty.setWordWrap(True)
        self.empty.setOpenExternalLinks(False)
        self.empty.linkActivated.connect(lambda _link: self.indexing_requested.emit())
        self.empty.setVisible(False)

        self.results = RepoTree(self)
        self.results.setAccessibleName("Repositories and their files")
        self.results.files_requested.connect(self._load_files)
        self.results.repo_activated.connect(self.search_repo_requested)
        self.results.file_activated.connect(self.open_requested)
        self.results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.results.customContextMenuRequested.connect(self._on_context_menu)

        header = self.results.header()
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(
            lambda point: self.view_button.show_menu(header.mapToGlobal(point)))

        self.view_button = view_button(
            self, store, PREFS_KEY,
            columns=[(key, heading) for key, heading, _r in COLUMNS],
            on_change=self._prefs_changed,
        )

        # Off until asked for - `Ctrl+P` or the View menu, as everywhere else.
        # A repository row has no file to draw and shows the card; expanding it
        # and selecting a file is what fills the pane, which is the sequence the
        # tab is for.
        self.preview, self.split = attach_preview(
            self.results,
            lambda row: self.open_requested.emit(getattr(row, "full_path", "")),
            self.error.emit,
        )

        # **The repository search, under the tree that chooses what it searches.**
        # `GitSearch.txt`'s switches, in the same `/` grammar - but a different
        # engine over a different store, so it has its own box, its own button
        # and its own catalogue. It runs on Enter and never on a keystroke:
        # `git log -S` diffs every commit it walks.
        self.git = GitSearchPanel(self)
        self.git.error.connect(self.error)
        self.git.open_requested.connect(self.open_requested)
        self.results.selected.connect(
            lambda _row: self.git.set_repository(self._selected_root()))

        top = QHBoxLayout()
        top.addWidget(self.input, stretch=1)
        top.addWidget(self.view_button)

        self.panes = attach_below(self.split, self.git)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.summary)
        layout.addWidget(self.empty)
        layout.addWidget(self.panes, 1)
        self._apply_prefs()

    # -- lifecycle -----------------------------------------------------------

    def shutdown(self) -> None:
        """No timers of its own, but stale anything still in flight."""
        from app.ui.workers import stop_timers

        stop_timers(self)
        self.preview.shutdown()
        self.git.shutdown()

    def focus(self) -> None:
        """The filter box, like Files and Mail."""
        self.input.setFocus()
        self.input.selectAll()

    def refresh(self) -> None:
        """Re-read the repositories on a worker - it counts files across every
        one of them, and runs each time the tab comes forward."""
        self._generation += 1
        generation = self._generation
        worker = CallableWorker(self._store.repos_list, component="ui.code")
        worker.signals.finished.connect(
            lambda rows, g=generation: self._show(rows, g))
        worker.signals.failed.connect(self.error)
        run(QThreadPool.globalInstance(), worker)

    # -- drawing -------------------------------------------------------------

    def _show(self, rows: Any, generation: int) -> None:
        if generation != self._generation:
            return

        self._rows = repo_rows(rows or [])
        self.results.show_repos(self._rows)
        self._available = available_columns(
            self._rows, [(key, key) for key, _h, _r in COLUMNS],
            always=ALWAYS_OFFERED,
        )
        self.view_button.available = self._available
        self._apply_prefs()
        self._show_state()
        self._apply_filter()     # a refresh must not drop what was typed

    def _load_files(self, row: Any) -> None:
        """One repository's files, on a worker. **Never on this thread.**"""
        generation = self._generation
        worker = CallableWorker(
            read_repo_files, self._store, row, component="ui.code")
        worker.signals.finished.connect(
            lambda found, r=row, g=generation: self._show_files(r, found, g))
        worker.signals.failed.connect(
            lambda err, r=row: self.results.failed(r.name, str(err)))
        run(QThreadPool.globalInstance(), worker)

    def _show_files(self, row: Any, found: Any, generation: int) -> None:
        if generation != self._generation:
            return                                # refreshed while we were out
        records = list(found or [])
        over = len(records) > REPO_FILE_LIMIT
        self.results.set_files(
            row.name, repo_file_rows(records[:REPO_FILE_LIMIT]),
            truncated=row.file_count if over else 0,
        )
        self._apply_filter()     # newly arrived children obey what was typed

    def _apply_filter(self) -> None:
        """Hide what does not match, and say so in the summary."""
        chosen = repo_filter(self.input.text())
        shown = self.results.apply_filter(chosen)
        if not self._rows:
            return
        self.summary.setText(repo_filter_summary(
            chosen, shown=shown, total=len(self._rows),
            files=sum(row.file_count for row in self._rows),
        ))

    def _show_state(self) -> None:
        """Three empty states, because they are three different questions.

        A generic "no results" would waste the one that matters: an indexed
        corpus with no repositories needs to be told what one is here, and that
        adding its parent as an indexed root is what makes it appear.
        """
        if self._rows:
            # The summary is `_apply_filter`'s to write - it is the only one
            # that knows whether a filter is narrowing the count.
            self.empty.setVisible(False)
            self.panes.setVisible(True)
            self.input.setVisible(True)
            return
        self.input.setVisible(False)     # nothing to narrow
        # The whole pane, not the tree: hiding the tree alone would leave the
        # preview and the repository search floating beside nothing, above a
        # message explaining that there is nothing.
        self.panes.setVisible(False)
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
        apply_to_tree(
            self.results, self.view_button.prefs,
            columns=[(key, heading) for key, heading, _r in COLUMNS],
            available=self._available,
        )
        self.preview.apply_preference(
            self.view_button.prefs, self.results.current_row())

    def _prefs_changed(self, _prefs: Any) -> None:
        self._apply_prefs()

    # -- acting on a row -----------------------------------------------------

    def _selected_root(self) -> str:
        """The folder of the repository the selection belongs to.

        The repository search needs a path on disk; `selected_repo` returns a
        *name*, which is what the search box and the menu want. Two questions,
        two answers - conflating them put a repository name where git expected
        a directory and failed with a message about neither.
        """
        name = self.results.selected_repo()
        for row in self._rows:
            if row.name == name:
                return getattr(row, "root", "")
        return ""

    def selected_repo(self) -> str:
        """The repository the selection belongs to, whichever level it is on."""
        return self.results.selected_repo()

    def _on_context_menu(self, point: Any) -> None:
        """The same menu as the other lists - see widgets/file_menu.py.

        What it offers depends on what is under the cursor: a file can be
        revealed and opened, a repository can only be searched.
        """
        item = self.results.itemAt(viewport_point(self.results, point))
        if item is not None:
            self.results.setCurrentItem(item)
        row = self.results.selected_row()
        name = self.results.selected_repo()
        if row is None or not name:
            return

        is_repo = hasattr(row, "root")
        path = row.root if is_repo else row.full_path
        show_for(self.results, point, path, FileActions(
            # A repository is a folder: opening it is the file manager's job,
            # and there is nothing to open *with*. Revealing it still makes
            # sense, so that is the one both levels offer.
            open_file=None if is_repo else (lambda: self.open_requested.emit(path)),
            reveal=lambda: self.reveal_requested.emit(path),
            search_inside=lambda: self.search_repo_requested.emit(name),
            copy=[("Copy repository name", name), ("Copy full path", path)],
        ))
