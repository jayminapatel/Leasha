r"""Do this shortly, unless the thing that asked has gone away first.

Layer: L5 - the smallest possible Qt helper, and one rule.

**Why this exists.** `QTimer.singleShot(ms, callback)` keeps no owner. When the
callback is a **lambda**, nothing tells Qt which object it belongs to, so the timer
fires whatever has happened in the meantime - including into a widget whose C++ half
has already been deleted. That is a crash, or at best a `RuntimeError` raised inside a
Qt slot where nobody catches it, and this application has already had one: a late search
answer painted into a results view the window had thrown away.

**A bound method does not have that problem**, which is the distinction worth knowing
rather than memorising a rule. `QTimer.singleShot(ms, self._method)` gives Qt a receiver
- the QObject the method is bound to - and Qt drops the connection when that object
dies. Measured here on PyQt6 6.11 (`test_later.py` pins it):

    singleShot(5, obj.method)        obj deleted first -> never fires
    singleShot(5, lambda: ...)       obj deleted first -> fires anyway

So a lambda needs an owner given to it explicitly, and that is all `later` is: a
single-shot `QTimer` **parented to the object that cares**. Destroy the owner and the
timer is destroyed with it, so the callback cannot run against a corpse. PyQt6 6.11 does
not expose Qt's own three-argument `singleShot(ms, context, slot)` overload - checked,
it raises `TypeError` - or this would be a one-line wrapper around that.

    later(self, 0, lambda: self._build_the_pages(store))

Use a bound method where the call takes no arguments; use this where it takes some.
`test_no_orphan_single_shot.py` keeps new lambdas from appearing without an owner.
"""

from __future__ import annotations

from typing import Any, Callable

__all__ = ["later"]


def later(owner: Any, msec: int, call: Callable[[], Any]) -> Any:
    """Run `call` in `msec` milliseconds, unless `owner` is destroyed first.

    Returns the timer, for a caller that wants to stop it sooner. `owner` must be a
    QObject (every widget is one); the timer is its child, so Qt's own ownership does
    the cancelling and there is nothing to remember to undo.
    """
    from PyQt6.QtCore import QTimer

    timer = QTimer(owner)
    timer.setSingleShot(True)
    timer.timeout.connect(call)
    timer.start(max(0, int(msec)))
    return timer
