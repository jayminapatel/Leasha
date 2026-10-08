"""The context shelf: exactly the documents the conversation may look at.

Layer: L5 view

Work order 202626270611 3c. Documents the chat touches accumulate here.
Keep one (pin) and it is searched first and never falls off; take one away and
it is out of scope, and stays out until you put it back; drag a result in to add
it yourself. Nothing the assistant can see is hidden - what is on this shelf is
the whole of it.

**2026-10-08, the owner: "the second attachment looks cluttered".** The shelf was
a row of chips under the answer, one "<name> Keep Remove" per document, and a
message's name was its entry id ("2109476 Keep Remove"). It is now **one compact
control** - "4 sources" - that opens the list: one line per document, named by
its subject or file name (cut short with an ellipsis, the whole name in the
tooltip), with its own Keep and Remove. Each line is still a `_Chip` with the
same three buttons, so everything that pressed a chip presses the same thing.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from PySide6.QtCore import QPoint, QSize, Qt, Signal
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from app.core.row_facts import is_message_key
from app.ui.presenter.chat import SHELF_EMPTY, SHELF_LIST_TIP, Shelf, shelf_count_label
from app.ui.widgets.icons import icon

__all__ = ["ShelfBar", "NAME_WIDTH"]

#: The widest a document's name is drawn in the list, in pixels, before it is cut
#: short. The whole name is in the tooltip.
NAME_WIDTH = 300

_SHEET = """
#chatShelfButton {{
    border: 1px solid {border}; border-radius: 11px; background: transparent;
    color: {text_dim}; padding: 2px 10px 2px 6px;
}}
#chatShelfButton:hover {{ background: {surface_hover}; color: {text}; }}
#chatShelfButton:pressed {{ background: {accent_soft}; }}
#chatShelfEmpty {{ color: {text_faint}; }}
"""

_LIST_SHEET = """
#chatShelfList {{ background: {surface}; border: 1px solid {border_strong}; border-radius: 8px; }}
#chatChip {{ background: transparent; border: none; border-radius: 6px; }}
#chatChip:hover {{ background: {surface_alt}; }}
#chatChip QToolButton {{
    border: none; background: transparent; color: {text}; padding: 3px 6px; border-radius: 6px;
}}
#chatChip QToolButton:hover {{ background: {surface_hover}; }}
#chatChip QToolButton[chipAction="true"] {{ color: {text_dim}; }}
#chatChip QToolButton[chipAction="true"]:checked {{ color: {accent_text}; font-weight: 600; }}
"""


def _colours(colours: Optional[Mapping[str, str]] = None) -> dict:
    """The theme's tokens with a light-theme fallback for each one the sheet
    needs, so a palette missing a key never breaks the sheet's `format`.
    """
    if colours is None:
        from app.ui.theme import theme_colours

        colours = theme_colours()
    base = {"border": "#dcdee2", "border_strong": "#c2c6cc", "surface": "#ffffff",
            "surface_alt": "#f0f1f3", "surface_hover": "#e8eaed", "text": "#1b1d20",
            "text_dim": "#585e66", "text_faint": "#6b7178", "accent_soft": "#e9e4fb",
            "accent_text": "#15084b"}
    return {**base, **{k: v for k, v in dict(colours).items() if isinstance(v, str)}}


class _Chip(QFrame):
    """One document in scope: its name (opens it), Keep, and Remove."""

    def __init__(self, item, on_open, on_pin, on_remove, colours: Mapping[str, str]) -> None:
        super().__init__()
        self.setObjectName("chatChip")
        self.path = item.path
        name = QToolButton()
        shown = QFontMetrics(name.font()).elidedText(
            item.name, Qt.TextElideMode.ElideRight, NAME_WIDTH)
        name.setText(shown)
        name.setAutoRaise(True)
        name.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        name.setIcon(icon("mail" if is_message_key(item.path) else "file-text",
                          colours.get("text_dim", "#888888")))
        name.setIconSize(QSize(16, 16))
        name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        name.setToolTip(f"Open {item.name}.\n{item.path}")
        name.setAccessibleName(f"Open {item.name}")
        name.clicked.connect(lambda _c=False: on_open(item.path))
        pin = QToolButton()
        pin.setText("Kept" if item.pinned else "Keep")
        pin.setCheckable(True)
        pin.setChecked(item.pinned)
        pin.setAutoRaise(True)
        pin.setProperty("chipAction", True)
        pin.setToolTip(
            "Keep this document in scope: follow-up questions look here first "
            "and it never drops off the shelf. Press again to let it go.")
        pin.clicked.connect(lambda _c=False: on_pin(item.path))
        drop = QToolButton()
        drop.setText("Remove")
        drop.setAutoRaise(True)
        drop.setProperty("chipAction", True)
        drop.setToolTip(
            "Take this document out of scope. Questions stop looking at it "
            "until you drag it back in. The file itself is not touched.")
        drop.clicked.connect(lambda _c=False: on_remove(item.path))
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 1, 2, 1)
        row.setSpacing(2)
        row.addWidget(name, stretch=1)
        row.addWidget(pin)
        row.addWidget(drop)
        self.name_button, self.pin_button, self.remove_button = name, pin, drop


class ShelfBar(QWidget):
    """One "N sources" button that opens the list of chips, and a drop target."""

    #: The person changed what is in scope (pin, remove, add by drag).
    changed = Signal()
    open_requested = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatShelf")
        self.shelf = Shelf()
        self.setAcceptDrops(True)
        self.setAccessibleName("Documents in scope")
        self.empty = QLabel(SHELF_EMPTY)
        self.empty.setObjectName("chatShelfEmpty")
        self.button = QToolButton()
        self.button.setObjectName("chatShelfButton")
        self.button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.button.setIconSize(QSize(16, 16))
        self.button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.button.setToolTip(SHELF_LIST_TIP)
        self.button.setAccessibleName("Documents in scope")
        self.button.clicked.connect(lambda _c=False: self.open_list())

        # The list: a pop-up under the window's own frame, closed by a click
        # anywhere else or Esc, as a menu is.
        self.popup = QFrame(self, Qt.WindowType.Popup)
        self.popup.setObjectName("chatShelfList")
        self.popup.setAccessibleName("Documents in scope")
        self._rows = QVBoxLayout(self.popup)
        self._rows.setContentsMargins(6, 6, 6, 6)
        self._rows.setSpacing(2)

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        row.addWidget(self.button)
        row.addWidget(self.empty)
        row.addStretch(1)
        self._colours = _colours()
        self.retint()
        self.refresh()

    def set_shelf(self, shelf: Shelf) -> None:
        self.shelf = shelf
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the list from `shelf.items`. The old chips are `deleteLater`d, so
        a click still being delivered to one cannot land on a freed widget.
        """
        for chip in self.chips():
            self._rows.removeWidget(chip)
            chip.deleteLater()
        for position, item in enumerate(self.shelf.items):
            self._rows.insertWidget(position, _Chip(
                item, self._open, self._pin, self._remove, self._colours))
        items = self.shelf.items
        self.empty.setVisible(not items)
        self.button.setVisible(bool(items))
        self.button.setText(shelf_count_label(len(items), sum(1 for i in items if i.pinned)))
        if not items:
            self.popup.hide()
        elif self.popup.isVisible():
            self._place()

    def chips(self) -> list[_Chip]:
        return [self._rows.itemAt(i).widget() for i in range(self._rows.count())
                if isinstance(self._rows.itemAt(i).widget(), _Chip)]

    def open_list(self) -> None:
        """Show the list of documents above the button (the shelf sits low in the
        window, over the message box)."""
        if not self.shelf.items:
            return
        self._place()
        self.popup.show()
        self.popup.raise_()
        chips = self.chips()
        if chips:
            chips[0].name_button.setFocus()

    def _place(self) -> None:
        """Put the pop-up just above the button, or below it when there is no room
        above (the shelf sits low in the window).
        """
        self.popup.adjustSize()
        width = max(self.popup.sizeHint().width(), 320)
        self.popup.resize(width, self.popup.sizeHint().height())
        anchor = self.button.mapToGlobal(QPoint(0, 0))
        top = anchor.y() - self.popup.height() - 4
        if top < 0:                                   # no room above: open below
            top = anchor.y() + self.button.height() + 4
        self.popup.move(anchor.x(), top)

    def retint(self, colours: Optional[Mapping[str, str]] = None) -> None:
        """Repaint for the palette in use - the window calls this on a theme change."""
        self._colours = _colours(colours)
        self.setStyleSheet(_SHEET.format(**self._colours))
        self.popup.setStyleSheet(_LIST_SHEET.format(**self._colours))
        self.button.setIcon(icon("file-text", self._colours.get("text_dim", "#888888")))
        if self.shelf.items:
            self.refresh()

    def add_receipt(self, receipt) -> None:
        """A document the answer touched. Not a person's choice: no `changed`."""
        if self.shelf.add_receipt(receipt):
            self.refresh()

    def _open(self, path: str) -> None:
        self.popup.hide()
        self.open_requested.emit(path)

    def _pin(self, path: str) -> None:
        self.shelf.toggle_pin(path)
        self.refresh()
        self.changed.emit()

    def _remove(self, path: str) -> None:
        self.shelf.remove(path)
        self.refresh()
        self.changed.emit()

    # -- adding a result by dragging it in -----------------------------------
    def dragEnterEvent(self, event: Any) -> None:                # noqa: N802 - Qt
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event: Any) -> None:                 # noqa: N802 - Qt
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: Any) -> None:                     # noqa: N802 - Qt
        """A result dragged in: each local file goes on the shelf as the person's own choice."""
        added = False
        for url in event.mimeData().urls():
            if url.isLocalFile():
                added |= self.shelf.add(url.toLocalFile(), explicit=True)
        if added:
            self.refresh()
            self.changed.emit()
        event.acceptProposedAction()
