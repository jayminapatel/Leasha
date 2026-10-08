r"""The quick search box's window: rounded, shadowed, movable, resizable.

Layer: L5

The owner, 2026-10-08: *"the search from ctrl alt L is not moveable window
which it should be"*. This is the window half of the box - nothing here
searches. `MiniSearch` is built on it.

* **A rounded card on a transparent window**, with a soft shadow painted
  around it here rather than by a `QGraphicsDropShadowEffect`: an effect
  re-renders everything inside the card on every caret blink and keystroke,
  and this box is typed into.
* **Dragged by its top bar**, anywhere, on any screen. The operating system
  moves it when it can (`startSystemMove`: snapping and the second monitor
  work as for any window); where it cannot - the offscreen test platform -
  the move is done here, by hand.
* **Resized from its edges** and from the grip in the corner, with the same
  two routes.
* **Put back where it was left.** `fit_on_screens` (presenter) decides where
  a saved rectangle may go on the screens that exist now.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPainterPath
from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget

__all__ = ["CardWindow", "SHADOW", "CARD_RADIUS", "screens_now"]

#: Room around the card for its shadow, in pixels.
SHADOW = 18

#: The card's corner radius.
CARD_RADIUS = 14

#: How close to the card's edge a press starts a resize.
_EDGE = 6


def screens_now() -> tuple[list, int]:
    r"""`([(x, y, w, h), ...], index)` - every screen's usable area, and the
    one under the pointer (else the primary). For `fit_on_screens`."""
    screens = QGuiApplication.screens()
    found = [(s.availableGeometry().x(), s.availableGeometry().y(),
              s.availableGeometry().width(), s.availableGeometry().height()) for s in screens]
    here = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    index = screens.index(here) if here in screens else 0
    return found, index


class CardWindow(QFrame):
    """A frameless, translucent top-level window holding one rounded `card`."""

    def __init__(self, flags: Any) -> None:
        super().__init__(None, flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.card = QFrame(self)
        self.card.setObjectName("miniCard")
        self.card.setMouseTracking(True)
        self.card.installEventFilter(self)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW, SHADOW - 6, SHADOW, SHADOW + 4)
        outer.addWidget(self.card)
        #: The widgets a press on starts a move (the top bar and its handle).
        self._drag_handles: list = []
        self._dragging: Optional[QPoint] = None
        self._resizing: Optional[tuple] = None
        self._shadow_ink = QColor(0, 0, 0)

    # -- moving ----------------------------------------------------------------

    def add_drag_handle(self, widget: QWidget) -> None:
        """A press on `widget` (not on a control inside it) moves the window."""
        widget.installEventFilter(self)
        self._drag_handles.append(widget)

    def moved_by_hand(self) -> None:
        """Called when a move or resize ends. `MiniSearch` saves the place."""

    def _start_move(self, global_pos: QPoint) -> None:
        """Begin a drag: the window system's own move when it offers one, else a
        hand move tracked in `eventFilter` (the offscreen platform has none).
        """
        handle = self.windowHandle()
        try:
            if handle is not None and handle.startSystemMove():
                return
        except Exception:                        # noqa: BLE001 - the hand route below
            pass
        self._dragging = global_pos - self.frameGeometry().topLeft()

    def _edges_at(self, pos: QPoint) -> Any:
        """Which card edges `pos` (in card coordinates) is on."""
        rect = self.card.rect()
        edges = Qt.Edge(0)
        if pos.x() <= _EDGE:
            edges |= Qt.Edge.LeftEdge
        elif pos.x() >= rect.width() - _EDGE:
            edges |= Qt.Edge.RightEdge
        if pos.y() <= _EDGE:
            edges |= Qt.Edge.TopEdge
        elif pos.y() >= rect.height() - _EDGE:
            edges |= Qt.Edge.BottomEdge
        return edges

    @staticmethod
    def _cursor_for(edges: Any) -> Any:
        """The resize cursor for a set of edges, or None away from any edge."""
        left, right = bool(edges & Qt.Edge.LeftEdge), bool(edges & Qt.Edge.RightEdge)
        top, bottom = bool(edges & Qt.Edge.TopEdge), bool(edges & Qt.Edge.BottomEdge)
        if (left and top) or (right and bottom):
            return Qt.CursorShape.SizeFDiagCursor
        if (right and top) or (left and bottom):
            return Qt.CursorShape.SizeBDiagCursor
        if left or right:
            return Qt.CursorShape.SizeHorCursor
        if top or bottom:
            return Qt.CursorShape.SizeVerCursor
        return None

    def _start_resize(self, edges: Any, global_pos: QPoint) -> None:
        """Begin a resize, by the window system or by hand - as `_start_move`."""
        handle = self.windowHandle()
        try:
            if handle is not None and handle.startSystemResize(edges):
                return
        except Exception:                        # noqa: BLE001 - the hand route below
            pass
        self._resizing = (edges, global_pos, QRect(self.geometry()))

    def _resize_to(self, global_pos: QPoint) -> None:
        """A hand resize: move the grabbed edges, never below the minimum size."""
        edges, start, geometry = self._resizing
        delta = global_pos - start
        rect = QRect(geometry)
        minimum = self.minimumSize()
        if edges & Qt.Edge.LeftEdge:
            rect.setLeft(min(rect.left() + delta.x(), rect.right() - minimum.width()))
        if edges & Qt.Edge.RightEdge:
            rect.setRight(max(rect.right() + delta.x(), rect.left() + minimum.width()))
        if edges & Qt.Edge.TopEdge:
            rect.setTop(min(rect.top() + delta.y(), rect.bottom() - minimum.height()))
        if edges & Qt.Edge.BottomEdge:
            rect.setBottom(max(rect.bottom() + delta.y(), rect.top() + minimum.height()))
        self.setGeometry(rect)

    def eventFilter(self, obj: Any, event: Any) -> bool:     # noqa: N802 - Qt's name
        """Mouse events on the drag handles (move) and the card (resize cursor and
        resize). UI thread.
        """
        kind = event.type()
        if obj in self._drag_handles:
            if (kind == QEvent.Type.MouseButtonPress
                    and event.button() == Qt.MouseButton.LeftButton):
                self._start_move(event.globalPosition().toPoint())
                return True
            if kind == QEvent.Type.MouseMove and self._dragging is not None:
                self.move(event.globalPosition().toPoint() - self._dragging)
                return True
            if kind == QEvent.Type.MouseButtonRelease and self._dragging is not None:
                self._dragging = None
                self.moved_by_hand()
                return True
        if obj is self.card:
            if kind == QEvent.Type.MouseMove and self._resizing is not None:
                self._resize_to(event.globalPosition().toPoint())
                return True
            if kind == QEvent.Type.MouseMove and not event.buttons():
                shape = self._cursor_for(self._edges_at(event.position().toPoint()))
                if shape is None:
                    self.card.unsetCursor()
                else:
                    self.card.setCursor(shape)
            if (kind == QEvent.Type.MouseButtonPress
                    and event.button() == Qt.MouseButton.LeftButton):
                edges = self._edges_at(event.position().toPoint())
                if edges != Qt.Edge(0):
                    self._start_resize(edges, event.globalPosition().toPoint())
                    return True
            if kind == QEvent.Type.MouseButtonRelease and self._resizing is not None:
                self._resizing = None
                self.moved_by_hand()
                return True
        return super().eventFilter(obj, event)

    # -- the shadow --------------------------------------------------------------

    def set_shadow_strength(self, dark: bool) -> None:
        """A deeper shadow on a dark theme, where a light one would not show."""
        self._shadow_ink = QColor(0, 0, 0)
        self._shadow_alpha = 70 if dark else 34
        self.update()

    def paintEvent(self, event: Any) -> None:            # noqa: N802 - Qt's name
        """The soft shadow, as rings of falling opacity around the card."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        card = QRectF(self.card.geometry())
        strength = getattr(self, "_shadow_alpha", 34)
        steps = SHADOW - 2
        for step in range(steps, 0, -1):
            share = (1.0 - step / steps) ** 2
            colour = QColor(self._shadow_ink)
            colour.setAlpha(max(0, int(strength * share / 4)))
            grown = card.adjusted(-step, -step + 4, step, step + 4)
            path = QPainterPath()
            path.addRoundedRect(grown, CARD_RADIUS + step, CARD_RADIUS + step)
            painter.fillPath(path, colour)
        painter.end()
