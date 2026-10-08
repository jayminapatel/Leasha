r"""Coalesce a burst of changes into one, so holding an arrow does not hammer disk.

Layer: L5

**The unresponsive up/down buttons were disk writes, not Qt.** Every notch on
`Memory ceiling` fired `valueChanged`, which persisted *five* settings keys, each
in its own SQLite transaction, on the UI thread. Holding the arrow auto-repeats
about ten times a second, so the window was asked for fifty committed
transactions a second and stopped repainting between them. The button was
working perfectly; it was the handler behind it that could not keep up.

Typing made it worse. `QSpinBox` tracks the keyboard by default, so typing
`1500` emits at 1, 15, 150 and 1500 - four rounds of writes for one number, and
three of them for values nobody chose.

**The rule: settle first, then persist.** A change restarts a short timer; the
work runs once the person stops. `flush()` forces it early, for the case that
actually matters - the window closing before the timer fires.

`DEFAULT_DELAY_MS` is 350: long enough to swallow auto-repeat and typing, short
enough that letting go of the mouse and reading "Saved" feels immediate.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QTimer

__all__ = ["Debounced", "DEFAULT_DELAY_MS"]

#: Milliseconds of quiet before the work runs. Windows key auto-repeat is around
#: 30/sec at its fastest and spin-box repeat about 10/sec, so anything above
#: ~120ms coalesces a held button; the rest of the budget buys unhurried typing.
DEFAULT_DELAY_MS = 350


class Debounced(QObject):
    """Runs `work` once the changes stop arriving.

    Deliberately not a decorator or a signal subclass. It holds the pending
    argument, which means the *last* value wins - the right answer for a
    setting, where intermediate values were never chosen by anybody.
    """

    def __init__(
        self,
        work: Callable[..., Any],
        *,
        delay_ms: int = DEFAULT_DELAY_MS,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._work = work
        self._pending: Optional[tuple] = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(max(0, int(delay_ms)))
        self._timer.timeout.connect(self._fire)

    # -- the two things a caller does ---------------------------------------

    def __call__(self, *args: Any) -> None:
        """Note a change. Restarts the clock; nothing is written yet."""
        self._pending = args
        self._timer.start()

    def flush(self) -> bool:
        """Run any pending work now. True if there was some.

        **This is the one that prevents data loss.** Without it, closing the
        window inside the delay throws away the change - which is a worse bug
        than the slowness this class exists to fix, so every place that
        debounces a setting must flush on close.
        """
        if self._pending is None:
            return False
        self._timer.stop()
        self._fire()
        return True

    def cancel(self) -> bool:
        """Drop any pending work. True if there was some.

        For loading: values queued before a reload were about the *old*
        settings, and firing them afterwards writes them straight back over
        what was just loaded. `blockSignals` cannot help - the signal was
        already emitted, it is the timer that is still holding it.
        """
        if self._pending is None:
            return False
        self._timer.stop()
        self._pending = None
        return True

    # -- internals -----------------------------------------------------------

    def _fire(self) -> None:
        """The timer fired: run the work once with the last arguments noted."""
        args, self._pending = self._pending, None
        if args is None:
            return
        self._work(*args)

    @property
    def pending(self) -> bool:
        return self._pending is not None
