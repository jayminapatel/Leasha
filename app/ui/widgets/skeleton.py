r"""Skeleton rows while a search is still out.

Layer: L5

UI Redesign (202626160950 §6d). A search that takes longer than 300ms used
to leave the list exactly as it was - nothing said "coming". `arm()` starts
a single-shot timer when a search is dispatched; if it fires before results
land and nothing is on screen, four `Skeleton` payloads go into the same
model the real rows use and the same delegate paints them as grey bars.
`disarm()` is called by the view when real rows or an empty message arrive,
which also clears any skeletons that were showing.

Its own module rather than lines in `results_view.py` because that file is
past the 250-line guard already; the view gains two calls and nothing else.
"""

from __future__ import annotations

from typing import Any

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QStandardItem

from app.ui.result_delegate import ROLE_PAYLOAD, Skeleton

__all__ = ["arm", "disarm", "gone", "showing", "SKELETON_DELAY_MS", "SKELETON_ROWS"]

SKELETON_DELAY_MS = 300
SKELETON_ROWS = 4


def _live_timer(view: Any) -> Any:
    """The cached timer, or None when there is none or its C++ side is gone.

    The timer is a child of the view and is cached on it, so anything that
    deletes the view's children - a rebuild, a re-parent - leaves a Python
    wrapper pointing at nothing. A late search answer then reached `disarm`
    and raised `RuntimeError: wrapped C/C++ object of type QTimer has been
    deleted` inside a Qt slot (found by test_adoption_scenarios.py). Probing
    with `isActive()` is the cheapest call that raises when it is gone.
    """
    timer = getattr(view, "_skeleton_timer", None)
    if timer is None:
        return None
    try:
        timer.isActive()
    except RuntimeError:
        view._skeleton_timer = None
        return None
    return timer


def _timer(view: Any) -> QTimer:
    timer = _live_timer(view)
    if timer is None:
        timer = QTimer(view)
        timer.setSingleShot(True)
        timer.setInterval(SKELETON_DELAY_MS)
        timer.timeout.connect(lambda: _show(view))
        view._skeleton_timer = timer
    return timer


def arm(view: Any, *, delay_ms: int = SKELETON_DELAY_MS) -> None:
    """Called at dispatch. Fires only if nothing has answered by then."""
    timer = _timer(view)
    timer.setInterval(delay_ms)
    timer.start()


def disarm(view: Any) -> None:
    """Called when rows (or an empty message) arrive."""
    timer = _live_timer(view)
    if timer is not None:
        timer.stop()
    if showing(view):
        view._model.clear()


def gone(view: Any) -> bool:
    """True when the results view, or the model it paints into, no longer exists.

    A search answer that arrives after the window (or the page's rows) were torn
    down has nowhere to go, and painting into the wreck raised `RuntimeError:
    wrapped C/C++ object ... has been deleted` inside a Qt slot. PyQt6 treats an
    unhandled exception in a slot as fatal unless a hook is installed, so this is
    asked first and the late answer is dropped (found by
    test_adoption_scenarios.py).
    """
    from PyQt6 import sip

    try:
        return bool(sip.isdeleted(view) or sip.isdeleted(getattr(view, "_model", view)))
    except TypeError:
        return False


def showing(view: Any) -> bool:
    model = getattr(view, "_model", None)
    if model is None or model.rowCount() == 0:
        return False
    return isinstance(model.item(0).data(ROLE_PAYLOAD), Skeleton)


def _show(view: Any) -> None:
    model = getattr(view, "_model", None)
    if model is None or model.rowCount() > 0:
        # Something real is on screen - keep it (the view's own rule: never
        # blank a list somebody is reading).
        return
    for n in range(SKELETON_ROWS):
        item = QStandardItem()
        item.setEditable(False)
        item.setEnabled(False)
        item.setSelectable(False)
        item.setData(Skeleton(n), ROLE_PAYLOAD)
        item.setData("Searching…", int(Qt.ItemDataRole.AccessibleTextRole))
        model.appendRow(item)
