r"""Filter chips under the search box: a view of the box, never a second parser.

Layer: L5

UI Redesign (202626160950 §3d). `chips_logic.chips_for` decides what is a
chip; this widget only draws them and, when one is removed, writes
`chips_logic.without(...)` back into the box. The box's own `textChanged`
then re-dispatches through the debounce typing already uses - one path, one
generation counter, no second dispatch from here.

**Applied chips are the one exception, and they still do not parse.** Since
2026-09-27 a sentence like "mail from 2017" runs with the filters the rules
are sure of (`presenter.auto_filters`); those arrive with the results
(`show_applied`) rather than from the box, because the box keeps exactly what
was typed. Removing one leaves the box alone, records the refusal in
`declined`, and asks for the search again (`declined_changed`).

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
    #: An *applied* chip was removed: its words go back to being search
    #: terms. The box text is unchanged, so nothing else would re-run the
    #: search - `search_bar.build_toolbar` connects this to `search_now`.
    declined_changed = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chipRow")
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)
        self._text = ""
        self._chips: list[Chip] = []
        #: Filters the search applied from the typed words ("mail from 2017"
        #: runs as `type:mail` and a 2017 range) - drawn after the typed
        #: operators, from the results they belong to. See
        #: `presenter.auto_filters`.
        self._applied: tuple = ()
        #: Keys of applied chips the person removed. Read by the view into
        #: every dispatch; kept while the box has text, so typing on after
        #: removing one does not bring it back. An empty box forgets them.
        self.declined: set = set()
        self.setVisible(False)

    def show_for(self, text: str) -> None:
        """Redraw for the box's current text. Cheap; called per keystroke."""
        self._text = text
        if not text.strip():
            self.declined.clear()
            self._applied = ()
        chips = chips_for(text)
        if [c.label for c in chips] == [c.label for c in self._chips]:
            self._chips = chips
            if not text.strip():
                self._redraw()
            return
        self._chips = chips
        self._redraw()

    def show_applied(self, applied) -> None:
        """Redraw with the filters the latest search applied. Called when its
        results land, so the chips always describe the page under them."""
        applied = tuple(applied or ())
        if [a.key for a in applied] == [a.key for a in self._applied]:
            return
        self._applied = applied
        self._redraw()

    def _redraw(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for chip in self._chips:
            self._layout.addWidget(self._button_for(chip))
        for applied in self._applied:
            self._layout.addWidget(self._button_for_applied(applied))
        self._layout.addStretch(1)
        self.setVisible(bool(self._chips or self._applied))

    def labels(self) -> list[str]:
        return [chip.label for chip in self._chips] + [a.label for a in self._applied]

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

    def _button_for_applied(self, applied) -> QToolButton:
        """A chip for a filter read from the typed words. Same shape and the
        same one tab stop as a typed filter's; removing it keeps the words and
        searches for them as words instead."""
        button = QToolButton()
        button.setObjectName("chip")
        button.setText(f"{applied.label}  ×")
        button.setAutoRaise(True)
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        button.setToolTip(f'Read from "{applied.words}". Remove it to search '
                          f"for those words instead.")
        button.setAccessibleName(f"{applied.label} filter, read from your words, "
                                 f"press to remove")
        button.clicked.connect(lambda _c=False, a=applied: self._decline(a))
        return button

    def _decline(self, applied) -> None:
        self.declined.add(applied.key)
        self._applied = tuple(a for a in self._applied if a.key != applied.key)
        self._redraw()
        self.declined_changed.emit()

    def _remove(self, chip: Chip) -> None:
        self.text_edited.emit(without(self._text, chip))

    def retint(self, _colours: dict[str, str]) -> None:
        """Chips are stylesheet-drawn; nothing to re-render."""
