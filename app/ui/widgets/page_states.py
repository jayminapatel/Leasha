r"""The one empty state and the one busy state every list and page uses.

Layer: L5

**Why this exists** (owner, 2026-10-13, UI phase B): *"every screen ... look and feel like using
different apps not one app"*. A list with nothing in it said so in a plain label - centred on
Photos' info pane, top-left on Code, faint on Tidy's history, full colour on Offline - and a page
that was working said so in words alone, so "Looking through what Leasha has from 2019..." read
exactly like the sentence that replaced it.

* **`EmptyState`** - an icon, one plain sentence, and optionally one action button, centred in the
  space the list would fill. It answers to the calls the labels it replaces answered to:
  `setText`, `text`, `setWordWrap`, `setAlignment`, `linkActivated` (Code's "index" link), so a
  page swaps `QLabel(...)` for `EmptyState(...)` and nothing else changes.
* **`BusyLine`** - a small moving bar and the words saying what is happening. `start(words)` shows
  the bar, `stop(words)` puts it away and leaves the words; plain `setText` is a sentence with no
  bar, as the label it replaces was.

The words are always the page's own; nothing here writes one.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from app.ui.theme import SPACE_S

__all__ = ["EmptyState", "BusyLine", "EMPTY_ICON_PX", "BUSY_BAR_WIDTH"]

#: The empty state's picture: large enough to read as a picture, small enough to stay quiet.
EMPTY_ICON_PX = 32
#: The busy bar's width; it moves, so it need not be long.
BUSY_BAR_WIDTH = 96


class EmptyState(QWidget):
    """Nothing to show: an icon, one sentence, an optional action."""

    linkActivated = Signal(str)                                 # noqa: N815 - QLabel's name

    def __init__(self, text: str = "", *, glyph: str = "info", parent: Optional[QWidget] = None,
                 align: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignCenter, **props: Any) -> None:
        super().__init__(parent)
        self.setObjectName("emptyState")
        self._glyph = glyph
        self.icon = QLabel()
        self.icon.setObjectName("emptyStateIcon")
        self.icon.setFixedSize(QSize(EMPTY_ICON_PX, EMPTY_ICON_PX))
        self.label = QLabel(text)
        self.label.setObjectName("emptyStateText")
        self.label.setWordWrap(True)
        self.label.setOpenExternalLinks(False)
        self.label.linkActivated.connect(self.linkActivated)
        self.action: Optional[QPushButton] = None
        column = QVBoxLayout(self)
        column.setContentsMargins(0, SPACE_S, 0, SPACE_S)
        column.setSpacing(SPACE_S)
        column.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignHCenter)
        column.addWidget(self.label)
        self._column = column
        self.setAlignment(align)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self._paint_icon()
        if "visible" in props:                               # as `QLabel(..., visible=False)` was
            self.setVisible(bool(props.pop("visible")))
        for name, value in props.items():
            self.setProperty(name, value)

    # -- the QLabel calls it stands in for ---------------------------------------
    def text(self) -> str:
        return self.label.text()

    def setText(self, text: str) -> None:                       # noqa: N802 - QLabel's name
        self.label.setText(text)

    def setWordWrap(self, on: bool) -> None:                    # noqa: N802 - QLabel's name
        self.label.setWordWrap(on)

    def setAlignment(self, align: Any) -> None:                 # noqa: N802 - QLabel's name
        """Centred (the default) puts the icon over the middle; left keeps it over the words."""
        centred = bool(align & Qt.AlignmentFlag.AlignHCenter)
        self.label.setAlignment(Qt.AlignmentFlag.AlignHCenter if centred else Qt.AlignmentFlag.AlignLeft)
        self._column.setAlignment(self.icon, Qt.AlignmentFlag.AlignHCenter if centred
                                  else Qt.AlignmentFlag.AlignLeft)

    # -- the action ------------------------------------------------------------
    def set_action(self, words: str, on_click: Callable[[], Any], *, glyph: str = "play",
                   tip: str = "") -> QPushButton:
        """One button under the sentence, doing the obvious next thing."""
        from app.ui.widgets.buttons import style_button

        if self.action is None:
            self.action = QPushButton(words)
            self._column.addWidget(self.action, 0, Qt.AlignmentFlag.AlignHCenter)
        self.action.setText(words)
        self.action.setToolTip(tip or words)
        style_button(self.action, glyph, "secondary")
        self.action.clicked.connect(lambda _c=False: on_click())
        return self.action

    # -- the picture follows the theme -------------------------------------------
    def _paint_icon(self) -> None:
        from app.ui.theme import theme_colours
        from app.ui.widgets.icons import icon

        picture = icon(self._glyph, theme_colours()["text_faint"])
        self.icon.setPixmap(picture.pixmap(EMPTY_ICON_PX, EMPTY_ICON_PX))

    def changeEvent(self, event: Any) -> None:                  # noqa: N802 - Qt's name
        if event.type() in (QEvent.Type.StyleChange, QEvent.Type.PaletteChange):
            self._paint_icon()
        super().changeEvent(event)


class BusyLine(QWidget):
    """Something is happening: a small moving bar, then what it is."""

    def __init__(self, text: str = "", parent: Optional[QWidget] = None) -> None:
        from app.ui.widgets.shimmer_bar import ShimmerBar

        super().__init__(parent)
        self.setObjectName("busyLine")
        self.bar = ShimmerBar()
        self.bar.setObjectName("busyBar")
        self.bar.setTextVisible(False)
        self.bar.setFixedWidth(BUSY_BAR_WIDTH)
        self.bar.setRange(0, 1)
        self.bar.hide()
        self.label = QLabel(text)
        self.label.setObjectName("resultsSummary")
        self.label.setWordWrap(True)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE_S)
        row.addWidget(self.bar, 0, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self.label, 1)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

    def start(self, text: str) -> None:
        """Busy: the bar moves and the words say what with."""
        self.label.setText(text)
        self.bar.setRange(0, 0)
        self.bar.show()

    def stop(self, text: Optional[str] = None) -> None:
        """Done: the bar goes; `text`, when given, is what is left on the line."""
        self.bar.setRange(0, 1)
        self.bar.hide()
        if text is not None:
            self.label.setText(text)

    def busy(self) -> bool:
        return not self.bar.isHidden()

    # -- the QLabel calls it stands in for ---------------------------------------
    def text(self) -> str:
        return self.label.text()

    def setText(self, text: str) -> None:                       # noqa: N802 - QLabel's name
        """A sentence, not work: the bar goes."""
        self.stop(text)

    def setWordWrap(self, on: bool) -> None:                    # noqa: N802 - QLabel's name
        self.label.setWordWrap(on)
