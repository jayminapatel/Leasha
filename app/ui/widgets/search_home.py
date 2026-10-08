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

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout, QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget,
)

from app.ui.first_contact import SUGGESTIONS, greeting
from app.ui.widgets.flow_layout import FlowLayout

__all__ = ["SearchHome", "HEADLINE", "BOX_WIDTH"]

#: New copy for a new control (the page had no headline before).
HEADLINE = "What are you looking for?"
BOX_WIDTH = 640


class SearchHome(QWidget):
    """The empty-state page. Draws from lists already in memory; the only store
    read behind it is the window's totals worker (`set_count`).
    """
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

        # **Wrapping, not squeezing** (order 0x section 9, review finding 14).
        # In a `QHBoxLayout` the four pills were squeezed below their words on
        # a narrow window and Qt cut each one in the middle - "the pdf …e
        # boiler". A centred flow keeps every pill whole and moves the last
        # ones to a second line only when a line cannot hold them; at 1100
        # wide they still sit on one line, as before.
        #
        # In a box this page sizes itself (`_fit_pills`, from `resizeEvent`)
        # rather than one Qt sizes by height-for-width: see the flag's note in
        # `flow_layout.py` for the resize cost that avoids.
        self._pill_box = QWidget()
        self._pills = FlowLayout(self._pill_box, spacing=8, centred=True,
                                 height_for_width=False)
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
            self._pills.addWidget(pill)
            self.suggestions.append(pill)
        outer.addWidget(self._pill_box)

        self.recent = QWidget()
        self.recent.setObjectName("recentList")
        self.recent.setMaximumWidth(BOX_WIDTH)
        self._recent_layout = QVBoxLayout(self.recent)
        self._recent_layout.setContentsMargins(0, 8, 0, 0)
        self._recent_layout.setSpacing(6)
        outer.addWidget(self.recent, 0, Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch(3)

    # -- sizing -----------------------------------------------------------------

    def resizeEvent(self, event: Any) -> None:  # noqa: N802 - Qt's name
        """A new width may wrap the pills differently: give their box the height it needs."""
        super().resizeEvent(event)
        self._fit_pills(event.size().width())

    def _fit_pills(self, width: int) -> None:
        """Give the pills' box the height its lines need at this width.

        Only a change is written: at any width where the lines stay the same,
        which is nearly every resize, this is one small sum and nothing else.
        """
        margins = self.layout().contentsMargins()
        needed = self._pills.heightForWidth(max(0, width - margins.left() - margins.right()))
        if needed > 0 and needed != self._pill_box.maximumHeight():
            self._pill_box.setFixedHeight(needed)

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
    """Empty a layout recursively; widgets are `deleteLater`d."""
    while layout.count():
        item = layout.takeAt(0)
        if item.widget() is not None:
            item.widget().deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())
