"""A field is as wide as what it holds, not as wide as the window.

Layer: L5

2026-10-05, the UI review. On Settings and Indexing every drop-down and number
field stretched across the page: "30 days" in a box a thousand pixels wide,
"Only when I ask" in another. A wide box says "type something long here", and
it put the value a long way from the label it belongs to.

One pass over a finished page, the way `no_scroll.protect_all` guards the
wheel: doing it per control would mean the one somebody forgets is the one
that still spans the window, and a new control gets it for free.

Text boxes are left alone - a folder path or a model name really is long.
"""

from __future__ import annotations

from PyQt6.QtWidgets import QAbstractSpinBox, QComboBox, QSizePolicy, QWidget

__all__ = ["fit_fields", "COMBO_MIN_PX", "NUMBER_MIN_PX"]

#: The least a drop-down is drawn at, so a column of short ones lines up.
COMBO_MIN_PX = 220
#: The least a number field is drawn at: its value, its unit and its reset button.
NUMBER_MIN_PX = 150


def fit_fields(root: QWidget) -> int:
    """Size every drop-down and number field under `root` to its contents.

    Returns how many were sized. Safe to call twice.
    """
    # The size policy alone does it: a form grows every field that is allowed
    # to grow, and `_hold` says these are not. The form's own growth rule is
    # left as it was - changing it also narrowed the wrapped hint lines.
    sized = 0
    for combo in root.findChildren(QComboBox):
        if combo.isEditable():
            continue                              # typed into: it is a text box
        # Follows its longest entry, including entries added after it is shown.
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        _hold(combo, COMBO_MIN_PX)
        sized += 1
    for field in root.findChildren(QAbstractSpinBox):
        _hold(field, NUMBER_MIN_PX)
        sized += 1
    return sized


def _hold(widget: QWidget, least: int) -> None:
    """No wider than its own size hint, and no narrower than `least`."""
    policy = widget.sizePolicy()
    policy.setHorizontalPolicy(QSizePolicy.Policy.Maximum)
    widget.setSizePolicy(policy)
    widget.setMinimumWidth(max(widget.minimumWidth(), least))
