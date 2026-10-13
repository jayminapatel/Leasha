"""The Duplicates screen (order 1k 6n).

Layer: L5

A scan of the whole book groups contacts that look like one person, each group showing why
("same email", "same phone", "same name and company"), a confidence and a total ("14 groups,
31 contacts"). The scan runs on a worker and its result is kept until the book changes
(`actions.scan_book`).

* **Merge all safe matches** lists what it will do first, then merges only the groups flagged
  safe (an exact email or an exact normalised phone number; D9) as ONE journal batch. The
  **Undo this merge** button that follows calls `hub.undo_batch` and restores them all.
* **Not a duplicate** remembers every pair of the selected group (`hub.reject_pair`); the
  **Rejected** tab lists them and **Suggest again** takes them back (`hub.unreject_pair`).
* **Review** opens the merge dialog (6g) for the selected group.

Nothing is written until a button says so, and every write is a journal batch.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QMenu, QPlainTextEdit, QTableWidgetItem, QTabWidget, QToolButton, QVBoxLayout,
    QWidget,
)

from app.contacts.card import display_name
from app.ui.contacts_tools import actions
from app.ui.contacts_tools._common import ToolDialog, btn, ink, make_table, named, plural
from app.ui.widgets.icons import icon
from app.ui.widgets.select_all import SelectBar
from app.ui.widgets.dialog_frame import frame

__all__ = ["DuplicatesDialog", "PlanDialog"]


class PlanDialog(QDialog):
    """The list of what 'Merge all safe matches' is about to do, with OK / Cancel."""

    def __init__(self, steps: list, parent: Optional[QWidget] = None, *, title: str = "Merge all safe matches",
                 why: str = "Only exact email or phone matches are here. You can undo all of it in one step.") -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setAccessibleName("What will be merged")
        self.resize(560, 420)
        layout = frame(self)                      # the one dialog frame (UI phase B)
        intro = QLabel(f"These {plural(len(steps), 'group')} will each become one contact. {why}")
        intro.setWordWrap(True)
        named(intro, "What this will do", "What this will do")
        text = QPlainTextEdit()
        text.setReadOnly(True)
        lines = []
        for s in steps:
            lines.append(f"{' + '.join(s.names)}  ->  {s.names[s.ids.index(s.survivor)]}   ({', '.join(s.reasons)})")
        text.setPlainText("\n".join(lines))
        named(text, "Groups to merge", "Every group that will be merged")
        row = QHBoxLayout()
        row.addStretch(1)
        self.cancel = btn("Cancel", "x", "secondary", "Do not merge anything", "Cancel")
        self.ok = btn("Merge these", "merge", "primary", "Merge every group listed", "Merge these")
        self.cancel.clicked.connect(self.reject)
        self.ok.clicked.connect(self.accept)
        row.addWidget(self.cancel)
        row.addWidget(self.ok)
        layout.addWidget(intro)
        layout.addWidget(text, 1)
        layout.addLayout(row)


class DuplicatesDialog(ToolDialog):
    def __init__(self, ctx: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(ctx, parent, title="Possible duplicates", glyph="merge",
                         subtitle="People who are in your book more than once. Nothing changes until you press a button, "
                                  "and every merge can be undone.",
                         size=(940, 640), name="contacts-duplicates")
        self.hub = ctx.hub
        self.scan = None
        self.last_batch: Optional[str] = None
        self.last_pairs: list[tuple[int, int]] = []

        self.summary = QLabel("")
        named(self.summary, "Summary", "How many groups and contacts were found")
        self.tabs = QTabWidget()
        named(self.tabs, "Duplicates and rejected", "Switch between the groups found and the ones you rejected")

        groups = QWidget()
        gl = QVBoxLayout(groups)
        gl.setContentsMargins(0, 6, 0, 0)
        self.table = make_table(self, ["Why", "Confidence", "Contacts", "Merge all safe?"], "Duplicate groups",
                                "Each row is a group of contacts that may be one person", stretch=2)
        self.table.itemDoubleClicked.connect(lambda _i: self.review())
        self.table.itemSelectionChanged.connect(self._sync_buttons)
        self.table.itemChanged.connect(lambda _i: self._sync_buttons())
        # Tick which groups to merge: all, none, the safe ones, the ones above a confidence - so the
        # low-confidence ones can be reviewed and accepted in one go instead of one dialog each.
        self.tick_button = QToolButton()
        self.tick_button.setText("Tick")
        self.tick_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.tick_button.setToolTip("Tick groups to merge: all, none, the safe ones, or those above a confidence")
        self.tick_button.setAccessibleName("Tick groups")
        menu = QMenu(self.tick_button)
        for words, rule in (("All groups", lambda g: True), ("None", None),
                            ("Only the safe ones", lambda g: g.safe),
                            ("Only the ones to look at first", lambda g: not g.safe),
                            ("Confidence 90% or more", lambda g: g.confidence >= 0.9),
                            ("Confidence 70% or more", lambda g: g.confidence >= 0.7)):
            menu.addAction(words).triggered.connect(lambda _c=False, r=rule: self.tick(r))
        self.tick_button.setMenu(menu)
        self.ticked_label = QLabel("")
        named(self.ticked_label, "Groups ticked", "How many groups are ticked to merge")
        gl.addLayout(_row(self.tick_button, self.ticked_label))
        gl.addWidget(self.table, 1)
        self.tabs.addTab(groups, icon("merge", ink()), "Groups")

        rejected = QWidget()
        rl = QVBoxLayout(rejected)
        rl.setContentsMargins(0, 6, 0, 0)
        self.rejected_table = make_table(self, ["Contact", "Not the same as"], "Rejected pairs",
                                         "Pairs you said are not duplicates", stretch=1, select="multi")
        self.rejected_table.itemSelectionChanged.connect(self._sync_buttons)
        self.reject_select = SelectBar(self.rejected_table, "rejected pairs")
        self.reject_select.changed.connect(lambda _n: self._sync_buttons())
        rl.addWidget(self.reject_select)
        rl.addWidget(self.rejected_table, 1)
        self.suggest_button = btn("Suggest again", "undo-2", "secondary",
                                  "Put the selected pairs back so they can be suggested again", "Suggest again")
        self.suggest_button.clicked.connect(self.suggest_selected)
        rl.addLayout(_row(self.suggest_button))
        self.tabs.addTab(rejected, icon("ban", ink()), "Rejected")

        self.body.addWidget(self.summary)
        self.body.addWidget(self.tabs, 1)

        self.scan_button = btn("Scan again", "refresh-cw", "secondary", "Look through the whole book again", "Scan again")
        self.safe_button = btn("Merge all safe matches", "merge", "primary",
                               "Merge every group matched by an exact email or phone number, after showing you the list",
                               "Merge all safe matches")
        self.ticked_button = btn("Merge ticked", "check-check", "primary",
                                 "Merge the groups with a tick in the first column, after showing you the list",
                                 "Merge ticked")
        self.review_button = btn("Review", "eye", "secondary", "Open the selected group side by side", "Review")
        self.not_button = btn("Not a duplicate", "ban", "secondary",
                              "Remember that the selected group are different people", "Not a duplicate")
        self.undo_button = btn("Undo this merge", "undo-2", "secondary",
                               "Split every contact that was just merged", "Undo this merge")
        self.undo_button.hide()
        self.scan_button.clicked.connect(lambda: self.rescan(force=True))
        self.safe_button.clicked.connect(self.confirm_and_merge_safe)
        self.ticked_button.clicked.connect(self.confirm_and_merge_ticked)
        self.review_button.clicked.connect(self.review)
        self.not_button.clicked.connect(self.not_a_duplicate)
        self.undo_button.clicked.connect(self.undo_merge)
        self.add_footer_buttons(self.safe_button, self.ticked_button, self.review_button, self.not_button, self.undo_button, self.scan_button)
        self._sync_buttons()
        self.rescan()

    # -- scan ----------------------------------------------------------------
    def rescan(self, force: bool = False) -> None:
        self.busy(True, "Looking through your contacts…")
        self.scan_button.setEnabled(False)
        hub = self.hub
        self.job(lambda: (actions.scan_book(hub, use_cache=not force), actions.rejected_rows(hub)), self._scanned)

    def _scanned(self, payload: Any) -> None:
        result, rejected = payload
        self.scan = result
        self.scan_button.setEnabled(True)
        self.busy(False)
        self._fill_groups()
        self._fill_rejected(rejected)
        self._sync_buttons()
        self.say("Shown from the last scan; nothing has changed since." if result.cached else "")

    def _fill_groups(self) -> None:
        groups = self.scan.groups if self.scan else []
        cards = self.scan.cards if self.scan else {}
        self.table.blockSignals(True)
        self.table.setRowCount(len(groups))
        for row, g in enumerate(groups):
            why = ", ".join(g.reasons) or "similar"
            names = "  +  ".join(display_name(cards[i]) if i in cards else str(i) for i in g.ids)
            safe = QTableWidgetItem(icon("circle-check" if g.safe else "eye", ink()), "Safe" if g.safe else "Look first")
            safe.setToolTip("An exact email or phone number matches" if g.safe else "Check this one by eye")
            for col, text in enumerate((why, f"{round(g.confidence * 100)}%", names)):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, row)
                if col == 0:
                    item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Unchecked)
                self.table.setItem(row, col, item)
            self.table.setItem(row, 3, safe)
        self.table.blockSignals(False)
        if groups:
            self.table.selectRow(0)
        total = len(groups)
        contacts = self.scan.contacts if self.scan else 0
        safe_n = sum(1 for g in groups if g.safe)
        if total:
            self.summary.setText(f"{plural(total, 'group')}, {plural(contacts, 'contact')}"
                                 f"  ·  {safe_n} safe to merge in one go")
        else:
            self.summary.setText("No duplicates found.")

    def _fill_rejected(self, rows: list) -> None:
        self.rejected_table.setRowCount(len(rows))
        for r, (a, b, na, nb) in enumerate(rows):
            first = QTableWidgetItem(na)
            first.setData(Qt.ItemDataRole.UserRole, (a, b))
            self.rejected_table.setItem(r, 0, first)
            self.rejected_table.setItem(r, 1, QTableWidgetItem(nb))
        self.tabs.setTabText(1, f"Rejected ({len(rows)})")

    # -- selection -----------------------------------------------------------
    def selected_group(self):
        if not self.scan:
            return None
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        index = rows[0].row()
        return self.scan.groups[index] if 0 <= index < len(self.scan.groups) else None

    def ticked_groups(self) -> list:
        """The groups with a tick, in the order shown."""
        if not self.scan:
            return []
        out = []
        for row, g in enumerate(self.scan.groups):
            item = self.table.item(row, 0)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                out.append(g)
        return out

    def tick(self, rule: Any) -> None:
        """Tick the groups `rule` accepts and untick the rest; None unticks all."""
        groups = self.scan.groups if self.scan else []
        self.table.blockSignals(True)
        for row, g in enumerate(groups):
            item = self.table.item(row, 0)
            if item is not None:
                item.setCheckState(Qt.CheckState.Checked if rule is not None and rule(g) else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self._sync_buttons()

    def confirm_and_merge_ticked(self) -> None:
        groups = self.ticked_groups()
        steps = actions.plan_chosen_merges(groups, self.scan.cards, self.hub.region) if groups and self.scan else []
        if not steps:
            self.say("Nothing is ticked: use Tick, or the box at the start of a row.")
            return
        plan = PlanDialog(steps, self, title="Merge ticked groups",
                          why="You ticked these. Look at the list; you can undo all of it in one step.")
        if plan.exec() == QDialog.DialogCode.Accepted:
            self.run_merge_safe(steps)

    def _sync_buttons(self) -> None:
        ticked = len(self.ticked_groups())
        self.ticked_label.setText(f"{ticked} ticked" if ticked else "")
        self.ticked_button.setEnabled(ticked > 0)
        self.ticked_button.setText(f"Merge ticked ({ticked})" if ticked else "Merge ticked")
        group = self.selected_group()
        self.review_button.setEnabled(group is not None)
        self.not_button.setEnabled(group is not None)
        self.safe_button.setEnabled(bool(self.scan and any(g.safe for g in self.scan.groups)))
        self.suggest_button.setEnabled(bool(self.rejected_table.selectionModel().selectedRows()))

    # -- merge all safe ------------------------------------------------------
    def plan_safe(self) -> list:
        """What 'Merge all safe matches' would do, from the last scan. Writes nothing."""
        if not self.scan:
            return []
        return actions.plan_safe_merges(self.scan.groups, self.scan.cards, self.hub.region)

    def confirm_and_merge_safe(self) -> None:
        steps = self.plan_safe()
        if not steps:
            self.say("There is nothing safe to merge: every group needs a look first.")
            return
        if PlanDialog(steps, self).exec() == QDialog.DialogCode.Accepted:
            self.run_merge_safe(steps)

    def run_merge_safe(self, steps: list) -> None:
        self.busy(True, "Merging…")
        self.safe_button.setEnabled(False)
        hub = self.hub
        self.job(lambda: actions.merge_safe(hub, steps), self._merged)

    def _merged(self, summary: Any) -> None:
        self.last_batch = summary.batch_id
        self.undo_button.setVisible(bool(summary.batch_id))
        self.busy(False, f"Merged {plural(summary.groups, 'group')} into one contact each."
                  if summary.batch_id else "Nothing was merged.")
        self.refresh_page()
        self.rescan(force=True)

    def undo_merge(self) -> None:
        batch = self.last_batch
        if not batch:
            return
        self.busy(True, "Splitting them again…")
        hub = self.hub
        self.job(lambda: (hub.undo_batch(batch), actions.forget_scan(hub))[0], self._undone)

    def _undone(self, result: Any) -> None:
        self.last_batch = None
        self.undo_button.hide()
        self.busy(False, f"Undone: {plural(result.undone, 'change')} put back.")
        self.refresh_page()
        self.rescan(force=True)

    # -- one group -----------------------------------------------------------
    def review(self) -> None:
        group = self.selected_group()
        if group is None:
            return
        from app.ui.contacts_tools.merge_dialog import MergeDialog          # noqa: PLC0415

        dialog = MergeDialog(self.ctx, list(group.ids), self)
        dialog.exec()
        self.rescan(force=True)

    def not_a_duplicate(self) -> None:
        group = self.selected_group()
        if group is None:
            return
        ids = list(group.ids)
        hub = self.hub
        self.job(lambda: (actions.reject_group(hub, ids), actions.forget_scan(hub))[0], self._rejected)

    def _rejected(self, pairs: int) -> None:
        self.say("Remembered. These will not be suggested again; find them under Rejected.")
        self.rescan(force=True)

    def suggest_selected(self) -> None:
        pairs = []
        for index in self.rejected_table.selectionModel().selectedRows():
            data = self.rejected_table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole)
            if data:
                pairs.append(tuple(data))
        if not pairs:
            return
        hub = self.hub
        self.job(lambda: (actions.suggest_again(hub, pairs), actions.forget_scan(hub))[0], self._suggested)

    def _suggested(self, n: int) -> None:
        self.say(f"{plural(n, 'pair')} can be suggested again.")
        self.rescan(force=True)


def _row(*buttons: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(8)
    for b in buttons:
        row.addWidget(b)
    row.addStretch(1)
    return row
