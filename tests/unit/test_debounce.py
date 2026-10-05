"""Coalescing a burst of changes - and never losing the last one.

Layer: L5

The bug: every notch on `Memory ceiling` persisted five settings keys, each its
own SQLite transaction, on the UI thread. Auto-repeat is about ten notches a
second, so holding the arrow asked the window for fifty commits a second and it
stopped repainting. Reported as *"the up down buttons are not always
responsive"*. The buttons were fine.

The risk in the fix is worse than the bug it fixes: a delayed write that never
happens is data loss, where the original was only slow. `flush` and `cancel`
are therefore tested harder than the coalescing is.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtCore")

from app.ui.widgets.debounce import DEFAULT_DELAY_MS, Debounced   # noqa: E402


class FakeTimer:
    """A QTimer with the clock under the test's control.

    Real timers need an event loop, and a test that sleeps 350ms to assert a
    350ms delay is a test that takes 350ms and still passes when the delay is
    340. This asserts the *shape*: started, restarted, stopped, fired.
    """

    def __init__(self, *_args, **_kwargs) -> None:
        self.interval = 0
        self.running = False
        self.starts = 0
        self._slot = None
        self.timeout = self

    # QTimer's surface, as much of it as Debounced uses
    def setSingleShot(self, _value): pass                 # noqa: N802
    def setInterval(self, value): self.interval = value   # noqa: N802
    def connect(self, slot): self._slot = slot
    def start(self):
        self.running = True
        self.starts += 1
    def stop(self): self.running = False

    def elapse(self):
        """Pretend the interval passed."""
        if self.running:
            self.running = False
            self._slot()


@pytest.fixture
def debounced(monkeypatch):
    monkeypatch.setattr("app.ui.widgets.debounce.QTimer", FakeTimer)
    calls: list[tuple] = []
    target = Debounced(lambda *args: calls.append(args))
    return target, calls, target._timer


def test_nothing_happens_until_the_changes_stop(debounced):
    target, calls, timer = debounced
    for value in range(10):
        target(value)
    assert calls == [], "ten notches must not be ten writes"
    timer.elapse()
    assert calls == [(9,)]


def test_the_last_value_wins(debounced):
    """The right answer for a setting: the intermediate values on the way from
    1500 to 2000 were never chosen by anybody."""
    target, calls, timer = debounced
    target(1500); target(1600); target(2000)
    timer.elapse()
    assert calls == [(2000,)]


def test_each_change_restarts_the_clock(debounced):
    target, _calls, timer = debounced
    target(1); target(2); target(3)
    assert timer.starts == 3


def test_flush_writes_immediately(debounced):
    """**The one that prevents data loss.** Closing the window inside the delay
    must not throw the change away."""
    target, calls, timer = debounced
    target(42)
    assert target.flush() is True
    assert calls == [(42,)]
    assert timer.running is False


def test_flush_with_nothing_pending_does_nothing(debounced):
    target, calls, _timer = debounced
    assert target.flush() is False
    assert calls == []


def test_flush_does_not_write_twice(debounced):
    """A flush followed by a timer that had already been queued would persist
    the same change twice - harmless here, but not everywhere it might be used."""
    target, calls, timer = debounced
    target(7)
    target.flush()
    timer.elapse()
    assert calls == [(7,)]


def test_cancel_drops_a_change_that_is_no_longer_wanted(debounced):
    """Reloading the panel must not fire values queued from before the reload -
    they would be written straight back over what was just loaded."""
    target, calls, timer = debounced
    target(99)
    assert target.cancel() is True
    timer.elapse()
    assert calls == []


def test_pending_reports_honestly(debounced):
    target, _calls, timer = debounced
    assert target.pending is False
    target(1)
    assert target.pending is True
    timer.elapse()
    assert target.pending is False


def test_the_delay_swallows_auto_repeat_but_is_not_a_lag(debounced):
    """Spin-box auto-repeat is roughly ten a second, so the window must exceed
    100ms. Above about half a second, letting go and waiting for "Saved" starts
    to read as the app having missed the click."""
    _target, _calls, timer = debounced
    assert 120 <= timer.interval <= 500
    assert timer.interval == DEFAULT_DELAY_MS


def test_a_negative_delay_is_clamped_rather_than_crashing(monkeypatch):
    monkeypatch.setattr("app.ui.widgets.debounce.QTimer", FakeTimer)
    target = Debounced(lambda: None, delay_ms=-5)
    assert target._timer.interval == 0
