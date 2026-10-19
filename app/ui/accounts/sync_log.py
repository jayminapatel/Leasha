"""The sync log (10s): what every sync did, with the fix for what went wrong.

Layer: L5

A table filtered by account and kind. A row can be undone (3e), a first-sync dry
run shows its plan with Approve, and a sync the delete brake held shows what it
was about to do with Allow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QLabel, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from app.ui.accounts import logic, parts
from app.ui.accounts.backend import Backend
from app.ui.accounts.parts import btn, hint, notice
from app.ui.widgets.row_ring import install_row_ring

__all__ = ["SyncLog", "ask_save_path"]

COLUMNS = ("Time", "Account", "Kind", "Started by", "Added", "Changed", "Deleted", "Held", "Took",
           "Result", "Problem and how to fix it")
_RUN = Qt.ItemDataRole.UserRole


def _undo(factory: Any, run_id: int) -> Any:
    return factory().undo_run(run_id)


def _write(path: str, text: str) -> str:
    Path(path).write_text(text, encoding="utf-8", newline="")
    return path


def ask_save_path(parent: Optional[QWidget]) -> str:
    """Where to save the CSV; "" when cancelled. Tests replace this function."""
    path, _f = QFileDialog.getSaveFileName(parent, "Export the sync log", "sync-log.csv", "CSV (*.csv)")
    return path


class SyncLog(QWidget):
    approve_requested = Signal(int)       # account id
    allow_requested = Signal(int)         # account id

    def __init__(self, backend: Backend, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.backend = backend
        self._runs: list[Any] = []
        self._shown: list[Any] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        self.account_filter = QComboBox()
        self.account_filter.setAccessibleName("Show the log of")
        self.kind_filter = QComboBox()
        self.kind_filter.setAccessibleName("Show the log for")
        self.kind_filter.addItem("Mail and contacts", "")
        self.kind_filter.addItem("Contacts", "contacts")
        self.kind_filter.addItem("Mail", "mail")
        self.account_filter.setToolTip("Show the syncs of one account, or of all.")
        self.kind_filter.setToolTip("Show contact syncs, mail syncs, or both.")
        row.addWidget(QLabel("Show"))
        row.addWidget(self.account_filter)
        row.addWidget(self.kind_filter)
        row.addStretch(1)
        lay.addLayout(row)
        self.tree = QTreeWidget()
        install_row_ring(self.tree)
        self.tree.setObjectName("syncLog")
        self.tree.setAccessibleName("Sync log")
        self.tree.setHeaderLabels(list(COLUMNS))
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setMinimumHeight(180)
        lay.addWidget(self.tree)
        self.plan = QLabel("")
        self.plan.setWordWrap(True)
        self.plan.setVisible(False)
        lay.addWidget(self.plan)
        buttons = QHBoxLayout()
        self.undo = btn("Undo this sync", "history", tip="Put back what the selected sync changed")
        self.approve = btn("Approve", "circle-check", tip="Run the first sync of this account for real")
        self.allow = btn("Allow", "shield-check", tip="Let the held sync go ahead and delete")
        self.export = btn("Export log", "download", tip="Save the log shown as a CSV file")
        for b in (self.undo, self.approve, self.allow, self.export):
            buttons.addWidget(b)
        buttons.addStretch(1)
        lay.addLayout(buttons)
        lay.addWidget(hint("Undo puts the contact book back as it was before that sync and writes the "
                           "accounts to match. Approve and Allow are only offered where a sync is waiting "
                           "for you."))
        self.notice = notice()
        lay.addWidget(self.notice)
        self.account_filter.currentIndexChanged.connect(lambda _i: self._fill())
        self.kind_filter.currentIndexChanged.connect(lambda _i: self._fill())
        self.tree.itemSelectionChanged.connect(self._selection)
        self.undo.clicked.connect(self._undo)
        self.export.clicked.connect(self._export)
        self.approve.clicked.connect(lambda: self.approve_requested.emit(self._selected().account_id))
        self.allow.clicked.connect(self._allow)
        self._selection()

    def set_runs(self, runs: list[Any], accounts: list[Any]) -> None:
        self._runs = list(runs)
        keep = self.account_filter.currentData()
        blocked = self.account_filter.blockSignals(True)
        self.account_filter.clear()
        self.account_filter.addItem("All accounts", 0)
        for a in accounts:
            self.account_filter.addItem(a.name, a.id)
        self.account_filter.setCurrentIndex(max(0, self.account_filter.findData(keep or 0)))
        self.account_filter.blockSignals(blocked)
        self._fill()

    def _fill(self) -> None:
        aid = int(self.account_filter.currentData() or 0)
        kind = str(self.kind_filter.currentData() or "")
        self._shown = [r for r in self._runs if (not aid or r.account_id == aid) and (not kind or r.kind == kind)]
        self.tree.clear()
        for r in self._shown:
            d = logic.run_row(r)
            problem = d["error"] + (f"  FIX: {d['fix']}" if d["fix"] else "")
            item = QTreeWidgetItem([d["time"], d["account"], d["kind"], d["trigger"], d["added"], d["changed"],
                                    d["deleted"], d["held"], d["took"], d["result"], problem])
            item.setData(0, _RUN, r.id)
            item.setToolTip(10, problem)
            self.tree.addTopLevelItem(item)
        for c in range(len(COLUMNS) - 1):
            self.tree.resizeColumnToContents(c)
        self._selection()

    def _selected(self) -> Any:
        item = self.tree.currentItem()
        if item is None or not item.isSelected():
            return None
        rid = item.data(0, _RUN)
        return next((r for r in self._shown if r.id == rid), None)

    def _selection(self) -> None:
        run = self._selected()
        mine = [r for r in self._runs if run is not None and r.account_id == run.account_id
                and r.kind == run.kind]
        latest = bool(mine) and mine[0].id == run.id
        self.undo.setEnabled(run is not None and logic.can_undo(run))
        self.approve.setVisible(run is not None and logic.can_approve(run, latest))
        self.allow.setVisible(run is not None and logic.can_allow(run))
        text = logic.plan_text(run) if run is not None and run.status in ("dry-run", "needs-approval", "held") else ""
        self.plan.setText(text)
        self.plan.setVisible(bool(text))

    def _undo(self) -> None:
        run = self._selected()
        if run is None:
            return
        if not parts.confirm(
                self, "Undo this sync?",
                f"This puts back what the sync of {run.account_name} changed in your contact book "
                f"({run.added} added, {run.changed} changed, {run.deleted} deleted), and writes the "
                "accounts to match, so contacts it added go away and contacts it changed return to how "
                "they were. Later edits to those same contacts are not kept.", "Undo this sync", danger=True):
            return
        self.say("Undoing…")
        self.backend.call(_undo, self.backend.factory, run.id, serial=True, refresh=True,
                          done=lambda _r: self.say("Undone."),
                          fail=lambda e: self.say(f"{getattr(e, 'message', e)} {getattr(e, 'suggestion', '')}"))

    def _allow(self) -> None:
        """Allow on the held run (CS-2): ask first, naming what was held, then ask the page to sync."""
        from app.ui.presenter.account_actions import allow_question  # noqa: PLC0415
        run = self._selected()
        if run is None:
            return
        if parts.ask_allow(self, allow_question(run.account_name, run.plan, run.error_code or "",
                                                run.error_text or "")):
            self.allow_requested.emit(run.account_id)

    def _export(self) -> None:
        path = ask_save_path(self)
        if not path:
            return
        text = logic.runs_csv(self._shown)
        self.backend.call(_write, path, text, done=lambda p: self.say(f"Saved {p}."),
                          fail=lambda e: self.say(f"{getattr(e, 'message', e)} {getattr(e, 'suggestion', '')}"))

    def say(self, text: str) -> None:
        self.notice.setText(text)
        self.notice.setVisible(bool(text))
