r"""One card: a raised block of related facts on the surface colour, with a hairline edge.

Layer: L5 widget

**Why this exists** (review 2026-10-14, DS-7). Four widgets were cards and each drew itself
its own way: the answer above the search results a `StyledPanel` frame with no rule, the
mail header a hairline under the block, the contact card an inline sheet with its own 8px
corner, and This machine a group box. Four cards, four looks.

`Card` is the one look. It marks itself `card="true"`, and the theme's `*[card="true"]` rule
gives it the surface, the `border` hairline and `radius_box` - the corner a group box has, so a
card beside the Settings groups reads as one of them. The SPACE_M inset is the layout's margin,
`CARD_MARGINS`, set by the card's own layout rather than by the sheet, so the two can never both
add one. The object name stays the subclass's own (`answerCard`, `personCard`...), which is what
tests and `findChild` go by.

`card_title(text)` is a card's heading drawn as a group box's title is: small, dim, semibold.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtWidgets import QFrame, QLabel, QWidget

from app.ui.theme import SPACE_M

__all__ = ["Card", "CARD_MARGINS", "card_title"]

#: left, top, right, bottom: the card's inset, `theme.SPACE_M` all round.
CARD_MARGINS = (SPACE_M, SPACE_M, SPACE_M, SPACE_M)


class Card(QFrame):
    """A `QFrame` the theme draws as a card. Lay it out with `CARD_MARGINS`."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        # The sheet draws the edge; a native frame would draw a second one inside it.
        self.setFrameShape(QFrame.Shape.NoFrame)


def card_title(text: str) -> QLabel:
    """A card's heading, in the group box title's look (`#cardTitle` in the theme)."""
    label = QLabel(text)
    label.setObjectName("cardTitle")
    return label
