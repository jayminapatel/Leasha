r"""Filter chips under the search box: a view of the box, never a second parser.

Layer: L5

UI Redesign (202626160950 §3d). `chips_logic.chips_for` decides what is a
chip; this widget only draws them and, when one is removed, writes
`chips_logic.without(...)` back into the box. The box's own `textChanged`
then re-dispatches through the debounce typing already uses - one path, one
generation counter, no second dispatch from here.

**One tab stop per filter.** The chip is a `QToolButton` whose whole face
removes the filter; the `×` is drawn as part of the label. Its tooltip says
what pressing it does, and its accessible name is the filter's own words.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QToolButton, QWidget

from app.ui.chips_logic import Chip, chips_for, without

__all__ = ["ChipRow"]


class ChipRow(QWidget):
    #: The box's new text after a chip was removed. The view writes it into
    #: the box; the box's own signal does the rest.
    text_edited = pyqtSignal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chipRow")
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self._text = ""
        self._chips: list[Chip] = []
        self.setVisible(False)

    def show_for(self, text: str) -> None:
        """Redraw for the box's current text. Cheap; called per keystroke."""
        self._text = text
        chips = chips_for(text)
        if [c.label for c in chips] == [c.label for c in self._chips]:
            self._chips = chips
            return
        self._chips = chips
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for chip in chips:
            self._layout.addWidget(self._button_for(chip))
        self._layout.addStretch(1)
        self.setVisible(bool(chips))

    def labels(self) -> list[str]:
        return [chip.label for chip in self._chips]

    def _button_for(self, chip: Chip) -> QToolButton:
        button = QToolButton()
        button.setObjectName("chip")
        button.setText(f"{chip.label}  ×")
        button.setAutoRaise(True)
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        button.setToolTip(f"Remove the {chip.label} filter")
        button.setAccessibleName(f"{chip.label} filter, press to remove")
        button.clicked.connect(lambda _c=False, c=chip: self._remove(c))
        return button

    def _remove(self, chip: Chip) -> None:
        self.text_edited.emit(without(self._text, chip))

    def retint(self, _colours: dict[str, str]) -> None:
        """Chips are stylesheet-drawn; nothing to re-render."""
