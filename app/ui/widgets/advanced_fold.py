"""A closed "Advanced" section: expert settings one click away, not in the way.

Layer: L5

2026-10-05, the UI review, on the owner's word ("do the recommended for the
advanced fold"). The benchmark for this window is an eight-year-old finding
her homework, and Settings showed her "Which files count as code", a reranker
model and "AI programs" at the same weight as "Folders to index". Nothing is
removed and nothing is reworded: each group is the same box with the same
words, and it now sits under a heading that is closed until it is asked for.

**A group folds whole.** Folding single rows out of a box would leave a box
that reads differently open and closed; a whole box either is there or is not.

**Still findable.** The settings filter opens every fold while there is
something typed (`reveal`), so a setting that is folded away is never a
setting that cannot be found - non-negotiable 11, anything tunable has a UI.
"""

from __future__ import annotations

from typing import Iterable, Optional

from PyQt6.QtCore import QEvent, QSize, Qt, pyqtSignal
from PyQt6.QtWidgets import QToolButton, QVBoxLayout, QWidget

__all__ = ["AdvancedFold", "TITLE"]

#: The heading. A new control, so its own word.
TITLE = "Advanced"


class AdvancedFold(QWidget):
    """A heading that opens and closes the boxes under it."""

    #: Opened (True) or closed by a click - never by `set_open` or `reveal`.
    toggled = pyqtSignal(bool)

    def __init__(self, boxes: Iterable[QWidget], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._open = False
        self._revealed = False

        self.header = QToolButton()
        self.header.setObjectName("advancedToggle")
        self.header.setText(TITLE)
        self.header.setCheckable(True)
        self.header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.header.setIconSize(QSize(16, 16))
        self.header.setAccessibleName("Show or hide the advanced settings")
        self.header.setToolTip(
            "Settings most people never need to change. Click to show them; "
            "click again to put them away. Filtering the settings finds them "
            "either way.")
        self.header.clicked.connect(self._clicked)

        self.body = QWidget()
        inside = QVBoxLayout(self.body)
        inside.setContentsMargins(0, 0, 0, 0)
        for box in boxes:
            inside.addWidget(box)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.header, alignment=Qt.AlignmentFlag.AlignLeft)
        layout.addWidget(self.body)
        self._show()

    @property
    def is_open(self) -> bool:
        return self._open

    def set_open(self, on: bool) -> None:
        """Open or close it without saying somebody clicked."""
        self._open = bool(on)
        self._show()

    def reveal(self, on: bool) -> None:
        """Show the boxes whatever the heading says - while the filter has
        something typed - and go back to what the heading says afterwards."""
        self._revealed = bool(on)
        self._show()

    def _clicked(self, _checked: bool = False) -> None:
        self._open = not self._open
        self._show()
        self.toggled.emit(self._open)

    def _show(self) -> None:
        self.header.setChecked(self._open)
        self.header.setVisible(not self._revealed)
        self.body.setVisible(self._open or self._revealed)
        self._draw_arrow()

    def _draw_arrow(self) -> None:
        # The same chevrons the rest of the window uses, in the theme's own
        # quiet colour. **Never raises**: a heading with no arrow still works.
        try:
            from app.ui.theme import theme_colours
            from app.ui.widgets.icons import icon

            name = "chevron-down" if self._open else "chevron-right"
            self.header.setIcon(icon(name, theme_colours()["text_dim"]))
        except Exception:                        # noqa: BLE001 - decoration
            return

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt's name
        # A theme change re-styles every widget; the arrow is a picture and
        # has to be drawn again in the new colour.
        if event.type() == QEvent.Type.StyleChange:
            self._draw_arrow()
        super().changeEvent(event)
