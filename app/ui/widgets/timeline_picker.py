r"""The timeline's controls: what to show, which year, which month, or any dates.

Layer: L5 widget. It chooses a `Period` and says so; it reads no index and
loads nothing. `timeline_view.py` owns the loading.

**Year, then month.** The year box lists only years that have anything in
them, with the count; choosing one shows the whole year and enables the months
that have anything. A month with nothing in it is greyed rather than offered
and found empty.

**Or any dates.** Two boxes that take what the search box takes after
``after:`` and ``before:`` (`Period.from_words`), so ``2015``, ``2015-06`` and
``2015-06-01`` all work and ``before:2015`` means all of 2015.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QToolButton, QVBoxLayout, QWidget,
)

from app.reports.timeline import KINDS, Period
from app.reports.timeline_words import BAD_DATE, KIND_TIPS, KIND_WORDS

__all__ = ["TimelinePicker", "KIND_KEY", "FOLD_KEY"]

#: Remembered between sessions: what to show, and whether to group. Namespaced
#: `ui:` like every other per-surface preference.
KIND_KEY = "ui:timeline_kind"
FOLD_KEY = "ui:timeline_fold"
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class TimelinePicker(QWidget):
    """Three rows of controls. Emits what was chosen; keeps no results."""

    kind_changed = pyqtSignal(str)
    fold_changed = pyqtSignal(bool)
    period_chosen = pyqtSignal(object)         # a `Period`
    bad_date = pyqtSignal(str)                 # a sentence for the status line

    def __init__(self, store: Any = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._overview: Any = None
        self._year: Optional[int] = None
        kind = self._remembered(KIND_KEY, "everything")

        self.kind_box = QComboBox()
        self.kind_box.setToolTip("Choose what the timeline shows. Program code is left out of "
                                 "\"everything\" so it does not crowd the rest.")
        for key in KINDS:
            self.kind_box.addItem(KIND_WORDS[key], key)
            self.kind_box.setItemData(self.kind_box.count() - 1, KIND_TIPS[key],
                                      Qt.ItemDataRole.ToolTipRole)
        self.kind_box.setCurrentIndex(KINDS.index(kind) if kind in KINDS else 0)
        self.kind_box.currentIndexChanged.connect(self._kind_chosen)

        self.fold_box = QCheckBox("Group near-identical photos")
        self.fold_box.setToolTip(
            "Show a burst of almost-identical photos, or two copies of the same file, as one "
            "line that says how many are behind it. Right-click it to show them all.")
        self.fold_box.setChecked(self._remembered(FOLD_KEY, "on") not in ("off", "0", "false"))
        self.fold_box.toggled.connect(self._fold_chosen)

        self.year_box = QComboBox()
        self.year_box.setToolTip("Pick a year, then a month. Every year that has anything in it "
                                 "is listed.")
        self.year_box.setAccessibleName("Year")
        self.year_box.currentIndexChanged.connect(self._year_chosen)
        self.months = QButtonGroup(self)
        self.months.setExclusive(True)
        self.month_buttons: list = []
        months = QHBoxLayout()
        for index, label in enumerate(("Whole year", *_MONTHS)):
            button = QToolButton()
            button.setText(label)
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.setToolTip("Everything from this year.")
            self.months.addButton(button, index)
            months.addWidget(button)
            self.month_buttons.append(button)
        months.addStretch(1)
        self.months.idClicked.connect(self._month_chosen)

        self.range_from = QLineEdit()
        self.range_from.setPlaceholderText("From, like 2015-06-01")
        self.range_from.setAccessibleName("From date")
        self.range_from.setMinimumWidth(200)             # the whole placeholder, at 125% too
        self.range_to = QLineEdit()
        self.range_to.setPlaceholderText("To, like 2015-08-31")
        self.range_to.setAccessibleName("To date")
        self.range_to.setMinimumWidth(200)
        self.range_go = QPushButton("Show")
        self.range_go.setToolTip("Show everything between these two dates, both days included. "
                                 "Leave one empty to have no limit on that side.")
        for box in (self.range_from, self.range_to):
            box.returnPressed.connect(self.choose_range)
        self.range_go.clicked.connect(self.choose_range)

        top = QHBoxLayout()
        for widget in (QLabel("Show"), self.kind_box, self.fold_box):
            top.addWidget(widget)
        top.addStretch(1)
        pick = QHBoxLayout()
        pick.addWidget(QLabel("Year"))
        pick.addWidget(self.year_box)
        pick.addLayout(months, 1)
        free = QHBoxLayout()
        for widget in (QLabel("Or any dates"), self.range_from, QLabel("to"), self.range_to,
                       self.range_go):
            free.addWidget(widget)
        free.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for row in (top, pick, free):
            layout.addLayout(row)
        self._enable_months(None)

    # -- preferences (keyed reads of `index_state`, never a scan) -----------------

    def _remembered(self, key: str, default: str) -> str:
        try:
            value = self._store.get_state(key, None) if self._store is not None else None
        except Exception:                            # noqa: BLE001 - a preference
            value = None
        return default if value is None else str(value)

    def _remember(self, key: str, value: str) -> None:
        try:
            if self._store is not None:
                self._store.set_state(key, value)
        except Exception:                            # noqa: BLE001 - a preference
            pass

    def kind(self) -> str:
        return str(self.kind_box.currentData() or "everything")

    def fold(self) -> bool:
        return self.fold_box.isChecked()

    # -- what the counts say ----------------------------------------------------

    def set_overview(self, overview: Any) -> None:
        self._overview = overview
        self.year_box.blockSignals(True)
        self.year_box.clear()
        for year, count in overview.years:
            self.year_box.addItem(f"{year}   ({count:,})", year)
        self.year_box.setCurrentIndex(-1)
        self.year_box.blockSignals(False)
        if self._year is not None:
            self.show_year(self._year)

    def show_year(self, year: int) -> None:
        """Reflect a year without choosing it (no signal)."""
        self._year = year
        at = self.year_box.findData(year)
        if at != self.year_box.currentIndex():
            self.year_box.blockSignals(True)
            self.year_box.setCurrentIndex(at)
            self.year_box.blockSignals(False)
        self._enable_months(year)

    def show_month(self, year: int, month: int) -> None:
        self.show_year(year)
        self.month_buttons[month].setChecked(True)

    def _enable_months(self, year: Optional[int]) -> None:
        counts = self._overview.months_of(year) if (year is not None and self._overview) else {}
        for index, button in enumerate(self.month_buttons):
            button.setEnabled(year is not None and (index == 0 or bool(counts.get(index))))
            if index:
                button.setToolTip(f"{counts.get(index, 0):,} items from {_MONTHS[index - 1]}"
                                  f"{f' {year}' if year else ''}.")

    # -- what was chosen --------------------------------------------------------

    def _year_chosen(self, at: int) -> None:
        year = self.year_box.itemData(at) if at >= 0 else None
        if year is not None:
            self.show_year(int(year))
            self.month_buttons[0].setChecked(True)
            self.period_chosen.emit(Period.year(int(year)))

    def _month_chosen(self, index: int) -> None:
        if self._year is not None:
            self.period_chosen.emit(Period.year(self._year) if index == 0
                                    else Period.month(self._year, index))

    def choose_range(self) -> None:
        period = Period.from_words(self.range_from.text(), self.range_to.text())
        if period is None:
            self.bad_date.emit(BAD_DATE)
        else:
            self.period_chosen.emit(period)

    def set_range_text(self, after: str, before: str) -> None:
        self.range_from.setText(after)
        self.range_to.setText(before)

    def _kind_chosen(self, _at: int) -> None:
        self._remember(KIND_KEY, self.kind())
        self.kind_changed.emit(self.kind())

    def _fold_chosen(self, on: bool) -> None:
        self._remember(FOLD_KEY, "on" if on else "off")
        self.fold_changed.emit(bool(on))
