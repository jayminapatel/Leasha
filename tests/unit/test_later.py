r"""A deferred call dies with the thing that asked for it.

Layer: L5.

**The bug class.** `QTimer.singleShot(ms, callback)` has no owner. Give it a *lambda*
and Qt has no receiver to disconnect, so it fires however long it waits and whatever
happened meanwhile - including into a widget whose C++ half is gone. The application has
already had one of these: a late search answer painted into a destroyed results view,
raising inside a Qt slot where nothing catches it.

A *bound method* of a QObject is different, and the difference is worth knowing rather
than memorising: it gives Qt a receiver, and Qt drops the connection when that object
dies. `test_the_two_forms_of_single_shot_really_do_differ` measures both, so the claim
in `app/ui/later.py`'s docstring is checked rather than asserted - and so that if a
future PyQt changes it, this says so instead of the rule quietly becoming folklore.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("PyQt6")

from PyQt6 import sip                                                    # noqa: E402
from PyQt6.QtCore import QCoreApplication, QObject, QTimer               # noqa: E402

from app.ui.later import later                                           # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def app():
    return QCoreApplication.instance() or QCoreApplication([])


def _pump(app, ms: int = 80, until=None) -> None:
    """Turn the event loop for `ms`, or until `until()` is true if that is sooner.

    **By the clock, not by `app.exec()` and a timer that quits it** (2026-10-05).
    That form ended whenever *anything* asked the application to quit - a quit
    left queued by an earlier test file in the same process ends the loop at
    once - and it gave a 5 ms timer exactly one fixed window to fire in.
    `test_it_runs_when_the_owner_is_still_alive` failed that way in three of
    one day's whole-suite runs (`assert [] == [True]`) and never alone. Which
    of the two it was is not established; this form is immune to both.
    """
    import time

    end = time.monotonic() + ms / 1000.0
    while time.monotonic() < end:
        app.processEvents()
        if until is not None and until():
            return
        time.sleep(0.002)


class _Owner(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.rang = False

    def ring(self) -> None:
        self.rang = True


def test_it_runs_when_the_owner_is_still_alive(app):
    owner = _Owner()
    fired = []
    later(owner, 5, lambda: fired.append(True))
    # Up to five seconds, and no longer than it takes: a busy machine may be
    # late with a 5 ms timer, and "late" is not what this test is about.
    _pump(app, 5000, until=lambda: bool(fired))
    assert fired == [True], "a deferred call must still happen in the ordinary case"


def test_it_does_not_run_when_the_owner_has_gone(app):
    """The whole point: no callback against a destroyed widget."""
    owner = _Owner()
    fired = []
    later(owner, 5, lambda: fired.append(True))
    sip.delete(owner)                       # the widget is torn down before it fires
    _pump(app)
    assert fired == [], "the callback ran after its owner was destroyed - this is the crash"


def test_the_two_forms_of_single_shot_really_do_differ(app):
    """Measured, because `later` exists only if this is true.

    If a future PyQt makes a bare lambda safe too, this fails and `later` can go.
    """
    kept, lost = _Owner(), _Owner()
    fired = []
    QTimer.singleShot(5, kept.ring)                     # bound method: has a receiver
    QTimer.singleShot(5, lambda: fired.append("lambda"))  # lambda: has none
    sip.delete(kept)
    sip.delete(lost)
    _pump(app)
    assert fired == ["lambda"], "a bare single-shot lambda no longer fires after a delete"


def test_a_zero_delay_still_defers(app):
    """`later(owner, 0, ...)` must behave like `singleShot(0, ...)`: next turn of the
    loop, not now - the deferred page construction depends on it."""
    owner = _Owner()
    order = []
    later(owner, 0, lambda: order.append("deferred"))
    order.append("immediately")
    _pump(app)
    assert order == ["immediately", "deferred"]


# ---------------------------------------------------------------------------
# the guard
# ---------------------------------------------------------------------------

#: `QTimer.singleShot(<anything>, lambda ...)` - a deferred call with no owner.
ORPHAN = re.compile(r"singleShot\s*\([^)]*\blambda\b")


def _sources():
    for path in (ROOT / "app").rglob("*.py"):
        if "__pycache__" in path.parts or path.name == "later.py":
            continue
        yield path


def test_no_deferred_lambda_in_the_app_is_left_without_an_owner():
    offenders = []
    for path in _sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if ORPHAN.search(line):
                offenders.append(f"{path.relative_to(ROOT).as_posix()}:{number}: {line.strip()}")
    assert not offenders, (
        "a single-shot lambda with no owner fires even if its widget has been destroyed. "
        "Use `later(self, ms, ...)` from app/ui/later.py, or a bound method - see this "
        "module's docstring:\n  " + "\n  ".join(offenders))


def test_the_guard_would_catch_the_shapes_that_were_found():
    for line in ("QTimer.singleShot(0, lambda: self._build(store))",
                 "QTimer.singleShot(STOP_GRACE_MS, lambda a=ask: self._release(a))",
                 "    QTimer.singleShot(0, lambda p=path: self._start_decode(p))"):
        assert ORPHAN.search(line), line
    for line in ("QTimer.singleShot(0, self._hide_to_tray)",
                 "later(self, 0, lambda: self._build(store))"):
        assert not ORPHAN.search(line), f"false positive on {line}"
