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

from PyQt6.QtCore import QEvent, QObject, Qt
from PyQt6.QtWidgets import QAbstractSpinBox, QComboBox, QSlider, QWidget

__all__ = ["WheelGuard", "protect", "protect_all", "SCROLLABLE_TYPES"]

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


#: Shared, and kept alive for the life of the process. An event filter that is
#: garbage collected stops filtering, silently - the bug would come back and
#: look intermittent.
_GUARD = WheelGuard()


def protect(widget: QWidget) -> QWidget:
    """Guard one control. Returns it, so it can be used inline."""
    widget.installEventFilter(_GUARD)
    # Without this the control takes focus on a wheel event *and then* responds
    # to it, which defeats the filter on the second notch of a scroll.
    widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    return widget


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
