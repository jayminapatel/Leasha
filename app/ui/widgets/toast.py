r"""Toasts: what the status bar used to say, said where the eye is.

Layer: L5

UI Redesign (202626160950 §6). `shell.py` called the status bar's `showMessage`
at 39 sites; the owner decided the status bar goes entirely ([FINALISE 1]).
Every one of those strings now arrives here, verbatim, through
`MainWindow.notify(text, level, timeout_ms)`.

**One line, bottom-centre of the central widget, queued.** A second message
while one is showing waits its turn rather than overwriting it, because two
of the 39 fire within a second of each other and the first was never read.
A click dismisses; so does the timeout. It never takes focus.

**Announced as well as shown** (§6c): the text is also set on a hidden label
with the static-text accessible role, so a screen reader hears what a
sighted person glimpses.

**Level is a dot beside the text, never instead of it.** Colour alone is not
a signal everybody receives - the same reasoning as `#statWarn`.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

__all__ = ["Toast", "LEVELS", "DEFAULT_TIMEOUT_MS"]

LEVELS = ("info", "warning", "danger")
DEFAULT_TIMEOUT_MS = 4000


class Toast(QFrame):
    """The one-line notice over the central widget, queued and timed. UI
    thread; `show_message` is what `MainWindow.notify` calls.
    """
    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self.setObjectName("toast")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._host = host
        self._queue: deque[tuple[str, str, int]] = deque()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._advance)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 14, 6)
        layout.setSpacing(10)
        self.dot = QFrame()
        self.dot.setObjectName("toastDot")
        self.dot.setProperty("level", "info")
        self.dot.setFixedSize(8, 8)
        self.label = QLabel("")
        self.label.setObjectName("toastText")
        layout.addWidget(self.dot)
        layout.addWidget(self.label)

        # §6c: the live region. Hidden from sight, present to assistive tech.
        self.announcer = QLabel("", host)
        self.announcer.setObjectName("toastAnnouncer")
        self.announcer.setAccessibleName("Notice")
        self.announcer.setFixedSize(1, 1)
        self.announcer.move(-10, -10)
        self.setVisible(False)
        host.installEventFilter(self)

    # -- API ----------------------------------------------------------------------

    def show_message(self, text: str, level: str = "info",
                     timeout_ms: int = DEFAULT_TIMEOUT_MS) -> None:
        """Queue `text`. Same name shape as `QStatusBar.showMessage`."""
        text = str(text or "").strip()
        if not text:
            return
        if level not in LEVELS:
            level = "info"
        self._queue.append((text, level, max(500, int(timeout_ms or DEFAULT_TIMEOUT_MS))))
        if not self._timer.isActive():
            self._advance()

    def current_text(self) -> str:
        """The message on screen, or `""`."""
        # `isHidden`, not `isVisible`: a toast in a window that has not been
        # shown yet (every headless test) is still "showing" its message.
        return "" if self.isHidden() else self.label.text()

    def clear(self) -> None:
        """Drop the queue and hide at once."""
        self._queue.clear()
        self._timer.stop()
        self.setVisible(False)

    def pending(self) -> int:
        return len(self._queue)

    # -- internals ----------------------------------------------------------------

    def _advance(self) -> None:
        """Show the next queued message, or hide when the queue is empty."""
        if not self._queue:
            self.setVisible(False)
            return
        text, level, timeout = self._queue.popleft()
        self.label.setText(text)
        self.dot.setProperty("level", level)
        self.dot.style().unpolish(self.dot)
        self.dot.style().polish(self.dot)
        self.setAccessibleName(f"{level}: {text}")
        self.announcer.setText(text)
        self.announcer.setAccessibleDescription(text)
        try:
            # Moved inside the try, both of them: this PySide6 build does not
            # expose QAccessible/QAccessibleEvent at all (checked - neither
            # is in QtCore, QtGui or QtWidgets on 6.11.0), and the eager
            # module-level import used to crash app.ui.shell entirely on
            # import, taking every Qt-dependent test down with it. The
            # live-region label two lines up (setText/setAccessibleDescription)
            # is what actually reaches assistive tech on this build; this stays
            # only in case a future PySide6 build restores the symbol.
            from PySide6.QtGui import QAccessible, QAccessibleEvent
            QAccessible.updateAccessibility(
                QAccessibleEvent(self.announcer, QAccessible.Event.Alert))
        except Exception:                        # noqa: BLE001 - a11y is best-effort
            pass
        self.adjustSize()
        self._place()
        self.setVisible(True)
        self.raise_()
        self._timer.start(timeout)

    def _place(self) -> None:
        """Bottom-centre of the host, never off its top-left."""
        host = self._host
        self.adjustSize()
        x = (host.width() - self.width()) // 2
        y = host.height() - self.height() - 16
        self.move(max(8, x), max(8, y))

    def eventFilter(self, obj: Any, event: Any) -> bool:      # noqa: N802
        """The host moved or resized: keep the toast at the bottom centre."""
        if obj is self._host and not self.isHidden():
            from PySide6.QtCore import QEvent
            if event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
                self._place()
        return False

    def mousePressEvent(self, event: Any) -> None:            # noqa: N802
        """A click dismisses the message and shows the next one, if any."""
        self._timer.stop()
        self._advance()
        event.accept()
