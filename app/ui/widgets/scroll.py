"""Make a view scroll, once, in a way the next view cannot forget.

Layer: L5

A window has two failure modes and this fixes both. **Too small**, and controls
at the bottom are simply unreachable - Qt shrinks widgets below their sensible
size until the layout is unusable, and there is no scrollbar because nothing
asked for one. **Maximised**, and a form laid out for 760px of height stretches
its spacers into acres of grey while the content it was meant to show sits in a
band at the top.

The fix is a `QScrollArea`, and the reason it lives here rather than inside each
view is that it was applied to exactly one of five tabs. A helper called at the
point tabs are added is applied to all of them by construction, and to the sixth
one somebody adds next year.

**`setWidgetResizable(True)` is the whole trick, and the one everybody misses.**
Without it the scroll area gives the inner widget its `sizeHint()` and never
resizes it, so the view stays 400px wide inside a maximised window with a
horizontal scrollbar under it. With it, the inner widget tracks the viewport's
width and scrolls only vertically, which is what a settings page should do.

The horizontal bar is off for the same reason: a form that scrolls sideways is a
form with a layout bug, and hiding the bar makes that visible during development
instead of papering over it in front of a user.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QScrollArea, QWidget

__all__ = ["scrollable", "MIN_CONTENT_WIDTH"]

#: Below this the layout is squeezed rather than merely small, so the scroll
#: area starts scrolling horizontally instead of crushing the content. Chosen to
#: fit the widest form row - a folder path next to two buttons - without wrapping.
MIN_CONTENT_WIDTH = 520


def scrollable(
    view: QWidget,
    *,
    min_width: int = MIN_CONTENT_WIDTH,
    horizontal: bool = False,
) -> QScrollArea:
    """Wrap `view` so it scrolls vertically and fills the width it is given.

    Returns the `QScrollArea`, which is what goes into the tab widget. The view
    itself is unchanged and every signal on it still works, so callers keep
    holding the original: `self.settings_view` stays a `SettingsView`, and only
    what is *added to the tab bar* is the wrapper.

    Views that manage their own scrolling internally - anything built around a
    `QTableWidget`, `QListWidget` or `QSplitter` that already fills the window -
    should not be wrapped. Two nested scroll areas fight over the wheel event and
    the outer one usually wins, which feels broken in a way that is very hard to
    describe in a bug report.
    """
    area = QScrollArea()
    area.setWidget(view)

    # Without this the inner widget keeps its sizeHint forever and the window
    # gets a horizontal scrollbar the moment it is maximised.
    area.setWidgetResizable(True)

    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(
        Qt.ScrollBarPolicy.ScrollBarAsNeeded if horizontal
        else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)

    # A floor, not a fixed size. The view still stretches to fill a maximised
    # window; it just stops being compressed past the point of usefulness, and
    # the scrollbar appears instead.
    view.setMinimumWidth(min_width)

    # So the scroll area itself never forces the window wider than the screen.
    area.setMinimumWidth(0)
    return area


def wrap_if_needed(view: QWidget, *, scroll: bool) -> QWidget:
    """`scrollable(view)` when `scroll`, otherwise `view` untouched.

    Exists so the tab table in `shell.py` can be one list of
    `(view, title, scroll)` rows rather than a branch per tab - the decision is
    then visible in one place, next to the tab it applies to.
    """
    return scrollable(view) if scroll else view
