r"""One row of the "N files skipped — review" panel.

Layer: L5

Split out of `indexing_view.py` for the 250-line guard.

**The panel is not an error list.** A 100GB run skips files in the thousands and
that is ordinary - a scanned PDF holds text as pixels, a `.pst` is open in
Outlook, a drive went away. Listing them individually would be a wall nobody
reads. One row per reason, with the count, the fix, and a retry button *only*
when retrying could change the answer: offering to retry four thousand scanned
PDFs would do nothing at all, which is worse than not offering.

`update_count` exists because this row used to be destroyed and rebuilt on every
progress tick, for the hours a large index takes. The reason, its fix and
whether it can be retried are all fixed by the error code; only the number
moves, so only the number is redrawn.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QLabel, QPushButton, QVBoxLayout, QWidget

from app.ui.presenter import format_count
from app.ui.widgets.buttons import style_button

__all__ = ["SkipRow"]


class SkipRow(QWidget):
    """One reason, its count, its fix, and a retry button only when one helps."""

    def __init__(
        self, group: Any, retry_signal: Any, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self._group = group

        self._heading = QLabel(self._heading_text(group))
        self._heading.setWordWrap(True)
        self._heading.setObjectName("skipHeading")

        fix = QLabel(group.suggestion)
        fix.setWordWrap(True)
        fix.setObjectName("skipFix")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.addWidget(self._heading)
        layout.addWidget(fix)

        if group.examples:
            examples = QLabel("e.g. " + ", ".join(group.examples))
            examples.setWordWrap(True)
            examples.setObjectName("skipExamples")
            layout.addWidget(examples)

        if group.retryable:
            button = QPushButton("Retry these")
            button.setToolTip(
                "Index these files again. Worth it when the cause has gone - a "
                "file that was locked, a converter since installed - and "
                "harmless when it has not.")
            # Its own width (the button system, buttons.py) rather than a
            # 140px ceiling, which would cut the words off at a larger font.
            style_button(button)
            button.setAccessibleName(f"Retry the files skipped because {group.message}")
            button.clicked.connect(lambda: retry_signal.emit(group.code))
            layout.addWidget(button, alignment=Qt.AlignmentFlag.AlignLeft)

    @staticmethod
    def _heading_text(group: Any) -> str:
        return f"{format_count(group.count)} × {group.message}"

    def update_count(self, group: Any) -> None:
        """Change the number without rebuilding the row."""
        self._group = group
        self._heading.setText(self._heading_text(group))
