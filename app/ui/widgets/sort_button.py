"""Sort: Best match / Newest / Oldest / Name, on the Search toolbar.

Layer: L5

UI phase B (owner, 2026-10-13). Sorting existed only as something typed -
`/newest`, `/oldest` - and nothing on the screen said so. This is that same sort
with a face: choosing an order **writes the switch into the box**
(`presenter.with_sort`) and searches, and typing one moves the control
(`presenter.sort_in`). There is one sort, `ParsedQuery.sort`; a control holding
an order of its own would be a second one, free to disagree with the box and
with the notice that says how to undo it.

Drawn inside a `#segmented` frame so it reads as the scope control's neighbour:
the same strip, the same small type, one segment wide.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtGui import QAction, QActionGroup
from PySide6.QtWidgets import QFrame, QHBoxLayout, QMenu, QToolButton, QWidget

from app.ui.presenter import SORT_CHOICES, sort_in, with_sort

__all__ = ["SortButton", "build_sort"]


class SortButton(QToolButton):
    """A one-segment strip opening the four orders. `choose(value)` is the click."""

    def __init__(self, box: Any, on_chosen: Callable[[], None],
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("sortButton")
        self._box = box
        self._on_chosen = on_chosen
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setAutoRaise(True)
        self.setToolTip("Put the results in another order. The same as typing "
                        "/newest, /oldest or /sort name in the box.")
        self.setAccessibleName("Sort the results")
        menu = QMenu(self)
        group = QActionGroup(menu)
        group.setExclusive(True)
        self._actions: dict[str, QAction] = {}
        for label, value in SORT_CHOICES:
            action = QAction(label, menu)
            action.setCheckable(True)
            action.triggered.connect(lambda _c=False, v=value: self.choose(v))
            group.addAction(action)
            menu.addAction(action)
            self._actions[value] = action
        self.setMenu(menu)
        box.textChanged.connect(self.follow)
        self.follow(box.text())
        from app.ui.widgets.buttons import give_icon

        give_icon(self)                     # "Sort: ..." carries its icon, as every control does (2026-10-14)

    def choose(self, value: str) -> None:
        """Put `value`'s switch in the box (or take it out) and search now."""
        wanted = with_sort(self._box.text(), value)
        if wanted != self._box.text():
            self._box.setText(wanted)
        self.follow(wanted)
        if wanted.strip():
            self._on_chosen()

    def follow(self, text: str) -> None:
        """Show the order the box's text asks for."""
        value = sort_in(text)
        label = dict((v, l) for l, v in SORT_CHOICES).get(value, SORT_CHOICES[0][0])
        self.setText(f"Sort: {label}")
        action = self._actions.get(value)
        if action is not None and not action.isChecked():
            action.setChecked(True)


def build_sort(view: Any) -> tuple[QFrame, SortButton]:
    """The control in its `#segmented` frame, for the toolbar row."""
    frame = QFrame(view)
    frame.setObjectName("segmented")
    row = QHBoxLayout(frame)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(0)
    button = SortButton(view.input, view.search_now, frame)
    row.addWidget(button)
    return frame, button
