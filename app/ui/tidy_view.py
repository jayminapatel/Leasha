r"""The Tidy page: what is indexed, and the copies, similar photos and leftovers to clear.

Layer: L5, driving L4 (`app/tidy/`).

Order 1q (2026-10-11), the owner: "think of that as a place where i find what is
indexed, duplicate remover, similar pictures remover etc". It took the Reports
page's place on the rail (D8). The left list is the 1p `SideList`; the right side
is the picked entry. What is marked lives in `presenter.tidy_marks.TidyMarks`
until **Review and remove...**: the plan is shown, asked about (twice for a big
batch, D7), and carried out on a worker by `app.tidy.remove`, which checks every
file again before it moves (D2, D3). **This view moves nothing itself.**
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QThreadPool, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QSplitter, QStackedWidget, QTextBrowser,
    QVBoxLayout, QWidget,
)

from app.ui.presenter import tidy as words
from app.ui.presenter.tidy_marks import TidyMarks
from app.ui.reports_view import ReportsView
from app.ui.widgets.side_list import SideList
from app.ui.widgets.tidy_parts import HistoryPane, read_file_sets, read_quality, read_snapshot
from app.ui.widgets.tidy_set_pane import TidySetPane
from app.ui.workers import CallableWorker, run

__all__ = ["TidyView", "RULE_KEY"]

#: D4: the keep rule, remembered.
RULE_KEY = "ui:tidy_keep_rule"
_SET_KINDS = ("copies", "similar", "large", "leftovers")



class TidyView(QWidget):
    """A list on the left; Overview, a set, the Space Report or History on the right."""

    error = Signal(object)

    def __init__(self, store: Any = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._snapshot: Any = None
        self._kind = "overview"
        #: D5: the sets "Find copies of this..." opened, shown as kind "file".
        self._file_list: list = []
        self._file_path = ""
        self._loading = self._refresh_again = False
        #: The last thing said about a removal or a put back, kept past the reload it starts.
        self._said = ""
        try:
            saved = str(store.get_state(RULE_KEY, "") or "") if store is not None else ""
        except Exception:                        # noqa: BLE001 - the default rule
            saved = ""
        self.marks = TidyMarks(saved)

        self.list = SideList("tidySide", accessible="Tidy", shown=None,
                             tip="Pick what to look at; nothing moves until you review it")
        self.list.picked.connect(lambda entry: self.show_kind(str(entry.value)))
        self.list.set_entries(words.side_entries(words.TidyCounts()))
        self.queue = QLabel(words.queue_words(0, 0))
        self.queue.setObjectName("resultsSummary")
        self.review = QPushButton("Review and remove…")
        self.review.setToolTip("See every marked file, then move them to the Recycle Bin. "
                               "Each is checked again first, and one copy of anything always stays.")
        self.review.clicked.connect(self._review)
        self.progress = QLabel("")
        self.progress.setObjectName("resultsSummary")
        bar = QHBoxLayout()
        bar.addWidget(self.progress, 1)
        bar.addWidget(self.queue)
        bar.addWidget(self.review)

        self.overview = QTextBrowser()
        self.overview.setAccessibleName("What is indexed")
        self.only = QTextBrowser()
        self.only.setAccessibleName("Files with only one copy")
        self.sets = TidySetPane()
        self.sets.bind(lambda path: path in self.marks.marked,
                       lambda tset: words.set_sentence(tset, self.marks.marked))
        self.sets.set_rule(self.marks.rule)
        self.sets.bind_store(store)
        self.sets.toggled.connect(self._toggled)
        for signal, change in ((self.sets.toggled_in, self.marks.toggle), (self.sets.keep_only_in, self.marks.keep_only)):
            for slot in (change, self._update_queue, self.sets.refresh_marks):     # the List tab's ticks, for any set
                signal.connect(slot)
        self.sets.error.connect(self.error.emit)
        self.sets.keep_only.connect(self._keep_only)
        self.sets.rule_chosen.connect(self._rule_chosen)
        self.sets.mark_all.connect(self._mark_all)
        self.sets.showing.connect(self._apply_rule)
        self.report = ReportsView(store)
        self.report.error.connect(self.error.emit)
        self.history = HistoryPane()
        self.history.put_back.connect(self._put_back)
        self.stack = QStackedWidget()
        self._panes = {"overview": self.overview, "only": self.only, "report": self.report,
                       "history": self.history}
        for pane in (self.overview, self.sets, self.only, self.report, self.history):
            self.stack.addWidget(pane)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.addLayout(bar)
        right_layout.addWidget(self.stack, 1)
        self.split = QSplitter()
        self.split.addWidget(self.list)
        self.split.addWidget(right)
        self.split.setStretchFactor(0, 1)
        self.split.setStretchFactor(1, 4)
        QVBoxLayout(self).addWidget(self.split, 1)
        self.list.select("overview")
        self._update_queue()

    # -- loading -----------------------------------------------------------------------

    def refresh(self, *, force: bool = False) -> None:
        """Read again on a worker each time the page comes forward - only what moved
        (`read_snapshot`). One load at a time; one asked for meanwhile runs after it."""
        if self._store is None:
            return
        if self._loading:
            self._refresh_again = True
            return
        self._loading = True
        last = () if force or self._snapshot is None else self._snapshot.stamp
        worker = CallableWorker(read_snapshot, self._store, last, component="ui.tidy",
                                report_progress=True)
        worker.signals.progress.connect(lambda text: self.progress.setText(str(text)))
        worker.signals.finished.connect(self._loaded)
        worker.signals.failed.connect(self.error.emit)
        worker.signals.done.connect(self._done)
        run(QThreadPool.globalInstance(), worker)
        if self._kind == "report":
            self.report.refresh()

    def _done(self) -> None:
        self._loading = False
        self.progress.setText(self._said)
        if self._refresh_again:
            self._refresh_again = False
            self.refresh(force=True)

    def _loaded(self, snapshot: Any) -> None:
        if snapshot is None:
            return                                 # nothing moved since the last load
        self._snapshot = snapshot
        self.list.set_entries(words.side_entries(snapshot.counts))
        self.overview.setMarkdown(snapshot.overview)
        self.only.setMarkdown(snapshot.only)
        self.history.set_batches(snapshot.batches)
        self.marks.keep_only_live({f.path for s in self._all_sets() for f in s.files})
        self.show_kind(self._kind)

    # -- what is shown -----------------------------------------------------------------

    def show_kind(self, kind: str) -> None:
        self._kind = kind
        if kind == "file" or kind in _SET_KINDS:
            counts = self._snapshot.counts if self._snapshot is not None else None
            self.sets.found = {"copies": getattr(counts, "copy_sets", 0),
                               "similar": getattr(counts, "similar_sets", 0)}.get(kind, 0)
            self.sets.set_sets(self._file_list if kind == "file" else self._sets_of(kind))
            self.stack.setCurrentWidget(self.sets)
            if kind == "file" and not self._file_list:
                self.sets.why.setText(f"Leasha has not found a copy of {self._file_path} on this computer.")
        else:
            self.stack.setCurrentWidget(self._panes.get(kind, self.overview))
            if kind == "report":
                self.report.refresh()
        self.list.select("copies" if kind == "file" else kind)

    def open_file(self, path: str) -> None:
        """D5, "Find copies of this...": that file's sets, nothing marked."""
        if self._store is None:
            return
        worker = CallableWorker(read_file_sets, self._store, path, component="ui.tidy.file")
        worker.signals.finished.connect(self._file_sets)
        worker.signals.failed.connect(self.error.emit)
        run(QThreadPool.globalInstance(), worker)

    def _file_sets(self, result: tuple) -> None:
        self._file_path, sets = result
        self.marks.settle(sets)                   # D5 is by hand: no rule marks them
        self._file_list = list(sets)
        self.show_kind("file")

    def _sets_of(self, kind: str) -> list:
        return self._snapshot.sets.get(kind, []) if self._snapshot is not None else []

    def _all_sets(self) -> list:
        sets = [s for kind in _SET_KINDS for s in self._sets_of(kind)]
        keys = {s.key for s in sets}
        return sets + [s for s in self._file_list if s.key not in keys]

    # -- marks -------------------------------------------------------------------------

    def _apply_rule(self, tset: Any) -> None:
        """A set coming into view is marked by the rule, unless it is settled."""
        if self.marks.needs_quality(tset):
            worker = CallableWorker(read_quality, tset, component="ui.tidy.quality")
            worker.signals.finished.connect(self._quality_in)
            run(QThreadPool.globalInstance(), worker)
        elif self.marks.apply_rule(tset):
            self._update_queue()

    def _quality_in(self, result: tuple) -> None:
        key, quality = result
        self.marks.set_quality(key, quality)
        current = self.sets.current()
        if current is not None and current.key == key and self.marks.apply_rule(current):
            self._update_queue()
            self.sets.refresh_marks()

    def _toggled(self, path: str, on: bool) -> None:
        self.marks.toggle(self.sets.current(), path, on)
        self._update_queue()
        self.sets.why.setText(words.set_sentence(self.sets.current(), self.marks.marked))

    def _keep_only(self, path: str) -> None:
        if self.sets.current() is not None:
            self.marks.keep_only(self.sets.current(), path)
            self._update_queue()
            self.sets.refresh_marks()

    def _rule_chosen(self, rule: str) -> None:
        from app.ui.state_writes import save_state

        if self._store is not None:
            save_state(self._store, RULE_KEY, rule, component="ui.tidy.rule")
        self.marks.choose_rule(rule, self.sets.current())
        self._update_queue()
        self.sets.refresh_marks()

    def _mark_all(self) -> None:
        self.marks.mark_all(self._sets_of("copies"))
        self._update_queue()
        self.sets.refresh_marks()

    def _update_queue(self) -> None:
        plan = self.marks.plan(self._all_sets())
        self.queue.setText(words.queue_words(plan.files, plan.size_bytes))
        self.review.setEnabled(plan.files > 0)

    # -- removing and putting back --------------------------------------------------------

    def ask(self, title: str, text: str) -> bool:
        """The question. Overridable, so tests need no dialog."""
        answer = QMessageBox.question(self, title, text, QMessageBox.StandardButton.Yes
                                      | QMessageBox.StandardButton.Cancel, QMessageBox.StandardButton.Cancel)
        return answer == QMessageBox.StandardButton.Yes

    def _review(self) -> None:
        from app.tidy.remove import carry_out

        plan = self.marks.plan(self._all_sets())
        if not plan.items or not self.ask(*words.plan_question(plan.files, plan.size_bytes, len(plan.refused))):
            return
        second = plan.needs_second_yes and self.ask(*words.big_question(plan.files, plan.size_bytes))
        if plan.needs_second_yes and not second:
            return
        worker = CallableWorker(carry_out, self._store, plan, yes=True, second_yes=second,
                                component="ui.tidy.remove", report_progress=True)
        worker.signals.progress.connect(lambda text: self.progress.setText(str(text)))
        worker.signals.finished.connect(self._removed)
        worker.signals.failed.connect(self.error.emit)
        run(QThreadPool.globalInstance(), worker)

    def _removed(self, outcome: Any) -> None:
        from dataclasses import replace

        moved = {i.file.path for i in outcome.moved}
        self.marks.forget(moved)
        self._said = words.outcome_words(len(outcome.moved), outcome.size_moved, outcome.left)
        self.progress.setText(self._said)
        kept = [replace(s, files=tuple(f for f in s.files if f.path not in moved)) for s in self._file_list]
        self._file_list = [s for s in kept if len(s.files) > 1]
        self.refresh(force=True)

    def _put_back(self, batch_id: str) -> None:
        from app.tidy.remove import put_back

        worker = CallableWorker(put_back, self._store, batch_id, component="ui.tidy.put_back")
        worker.signals.finished.connect(self._put_back_done)
        worker.signals.failed.connect(self.error.emit)
        run(QThreadPool.globalInstance(), worker)

    def _put_back_done(self, outcome: Any) -> None:
        self._said = words.put_back_words(len(outcome.moved), outcome.left)
        self.progress.setText(self._said)
        self.refresh(force=True)

    def retint(self, colours: dict) -> None:
        """Icons follow the theme: the list's and the set's views."""
        self.list.retint(colours)
        self.sets.retint(colours)
