"""What could not be read, grouped by cause.

Layer: L6 (UI)

**Not an error log.** On 100GB this is the answer to "what is missing from my
search results, and why?" - a question no search box can answer and most search
tools never acknowledge. Four thousand scanned PDFs is one row saying "4,000
files hold text as images", with the fix beside it, not four thousand lines
nobody reads.

Lifted out of `indexing_view.py` unchanged, because that view had reached the
250-line guard and the guard is right: a view that keeps growing is a view where
logic starts to live. The rebuilding rules below are the whole substance of this
widget and they are worth reading before changing it.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtWidgets import QGroupBox, QScrollArea, QVBoxLayout, QWidget

from app.ui.presenter import format_count, group_skips
from app.ui.widgets.skip_row import SkipRow

__all__ = ["SkipsPanel"]


class SkipsPanel(QScrollArea):
    """A scrolling box of skip reasons, rebuilt as rarely as possible."""

    def __init__(self, retry_signal: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._retry = retry_signal
        self.box = QGroupBox("Skipped files")
        self.rows = QVBoxLayout(self.box)
        self.box.setVisible(False)

        self.setWidgetResizable(True)
        self.setWidget(self.box)
        # Only claims layout space once it has content. Left permanently
        # visible it swallowed the whole window when maximised.
        self.setVisible(False)

        self._shown: dict[str, int] = {}
        self._widgets: dict[str, Any] = {}
        #: Code -> the detail lines last given for it, kept so a rebuild of
        #: the rows does not lose them.
        self._details: dict[str, list] = {}

    def show_skips(self, summary: dict[str, int]) -> None:
        """Update the panel, rebuilding it only when it has to.

        **This ran on every progress tick, for hours.** A 100GB index reports
        progress constantly, and each report destroyed every `SkipRow` - three
        or four labels and a button apiece - and built them again, to show
        numbers that had usually not changed. Widget churn at that rate is the
        kind of cost that never appears in a profile of one operation and
        dominates an afternoon.

        Two cheap checks in order: an identical tally does nothing at all, and a
        tally with the same reasons but different counts updates the labels in
        place. Only a genuinely new reason rebuilds, which happens a handful of
        times in a run.
        """
        summary = summary or {}
        if summary == self._shown:
            return

        groups = group_skips(summary)
        if {g.code for g in groups} == set(self._widgets) and groups:
            for group in groups:                    # same reasons, new numbers
                self._widgets[group.code].update_count(group)
            self._retitle(groups)
            self._shown = dict(summary)
            return

        while self.rows.count():
            item = self.rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._widgets.clear()
        self._shown = dict(summary)
        self.box.setVisible(bool(groups))
        self.setVisible(bool(groups))
        if not groups:
            return

        self._retitle(groups)
        for group in groups:
            row = SkipRow(group, self._retry)
            row.show_details(self._details.get(group.code))
            self._widgets[group.code] = row
            self.rows.addWidget(row)
        self.rows.addStretch(1)

    def show_details(self, code: str, rows: Any) -> None:
        """The files skipped for `code` and what was recorded for each
        (`SqliteStore.skip_details`), drawn under that reason. 2026-10-05: the
        unexpected-error sentence says "the detail below", and there was none."""
        lines = []
        for row in rows or ():
            name = str(row.get("path", "")).replace("\\", "/").rsplit("/", 1)[-1]
            lines.append(f"{name}: {row.get('detail', '')}".strip())
        self._details[code] = lines
        widget = self._widgets.get(code)
        if widget is not None:
            widget.show_details(lines)

    def _retitle(self, groups) -> None:
        total = sum(group.count for group in groups)
        self.box.setTitle(f"{format_count(total)} files skipped — review")
