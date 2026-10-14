r"""Focus that shows when the keyboard put it there: Qt's nearest thing to `:focus-visible`.

Layer: L5 widget

**Why** (review 2026-10-14, DS-4). Every button and tab has a 2px focus ring now, and a ring
that appears on whatever was just clicked reads as a fault rather than as help: it is for
somebody moving with Tab, who otherwise loses their place. Browsers solve this with
`:focus-visible`; Qt stylesheets have no such pseudo-state.

**What this does instead.** One application-wide event filter watches focus arriving at a
button or a tab bar. When the focus came from the mouse it sets the dynamic property
`pointerFocus` to true on that widget (false for Tab, a shortcut, or the window opening), and
re-polishes it so the sheet sees the change. The theme's `[pointerFocus="true"]:focus` rules
then draw the ring in the control's resting edge colour - the width and padding stay, so a
click never moves a word. A widget that never received a mouse focus has no property, so the
ring shows: a window without this filter (a test, a tool run on its own) is ringed, never
silently unringed.

Text boxes, drop-downs and lists are not touched - they keep the ring however focus arrived,
as a browser does for a text field: the caret alone is easy to lose.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QAbstractButton, QTabBar

__all__ = ["FocusVisible", "install", "PROPERTY"]

#: The dynamic property the theme's rules read.
PROPERTY = "pointerFocus"

_installed: Optional["FocusVisible"] = None


class FocusVisible(QObject):
    """Sets `pointerFocus` on a button or tab bar from the reason it took the focus."""

    def eventFilter(self, watched: Any, event: Any) -> bool:            # noqa: N802 - Qt's name
        if event.type() == QEvent.Type.FocusIn and isinstance(watched, (QAbstractButton, QTabBar)):
            mark(watched, event.reason() == Qt.FocusReason.MouseFocusReason)
        return False


def mark(widget: Any, pointer: bool) -> None:
    """Record how `widget` got the focus, and re-polish it only when that changed."""
    if bool(widget.property(PROPERTY)) == pointer and widget.property(PROPERTY) is not None:
        return
    widget.setProperty(PROPERTY, pointer)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)
    widget.update()


def install(app: Any) -> FocusVisible:
    """Watch `app` once. Safe to call again; returns the one filter."""
    global _installed
    if _installed is None:
        _installed = FocusVisible(app)
        app.installEventFilter(_installed)
    return _installed
