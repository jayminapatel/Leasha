"""The line under the Search box: how many documents, in plain words.

Layer: L5

UI phase B (owner, 2026-10-13). The line used to be built once from the search's
answer and say "37 result(s) · 212ms · ...": passages counted as results, and the
machinery read aloud. The words are `presenter.status_line`'s now; what this label
adds is the one thing the presenter cannot know - **how many rows the list really
drew**. Folding a mail with its attachments makes three files one row, and that
fold happens when the mail details land a moment after the rows, so the count is
taken again whenever the list redraws (`recount`).

A `QLabel` underneath, so every caller that sets plain text on it - the history
pass, the two-source summary, a failed search - still works, and stops the
recount: a line that is saying something else must not be overwritten by a count.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtWidgets import QLabel, QWidget

from app.ui.presenter import status_details, status_line

__all__ = ["StatusLine"]


class StatusLine(QLabel):
    """`QLabel` plus `counted` (a search's line) and `recount` (the list redrew)."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__("", parent)
        self.setObjectName("searchStatus")
        self._response: Any = None

    def setText(self, text: str) -> None:                    # noqa: N802 - Qt's name
        """Any other sentence: shown as given, and no longer recounted."""
        self._response = None
        self.setToolTip("")
        super().setText(text)

    def counted(self, response: Any, text: str) -> None:
        """A search's line, with its timing and caveats in the tooltip.

        Recounted later only when `text` *is* the count - a notice-like sentence
        in its place (a word nobody's files contain) is left alone.
        """
        super().setText(text)
        self.setToolTip(status_details(response))
        self._response = response if text == status_line(response) else None

    def recount(self, shown: int) -> None:
        """The list drew `shown` rows: say that number, if the line is a count."""
        if self._response is not None and shown:
            super().setText(status_line(self._response, shown=shown))
