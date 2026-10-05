r"""A progress bar that shows it is alive: a gradient fill, a light sweeping
across it while a run is going, and a gliding segment while the size of the job
is unknown.

Layer: L5

**The owner, 2026-09-30:** *"the progress bar is also not animating, make the
progress bar a gradient animation which animates when it is going on so we know
something is happening ... or you decide on how you want to show that which is
modern"*. Two things made the old bar look dead:

1. **A counted bar does not move while one big file is read.** Reading a 4 GB
   archive holds the count still for minutes, so the bar sat at the same width
   and read as "stuck". Now, while a run is going (`set_active(True)`), a soft
   highlight sweeps along the filled part every `SWEEP_S` seconds. It says
   "working" without drawing progress that has not happened: the fill's width
   is still exactly `value / maximum`.
2. **The busy bar (no total yet) was Qt's stylesheet block**, which on Windows
   under `QStyleSheetStyle` often paints once and never moves (see the
   `QProgressBar::chunk` comment in `theme.py`). It is painted here instead: a
   gradient segment that eases across the track, driven by this widget's own
   timer, so it moves on every platform.

**The look** - a rounded pill track in `surface_alt` with a hairline `border`,
the fill a left-to-right gradient from the theme's `accent_bar` to a lighter,
slightly shifted tint of it. The percentage, when the bar shows text, is drawn
twice through two clips: the theme's text colour on the track, and on the fill
whichever of white or near-black reads against it (`text_on`).
Colours are read from `theme.theme_colours()` at paint time, so a theme switch
needs nothing.

**What it costs.** One timer at `FRAME_MS`, running **only** while the bar is
visible, its window is not minimised, and it is busy or active. An idle page,
a hidden page or a finished run runs no timer at all. Each frame is one
repaint of one small widget.

**Everything else is still a `QProgressBar`**: `setRange`, `setValue`,
`value()`, `text()` behave as before, which is what `GlidingBar`
(`widgets/indexing_bar.py`) and every test rely on.
"""

from __future__ import annotations

import math
import time
from typing import Optional

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QProgressBar, QWidget

__all__ = ["ShimmerBar", "FRAME_MS", "SWEEP_S", "BUSY_S", "fill_colours", "text_on"]

#: Time between frames while animating: 30 a second, smooth to the eye.
FRAME_MS = 33

#: One sweep of the highlight across the fill.
SWEEP_S = 1.6

#: One pass of the busy segment across the track.
BUSY_S = 1.4


def fill_colours(accent: str) -> tuple[QColor, QColor]:
    """The gradient's two ends: the accent, and a lighter tint turned a little
    along the colour wheel - so the fill has depth in both themes rather than
    fading to white (the dark theme's accent is already light)."""
    start = QColor(accent)
    if not start.isValid():
        start = QColor("#6d5dfc")
    hue, sat, light, _alpha = start.getHslF()
    hue = 0.0 if hue < 0 else hue
    end = QColor.fromHslF((hue + 0.07) % 1.0, min(1.0, sat * 1.05 + 0.05),
                          min(0.78, light + 0.22))
    return start, end


def text_on(start: QColor, end: QColor) -> QColor:
    """Text that reads on the fill: white on the light theme's deep accent, near
    black on the dark theme's pale one - chosen by the fill's own brightness."""
    def luminance(colour: QColor) -> float:
        return 0.2126 * colour.redF() + 0.7152 * colour.greenF() + 0.0722 * colour.blueF()
    return QColor("#ffffff") if (luminance(start) + luminance(end)) / 2 < 0.55 else QColor("#15102e")


def _colours() -> dict[str, str]:
    try:
        from app.ui.theme import theme_colours

        return theme_colours()
    except Exception:                                   # noqa: BLE001 - paint anyway
        return {}


