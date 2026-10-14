r"""The command palette: Ctrl+K, a box, and every page, setting and action.

Layer: L5

IA-1 / MS-11 of the UX review (2026-10-14). What it lists and how a line of
typing is matched is the presenter's (`presenter/actions.py`); this is the
window: the quick search box's rounded card (`CardWindow`, the same frame and
the same tokens), a box and a list.

* **Typing filters**; **Up / Down / Page Up / Page Down** move the choice while
  the cursor stays in the box; **Enter** runs it; **Esc** closes. A click runs
  the line clicked.
* **A popup**: a click anywhere else closes it, as a menu closes.
* **What it runs, it runs after it has closed** - on the next turn of the event
  loop - so a page that puts the cursor in its own box gets the focus, not the
  palette that was in the way.

The entries are asked for at each opening (`provider`), never kept: the rail can
be reordered and Settings is built a beat after the window appears.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

from PySide6.QtCore import QEvent, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame, QLineEdit, QListWidget, QListWidgetItem, QStyledItemDelegate, QVBoxLayout,
)

from app.core.logging import logger
from app.ui.presenter.actions import PLACEHOLDER, Entry, match
from app.ui.theme import SPACE_M, SPACE_S, SPACE_XS
from app.ui.widgets.mini_search_frame import SHADOW, CardWindow

__all__ = ["CommandPalette", "WIDTH", "ROWS"]

_log = logger.bind(component="ui.palette")

#: The card's width, and the most lines shown before the list scrolls.
WIDTH = 600
ROWS = 10

_MOVES = {Qt.Key.Key_Down: 1, Qt.Key.Key_Up: -1,
          Qt.Key.Key_PageDown: ROWS - 1, Qt.Key.Key_PageUp: -(ROWS - 1)}
_ENTER = (Qt.Key.Key_Return, Qt.Key.Key_Enter)

_SHEET = """
#commandPalette {{ background: transparent; border: none; }}
#miniCard {{
    background: {surface}; border: 1px solid {border_strong}; border-radius: 14px;
}}
QLineEdit#paletteBox {{
    background: transparent; border: none; padding: 6px 2px;
    font-size: {field}; color: {text};
    selection-background-color: {accent}; selection-color: {selection_text};
}}
#paletteRule {{ background: {divider}; border: none; }}
QListWidget#paletteList {{ background: transparent; border: none; outline: none; }}
QListWidget#paletteList::item {{
    color: {text}; padding: 6px 8px; border-radius: 8px; font-size: {body};
}}
QListWidget#paletteList::item:hover {{ background: {surface_hover}; }}
QListWidget#paletteList::item:selected {{ background: {accent_soft}; color: {accent_text}; }}
"""


class _KeysDelegate(QStyledItemDelegate):
    """Qt's own line, then the shortcut on the right in the faint ink."""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.ink = QColor(0, 0, 0)

    def paint(self, painter: Any, option: Any, index: Any) -> None:
        super().paint(painter, option, index)
        keys = index.data(Qt.ItemDataRole.UserRole + 1)
        if not keys:
            return
        painter.save()
        painter.setPen(self.ink)
        rect = QRect(option.rect).adjusted(0, 0, -SPACE_M, 0)
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                         str(keys))
        painter.restore()


