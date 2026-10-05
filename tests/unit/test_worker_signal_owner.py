r"""A worker's answer dies with the widget it was going to paint into.

Layer: L5.

**The bug class, and it is the one `test_later.py` already pins, arriving the other way.**
`QTimer.singleShot(ms, lambda ...)` has no receiver; neither does
`worker.signals.finished.connect(lambda ...)`. The worker lands whenever the work is done -
a cold ANN probe, an Ollama caption, a git read on a big repository - and the lambda runs
then, whatever happened in between. If what it touches is a Qt object whose C++ half has
since been deleted, PyQt raises

    RuntimeError: wrapped C/C++ object of type QLabel has been deleted

*inside a Qt slot*, where nothing catches it: `workers._emit`'s own `except RuntimeError`
cannot help, because the connection is queued (measured below) and the slot therefore runs
on a later turn of the event loop, long after `emit` returned on the worker thread.

**Which sites this actually bites is the part worth measuring**, because the answer is not
"every lambda". Three facts, all checked here rather than asserted:

1. A lambda **captures** what it names, so it holds a Python reference. A widget Python
   alone owns - a pop-out `PreviewWindow` kept in `MainWindow._pinned`, the mini palette -
   cannot be collected while the connection exists. The lambda is what keeps it alive.
2. A Python reference does **not** keep the C++ half alive when Qt owns it. A child widget
   `deleteLater()`d by a rebuild loop, or one whose parent goes, leaves a live Python
   wrapper over a dead C++ object - and *that* is what raises.
3. A bound method of a QObject is dropped by Qt when that QObject dies, across threads and
   through a queued connection, exactly as `test_later.py` measures for `singleShot`.

So the sites that were changed are the ones whose receiver is a Qt-parented object
something deletes on its own schedule. The sites left alone are the ones whose receiver is
a long-lived view, controller or window - and they are left alone with a reason, recorded
in `test_the_long_lived_receivers_really_are_long_lived`, not by having been missed.
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, QThreadPool, QTimer                    # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QWidget                # noqa: E402

from app.ui.later import when_done                                       # noqa: E402
from app.ui.workers import CallableWorker, run                           # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def escapes(monkeypatch):
    """Every exception PyQt lets escape a slot, collected rather than printed.

    This is the whole point of the test: the failure is not an exception the caller can
    catch, it is one that escapes into `sys.excepthook` from inside Qt's own delivery.
    Asserting on a `pytest.raises` here would prove nothing, because there is no frame of
    ours for it to propagate through.
    """
    import sys

    seen: list[str] = []
    monkeypatch.setattr(sys, "excepthook",
                        lambda kind, value, tb: seen.append(f"{kind.__name__}: {value}"))
    return seen


def _pump(app, ms: int = 700) -> None:
    QTimer.singleShot(ms, app.quit)
    app.exec()


def _slow(tag: str) -> str:
    """Long enough that the widget is deleted while the worker is still in flight."""
    time.sleep(0.15)
    return tag


class _Chip(QWidget):
    """A widget with a child, so a method of it really does touch the C++ side."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.label = QLabel(self)
        self.painted: list[str] = []

    def paint(self, text: str) -> None:
        self.painted.append(str(text))
        self.label.setText(str(text))            # the line that needs a live C++ object


# ---------------------------------------------------------------------------
# the measurements the fix rests on
# ---------------------------------------------------------------------------

def test_a_bare_lambda_paints_into_a_deleted_child_and_raises(app, escapes):
    """Failing-first: this is the crash, reproduced rather than described."""
    page = QWidget()
    chip = _Chip(page)
    worker = CallableWorker(_slow, "answer")
    worker.signals.finished.connect(lambda text, c=chip: c.paint(text))
    run(QThreadPool.globalInstance(), worker)

    chip.deleteLater()                  # exactly what a rebuild loop does
    _pump(app)

    assert chip.painted == ["answer"], (
        "the slot did not run at all - if PyQt has started cancelling bare lambdas too, "
        "`when_done` can go and this module's reasoning needs rewriting")
    # PyQt6 says "has been deleted", PySide6 "already deleted": the same crash.
    assert any("has been deleted" in line or "already deleted" in line for line in escapes), (
        "painting into a deleted widget no longer raises; check what changed before "
        "trusting the rest of this module")


