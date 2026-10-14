"""The table of what each account is doing: one row for each account's contacts and mail. Draws; decides nothing.

Layer: L5 widget. Words from `app.ui.presenter.account_activity`; data from `AccountService.activity_snapshot`.
Used in the Sync window (every account) and in each account's panel (that account, without the Account column).
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QProgressBar, QTableWidget,
                               QTableWidgetItem, QToolButton, QWidget)

from app.ui.presenter.account_activity import Activity, STATE_ICONS, actions_for, describe
from app.ui.theme import theme_colours
from app.ui.widgets.icons import icon

__all__ = ["ActivityTable", "COLUMNS"]

COLUMNS = ("Account", "What", "Status", "Progress", "What it is doing", "Details", "Do")
#: A state's icon colour, as a theme token (`app.ui.theme`): red for what needs you, amber for what stopped or waits,
#: the accent for what is moving, the quiet text colour for the rest. Never a literal colour: a theme change repaints.
_TONE = {"failed": "danger", "signin": "danger", "stopped": "warning", "paused": "warning", "queued": "warning",
         "running": "accent_text",
         # Review 2026-10-14 CS-4/CS-10: what waits for you is amber, not the quiet grey of "up to date".
         "held": "warning", "partial": "warning", "mirror": "warning"}


class ActivityTable(QTableWidget):
    #: (account id, "contacts" | "mail", an `Action.key`): a button on a row was pressed.
    action_requested = Signal(int, str, str)

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
        head.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
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

    def _buttons(self, row: int, a: Activity) -> None:
        """The row's command buttons, rebuilt only when the set changes - the table is redrawn every few seconds
        and a button must not vanish under the pointer."""
        acts = actions_for(a)
        ink = theme_colours().get("text_dim", "#888888")
        key = (tuple(x.key for x in acts), ink)                   # the ink too: a theme change rebuilds them
        holder = self.cellWidget(row, 6)
        if holder is not None and holder.property("actions") == key:
            return
        holder = QWidget()
        holder.setProperty("actions", key)
        box = QHBoxLayout(holder)
        box.setContentsMargins(2, 0, 2, 0)
        box.setSpacing(2)
        for action in acts:
            button = QToolButton()
            button.setAutoRaise(True)
            button.setText(action.label)
            button.setIcon(icon(action.icon, ink))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setToolTip(f"{action.tip} ({a.what.lower()}, {a.name})")
            button.setAccessibleName(f"{action.label}: {a.name} {a.what.lower()}")
            button.clicked.connect(lambda _c=False, i=a.account_id, k=a.kind, x=action.key:
                                   self.action_requested.emit(i, k, x))
            box.addWidget(button)
        self.setCellWidget(row, 6, holder)

    def _draw(self, shown: list[Activity]) -> None:
        keys = [(a.account_id, a.kind) for a in shown]
        if keys != list(self._rows):
            self.setRowCount(len(shown))
            self._rows = {k: i for i, k in enumerate(keys)}
        colours = theme_colours()
        previous = None
        for row, a in enumerate(shown):
            ink = colours.get(_TONE.get(a.state, "text_dim"), colours.get("text_dim", "#888888"))
            status = QTableWidgetItem(icon(STATE_ICONS.get(a.state, "clock"), ink), a.word)
            status.setToolTip(a.headline)
            # An account's two rows read as one: its name once, then blank under it.
            first = QTableWidgetItem(a.name if a.account_id != previous else "")
            first.setToolTip(a.name)
            previous = a.account_id
            self.setItem(row, 0, first)
            self.setItem(row, 1, QTableWidgetItem(a.what))
            self.setItem(row, 2, status)
            bar = self.cellWidget(row, 3)
            if a.fraction is None and not a.busy:
                if bar is not None:
                    self.removeCellWidget(row, 3)                # nothing is running: no bar to read
            else:
                if not isinstance(bar, QProgressBar):
                    bar = QProgressBar()
                    bar.setTextVisible(True)
                    bar.setAccessibleName("Progress")
                    self.setCellWidget(row, 3, bar)
                if a.fraction is not None:
                    bar.setRange(0, 1000)
                    bar.setValue(int(a.fraction * 1000))
                    bar.setFormat(f"{int(a.fraction * 100)}%")
                else:
                    bar.setRange(0, 0)                           # work, no total: a moving bar
                bar.setToolTip(a.headline)
            doing = QTableWidgetItem(a.headline)
            doing.setToolTip(a.headline)
            more = QTableWidgetItem(a.detail)
            more.setToolTip(a.detail)
            self.setItem(row, 4, doing)
            self.setItem(row, 5, more)
            self._buttons(row, a)
        self.resizeRowsToContents()
