"""The model behind both views: a table of `ContactRow`s, sorted in memory.

Layer: L5

Order 1k 6b. One model feeds the List (a `QTableView`) and the Cards (a `QListView` on column
0), so a toggle between them keeps the selection and the order. The rows arrive from a worker
(`set_rows`); nothing here queries. `data` does arithmetic and dictionary lookups only - it
runs once per visible cell - so ten thousand rows cost what the screen shows, not what the book
holds. Sorting is a stable Python sort over cached keys with the persistent indexes moved, so a
sort keeps what was selected selected.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt

from app.contacts.hub import ContactRow
from app.ui.contacts.logic import COLUMNS, account_glyph, column_key, last_contacted_text, sort_rows, where_it_lives
from app.ui.widgets.icons import icon

__all__ = ["ContactModel", "RowRole"]

RowRole = Qt.ItemDataRole.UserRole + 1


class ContactModel(QAbstractTableModel):
    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__(parent)
        self._rows: list[ContactRow] = []
        self._at: dict[int, int] = {}
        self._names: dict[int, str] = {}
        self._accounts: dict[int, tuple[str, str, str]] = {}
        self._tag_colours: dict[str, str] = {}
        self._sort: tuple[int, bool] = (0, False)
        self._lives_icons: dict[str, Any] = {}
        self._ink = {"warning": "#b06000", "ok": "#188038", "dim": "#888888"}

    # ------------------------------------------------------------------ data in
    def retint(self, colours: dict[str, str]) -> None:
        self._ink = {"warning": colours.get("warning", "#b06000"), "ok": colours.get("text_dim", "#888888"),
                     "dim": colours.get("text_faint", "#888888")}
        self._lives_icons = {"single": icon("triangle-alert", self._ink["warning"]),
                             "none": icon("triangle-alert", self._ink["warning"]),
                             "multi": icon("circle-check", self._ink["ok"])}
        if self._rows:
            self.dataChanged.emit(self.index(0, 9), self.index(len(self._rows) - 1, 9))

    def set_accounts(self, accounts: Iterable[Any]) -> None:
        accounts = list(accounts)
        self._names = {a.id: (a.name or a.address) for a in accounts}
        self._accounts = {a.id: (a.name or a.address, a.colour, account_glyph(a)) for a in accounts}

    def chips(self, r: ContactRow, kind: str) -> list[tuple[str, str, str]]:
        """(words, colour, glyph) for each chip a cell or a card draws; colour "" is neutral."""
        if kind == "accounts":
            return [(*self._accounts[a][:1], self._accounts[a][1], self._accounts[a][2])
                    for a in r.accounts if a in self._accounts]
        if kind == "tags":
            return [(t, self._tag_colours.get(t, ""), "tag") for t in r.tags]
        return [(g, "", "users") for g in r.groups]

    def set_tag_colours(self, tags: Iterable[tuple]) -> None:
        self._tag_colours = {t[1]: t[2] for t in tags}

    def account_names(self) -> dict[int, str]:
        return self._names

    def tag_colour(self, name: str) -> str:
        return self._tag_colours.get(name, "")

    def set_rows(self, rows: list[ContactRow]) -> None:
        self.beginResetModel()
        self._rows = sort_rows(rows, column_key(self._sort[0]), self._sort[1], self._names) \
            if self._sort != (0, False) else list(rows)
        self._reindex()
        self.endResetModel()

    def _reindex(self) -> None:
        self._at = {r.id: i for i, r in enumerate(self._rows)}

    # ----------------------------------------------------------------- queries
    def row_at(self, i: int) -> Optional[ContactRow]:
        return self._rows[i] if 0 <= i < len(self._rows) else None

    def row_for_id(self, cid: int) -> Optional[ContactRow]:
        i = self._at.get(cid)
        return self._rows[i] if i is not None else None

    def position_of(self, cid: int) -> int:
        return self._at.get(cid, -1)

    def ids(self) -> list[int]:
        return [r.id for r in self._rows]

    # ------------------------------------------------------------------ Qt API
    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:                # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:             # noqa: N802
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = 0) -> Any:   # noqa: N802
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return COLUMNS[section][1]
        return None

    def text_for(self, r: ContactRow, col: int) -> str:
        key = COLUMNS[col][0]
        if key == "name":
            return r.display_name
        if key == "company":
            return r.company
        if key == "title":
            return r.title
        if key == "email":
            return r.email
        if key == "phone":
            return r.phone
        if key == "accounts":
            return ", ".join(self._names.get(a, "") for a in r.accounts)
        if key == "groups":
            return ", ".join(r.groups)
        if key == "tags":
            return ", ".join(r.tags)
        if key == "contacted":
            return last_contacted_text(r.last_contacted_at)
        return where_it_lives(r, self._names)[0]

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        r = self._rows[index.row()] if 0 <= index.row() < len(self._rows) else None
        if r is None:
            return None
        col = index.column()
        if role == RowRole:
            return r
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            text = self.text_for(r, col)
            if role == Qt.ItemDataRole.AccessibleTextRole and col == 0:
                return f"{text}, {r.company}" if r.company else text
            return text
        if role == Qt.ItemDataRole.ToolTipRole:
            return self.text_for(r, col) or None
        if role == Qt.ItemDataRole.DecorationRole and col == 9:
            return self._lives_icons.get(where_it_lives(r, self._names)[1])
        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        desc = order == Qt.SortOrder.DescendingOrder
        self._sort = (column, desc)
        if not self._rows:
            return
        self.layoutAboutToBeChanged.emit()
        moved = [(p, p.row(), p.column(), self._rows[p.row()].id) for p in self.persistentIndexList()
                 if 0 <= p.row() < len(self._rows)]
        self._rows = sort_rows(self._rows, column_key(column), desc, self._names)
        self._reindex()
        self.changePersistentIndexList(
            [self.index(r, c) for _p, r, c, _i in moved], [self.index(self._at[i], c) for _p, _r, c, i in moved])
        self.layoutChanged.emit()

    def sort_state(self) -> tuple[int, bool]:
        return self._sort
