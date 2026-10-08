"""The Reports page's list of reports, each with an icon.

2026-10-04, the owner: "there are no icons for the reports on the report
page" - the Indexing and Settings category lists had them (§0.3, icons
wherever possible) and this list, built the same way, never did. The rows are
`REPORTS` in `reports_view.py`; this is only how they are drawn. Its own
widget because `reports_view.py` is at the line guard.

Layer: L5
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QListWidget, QListWidgetItem

from app.ui.widgets.timeline_host import REPORT_KEY

__all__ = ["ReportList", "REPORT_ICONS"]

#: Report key -> Lucide glyph in `assets/icons`. A key not listed gets no
#: icon and the row is otherwise unchanged.
REPORT_ICONS = {
    "inheritance": "users",       # a map for someone who isn't you
    "space": "hard-drive",        # what the copies cost, drive by drive
    "timeline": "calendar",       # a month or a year, in the order it happened
}


class ReportList(QListWidget):
    """One row per report: title, key, description as the tooltip."""

    def __init__(self, reports, parent=None) -> None:
        super().__init__(parent)
        self.setAccessibleName("Available reports")
        self.setIconSize(QSize(16, 16))
        for key, title, description in reports:
            item = QListWidgetItem(title)
            item.setData(REPORT_KEY, key)
            item.setToolTip(description)
            self.addItem(item)

    def retint(self, colours: dict) -> None:
        """Re-render the icons for a palette (called by the window, with the
        category lists' - `CategoryNav.retint`)."""
        from app.ui.widgets.icons import icon

        for row in range(self.count()):
            item = self.item(row)
            name = REPORT_ICONS.get(item.data(REPORT_KEY))
            if name:
                item.setIcon(icon(name, colours.get("text_dim", "#888888")))
