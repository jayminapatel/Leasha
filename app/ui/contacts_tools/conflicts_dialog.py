"""Review the contacts that were changed in two places, and accept what to keep - in bulk.

Layer: L5

"Or have a mechanism to review and accept like merges" (the owner, 2026-10-12). Every clash the sync decided
(newest edit wins, or a priority) or held (ask me every time) is a row: the contact, the detail, Leasha's value,
the account's value and which one is kept now. Tick rows with the **Tick** menu or the boxes, then:

* **Keep what is there** - accept the kept value; the clash leaves the list.
* **Keep Leasha's** / **Keep the account's** - take that side for every ticked row; the other is written to the
  contact, and the next sync sends it on. One batch, so **Undo** takes it all back.

Nothing is written until a button says so. Notes changed on both sides are not here: they are joined.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QTableWidgetItem, QToolButton, QWidget

from app.ui.contacts.logic import conflict_items
from app.ui.contacts_tools import actions
from app.ui.contacts_tools._common import ToolDialog, btn, make_table, named, plural

__all__ = ["ConflictsDialog"]


class ConflictsDialog(ToolDialog):
    def __init__(self, ctx: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(ctx, parent, title="Changed in two places", glyph="triangle-alert",
                         subtitle="Details that were changed in Leasha and at an account since they last agreed. "
                                  "One value is kept for now; tick the ones to settle, and choose which side to keep.",
                         size=(980, 600), name="contacts-conflicts")
        self.hub = ctx.hub
        self.rows: list[dict] = []
        self.last_batch: Optional[str] = None

        self.summary = QLabel("")
        named(self.summary, "Summary", "How many clashes are waiting")
        self.table = make_table(self, ["Contact", "Detail", "In Leasha", "At the account", "Account", "Kept now"],
                                "Clashes", "Each row is one detail that was changed in two places", stretch=2,
                                select="multi")
        self.table.itemChanged.connect(lambda _i: self._sync())
        self.tick_button = QToolButton()
        self.tick_button.setText("Tick")
        self.tick_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.tick_button.setToolTip("Tick clashes: all, none, those where Leasha's value is kept, the account's, "
                                    "or those waiting for you")
        self.tick_button.setAccessibleName("Tick clashes")
        menu = QMenu(self.tick_button)
        for words, rule in (("All", lambda r: True), ("None", None),
                            ("Where Leasha's value is kept", lambda r: r["winner"] == "local"),
                            ("Where the account's value is kept", lambda r: r["winner"] == "remote"),
                            ("Waiting for me", lambda r: r["winner"] == "held")):
            menu.addAction(words).triggered.connect(lambda _c=False, f=rule: self.tick(f))
        self.tick_button.setMenu(menu)
        from app.ui.widgets.buttons import give_icon

        give_icon(self.tick_button)                  # "Tick" was words alone (2026-10-14)
        self.ticked_label = QLabel("")
        named(self.ticked_label, "Clashes ticked", "How many clashes are ticked")
        bar = QHBoxLayout()
        bar.addWidget(self.tick_button)
        bar.addWidget(self.ticked_label)
        bar.addStretch(1)
        self.body.addWidget(self.summary)
        self.body.addLayout(bar)
        self.body.addWidget(self.table, 1)

        self.keep_button = btn("Keep what is there", "check", "primary",
                               "Accept the value that is kept now, for every ticked clash", "Keep what is there")
        self.local_button = btn("Keep Leasha's", "user", "secondary",
                                "Use Leasha's value for every ticked clash; the account gets it at the next sync",
                                "Keep Leasha's")
        self.remote_button = btn("Keep the account's", "cloud", "secondary",
                                 "Use the account's value for every ticked clash", "Keep the account's")
        self.undo_button = btn("Undo these", "undo-2", "secondary", "Take back the values just switched", "Undo these")
        self.undo_button.hide()
        self.keep_button.clicked.connect(lambda: self.accept("kept"))
        self.local_button.clicked.connect(lambda: self.accept("local"))
        self.remote_button.clicked.connect(lambda: self.accept("remote"))
        self.undo_button.clicked.connect(self.undo)
        self.add_footer_buttons(self.keep_button, self.local_button, self.remote_button, self.undo_button)
        self.reload()

    # -- reading ------------------------------------------------------------
    def reload(self) -> None:
        self.busy(True, "Reading the clashes...")
        hub, ctx = self.hub, self.ctx
        self.job(lambda: _read(hub, ctx), self._loaded)

    def _loaded(self, rows: list[dict]) -> None:
        self.rows = rows
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        for r, item in enumerate(rows):
            kept = {"local": "Leasha's", "remote": "The account's", "held": "Waiting for you"}.get(item["winner"], "")
            cells = (item["contact_name"], item["what"], item["local"], item["remote"], item["account"], kept)
            for c, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                if c == 0:
                    cell.setFlags(cell.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    cell.setCheckState(Qt.CheckState.Unchecked)
                self.table.setItem(r, c, cell)
        self.table.blockSignals(False)
        self.table.resizeColumnsToContents()
        self.busy(False, "")
        self.summary.setText(f"{plural(len(rows), 'clash', 'clashes')} to look at." if rows
                             else "Nothing was changed in two places.")
        self._sync()

    # -- ticking ------------------------------------------------------------
    def ticked(self) -> list[dict]:
        out = []
        for r, item in enumerate(self.rows):
            cell = self.table.item(r, 0)
            if cell is not None and cell.checkState() == Qt.CheckState.Checked:
                out.append(item)
        return out

    def tick(self, rule: Any) -> None:
        self.table.blockSignals(True)
        for r, item in enumerate(self.rows):
            cell = self.table.item(r, 0)
            if cell is not None:
                cell.setCheckState(Qt.CheckState.Checked if rule is not None and rule(item)
                                   else Qt.CheckState.Unchecked)
        self.table.blockSignals(False)
        self._sync()

    def _sync(self) -> None:
        n = len(self.ticked())
        self.ticked_label.setText(f"{n} ticked" if n else "")
        for b in (self.keep_button, self.local_button, self.remote_button):
            b.setEnabled(n > 0)

    # -- accepting ----------------------------------------------------------
    def accept(self, side: str) -> None:
        """`kept` keeps each row's current value; `local` and `remote` take that side for every ticked row."""
        chosen = self.ticked()
        if not chosen:
            return
        picks = [(int(r["id"]), ("remote" if r["winner"] == "remote" else "local") if side == "kept" else side)
                 for r in chosen]
        self.busy(True, "Settling...")
        hub = self.hub
        self.job(lambda: actions.accept_conflicts(hub, picks), self._accepted)

    def _accepted(self, summary: Any) -> None:
        self.last_batch = summary.batch_id
        self.undo_button.setVisible(bool(summary.batch_id))
        self.busy(False, f"Settled {plural(summary.groups, 'clash', 'clashes')}"
                  + (f"; {summary.contacts} value(s) switched, sent on at the next sync." if summary.contacts else "."))
        self.refresh_page()
        self.reload()

    def undo(self) -> None:
        batch, hub = self.last_batch, self.hub
        if not batch:
            return
        self.job(lambda: hub.undo_batch(batch), self._undone)

    def _undone(self, result: Any) -> None:
        self.last_batch = None
        self.undo_button.hide()
        self.say(f"Undone: {plural(result.undone, 'change')} put back.")
        self.refresh_page()


def _read(hub: Any, ctx: Any) -> list[dict]:
    """On a worker: the open clashes, in words, with account names."""
    names: dict[int, str] = {}
    try:
        names = {a.id: (a.name or a.address) for a in ctx.accounts.list()}
    except Exception:                                   # noqa: BLE001 - no accounts store here: say "the account"
        pass
    out = []
    for raw in actions.open_conflict_rows(hub):
        item = conflict_items([raw], names)[0]
        item["contact_name"] = raw["contact_name"]
        item["winner"] = raw.get("winner") or ""
        out.append(item)
    return out
