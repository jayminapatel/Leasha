r"""The rail: one navy column of pages, replacing the tab strip.

Layer: L5

UI Redesign (202626160950 §2). `MainWindow` had a bare `QTabWidget` as its
central widget - eight bordered tabs across the top, the idiom of a 2012 Qt
application. This is a 72px column down the left with an icon over a label
per page, the brand mark at the top, and at the foot the indexing pill and
Settings.

**It exposes the `QTabWidget` surface `shell.py` already uses** - `addTab`,
`insertTab`, `currentChanged`, `currentIndex`, `indexOf`, `setCurrentIndex`,
`tabText`, `count` - so the eleven call sites in the window change one
attribute name and nothing else. Pages live in a `QStackedWidget` this widget
owns; `indexOf` and `tabText` answer in stack order, which is the only order
anything asks about.

**Labels are the strings the window passes** - "Search", "Files", "Mail",
"Code", "Offline Media", "Reports", "Settings" - verbatim (§2b). The icon per
page is chosen by the caller, never inferred from a label.

**Indexing is not an entry; it is the pill** (§2d). `addTab(..., pill=True)`
puts the page in the stack with no button and makes the pill open it. The
pill's words come from `rail_state.pill_text`, Qt-free.

**Selection is a filled pill AND a heavier label** (§2e), so it survives
greyscale; the rail is focusable and `Up`/`Down` move between entries. Every
button carries its label as text, so its accessible name is the label.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QButtonGroup, QFrame, QHBoxLayout, QLabel, QProgressBar, QSizePolicy,
    QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

from app.ui.rail_state import PillState
from app.ui.widgets.icons import icon as themed_icon

__all__ = ["Rail", "RAIL_WIDTH", "ICON_SIZE"]

RAIL_WIDTH = 72
ICON_SIZE = 20


class _Pill(QFrame):
    """The indexing pill: a frame that behaves like a button."""

    activated = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("railPill")
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Open the Indexing page")
        self.setAccessibleName("Indexing")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(3)
        self.headline = QLabel("Index")
        self.headline.setObjectName("railPillHeadline")
        self.headline.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.setRange(0, 1)
        self.bar.setValue(0)
        self.bar.setFixedHeight(3)
        self.detail = QLabel("")
        self.detail.setObjectName("railPillDetail")
        self.detail.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.detail.setWordWrap(True)
        for w in (self.headline, self.bar, self.detail):
            layout.addWidget(w)

    def show_state(self, state: PillState, fraction: Optional[float]) -> None:
        self.headline.setText(state.headline)
        self.detail.setText(state.detail)
        self.detail.setVisible(bool(state.detail))
        if state.busy and fraction is None:
            self.bar.setRange(0, 0)               # Qt's moving bar
        else:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(round((fraction or (0.0 if state.busy else 1.0)) * 1000)))
        self.setAccessibleName(f"Indexing. {state.headline}. {state.detail}".strip())

    def mousePressEvent(self, event: Any) -> None:      # noqa: N802
        self.activated.emit()
        super().mousePressEvent(event)

    def keyPressEvent(self, event: Any) -> None:        # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.activated.emit()
            return
        super().keyPressEvent(event)


class Rail(QWidget):
    """A page rail and the stack it drives. See the module docstring."""

    currentChanged = pyqtSignal(int)          # noqa: N815 - QTabWidget's name

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._titles: list[str] = []
        self._buttons: dict[int, QToolButton] = {}     # stack index -> button
        self._icons: dict[int, str] = {}
        self._foot: set[int] = set()
        self._pill_index: Optional[int] = None
        self._colours: dict[str, str] = {}

        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.column = QFrame()
        self.column.setObjectName("rail")
        self.column.setFixedWidth(RAIL_WIDTH)
        self.column.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.column.setAccessibleName("Pages")
        col = QVBoxLayout(self.column)
        col.setContentsMargins(6, 12, 6, 10)
        col.setSpacing(4)
        self.mark = QLabel("L")
        self.mark.setObjectName("railMark")
        self.mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.mark.setFixedSize(34, 34)
        self.mark.setAccessibleName("Leasha")
        # The real brand mark, not the "L" placeholder - same icon file the
        # taskbar/window icon uses (app.ui.tray.icon_path), scaled to fit.
        # Never raises and never guesses: a missing icon file leaves the
        # placeholder letter rather than breaking the rail (tray.py does the
        # same for the window icon).
        from app.ui.tray import icon_path
        found = icon_path()
        if found is not None:
            pixmap = QPixmap(str(found))
            if not pixmap.isNull():
                self.mark.setPixmap(pixmap.scaled(
                    28, 28, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation))
                self.mark.setText("")
        col.addWidget(self.mark, 0, Qt.AlignmentFlag.AlignHCenter)
        col.addSpacing(10)
        self._top = QVBoxLayout()
        self._top.setSpacing(4)
        col.addLayout(self._top)
        col.addStretch(1)
        self.pill = _Pill()
        self.pill.activated.connect(self._open_pill)
        col.addWidget(self.pill)
        self._bottom = QVBoxLayout()
        self._bottom.setSpacing(4)
        col.addLayout(self._bottom)

        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.group.idClicked.connect(self.setCurrentIndex)

        self.stack = QStackedWidget()
        self.stack.currentChanged.connect(self._stack_changed)
        outer.addWidget(self.column)
        outer.addWidget(self.stack, 1)
        self.column.keyPressEvent = self._column_key            # type: ignore[method-assign]

    # -- the QTabWidget surface ----------------------------------------------

    def addTab(self, widget: QWidget, title: str, *, icon: str = "",   # noqa: N802
               foot: bool = False, pill: bool = False) -> int:
        return self.insertTab(self.stack.count(), widget, title, icon=icon,
                              foot=foot, pill=pill)

    def insertTab(self, index: int, widget: QWidget, title: str, *,   # noqa: N802
                  icon: str = "", foot: bool = False, pill: bool = False) -> int:
        index = self.stack.insertWidget(index, widget)
        self._titles.insert(index, title)
        # Everything at or after `index` shifts by one.
        self._buttons = {(i + 1 if i >= index else i): b for i, b in self._buttons.items()}
        self._icons = {(i + 1 if i >= index else i): n for i, n in self._icons.items()}
        self._foot = {(i + 1 if i >= index else i) for i in self._foot}
        if self._pill_index is not None and self._pill_index >= index:
            self._pill_index += 1
        if pill:
            self._pill_index = index
        else:
            button = QToolButton()
            button.setText(title)
            button.setToolTip(f"Open {title}")
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setIconSize(QSize(ICON_SIZE, ICON_SIZE))
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            self._buttons[index] = button
            self._icons[index] = icon
            if foot:
                self._foot.add(index)
        self._relayout()
        self._retint_buttons()
        if self.stack.count() == 1:
            self._sync_checked(0)
        return index

    def count(self) -> int:
        return self.stack.count()

    def currentIndex(self) -> int:                                       # noqa: N802
        return self.stack.currentIndex()

    def setCurrentIndex(self, index: int) -> None:                       # noqa: N802
        self.stack.setCurrentIndex(index)

    def indexOf(self, widget: QWidget) -> int:                           # noqa: N802
        return self.stack.indexOf(widget)

    def widget(self, index: int) -> Optional[QWidget]:
        return self.stack.widget(index)

    def tabText(self, index: int) -> str:                                # noqa: N802
        return self._titles[index] if 0 <= index < len(self._titles) else ""

    # -- the pill -------------------------------------------------------------

    def show_pill(self, state: PillState, fraction: Optional[float]) -> None:
        self.pill.show_state(state, fraction)

    def _open_pill(self) -> None:
        if self._pill_index is not None:
            self.setCurrentIndex(self._pill_index)

    # -- theme ----------------------------------------------------------------

    def retint(self, colours: dict[str, str]) -> None:
        """Re-render every icon in the rail's own text colours."""
        self._colours = dict(colours)
        self._retint_buttons()

    def _retint_buttons(self) -> None:
        if not self._colours:
            return
        on, off = self._colours.get("rail_on", "#ffffff"), self._colours.get("rail_text", "#c9c1ee")
        current = self.stack.currentIndex()
        for index, button in self._buttons.items():
            name = self._icons.get(index)
            if name:
                button.setIcon(themed_icon(name, on if index == current else off))

    # -- internals -------------------------------------------------------------

    def _relayout(self) -> None:
        for layout in (self._top, self._bottom):
            while layout.count():
                item = layout.takeAt(0)
                if item.widget() is not None:
                    item.widget().setParent(None)
        for button in list(self.group.buttons()):
            self.group.removeButton(button)
        for index in sorted(self._buttons):
            button = self._buttons[index]
            self.group.addButton(button, index)
            (self._bottom if index in self._foot else self._top).addWidget(button)

    def _stack_changed(self, index: int) -> None:
        self._sync_checked(index)
        self._retint_buttons()
        self.currentChanged.emit(index)

    def _sync_checked(self, index: int) -> None:
        button = self._buttons.get(index)
        if button is not None:
            button.setChecked(True)
        else:
            # The pill's page, or nothing: no entry is "on".
            checked = self.group.checkedButton()
            if checked is not None:
                self.group.setExclusive(False)
                checked.setChecked(False)
                self.group.setExclusive(True)
        self.pill.setProperty("selected", index == self._pill_index)
        self.pill.style().unpolish(self.pill)
        self.pill.style().polish(self.pill)

    def _column_key(self, event: Any) -> None:
        order = sorted(self._buttons)
        if not order:
            return QFrame.keyPressEvent(self.column, event)
        current = self.stack.currentIndex()
        position = order.index(current) if current in order else -1
        if event.key() == Qt.Key.Key_Down:
            self.setCurrentIndex(order[min(position + 1, len(order) - 1)])
        elif event.key() == Qt.Key.Key_Up:
            self.setCurrentIndex(order[max(position - 1, 0)])
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._buttons[order[max(position, 0)]].click()
        else:
            return QFrame.keyPressEvent(self.column, event)
