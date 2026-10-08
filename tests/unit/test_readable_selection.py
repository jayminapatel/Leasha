"""Selected and matched text can be read, in both themes.

Layer: L5

2026-10-08, the owner: "the highlighting of blue for text and matches ... blue on
black text which cant be read". Two causes. The find box in a preview painted
its matches with the system's selection colour - Windows blue - and left the
text black. And `selection_text` was the wrong way round: dark text on the
light theme's near-black indigo, white text on the dark theme's light violet.
"""

from __future__ import annotations

import pytest

from app.ui.theme import PALETTES, Theme, stylesheet
from tests.unit.test_brand_colours import contrast


@pytest.mark.parametrize("theme", [Theme.LIGHT, Theme.DARK])
def test_text_selected_in_a_line_edit_reads(theme):
    colours = PALETTES[theme]
    assert contrast(colours["selection_text"], colours["accent"]) >= 4.5


@pytest.mark.parametrize("theme", [Theme.LIGHT, Theme.DARK])
def test_text_selected_in_a_text_view_reads(theme):
    colours = PALETTES[theme]
    assert contrast(colours["text"], colours["accent_soft"]) >= 4.5


@pytest.mark.parametrize("theme", [Theme.LIGHT, Theme.DARK])
def test_a_matched_word_reads(theme):
    colours = PALETTES[theme]
    assert contrast(colours["mark_text"], colours["mark"]) >= 4.5


def test_text_views_select_with_the_theme_not_the_system():
    sheet = stylesheet("light")
    rule = sheet[sheet.index("QTextEdit, QPlainTextEdit, QTextBrowser {"):]
    rule = rule[:rule.index("}")]
    assert "selection-background-color" in rule and "selection-color" in rule


def test_the_find_box_paints_matches_with_the_mark(qtbot):
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QTextEdit

    from app.ui.theme import theme_colours
    from app.ui.widgets.find_bar import FindBar

    view = QTextEdit()
    view.setPlainText("one match here, and one more match")
    bar = FindBar()
    qtbot.addWidget(view)
    qtbot.addWidget(bar)
    bar.attach(view)

    bar._highlight("match")

    marks = view.extraSelections()
    assert len(marks) == 2
    colours = theme_colours()
    assert marks[0].format.background().color() == QColor(colours["mark"])
    assert marks[0].format.foreground().color() == QColor(colours["mark_text"])