class CommandPalette(CardWindow):
    """The palette window. One per main window, built on first use."""

    def __init__(self, provider: Callable[[], Sequence[Entry]],
                 search: Optional[Callable[[str], Any]] = None) -> None:
        super().__init__(Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("commandPalette")
        self._provider = provider
        self._search = search
        self._entries: list[Entry] = []
        self._shown: list[Entry] = []
        #: The last entry run - for tests and the log.
        self.last_run: Optional[Entry] = None

        card = QVBoxLayout(self.card)
        card.setContentsMargins(SPACE_S, SPACE_S, SPACE_S, SPACE_S)
        card.setSpacing(SPACE_XS)
        self.box = QLineEdit()
        self.box.setObjectName("paletteBox")
        self.box.setPlaceholderText(PLACEHOLDER)
        self.box.setAccessibleName("Command palette")
        self.box.setFrame(False)
        self.box.textChanged.connect(self._refill)
        self.box.installEventFilter(self)
        card.addWidget(self.box)
        rule = QFrame()
        rule.setObjectName("paletteRule")
        rule.setFixedHeight(1)
        card.addWidget(rule)
        self.list = QListWidget()
        self.list.setObjectName("paletteList")
        self.list.setAccessibleName("Pages, settings and actions")
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setUniformItemSizes(True)
        self._delegate = _KeysDelegate(self.list)
        self.list.setItemDelegate(self._delegate)
        self.list.itemClicked.connect(lambda item: self._run_row(self.list.row(item)))
        card.addWidget(self.list, 1)

    # -- opening -------------------------------------------------------------------

    def open_over(self, window: Any, text: str = "") -> None:
        """Show over `window`'s upper middle, the entries read afresh, `text` typed."""
        self._apply_theme()
        try:
            self._entries = list(self._provider() or ())
        except Exception as exc:                 # noqa: BLE001 - an empty palette, never a crash
            _log.warning("the palette could not list its entries: {}", exc)
            self._entries = []
        self.box.blockSignals(True)
        self.box.setText(str(text or ""))
        self.box.blockSignals(False)
        self._refill()
        if window is not None:
            frame = window.frameGeometry()
            width = min(WIDTH, max(320, frame.width() - 2 * SHADOW)) + 2 * SHADOW
            line = max(self.list.sizeHintForRow(0), 24) if self.list.count() else 28
            height = self.box.sizeHint().height() + line * ROWS + 2 * SHADOW + 3 * SPACE_S
            self.setGeometry(frame.x() + (frame.width() - width) // 2,
                             frame.y() + max(40, frame.height() // 8), width, height)
        self.show()
        self.raise_()
        self.activateWindow()
        self.box.setFocus(Qt.FocusReason.PopupFocusReason)

    def _apply_theme(self) -> None:
        from app.ui import theme
        from app.ui.widgets.icons import icon_size

        colours = theme.theme_colours()
        sizes = theme.font_sizes()
        sizes["field"] = f"{round(float(sizes['body'][:-2]) * 1.4, 1)}pt"
        self.setStyleSheet(_SHEET.format(**colours, **sizes))
        dark = colours == theme.PALETTES.get("dark")
        self.set_shadow_strength(dark)
        self._delegate.ink = QColor(colours.get("text_faint", "#888888"))
        self._colours = colours
        self.list.setIconSize(icon_size("menu"))

    # -- the list ------------------------------------------------------------------

    def shown(self) -> list[Entry]:
        """The entries on screen now, top to bottom."""
        return list(self._shown)

    def _refill(self, *_args: Any) -> None:
        from app.ui.widgets.icons import icon

        self._shown = match(self._entries, self.box.text())
        dim = getattr(self, "_colours", {}).get("text_dim", "#888888")
        self.list.clear()
        for entry in self._shown:
            item = QListWidgetItem(entry.text)
            if entry.icon:
                item.setIcon(icon(entry.icon, dim))
            if entry.tip:
                item.setToolTip(entry.tip)
            item.setData(Qt.ItemDataRole.UserRole + 1, entry.keys)
            item.setSizeHint(QSize(0, 30))
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _move(self, step: int) -> None:
        count = self.list.count()
        if not count:
            return
        row = self.list.currentRow()
        self.list.setCurrentRow(max(0, min(count - 1, (row if row >= 0 else -1) + step)))

    # -- running -------------------------------------------------------------------

    def _run_row(self, row: int) -> None:
        if not 0 <= row < len(self._shown):
            return
        entry = self._shown[row]
        self.last_run = entry
        self.close()
        QTimer.singleShot(0, lambda: self._run(entry))

    def _run(self, entry: Entry) -> None:
        try:
            if entry.run is not None:
                entry.run()
            elif entry.query and self._search is not None:
                self._search(entry.query)
        except Exception as exc:                 # noqa: BLE001 - one line, not the window
            _log.warning("the palette's {!r} failed: {}", entry.text, exc)

    def eventFilter(self, watched: Any, event: Any) -> bool:      # noqa: N802 - Qt's name
        if watched is getattr(self, "box", None) and event.type() == QEvent.Type.KeyPress:
            key = event.key()
            toggle = key == Qt.Key.Key_K and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            if key == Qt.Key.Key_Escape or toggle:
                self.close()                     # Ctrl+K again puts it away, as it opened it
                return True
            if key in _MOVES:
                self._move(_MOVES[key])
                return True
            if key in _ENTER:
                self._run_row(self.list.currentRow())
                return True
            return False
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event: Any) -> None:                   # noqa: N802 - Qt's name
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def sizeHint(self) -> QSize:                                   # noqa: N802 - Qt's name
        return QSize(WIDTH + 2 * SHADOW, 420)


# The style tokens this window reads, so a test can hold them to both palettes.
TOKENS = ("surface", "border_strong", "text", "accent", "selection_text", "divider",
          "surface_hover", "accent_soft", "accent_text", "text_faint", "text_dim")
