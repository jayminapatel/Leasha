r"""A segmented control with a `QComboBox`-shaped surface.

Layer: L5

UI Redesign (202626160950 §3c). The scope was a `QComboBox` beside the search
box; it is now four segments under it, reading exactly the combo's four
strings. **The surface is the combo's** - `addItem`, `currentData`,
`findData`, `setCurrentIndex`, `currentIndex`, `currentIndexChanged` - so
`search_bar.scope_value` and `select_scope`, and everything the window does
with the scope, run unchanged.

A `QButtonGroup` of checkable `QToolButton`s: keyboard focus, arrow keys and
radio-button semantics for a screen reader come from Qt, not from here.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QToolButton, QWidget

__all__ = ["SegmentedControl"]


class SegmentedControl(QFrame):
    currentIndexChanged = pyqtSignal(int)          # noqa: N815 - QComboBox's name

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("segmented")
        self._data: list[Any] = []
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._group.idToggled.connect(self._toggled)
        self._current = -1

    # -- the QComboBox surface -------------------------------------------------

    def addItem(self, label: str, data: Any = None) -> None:               # noqa: N802
        button = QToolButton()
        button.setText(label)
        button.setCheckable(True)
        button.setAutoRaise(True)
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        index = len(self._data)
        self._data.append(data if data is not None else label)
        self._group.addButton(button, index)
        self._layout.addWidget(button)
        if index == 0:
            button.setChecked(True)
            self._current = 0

    def count(self) -> int:
        return len(self._data)

    def currentIndex(self) -> int:                                         # noqa: N802
        return self._current

    def currentData(self) -> Any:                                          # noqa: N802
        return self._data[self._current] if 0 <= self._current < len(self._data) else None

    def currentText(self) -> str:                                          # noqa: N802
        button = self._group.button(self._current)
        return button.text() if button is not None else ""

    def findData(self, data: Any) -> int:                                  # noqa: N802
        try:
            return self._data.index(data)
        except ValueError:
            return -1

    def setCurrentIndex(self, index: int) -> None:                         # noqa: N802
        button = self._group.button(index)
        if button is not None and not button.isChecked():
            button.setChecked(True)

    def setToolTip(self, text: str) -> None:                               # noqa: N802
        super().setToolTip(text)
        for button in self._group.buttons():
            button.setToolTip(text)

    def button(self, index: int) -> Optional[QToolButton]:
        return self._group.button(index)

    # -- internals ---------------------------------------------------------------

    def _toggled(self, index: int, checked: bool) -> None:
        if checked and index != self._current:
            self._current = index
            self.currentIndexChanged.emit(index)