class ShimmerBar(QProgressBar):
    """A `QProgressBar` painted as a gradient pill, animated while it matters."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._active = False
        self._anim = QTimer(self)
        self._anim.setInterval(FRAME_MS)
        self._anim.timeout.connect(self._frame)
        self._t0 = time.monotonic()
        #: Frames drawn by the timer since the bar was made - for tests.
        self.frames = 0

    # -- the one new call ------------------------------------------------------

    def set_active(self, active: bool) -> None:
        """A run is going (True) or not. While active the fill shimmers."""
        self._active = bool(active)
        self._sync()
        self.update()

    def active(self) -> bool:
        return self._active

    def animating(self) -> bool:
        """Is the animation timer running? For tests and the cost check."""
        return self._anim.isActive()

    def busy(self) -> bool:
        return self.minimum() == 0 and self.maximum() == 0

    # -- keeping the timer honest ----------------------------------------------

    def _wanted(self) -> bool:
        if not (self._active or self.busy()):
            return False
        if not self.isVisible():
            return False
        window = self.window()
        return window is None or not window.isMinimized()

    def _sync(self) -> None:
        if self._wanted():
            if not self._anim.isActive():
                self._t0 = time.monotonic()
                self._anim.start()
        elif self._anim.isActive():
            self._anim.stop()

    def _frame(self) -> None:
        self.frames += 1
        if not self._wanted():
            self._anim.stop()
        self.update()

    def setRange(self, minimum: int, maximum: int) -> None:  # noqa: N802 - Qt's name
        super().setRange(int(minimum), int(maximum))
        self._sync()
        self.update()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt's name
        super().showEvent(event)
        self._sync()

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt's name
        self._anim.stop()
        super().hideEvent(event)

    # -- painting --------------------------------------------------------------

    def _phase(self, period: float) -> float:
        return ((time.monotonic() - self._t0) % period) / period

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt's name
        tokens = _colours()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = min(rect.height() / 2.0, 8.0)
        track = QPainterPath()
        track.addRoundedRect(rect, radius, radius)

        # The track.
        painter.setPen(QPen(QColor(tokens.get("border", "#d0d0d8")), 1.0)
                       if rect.height() >= 6 else Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tokens.get("surface_alt", "#ececf2")))
        painter.drawPath(track)
        painter.setClipPath(track)

        start, end = fill_colours(tokens.get("accent_bar") or tokens.get("accent") or "")
        inner = rect.adjusted(1, 1, -1, -1) if rect.height() >= 6 else rect

        if self.busy():
            # A segment a third of the track wide, easing across and round again.
            eased = 0.5 - 0.5 * math.cos(math.pi * self._phase(BUSY_S))
            width = max(24.0, inner.width() * 0.32)
            left = inner.left() - width + (inner.width() + width) * eased
            segment = QRectF(left, inner.top(), width, inner.height())
            gradient = QLinearGradient(segment.topLeft(), segment.topRight())
            fade = QColor(start)
            fade.setAlphaF(0.0)
            gradient.setColorAt(0.0, fade)
            gradient.setColorAt(0.35, start)
            gradient.setColorAt(1.0, end)
            path = QPainterPath()
            path.addRoundedRect(segment, radius, radius)
            painter.fillPath(path, gradient)
            fill = QRectF()
        else:
            span = max(1, self.maximum() - self.minimum())
            fraction = min(1.0, max(0.0, (self.value() - self.minimum()) / span))
            fill = QRectF(inner.left(), inner.top(), inner.width() * fraction, inner.height())
            if fill.width() > 0.5:
                gradient = QLinearGradient(inner.topLeft(), inner.topRight())
                gradient.setColorAt(0.0, start)
                gradient.setColorAt(1.0, end)
                path = QPainterPath()
                path.addRoundedRect(fill, radius, radius)
                painter.fillPath(path, gradient)
                if self._active:
                    # The sweep: a soft light band crossing the filled part.
                    band = max(18.0, fill.width() * 0.35)
                    x = fill.left() - band + (fill.width() + band) * self._phase(SWEEP_S)
                    shine = QLinearGradient(x, 0, x + band, 0)
                    clear = QColor(255, 255, 255, 0)
                    shine.setColorAt(0.0, clear)
                    shine.setColorAt(0.5, QColor(255, 255, 255, 110))
                    shine.setColorAt(1.0, clear)
                    painter.save()
                    painter.setClipPath(path)
                    painter.fillRect(QRectF(x, fill.top(), band, fill.height()), shine)
                    painter.restore()

        if self.isTextVisible() and rect.height() >= 14 and not self.busy():
            text = self.text()
            if text:
                painter.setClipPath(track)
                painter.setPen(QColor(tokens.get("text", "#1a1a1a")))
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
                if fill.width() > 0.5:
                    over = QPainterPath()
                    over.addRect(fill)
                    painter.setClipPath(over)
                    painter.setPen(text_on(start, end))
                    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
        painter.end()
