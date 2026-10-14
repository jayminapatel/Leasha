"""What the editor shows when a contact needs a decision: deleted elsewhere, and clashes.

Layer: L5

Order 1k D7.1 and 10k/D6. Two things the person can act on, drawn above the fields:

* **Deleted from <account>** (D7.1): the account no longer has this contact. "Delete here too"
  confirms the deletion (the contact goes to the Bin and leaves its other accounts at the next
  sync); "Put it back" writes it to that account again. One bar per account that deleted it.
* **Changed in two places** (D6, 10k): for each account where both sides changed. Under
  "Ask me every time" (`winner == 'held'`) both values are shown in words per item with "Keep
  Leasha's" and "Use <account>'s". The engine decides per account, so the choice covers every
  held item of that account, and the box says so. Clashes already decided by the newest edit
  show which value won; the other is kept in History.

Nothing is written here: the buttons emit, and the page runs the hub or service call on a worker.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.ui.contacts.logic import conflict_items
from app.ui.theme import SPACE_S, theme_colours
from app.ui.widgets.buttons import action_button
from app.ui.widgets.icons import icon

__all__ = ["ReviewBox", "held_items", "decided_lines"]


def held_items(items: list[dict]) -> list[dict]:
    return [i for i in items if i["winner"] == "held"]


def decided_lines(items: list[dict]) -> list[str]:
    """One plain line per clash the newest edit already decided: what won, and where the other is."""
    lines = []
    for i in items:
        if i["winner"] == "held":
            continue
        won, lost = (i["local"], i["remote"]) if i["winner"] == "local" else (i["remote"], i["local"])
        source = "Leasha" if i["winner"] == "local" else i["account"]
        other = i["account"] if i["winner"] == "local" else "Leasha"
        lines.append(f"{i['what']}: kept \"{won}\" (from {source}). The other, \"{lost}\" from {other}, "
                     "is kept in History.")
    return lines


class ReviewBox(QWidget):
    #: contact id
    delete_here_requested = Signal(int)
    #: contact id, account id
    put_back_requested = Signal(int, int)
    #: contact id, account id, 'local' | 'remote'
    resolve_requested = Signal(int, int, str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._cid: Optional[int] = None
        self._held = False
        self._decided: list[str] = []
        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(0, 0, 0, 0)
        self._column.setSpacing(SPACE_S)
        self.setAccessibleName("Decisions for this contact")
        self.hide()

    @property
    def has_held(self) -> bool:
        return self._held

    @property
    def decided(self) -> list[str]:
        return list(self._decided)

    def clear(self) -> None:
        self._cid = None
        self._held = False
        self._decided = []
        while self._column.count():
            item = self._column.takeAt(0)
            if item is not None and item.widget() is not None:
                item.widget().deleteLater()
        self.hide()

    def show_for(self, cid: int, links: list, conflicts: list, names: dict[int, str]) -> None:
        """Draw the bars for this contact (nothing at all when it needs no decision)."""
        self.clear()
        self._cid = cid
        for link in links:
            if getattr(link, "state", "") == "deleted_remote":
                self._column.addWidget(self._deleted_bar(cid, int(link.account_id),
                                                         names.get(link.account_id, "an account")))
        items = conflict_items(conflicts, names)
        self._decided = decided_lines(items)
        held = held_items(items)
        self._held = bool(held)
        for account_id in dict.fromkeys(i["account_id"] for i in held):
            self._column.addWidget(self._held_bar(cid, account_id, [i for i in held if i["account_id"] == account_id]))
        self.setVisible(self._column.count() > 0)

    # ------------------------------------------------------------------ bars
    def _frame(self, glyph: str, colour: str) -> tuple[QFrame, QVBoxLayout, QHBoxLayout]:
        frame = QFrame()
        frame.setObjectName("contactReview")
        frame.setStyleSheet(f"#contactReview {{ border: 1px solid {colour}; border-radius: 6px; }}")
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(SPACE_S, SPACE_S, SPACE_S, SPACE_S)
        head = QHBoxLayout()
        pic = QLabel()
        pic.setPixmap(icon(glyph, colour).pixmap(18, 18))
        head.addWidget(pic)
        outer.addLayout(head)
        return frame, outer, head

    def _deleted_bar(self, cid: int, account_id: int, account: str) -> QWidget:
        frame, outer, head = self._frame("unlink", theme_colours().get("warning", "#b06000"))
        title = QLabel(f"Deleted from {account}")
        title.setObjectName("contactSectionTitle")
        title.setWordWrap(True)
        head.addWidget(title, 1)
        said = QLabel("It is still here and in your other accounts. Delete it here too, or put it back "
                      f"in {account}.")
        said.setWordWrap(True)
        said.setObjectName("resultsSummary")
        outer.addWidget(said)
        delete = action_button("Delete here too", "trash-2", "danger")
        delete.setToolTip("Move this contact to the Bin; it leaves its other accounts at the next sync")
        delete.setAccessibleName(f"Delete here too, deleted from {account}")
        delete.clicked.connect(lambda _c=False: self.delete_here_requested.emit(cid))
        back = action_button("Put it back", "undo-2", "secondary")
        back.setToolTip(f"Write this contact to {account} again, at the next sync")
        back.setAccessibleName(f"Put it back in {account}")
        back.clicked.connect(lambda _c=False: self.put_back_requested.emit(cid, account_id))
        row = QHBoxLayout()
        row.addWidget(delete)
        row.addWidget(back)
        row.addStretch(1)
        outer.addLayout(row)
        return frame

    def _held_bar(self, cid: int, account_id: Any, items: list[dict]) -> QWidget:
        account = items[0]["account"]
        frame, outer, head = self._frame("triangle-alert", theme_colours().get("warning", "#b06000"))
        title = QLabel(f"Changed in two places - {account}")
        title.setObjectName("contactSectionTitle")
        title.setWordWrap(True)
        head.addWidget(title, 1)
        for i in items:
            line = QLabel(f"{i['what']}: Leasha has \"{i['local']}\"; {account} has \"{i['remote']}\".")
            line.setWordWrap(True)
            outer.addWidget(line)
        if len(items) > 1:
            same = QLabel(f"Your choice covers all {len(items)} of these for {account}.")
            same.setWordWrap(True)
            same.setObjectName("resultsSummary")
            outer.addWidget(same)
        keep = action_button("Keep Leasha's", "check", "secondary")
        keep.setToolTip(f"Keep the values Leasha has, and write them to {account}")
        keep.setAccessibleName(f"Keep Leasha's, not {account}'s")
        keep.clicked.connect(lambda _c=False: self.resolve_requested.emit(cid, int(account_id), "local"))
        use = action_button(f"Use {account}'s", "check", "secondary")
        use.setToolTip(f"Take the values {account} has, into Leasha")
        use.setAccessibleName(f"Use {account}'s values")
        use.clicked.connect(lambda _c=False: self.resolve_requested.emit(cid, int(account_id), "remote"))
        row = QHBoxLayout()
        row.addWidget(keep)
        row.addWidget(use)
        row.addStretch(1)
        outer.addLayout(row)
        return frame