def test_when_done_drops_the_call_instead(app, escapes):
    """The fix: same worker, same deletion, nothing runs and nothing raises."""
    page = QWidget()
    chip = _Chip(page)
    worker = CallableWorker(_slow, "answer")
    when_done(chip, worker, finished=lambda text: chip.paint(text))
    run(QThreadPool.globalInstance(), worker)

    chip.deleteLater()
    _pump(app)

    assert chip.painted == [], "the callback ran after its owner was destroyed - the crash"
    assert not escapes, f"something escaped a slot: {escapes}"


def test_when_done_still_delivers_to_a_living_owner(app, escapes):
    """A guard that cancels everything would pass the test above and break the app."""
    page = QWidget()
    chip = _Chip(page)
    worker = CallableWorker(_slow, "answer")
    when_done(chip, worker, finished=chip.paint)
    run(QThreadPool.globalInstance(), worker)
    _pump(app)

    assert chip.painted == ["answer"], "the ordinary case must still happen"
    assert not escapes, f"something escaped a slot: {escapes}"


def test_when_done_routes_failures_and_done_too(app):
    page = QWidget()
    owner = _Chip(page)
    seen: list[str] = []

    def boom() -> None:
        raise ValueError("no")

    worker = CallableWorker(boom, component="test")
    when_done(owner, worker,
              failed=lambda error: seen.append(f"failed:{error.code}"),
              done=lambda: seen.append("done"))
    run(QThreadPool.globalInstance(), worker)
    _pump(app)

    assert [line.split(":")[0] for line in seen] == ["failed", "done"], seen


def test_the_relay_does_not_outlive_the_work(app):
    """One relay per worker, gone when the worker is - not one per thumbnail for ever."""
    page = QWidget()
    owner = _Chip(page)
    before = len(owner.findChildren(QObject))

    for _ in range(5):
        worker = CallableWorker(_slow, "x")
        when_done(owner, worker, finished=lambda _text: None)
        run(QThreadPool.globalInstance(), worker)
    _pump(app)
    # deleteLater needs one more turn of the loop than the `done` that queued it.
    _pump(app, 60)

    after = len(owner.findChildren(QObject))
    assert after == before, (
        f"{after - before} relay(s) left behind; a grid that decodes one worker per "
        "thumbnail would grow a child QObject per thumbnail for the view's whole life")


def test_a_lambda_capture_keeps_a_python_owned_widget_alive(app, escapes):
    """Fact 1, measured - this is why the pop-out windows were left alone.

    `PreviewWindow` has no parent; `MainWindow._pinned` and the lightbox's `open_windows`
    are its only references, and both drop it when it closes. It would be the obvious
    candidate for this bug - except that the connection's own lambda holds a reference
    too, so nothing collects it while the worker is in flight.
    """
    import gc

    holder = {"chip": _Chip()}                   # no parent: Python owns the C++ object
    chip = holder["chip"]
    worker = CallableWorker(_slow, "answer")
    worker.signals.finished.connect(lambda text: chip.paint(text))
    run(QThreadPool.globalInstance(), worker)

    holder.clear()                               # the list-of-windows reference goes
    gc.collect()
    _pump(app)

    assert chip.painted == ["answer"], "the widget was collected despite the capture"
    assert not escapes, (
        "an unparented widget was destroyed while a worker was in flight - the reasoning "
        "that left the pop-out windows alone no longer holds")


def test_the_delivery_really_is_queued(app):
    """Why `workers._emit`'s `except RuntimeError` cannot cover the receiving side.

    It wraps `emit`, on the worker thread. If delivery were direct the slot would run
    inside that `try` and every one of these would already be caught. It is not.
    """
    import threading

    seen: list[int] = []
    main = threading.get_ident()
    worker = CallableWorker(_slow, "x")
    worker.signals.finished.connect(lambda _v: seen.append(threading.get_ident()))
    run(QThreadPool.globalInstance(), worker)
    _pump(app)

    assert seen == [main], (
        "the slot ran on the worker thread, inside `_emit`'s try/except - if that is now "
        "true, the receiving side is already covered and this module can be reduced")


# ---------------------------------------------------------------------------
# the triage: what was deliberately left alone
# ---------------------------------------------------------------------------

