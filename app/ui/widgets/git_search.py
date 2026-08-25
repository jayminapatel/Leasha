r"""Searching a repository, on the Code tab.

Layer: L5

The other half of `GitSearch.txt`, in a window rather than a terminal. The
grammar, the plan and the reading are all in `app/search/gitquery.py` and
`app/search/gitsearch.py`, where they are tested without git and without a
display; this is the box, the button and the table.

**Enter, not a keystroke.** Everything else in this application searches as you
type, and this deliberately does not. `git log -S` diffs every commit it walks -
2.26s for 200 commits, measured on this project - so a search per keystroke
would be a window that is never idle. The first non-negotiable is that no
unbounded work sits behind the Enter key of the *search box*; this is a
different box, on a different tab, with the cost stated on it.

**Which repository is the one selected in the tree above.** No second picker,
no remembered setting that disagrees with what is on screen. Selecting a
repository and searching it is the sequence the tab is already for.

**Stop abandons the answer; git is bounded by its timeout.** Said plainly on
the button's tooltip rather than implied, because a Stop that leaves a process
running and does not say so is worse than no Stop at all. Killing git mid-walk
needs a `Popen` and a process group, which is a real change to the `runner`
seam every one of these tests depends on - `docs/WORKORDER-git-search-ui.md`
records it as the next thing worth doing here.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, QThreadPool, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.logging import logger
from app.search.gitquery import (
    GIT_COMMANDS, build, git_command_for, git_matching, parse_git_query,
)
from app.ui.widgets.command_popup import attach_to
from app.ui.widgets.preview import attach_preview
from app.ui.widgets.result_table import ResultTable
from app.ui.workers import CallableWorker, run

__all__ = ["GitSearchPanel", "attach_below", "COLUMNS"]

_log = logger.bind(component="ui.gitsearch")

#: (heading, right-aligned?). One set of columns for every kind of result:
#: a line of a file, a commit, a file's change. They differ in which are filled,
#: not in what they mean - and two tables that had to be swapped between would
#: cost more than the empty cells do.
COLUMNS: tuple[tuple[str, bool], ...] = (
    ("When", False),
    ("Who", False),
    ("Where", False),
    ("What", False),
)


def attach_below(above: QWidget, panel: QWidget) -> QWidget:
    """Put `panel` under `above`, in a splitter the person can drag.

    Here rather than in the view for the reason `attach_preview` is: the view
    it attaches to is already at the length a view is allowed to be, and this
    is where somebody looks to find out how the panel is used.

    A splitter rather than a fixed share, because how much of the tab is the
    repository list and how much is the search is a preference that changes
    with what you are doing, and the divider is where people expect to change
    it.
    """
    from PyQt6.QtWidgets import QSplitter

    split = QSplitter(Qt.Orientation.Vertical)
    split.addWidget(above)
    split.addWidget(panel)
    split.setStretchFactor(0, 3)
    split.setStretchFactor(1, 2)
    split.setChildrenCollapsible(False)
    return split


class GitSearchPanel(QWidget):
    """A repository search: a box with `/` switches, a table and a preview."""

    error = pyqtSignal(object)
    #: A file to open, by full path. The window owns opening things.
    open_requested = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._repo: str = ""
        self._generation = 0

        self.input = QLineEdit()
        self.input.setPlaceholderText(
            "Search this repository — press / for switches: "
            "/history  /branch  /class  /introduced")
        self.input.setClearButtonEnabled(True)
        self.input.setAccessibleName("Search the selected repository")
        self.input.returnPressed.connect(self.start)
        # The same menu as everywhere else, over the repository catalogue and
        # reading its values from git rather than from the index - see
        # `gitsearch.repo_values`.
        self._popup = attach_to(
            self.input, catalogue=GIT_COMMANDS, matcher=git_matching,
            resolve=git_command_for, lookup=self._values,
        )

        self.run_button = QPushButton("Search")
        self.run_button.setToolTip(
            "Search the selected repository.\n\n"
            "Switches that read history are slower - git diffs every commit it "
            "walks. The line below says what was searched.")
        self.run_button.clicked.connect(self.start)

        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip(
            "Stop waiting for this search.\n\n"
            "The result is discarded. git itself finishes or times out on its "
            "own - it is not killed.")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)

        self.status = QLabel("")
        self.status.setObjectName("resultsSummary")
        self.status.setWordWrap(True)

        self.results = ResultTable([heading for heading, _r in COLUMNS])
        self.results.setAccessibleName("Repository search results")
        self.results.itemDoubleClicked.connect(lambda _i: self._open_selected())

        self.preview, self.split = attach_preview(
            self.results, lambda _row: self._open_selected(), self.error.emit)

        top = QHBoxLayout()
        top.addWidget(self.input, stretch=1)
        top.addWidget(self.run_button)
        top.addWidget(self.stop_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(top)
        layout.addWidget(self.status)
        layout.addWidget(self.split, 1)
        self.set_repository("")

    # -- what it is pointed at -----------------------------------------------

    def set_repository(self, root: str) -> None:
        """Point it at a repository, or at none.

        Called as the selection in the tree above changes. Disabled rather than
        hidden when there is nothing selected: a control that vanishes is one
        somebody has to rediscover, and the reason it is off is worth saying.
        """
        self._repo = str(root or "")
        ready = bool(self._repo)
        self.input.setEnabled(ready)
        self.run_button.setEnabled(ready)
        if not ready:
            self.status.setText(
                "Select a repository above to search it — its files, its "
                "branches, or its whole history.")
            self.results.setRowCount(0)
            self.results.set_row_objects([])

    def shutdown(self) -> None:
        from app.ui.workers import stop_timers

        stop_timers(self)
        self.preview.shutdown()

    def focus(self) -> None:
        self.input.setFocus()
        self.input.selectAll()

    # -- running -------------------------------------------------------------

    def _values(self, kind: str, prefix: str, limit: int) -> list:
        """Branch, tag, author and commit names, for the `/` menu.

        **Runs on the menu's worker**, like the index's own value lookup - it
        shells out to git, which is not something to do between two keystrokes.
        """
        if not self._repo:
            return []
        from app.search.gitsearch import repo_values

        return repo_values(self._repo, kind, prefix=prefix, limit=limit)

    def start(self) -> None:
        """Run what is in the box. **Never on a keystroke** - see the module
        docstring for the measurement that decided that."""
        text = self.input.text().strip()
        if not self._repo or not text:
            return

        query = parse_git_query(text)
        plan = build(query)
        self._generation += 1
        generation = self._generation

        self.stop_button.setEnabled(True)
        self.run_button.setEnabled(False)
        # **The scope is on screen before the search starts.** "No matches"
        # means nothing until you know whether it looked at one commit or nine
        # thousand, and a slow search that says nothing reads as a hung window.
        self.status.setText(
            f"Searching {plan.explain}…"
            + ("   This one reads history, so it takes seconds rather than "
               "milliseconds." if plan.slow else ""))

        from app.search.gitsearch import run_query

        worker = CallableWorker(run_query, self._repo, query,
                                component="ui.gitsearch")
        worker.signals.finished.connect(
            lambda found, g=generation: self._show(found, g))
        worker.signals.failed.connect(
            lambda error, g=generation: self._failed(error, g))
        run(QThreadPool.globalInstance(), worker)

    def stop(self) -> None:
        """Abandon the answer. See the button's tooltip for what this does not
        do: git is left to finish or time out."""
        self._generation += 1
        self.stop_button.setEnabled(False)
        self.run_button.setEnabled(bool(self._repo))
        self.status.setText("Stopped waiting. Nothing was changed.")

    # -- drawing -------------------------------------------------------------

    def _failed(self, error: Any, generation: int) -> None:
        if generation != self._generation:
            return
        self._finished()
        self.error.emit(error)

    def _finished(self) -> None:
        self.stop_button.setEnabled(False)
        self.run_button.setEnabled(bool(self._repo))

    def _show(self, found: Any, generation: int) -> None:
        if generation != self._generation:
            return                      # stopped, or overtaken by a newer run
        self._finished()

        if not found.ok:
            # **Never silent.** git rejecting a revision or a pattern is the
            # commonest failure here, and its own message is the useful one.
            self.status.setText(
                f"git could not run that search: {found.error}\n"
                f"{' '.join(found.command)}")
            self.results.setRowCount(0)
            self.results.set_row_objects([])
            return

        rows = list(found.rows)
        self.results.setRowCount(len(rows))
        for index, row in enumerate(rows):
            for column, text in enumerate(_cells(row)):
                item = QTableWidgetItem(text)
                if column == 3:
                    item.setToolTip(row.text or row.subject)
                self.results.setItem(index, column, item)
        self.results.set_row_objects([_previewable(row, self._repo)
                                      for row in rows])

        parts = [f"{len(rows):,} result{'s' if len(rows) != 1 else ''} "
                 f"{found.explain}", f"{found.elapsed_s:.2f}s"]
        if found.truncated:
            parts.append("stopped at the limit — narrow it with /path, "
                         "/extension or /depth to see the rest")
        if not rows:
            parts.append(f"command: {' '.join(found.command)}")
        self.status.setText("  ·  ".join(parts))

    def _open_selected(self) -> None:
        row = self.results.current_row()
        path = getattr(row, "full_path", "")
        if path:
            self.open_requested.emit(path)


class _Row:
    """What the preview pane is given: a name and a path it can open.

    A deliberately tiny object rather than the `GitRow` itself, because a hit
    in a *historical* version of a file has no file on disk to preview - the
    path is only openable when the hit is in the checkout. Handing the pane a
    row whose path does not exist would show "file missing" for every history
    result, which reads as a broken preview rather than as a file that is
    genuinely no longer there.
    """

    __slots__ = ("name", "path", "full_path", "preview_text")

    def __init__(self, name: str, full_path: str, preview_text: str) -> None:
        self.name = name
        self.full_path = full_path
        self.path = full_path
        self.preview_text = preview_text


def _previewable(row: Any, repo: str) -> _Row:
    """One result, as something the preview pane can draw."""
    from pathlib import Path

    historical = bool(row.commit) and row.kind != "content"
    full = "" if historical or not row.path else str(Path(repo) / row.path)
    # A commit has no file: the pane shows the subject and the line, which is
    # what there is to show.
    text = row.text if (historical or not full) else ""
    if row.kind == "commit":
        text = f"{row.subject}\n\n{row.author}   {row.date}   {row.commit}"
    return _Row(name=row.path or row.subject or row.commit[:8],
                full_path=full, preview_text=text)


def _cells(row: Any) -> tuple[str, str, str, str]:
    """One result as four columns, whatever kind it is."""
    where = row.path or ""
    if row.line_no:
        where = f"{where}:{row.line_no}"
    what = row.text.strip() if row.text else row.subject
    if row.status in ("+", "-"):
        what = f"{row.status} {what}"
    elif row.status:
        what = f"[{row.status}] {what}".strip()
    return (row.date or "", row.author or "", where, what)
