r"""The opening state of the Search page: one box, and what sits around it.

Layer: L5

UI Redesign (202626160950 §3a). With nothing typed, the page is a headline,
the existing `first_contact.greeting` line, the search box at 640px, four
suggested searches as pills, and the recent/saved rows
`first_contact.sections()` already produces for the `/` popup - drawn here
as a two-column list instead. **The box is the same `QLineEdit`**; this widget
lends it a slot and hands it back to the toolbar on the first keystroke
(`search_bar.build_toolbar` does the moving).

Nothing here reads the store: the greeting's count is pushed in by the window
from the Indexing page's totals worker, and the recent rows come from lists
`SavedSearches` already holds in memory.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QGridLayout, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget,
)

from app.ui.first_contact import SUGGESTIONS, greeting

__all__ = ["SearchHome", "HEADLINE", "BOX_WIDTH"]

#: New copy for a new control (the page had no headline before).
HEADLINE = "What are you looking for?"
BOX_WIDTH = 640


class SearchHome(QWidget):
    def __init__(self, *, on_type: Callable[[str], None],
                 sections: Callable[[], Any], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("searchHome")
        self._on_type = on_type
        self._sections = sections

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 40, 24, 40)
        outer.setSpacing(18)
        outer.addStretch(2)

        self.headline = QLabel(HEADLINE)
        self.headline.setObjectName("emptyHeadline")
        self.headline.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        outer.addWidget(self.headline)

        self.greeting = QLabel("")
        self.greeting.setObjectName("emptyGreeting")
        self.greeting.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        outer.addWidget(self.greeting)

        # The box's slot. `lend()` puts the QLineEdit here; `reclaim` is the
        # toolbar's business.
        self.slot = QHBoxLayout()
        self.slot.addStretch(1)
        self.slot.addStretch(1)
        outer.addLayout(self.slot)

        pills = QHBoxLayout()
        pills.setSpacing(8)
        pills.addStretch(1)
        self.suggestions: list[QToolButton] = []
        for shown, typed in SUGGESTIONS:
            pill = QToolButton()
            pill.setObjectName("suggestion")
            pill.setText(shown)
            pill.setAutoRaise(True)
            pill.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            pill.setToolTip(f"Search for: {typed}")
            pill.setAccessibleName(f"Try searching: {shown}")
            pill.clicked.connect(lambda _c=False, t=typed: self._on_type(t))
            pills.addWidget(pill)
            self.suggestions.append(pill)
        pills.addStretch(1)
        outer.addLayout(pills)

        self.recent = QWidget()
        self.recent.setObjectName("recentList")
        self.recent.setMaximumWidth(BOX_WIDTH)
        self._recent_layout = QVBoxLayout(self.recent)
        self._recent_layout.setContentsMargins(0, 8, 0, 0)
        self._recent_layout.setSpacing(6)
        outer.addWidget(self.recent, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(3)

    # -- what the window and toolbar push in ------------------------------------

    def set_count(self, count: Optional[int]) -> None:
        """`first_contact.greeting`, verbatim, in the slot under the headline."""
        self.greeting.setText(greeting(count))

    def lend(self, box: QWidget) -> None:
        """Put the search box in this page's slot, centred, at `BOX_WIDTH`."""
        box.setMaximumWidth(BOX_WIDTH)
        box.setProperty("empty_state", True)
        box.style().unpolish(box)
        box.style().polish(box)
        # A stretch far above the two flanks': at equal weights the box got a
        # third of the page, so on a 1100px window it was 316px wide and its
        # placeholder was cut off mid-word. `BOX_WIDTH` is a ceiling; below it
        # the box takes what there is (found by reading the 1100x760 golden).
        self.slot.insertWidget(1, box, 100)

    def refresh(self) -> None:
        """Redraw the recent/saved rows from lists already in memory."""
        while self._recent_layout.count():
            item = self._recent_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
            elif item.layout() is not None:
                _clear(item.layout())
        try:
            sections = self._sections() or ()
        except Exception:                            # noqa: BLE001 - a list, not a page
            sections = ()
        for heading, rows in sections:
            label = QLabel(str(heading))
            label.setObjectName("recentHeading")
            self._recent_layout.addWidget(label)
            grid = QGridLayout()
            grid.setHorizontalSpacing(18)
            grid.setVerticalSpacing(4)
            for n, (shown, insert) in enumerate(tuple(rows)[:6]):
                row = QToolButton()
                row.setObjectName("recentRow")
                row.setText(str(shown))
                row.setAutoRaise(True)
                row.setFocusPolicy(Qt.FocusPolicy.TabFocus)
                row.setToolTip(f"Search again for: {shown}")
                row.clicked.connect(lambda _c=False, t=str(insert): self._on_type(t))
                grid.addWidget(row, n // 2, n % 2)
            self._recent_layout.addLayout(grid)
        self.recent.setVisible(bool(sections))


def _clear(layout: Any) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget() is not None:
            item.widget().deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())
