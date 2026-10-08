r"""The files that ran out of time, by type, and the button that reads a type
again with a longer limit.

Layer: L5

Work order 0z, item F3. A timed-out file is settled - an ordinary run does not
read it again until it changes - so without this panel the only ways to get a
large, healthy file read were to raise "Time limit per file" for every file,
for good, or to touch the file. One row per file type, because the limit is by
type; each row's button starts a run that reads that type's timed-out files
and nothing else (`app/index/timed_out_retry.py`).

**It shows what is in the index, not what the last run did**, so a file that
timed out last week is still here after a restart. The rows arrive with the
page's index summary - `tasks.read_index_summary`, on a worker - and
`show_groups` only paints them: no store is read here.

**Every word is in `presenter/timed_out.py`.** This file builds widgets.

The box beside the title is how much longer a retry gives. It is not a saved
setting: it is part of the request, read at the moment a button is pressed.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QGroupBox, QHBoxLayout, QLabel, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from app.ui.presenter.timed_out import (
    FACTOR_DEFAULT, FACTOR_LABEL, FACTOR_MAX, FACTOR_MIN, FACTOR_SUFFIX,
    FACTOR_TOOLTIP, PANEL_TITLE, RETRY_LABEL, RETRY_TOOLTIP, TimedOutRow,
    factor_words, panel_title, retry_accessible_name, timed_out_rows,
)
from app.ui.widgets.buttons import style_button
from app.ui.widgets.no_scroll import protect

__all__ = ["TimedOutPanel", "OBJECT_NAME"]

OBJECT_NAME = "timedOutPanel"


class TimedOutPanel(QGroupBox):
    """One row per file type that timed out. Hidden while there are none."""

    #: `(ext, factor)`: the type as `files.ext` holds it (`""` for files with
    #: no extension) and how many times the usual limit to give.
    retryRequested = Signal(str, float)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(PANEL_TITLE, parent)
        self.setObjectName(OBJECT_NAME)

        self.factor = QSpinBox()
        self.factor.setObjectName("timedOutFactor")
        self.factor.setRange(FACTOR_MIN, FACTOR_MAX)
        self.factor.setValue(FACTOR_DEFAULT)
        self.factor.setSuffix(FACTOR_SUFFIX)
        self.factor.setKeyboardTracking(False)
        self.factor.setToolTip(FACTOR_TOOLTIP)
        self.factor.setAccessibleName(f"{FACTOR_LABEL}: {factor_words(FACTOR_DEFAULT)}")
        self.factor.valueChanged.connect(self._factor_changed)
        # The page scrolls: the wheel must not change this on the way past.
        protect(self.factor)
        label = QLabel(FACTOR_LABEL)
        label.setBuddy(self.factor)

        top = QHBoxLayout()
        top.addWidget(label)
        top.addWidget(self.factor)
        top.addStretch(1)

        self.rows = QVBoxLayout()
        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addLayout(self.rows)

        #: What is drawn now, by type: `ext -> (row, heading label, button)`.
        self._drawn: dict[str, tuple[TimedOutRow, QLabel, QPushButton]] = {}
        self.setVisible(False)

    # -- what is drawn ---------------------------------------------------------

    def button_for(self, ext: str) -> Optional[QPushButton]:
        """The retry button on the row for `ext`, or None when it has no row."""
        drawn = self._drawn.get(ext)
        return drawn[2] if drawn else None

    # -- painting --------------------------------------------------------------

    def show_groups(self, groups: Any) -> None:
        """Paint the store's groups. UI thread, no I/O. `None` changes nothing:
        a summary that could not be read is not evidence that nothing timed out.

        Rebuilt only when the set of types changes; the same types with new
        counts are updated in place, as the skips panel does.
        """
        if groups is None:
            return
        rows = timed_out_rows(groups)
        if rows and [row.ext for row in rows] == list(self._drawn):
            for row in rows:
                _old, heading, button = self._drawn[row.ext]
                heading.setText(self._heading(row))
                heading.setToolTip(row.example)
                button.setAccessibleName(retry_accessible_name(row))
                self._drawn[row.ext] = (row, heading, button)
            self.setTitle(panel_title(rows))
            return

        while self.rows.count():
            item = self.rows.takeAt(0)
            holder = item.widget()
            if holder is not None:
                holder.deleteLater()
        self._drawn = {}
        self.setVisible(bool(rows))
        if not rows:
            self.setTitle(PANEL_TITLE)
            return
        self.setTitle(panel_title(rows))
        for row in rows:
            self._add_row(row)

    @staticmethod
    def _heading(row: TimedOutRow) -> str:
        """"3 PDF files - example.pdf and 2 more", from the presenter's row."""
        example = row.example_text
        return f"{row.heading} - {example}" if example else row.heading

    def _add_row(self, row: TimedOutRow) -> None:
        """One line: the heading and its Retry button, remembered in `_drawn`."""
        heading = QLabel(self._heading(row))
        heading.setObjectName("timedOutHeading")
        heading.setWordWrap(True)
        heading.setToolTip(row.example)

        button = QPushButton(RETRY_LABEL)
        button.setObjectName("timedOutRetry")
        button.setToolTip(RETRY_TOOLTIP)
        button.setAccessibleName(retry_accessible_name(row))
        style_button(button)
        button.clicked.connect(lambda _checked=False, ext=row.ext: self._pressed(ext))

        holder = QWidget()
        line = QHBoxLayout(holder)
        line.setContentsMargins(0, 0, 0, 0)
        line.addWidget(heading, stretch=1)
        line.addWidget(button, alignment=Qt.AlignmentFlag.AlignRight)
        self.rows.addWidget(holder)
        self._drawn[row.ext] = (row, heading, button)

    def _pressed(self, ext: str) -> None:
        """Retry for one type, with the factor the box says right now."""
        self.retryRequested.emit(ext, float(self.factor.value()))

    def _factor_changed(self, value: int) -> None:
        """Keep the factor box's accessible name saying what the number means."""
        self.factor.setAccessibleName(f"{FACTOR_LABEL}: {factor_words(value)}")
