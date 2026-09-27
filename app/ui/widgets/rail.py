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

from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
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


def _forget_sizes(layout: Any) -> None:
    """Make a layout, and every layout and widget slot inside it, forget the
    sizes it remembered.

    **Why this is needed.** A layout keeps a copy of each widget's size so it
    does not have to ask again, and it only throws that copy away when the
    event loop gets round to it. `invalidate()` on the outer layout alone does
    not reach the copies held inside the two button layouts, so a button that
    had just switched to icons-only (37 pixels tall) was still counted at its
    old 55 - measured 2026-09-27. Clearing every level by hand makes the very
    next `totalMinimumSize()` ask each widget afresh. It is a dozen items, so
    it costs nothing worth timing.
    """
    for number in range(layout.count()):
        item = layout.itemAt(number)
        inner = item.layout()
        if inner is not None:
            _forget_sizes(inner)
        else:
            item.invalidate()
    layout.invalidate()


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
        self._compact = False
        layout = QVBoxLayout(self)
        # 3px, not 6, and no side padding in the stylesheet either: the rail is
        # 72 wide and the pill 60, so the old 6 + 4 a side left 40 for the
        # headline - "Up to date" is 58px at 125% scaling, "Indexing" 48 - and it
        # was cut to "p to dat".
        layout.setContentsMargins(3, 6, 3, 6)
        layout.setSpacing(3)
        self.headline = QLabel("Index")
        self.headline.setObjectName("railPillHeadline")
        self.headline.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        # Wraps rather than clips: the headline is one of "Up to date",
        # "Needs attention", "Indexing", "Paused", "Stopped", "Index" and the
        # first two are wider than the pill at any scaling above 100%.
        self.headline.setWordWrap(True)
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

    def set_compact(self, compact: bool) -> None:
        """Drop the bar and the detail line - the headline is the state."""
        self._compact = compact
        self.bar.setVisible(not compact)
        self.detail.setVisible(bool(self.detail.text()) and not compact)

    def show_state(self, state: PillState, fraction: Optional[float]) -> None:
        self.headline.setText(state.headline)
        self.detail.setText(state.detail)
        self.detail.setVisible(bool(state.detail) and not self._compact)
        if state.busy and fraction is None:
            self.bar.setRange(0, 0)               # Qt's moving bar
        else:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(round((fraction or (0.0 if state.busy else 1.0)) * 1000)))
        self.setAccessibleName(f"Indexing. {state.headline}. {state.detail}".strip())
        self._fit_wrapped_text()

    def _fit_wrapped_text(self) -> None:
        """Give each wrapped label the height its wrapped text needs.

        A word-wrapped label inside a frame inside the rail's column reports the
        height of *one* line to the layouts above it, so a headline that wrapped
        to two was cut off at the pill's edge. Asking the label what it needs at
        the width it actually has, and holding it to that, does not depend on
        how far up the height-for-width request gets.
        """
        for label in (self.headline, self.detail):
            width = label.width()
            if width > 0 and label.wordWrap():
                label.setMinimumHeight(label.heightForWidth(width))

    def resizeEvent(self, event: Any) -> None:                 # noqa: N802
        super().resizeEvent(event)
        self._fit_wrapped_text()

    def showEvent(self, event: Any) -> None:                   # noqa: N802
        super().showEvent(event)
        self._fit_wrapped_text()

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
        #: See `_fit_height`. `_natural` is the column's minimum height with
        #: every label showing, re-read whenever the labels are showing.
        self._compact = False
        self._natural = 0

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
        headline = self.pill.headline.text()
        self.pill.show_state(state, fraction)
        # "Index" is one line; "Up to date" wraps to two and brings a detail
        # line with it, so the pill grows by about thirty pixels and the rail's
        # sums go stale. Measure again - but only when the headline word
        # changes, which is a handful of times a run, never on every progress
        # tick, and on the next turn of the event loop so the pill's own new
        # height has been worked out first.
        if headline != state.headline and self.isVisible():
            QTimer.singleShot(0, self._measure)

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
        # **The chosen page's icon takes the chosen page's text colour.** It was
        # `rail_on`, which is white in both themes - right when the rail was
        # navy, and 1.2 to 1 against the pale lavender fill the light theme's
        # rail uses now: the one icon that says "you are here" was the one you
        # could not see (grabbed 2026-09-27, order 0x section 9). `rail_on_text`
        # is the colour the label under it already uses: navy in the light
        # theme (11 to 1), white in the dark one, so dark is unchanged.
        on = self._colours.get("rail_on_text", self._colours.get("rail_on", "#ffffff"))
        off = self._colours.get("rail_text", "#c9c1ee")
        current = self.stack.currentIndex()
        for index, button in self._buttons.items():
            name = self._icons.get(index)
            if name:
                button.setIcon(themed_icon(name, on if index == current else off))

    # -- internals -------------------------------------------------------------

    def _relayout(self) -> None:
        # **Take each button out of its layout, but leave it where it lives.**
        # This used to say `item.widget().setParent(None)`, which quietly hides
        # the widget. Adding it back to a layout gives it a parent again, but Qt
        # only shows it *later*, from the event loop - so when `_measure` ran a
        # line below, every button was still hidden, a hidden widget takes up no
        # room, and the rail decided it needed 114 pixels instead of about 617.
        # Found on 2026-09-27 (order 0x section 9) by grabbing the window at
        # 560 pixels tall: the rail never switched to icons, and every label was
        # cut in half ("Searcn", "Uhat"). Taking the item out of the layout is
        # all a re-order needs; the button never stops being visible.
        for layout in (self._top, self._bottom):
            while layout.count():
                layout.takeAt(0)
        for button in list(self.group.buttons()):
            self.group.removeButton(button)
        for index in sorted(self._buttons):
            button = self._buttons[index]
            self.group.addButton(button, index)
            (self._bottom if index in self._foot else self._top).addWidget(button)
            # A button made a moment ago in `insertTab` has never been shown,
            # and the layout would only show it later, from the event loop -
            # too late for the measurement below, which would not count it.
            # Showing it now is what the layout was going to do anyway. A
            # button somebody hid on purpose carries Qt's "explicitly shown or
            # hidden" flag and is left exactly as it is.
            if not button.testAttribute(Qt.WidgetAttribute.WA_WState_ExplicitShowHide):
                button.show()
        if self.isVisible():
            self._measure()

    def _set_compact(self, compact: bool) -> None:
        """Icons alone, and a pill with no detail line."""
        self._compact = compact
        style = (Qt.ToolButtonStyle.ToolButtonIconOnly if compact
                 else Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        for button in self._buttons.values():
            button.setToolButtonStyle(style)
        self.pill.set_compact(compact)

    def _measure(self) -> None:
        """Set the column's floor to what it needs in its smallest form.

        **The window's own floor (480 tall) is lower than the rail needs**, and
        with a fixed-height button per page Qt then squeezes every one of them
        below its own height: measured on the real window at 125% scaling, seven
        47px buttons where 57 are needed, the last label's descenders cut off by
        the indexing pill. So the rail declares what it needs - the shell no
        longer sets a floor of its own for the height - and below what it needs
        with labels it drops to icons (the tooltips and accessible names still
        carry the words) rather than overlap.
        """
        was = self._compact
        self._set_compact(True)
        self.column.setMinimumHeight(0)
        _forget_sizes(self.column.layout())
        self.column.setMinimumHeight(self.column.layout().totalMinimumSize().height())
        # The same again with the labels showing, so `_fit_height` compares the
        # rail's real height against a figure worked out *now*, not one left
        # over from before the buttons or the pill changed shape. Measured
        # whether or not the rail is in icons-only form at the moment - a figure
        # only refreshed while the labels show is stale exactly when it is
        # needed, which is when deciding whether they can come back.
        self._set_compact(False)
        _forget_sizes(self.column.layout())
        self._natural = self.column.layout().totalMinimumSize().height()
        self._set_compact(was)
        self._fit_height()

    def _fit_height(self) -> None:
        if not self._compact:
            self._natural = self.column.layout().totalMinimumSize().height()
        should = self.column.height() < self._natural
        if should != self._compact and self._natural:
            self._set_compact(should)

    def resizeEvent(self, event: Any) -> None:                           # noqa: N802
        super().resizeEvent(event)
        self._fit_height()

    def showEvent(self, event: Any) -> None:                             # noqa: N802
        super().showEvent(event)
        self._measure()

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
