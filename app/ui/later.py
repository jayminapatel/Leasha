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
dies. Measured here on PySide6 6.11 (`test_later.py` pins it):

    singleShot(5, obj.method)        obj deleted first -> never fires
    singleShot(5, lambda: ...)       obj deleted first -> fires anyway

So a lambda needs an owner given to it explicitly, and that is all `later` is: a
single-shot `QTimer` **parented to the object that cares**. Destroy the owner and the
timer is destroyed with it, so the callback cannot run against a corpse. PySide6 6.11 does
not expose Qt's own three-argument `singleShot(ms, context, slot)` overload - checked,
it raises `TypeError` - or this would be a one-line wrapper around that.

    later(self, 0, lambda: self._build_the_pages(store))

Use a bound method where the call takes no arguments; use this where it takes some.
`test_no_orphan_single_shot.py` keeps new lambdas from appearing without an owner.

**A worker's answer is the same shape of problem**, and `when_done` below is the same
answer. `worker.signals.finished.connect(lambda ...)` has no receiver either, so the
lambda runs when the worker lands however long that took - and if it touches a Qt object
whose C++ half has since been deleted, the `RuntimeError` is raised inside a Qt slot
where nothing catches it. Measured here, same PySide6 6.11, in `test_worker_signal_owner.py`:

    signals.finished.connect(lambda: chip.setText(x))   chip deleteLater'd -> RuntimeError
    signals.finished.connect(relay.fire)                relay's owner gone  -> never fires

**Which sites that actually bites, and which it does not**, is worth writing down because
the answer is not "all of them". A lambda that captures `self` holds a Python reference to
it, so a widget Python alone owns - a pop-out window kept in a list, the mini palette -
cannot be collected while the worker is in flight: the connection is what keeps it alive.
What a Python reference does *not* keep alive is the C++ half of a widget Qt owns, so the
sites that are genuinely exposed are the ones whose receiver is a **Qt-parented object
that something deletes independently**: a child `deleteLater()`d by a rebuild loop, an
item taken out of a tree. Those get an owner. The rest are recorded as reasoned-about, not
overlooked - see `test_worker_signal_owner.py`, which holds the triage.
"""

from __future__ import annotations

from typing import Any, Callable

__all__ = ["later", "when_done"]


def later(owner: Any, msec: int, call: Callable[[], Any]) -> Any:
    """Run `call` in `msec` milliseconds, unless `owner` is destroyed first.

    Returns the timer, for a caller that wants to stop it sooner. `owner` must be a
    QObject (every widget is one); the timer is its child, so Qt's own ownership does
    the cancelling and there is nothing to remember to undo.
    """
    from PySide6.QtCore import QTimer

    timer = QTimer(owner)
    timer.setSingleShot(True)
    timer.timeout.connect(call)
    timer.start(max(0, int(msec)))
    return timer


def when_done(owner: Any, worker: Any, *, finished: Callable | None = None,
              failed: Callable | None = None, progress: Callable | None = None,
              done: Callable | None = None) -> Any:
    """Route `worker`'s signals to callables that stop mattering when `owner` dies.

    The same rule as `later`, for the other way a call arrives late. Each callable is
    reached through one small `QObject` **parented to `owner`**, and it is that QObject -
    not the lambda - that Qt sees as the receiver. Destroy `owner` and Qt destroys the
    relay with it and drops the queued call, exactly as it does for a bound method.

        when_done(self, worker, finished=lambda rows: self._show(rows, generation))

    `owner` must be the object the callables will *touch*, which is not always the object
    that started the worker: a per-item decode belongs to the item it paints, not to the
    page that dispatched it, or the page's own lifetime is the only one protected.

    The relay is deleted once the worker reports `done`, so a view that dispatches one
    worker per thumbnail does not accumulate a child QObject per thumbnail for as long as
    it lives. Returns the relay, for a caller that wants to cut the connection sooner.
    """
    from PySide6.QtCore import QObject

    class _Relay(QObject):
        """The receiver Qt needs. Its slots are bound methods of a real QObject."""

        def on_finished(self, value: Any) -> None:
            if finished is not None:
                finished(value)

        def on_failed(self, error: Any) -> None:
            if failed is not None:
                failed(error)

        def on_progress(self, stage: Any) -> None:
            if progress is not None:
                progress(stage)

        def on_done(self) -> None:
            if done is not None:
                done()

    relay = _Relay(owner)
    signals = worker.signals
    if finished is not None:
        signals.finished.connect(relay.on_finished)
    if failed is not None:
        signals.failed.connect(relay.on_failed)
    if progress is not None:
        signals.progress.connect(relay.on_progress)
    if done is not None:
        signals.done.connect(relay.on_done)
    # **Last, so it is delivered after the callables above.** Queued deliveries keep
    # their order, and `done` is emitted after `finished`/`failed`, so by the time this
    # runs the relay has already done its work. `deleteLater` is itself a bound method
    # of a QObject, so an owner that died first cancels this too rather than leaking.
    signals.done.connect(relay.deleteLater)
    return relay