def test_the_long_lived_receivers_really_are_long_lived():
    r"""The sites left as bare lambdas, and the property that makes that safe.

    Most of the hundred-odd `signals.*.connect(lambda ...)` sites hand the answer to a
    method of `self`, where `self` is a view, a controller or the window: built once in
    `MainWindow.__init__` (or its deferred page construction) and alive until the process
    ends. `MainWindow.closeEvent` calls `_drain_workers`, which pumps events while the
    pools empty, so anything still in flight is delivered *before* teardown rather than
    after it - and nothing is destroyed until `exec()` has already returned.

    Rewriting those would be churn with no defect behind it, which this project's
    "working version first" rule is explicit about. This test pins the premise instead:
    if the window ever starts deleting its views while it runs, this goes red and the
    reasoning above gets revisited rather than quietly becoming false.
    """
    from pathlib import Path

    shell = (Path(__file__).resolve().parents[2] / "app" / "ui" / "shell.py").read_text(
        encoding="utf-8")
    assert "stage(\"workers\", self._drain_workers)" in shell, (
        "closeEvent no longer drains the worker pools before tearing down; the reason "
        "the view-owned lambdas are safe has gone with it")
    assert "def _drain_workers" in shell and "processEvents" in shell, (
        "_drain_workers no longer pumps events while waiting, so a worker that lands "
        "during close is no longer delivered before teardown")


# ---------------------------------------------------------------------------
# the opt-in teardown that the leak investigation produced
# ---------------------------------------------------------------------------

def _sweep(app, before: dict) -> None:
    """What `no_leaked_widgets` does, so a test can check it rather than describe it."""
    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    for widget in list(QApplication.topLevelWidgets()):
        if id(widget) in before:
            continue
        widget.close()
        widget.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_processevents_alone_does_not_free_anything(app):
    r"""The trap the obvious teardown falls into, pinned.

    `deleteLater()` posts a `DeferredDelete`, and Qt holds those until the event loop
    *that posted them* returns - `processEvents()` does not deliver them. So the
    `deleteLater(); processEvents()` teardown everyone writes first frees nothing while
    looking exactly as though it did, and a fixture built on it is a no-op nobody
    notices. This is why `no_leaked_widgets` calls `sendPostedEvents` explicitly.
    """
    from app.ui import qtsip as sip

    widget = QWidget()
    widget.show()
    widget.close()
    widget.deleteLater()
    for _ in range(3):
        app.processEvents()

    assert not sip.isdeleted(widget), (
        "processEvents now delivers DeferredDelete; if that is really true, "
        "`no_leaked_widgets` can drop its sendPostedEvents call")

    from PySide6.QtCore import QEvent
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    assert sip.isdeleted(widget), "sendPostedEvents did not deliver the deferred delete"


def test_the_opt_in_teardown_deletes_what_the_test_built(app):
    """`no_leaked_widgets` is only worth having if it really frees the C++ objects."""
    from app.ui import qtsip as sip
    from PySide6.QtWidgets import QApplication

    # The widgets themselves, not only their ids - see `no_leaked_widgets`.
    before = {id(w): w for w in QApplication.topLevelWidgets()}
    made = [QWidget() for _ in range(3)]
    for widget in made:
        widget.show()

    _sweep(app, before)

    assert all(sip.isdeleted(widget) for widget in made), (
        "the sweep did not free the widgets, so the fixture is a no-op that looks "
        "like it works")


def test_the_teardown_keeps_widgets_it_did_not_create(app):
    """The property that makes it safe beside a module-scoped window fixture.

    A widget that already existed when the fixture took its snapshot must survive, or
    adopting it in a module with `gui_mainwindow` would delete the window mid-module.
    """
    from app.ui import qtsip as sip
    from PySide6.QtWidgets import QApplication

    # Everything alive before this test, so the clean-up at the end removes
    # only what the test built.
    outside = {id(w): w for w in QApplication.topLevelWidgets()}
    survivor = QWidget()
    survivor.show()
    # The widgets themselves, not only their ids - see `no_leaked_widgets`.
    before = {id(w): w for w in QApplication.topLevelWidgets()}

    newcomer = QWidget()
    newcomer.show()
    _sweep(app, before)

    assert not sip.isdeleted(survivor), (
        "a widget that predated the snapshot was deleted - a module-scoped MainWindow "
        "would go the same way, mid-module")
    assert sip.isdeleted(newcomer), "the widget the test created was not cleaned up"
    # Only `survivor` - not every top-level widget in the process. Sweeping with
    # an empty snapshot deleted the module-scoped MainWindows of earlier files
    # too, which is the crash `gui_mainwindow` warns about (exit -11 in the full
    # suite, in this very `_sweep`).
    _sweep(app, outside)


def test_the_fixture_itself_runs_and_cleans_up(no_leaked_widgets):
    """The fixture is requested here so its teardown really executes at least once."""
    from PySide6.QtWidgets import QApplication

    widget = QWidget()
    widget.show()
    assert widget in QApplication.topLevelWidgets()
