"""The timeline picker is as tall as its wrapped lines, whatever is below it.

Layer: L5 (pytest-qt)

2026-10-15, CI on `main`: with the runner's wider default font the picker's controls wrapped
to one line more, and in a short window the page gave the list below the room - the picker
kept the height of fewer lines and its last line (Show) was drawn outside it
(`test_ui_review_0x9::test_the_timeline_controls_wrap_instead_of_being_cut_or_piled_up`).
A flowing layout's height depends on its width, which a parent layout only honours when
there is room; the picker now asks for that height as its minimum.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QRect                                        # noqa: E402
from PySide6.QtWidgets import QListWidget, QVBoxLayout, QWidget       # noqa: E402

from app.ui.widgets.timeline_picker import TimelinePicker              # noqa: E402

pytestmark = pytest.mark.gui


@pytest.mark.parametrize("width", [760, 520, 380])
def test_the_last_line_stays_inside_the_picker_in_a_short_window(qtbot, width):
    page = QWidget()
    qtbot.addWidget(page)
    layout = QVBoxLayout(page)
    picker = TimelinePicker(None)
    rows = QListWidget()
    rows.setMinimumHeight(40)
    layout.addWidget(picker)
    layout.addWidget(rows, 1)
    page.resize(width, 110)        # less than the picker's lines and the list together
    page.show()
    qtbot.waitExposed(page)
    qtbot.wait(50)

    def box(widget) -> QRect:
        return QRect(widget.mapTo(picker, widget.rect().topLeft()), widget.size())

    for widget in (*picker.month_buttons, picker.range_from, picker.range_to, picker.range_go):
        assert picker.rect().contains(box(widget)), (width, getattr(widget, "text", str)(),
                                                     picker.rect(), box(widget))
