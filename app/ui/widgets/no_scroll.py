r"""Stop the mouse wheel changing a setting you were only scrolling past.

Layer: L5

**Qt's default behaviour here is a data-loss bug, not a preference.** A
`QComboBox`, `QSpinBox` or `QTimeEdit` accepts wheel events whether or not it has
focus, so scrolling down a settings page changes every control the pointer
crosses. On a trackpad, where scrolling is continuous and the pointer sits
wherever it happens to be, this is close to guaranteed.

Reported as *"the settings page using a trackpad has issues, it changes
settings"*, which is exactly what it does. The consequences are not cosmetic:
these controls set the memory ceiling, the CPU limit, the worker count and the
index schedule. Scrolling past the page can hand an index run four workers and a
500MB ceiling without a single deliberate click, and nothing announces it.

**The rule: the wheel works when the control has focus, and is passed to the
page when it does not.** Click a spin box and the wheel adjusts it, which is
what somebody deliberately using it expects. Scroll past it and the page scrolls,
which is what somebody scrolling expects. Nobody loses anything.

`Qt.FocusPolicy.StrongFocus` is set alongside, so these controls stop taking
focus merely by being scrolled over - the other half of the same problem.
"""

from __future__ import annotations

from typing import Any, Iterable

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QComboBox,
    QSlider,
    QWidget,
)

__all__ = [
    "WheelGuard",
    "ViewWheelGuard",
    "protect",
    "protect_all",
    "protect_view",
    "SCROLLABLE_TYPES",
]

#: The widget types Qt lets the wheel change. A `QCheckBox` is not among them,
#: which is why the list is short and specific rather than "every widget".
SCROLLABLE_TYPES = (QComboBox, QAbstractSpinBox, QSlider)


class WheelGuard(QObject):
    """Swallows wheel events on an unfocused control and lets the page have them.

    One instance is shared by every guarded widget - an event filter is
    stateless here, and one object per spin box would be a hundred QObjects for
    no reason.
    """

    def eventFilter(self, watched: Any, event: Any) -> bool:   # noqa: N802 - Qt's naming
        if event.type() != QEvent.Type.Wheel:
            return False

        if isinstance(watched, QWidget) and watched.hasFocus():
            # Deliberate use: the person clicked it first. The wheel is a
            # perfectly good way to nudge a number, and taking it away from
            # somebody who asked for it would be its own annoyance.
            return False

        # Not focused, so this is somebody scrolling the page. Ignoring the
        # event lets it propagate to the scroll area, which is where it was
        # always meant to go.
        event.ignore()
        return True


def _has_focus(candidate: Any) -> bool:
    """Does this hold keyboard focus? False for anything that cannot answer.

    Duck-typed rather than `isinstance(x, QWidget)`: an event filter is
    installed on whatever Qt hands it, the question is only ever "does this
    have focus", and asking directly keeps the **rule** testable without a
    shown, activated window - which the offscreen platform will not grant, and
    which has nothing to do with the decision being made.
    """
    getter = getattr(candidate, "hasFocus", None)
    if not callable(getter):
        return False
    try:
        return bool(getter())
    except RuntimeError:
        # The C++ object went away underneath us mid-event. Not focused, and
        # certainly not a reason to take down the wheel handler.
        return False


def _parent_of(candidate: Any) -> Any:
    """The widget's parent, or None when it has gone or cannot answer."""
    getter = getattr(candidate, "parent", None)
    if not callable(getter):
        return None
    try:
        return getter()
    # The C++ object went away mid-event - see `_has_focus`.
    except RuntimeError:
        return None


class ViewWheelGuard(QObject):
    """The same rule for a list or table embedded in a settings page.

    **A scrollable view inside a scrollable page is a trap.** The wheel goes to
    whatever is under the pointer, so scrolling down a settings page stops dead
    the moment the pointer crosses a table: the page freezes and the table
    scrolls instead, having never been clicked. Reported as *"scrolling moves
    from the main page into the list without clicking on it"* - which is exactly
    what it does, and there is no way to scroll past the table at all once the
    pointer is over it.

    Focus is the same signal used for spin boxes, and for the same reason: it is
    the difference between deliberately using a control and merely passing over
    it. Click the table and the wheel scrolls it. Scroll past it and the page
    keeps moving.

    Applied only to views **inside a settings page**, never to a main view where
    scrolling the list is the entire point of the widget.
    """

    def eventFilter(self, watched: Any, event: Any) -> bool:   # noqa: N802 - Qt's naming
        if event.type() != QEvent.Type.Wheel:
            return False

        # The filter is installed on the **viewport**, because that is where Qt
        # delivers wheel events - and a viewport never holds focus itself, so
        # the view behind it is the one worth asking.
        if _has_focus(watched) or _has_focus(_parent_of(watched)):
            return False

        event.ignore()
        return True


#: Shared, and kept alive for the life of the process. An event filter that is
#: garbage collected stops filtering, silently - the bug would come back and
#: look intermittent.
_GUARD = WheelGuard()
_VIEW_GUARD = ViewWheelGuard()


def protect(widget: QWidget) -> QWidget:
    """Guard one control. Returns it, so it can be used inline."""
    widget.installEventFilter(_GUARD)
    # Without this the control takes focus on a wheel event *and then* responds
    # to it, which defeats the filter on the second notch of a scroll.
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    return widget


def protect_view(view: QAbstractItemView) -> QAbstractItemView:
    """Let a settings-page table scroll only once it has been clicked.

    Filters the **viewport** rather than the view: wheel events are delivered to
    the viewport, and a filter on the view itself never sees them.

    `StrongFocus` matters as much as the filter. With `WheelFocus` the view takes
    focus on the first notch and then legitimately consumes every notch after it,
    so the guard appears to work once and then stop - which reads as flakiness
    rather than as a policy.
    """
    view.viewport().installEventFilter(_VIEW_GUARD)
    view.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    return view


def protect_all(root: QWidget, *, types: Iterable[type] = SCROLLABLE_TYPES) -> int:
    """Guard every scroll-sensitive control under `root`. Returns how many.

    Applied to a whole page rather than control by control, because the failure
    mode of forgetting one is a setting that changes silently - and the one
    somebody forgets will be the one that matters. A test asserts every page has
    been through this.
    """
    guarded = 0
    for widget_type in types:
        for widget in root.findChildren(widget_type):
            protect(widget)
            guarded += 1
    return guarded
