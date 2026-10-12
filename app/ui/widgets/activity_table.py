"""The table of what each account is doing: one row for each account's contacts and mail. Draws; decides nothing.

Layer: L5 widget. Words from `app.ui.presenter.account_activity`; data from `AccountService.activity_snapshot`.
Used in the Sync window (every account) and in each account's panel (that account, without the Account column).
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QHeaderView, QProgressBar, QTableWidget, QTableWidgetItem,
                               QWidget)

from app.ui.presenter.account_activity import Activity, STATE_ICONS, describe
from app.ui.theme import theme_colours
from app.ui.widgets.icons import icon

__all__ = ["ActivityTable", "COLUMNS"]

COLUMNS = ("Account", "What", "Status", "Progress", "What it is doing", "Details")
_TONE = {"failed": "danger", "signin": "danger", "running": "accent", "idle": "success"}


class ActivityTable(QTableWidget):
    def __init__(self, parent: Optional[QWidget] = None, *, single_account: bool = False) -> None:
        super().__init__(0, len(COLUMNS), parent)
        self.setHorizontalHeaderLabels(list(COLUMNS))
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setAlternatingRowColors(True)
        self.setWordWrap(True)
        self.verticalHeader().setVisible(False)
        self.setAccessibleName("What each account is doing")
        self.setToolTip("One row for each account's contacts and mail: its state, how far it has got, and what comes next")
        head = self.horizontalHeader()
        for column in (0, 1, 2):
            head.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        head.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        head.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.setColumnWidth(3, 130)
        self.setColumnHidden(0, single_account)
        self._rows: dict[tuple[int, str], int] = {}
        self.activities: list[Activity] = []

    def set_snapshots(self, snapshots: Iterable[Mapping[str, Any]], now: float,
                      live: Optional[Mapping[int, Mapping[str, Any]]] = None, *,
                      only: Optional[int] = None, hide_off: bool = True) -> list[Activity]:
        """Show `snapshots`; `live` is `{account id: the contacts progress event}` from the running sync."""
        shown: list[Activity] = []
        for snap in snapshots:
            if only is not None and int(snap.get("account_id") or 0) != only:
                continue
            if hide_off and only is None and not snap.get("enabled", True):
                continue
            snap = dict(snap)
            if snap.get("kind") == "contacts" and live and snap.get("live"):
                snap["live_progress"] = dict(live.get(int(snap["account_id"])) or {})
            shown.append(describe(snap, now))
        self.activities = shown
        self.setUpdatesEnabled(False)
        try:
            self._draw(shown)
        finally:
            self.setUpdatesEnabled(True)
        return shown

    def _draw(self, shown: list[Activity]) -> None:
        keys = [(a.account_id, a.kind) for a in shown]
        if keys != list(self._rows):
            self.setRowCount(len(shown))
            self._rows = {k: i for i, k in enumerate(keys)}
        colours = theme_colours()
        for row, a in enumerate(shown):
            ink = colours.get({"danger": "danger", "accent": "accent", "success": "success"}.get(
                _TONE.get(a.state, ""), "text_dim"), colours.get("text_dim", "#888888"))
            status = QTableWidgetItem(icon(STATE_ICONS.get(a.state, "clock"), ink), a.word)
            status.setToolTip(a.headline)
            self.setItem(row, 0, QTableWidgetItem(a.name))
            self.setItem(row, 1, QTableWidgetItem(a.what))
            self.setItem(row, 2, status)
            bar = self.cellWidget(row, 3)
            if not isinstance(bar, QProgressBar):
                bar = QProgressBar()
                bar.setTextVisible(True)
                bar.setAccessibleName("Progress")
                self.setCellWidget(row, 3, bar)
            if a.fraction is not None:
                bar.setRange(0, 1000)
                bar.setValue(int(a.fraction * 1000))
                bar.setFormat(f"{int(a.fraction * 100)}%")
            elif a.busy:
                bar.setRange(0, 0)                               # work, no total: a moving bar
            else:
                bar.setRange(0, 1)
                bar.setValue(0)
                bar.setFormat("")
            bar.setToolTip(a.headline)
            doing = QTableWidgetItem(a.headline)
            doing.setToolTip(a.headline)
            more = QTableWidgetItem(a.detail)
            more.setToolTip(a.detail)
            self.setItem(row, 4, doing)
            self.setItem(row, 5, more)
        self.resizeRowsToContents()
