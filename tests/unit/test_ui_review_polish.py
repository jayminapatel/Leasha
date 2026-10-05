"""The five faults the UI review of 2026-10-05 found, each held by a test.

The owner asked for the whole window to be reviewed ("is it world class and
modern") and then for the recommended fixes. Every picture was grabbed from the
real window with `tools/guide_pictures.py` before and after.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import (  # noqa: E402
    QApplication, QComboBox, QFormLayout, QLineEdit, QPlainTextEdit, QSpinBox, QWidget,
)

from app.ui import theme  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("scheme", [theme.Theme.LIGHT, theme.Theme.DARK])
def test_a_ticked_box_is_still_a_box(scheme):
    """A ticked box was drawn as a bare tick and an empty one as a box, so a
    column of ticked settings read as a list rather than as switches."""
    sheet = theme.stylesheet(scheme)
    assert "QCheckBox::indicator:checked" in sheet and "QTreeView::indicator:checked" in sheet
    tick = Path(theme._tick_file(theme.PALETTES[scheme]))
    assert tick.is_file(), tick
    # The tick is drawn in the colour that reads on the accent it sits on.
    assert theme.PALETTES[scheme]["accent_on"].lower() in tick.read_text(encoding="utf-8").lower()
    assert f"url({tick.as_posix()})" in sheet


def test_a_drop_down_is_as_wide_as_what_it_holds(app):
    """"30 days" sat in a box as wide as the page. A text box still takes the row."""
    from app.ui.widgets.field_width import COMBO_MIN_PX, fit_fields

    page = QWidget()
    form = QFormLayout(page)
    combo, number, text = QComboBox(), QSpinBox(), QLineEdit()
    combo.addItems(["Only when I ask", "Every night"])
    for label, field in (("Run", combo), ("Limit", number), ("Folder", text)):
        form.addRow(label, field)
    assert fit_fields(page) == 2
    page.resize(1200, 300)
    page.show()
    app.processEvents()
    try:
        assert COMBO_MIN_PX <= combo.width() < 500, combo.width()
        assert number.width() < 500, number.width()
        assert text.width() > 800, text.width()
    finally:
        page.close()
        page.deleteLater()


def test_the_chat_notice_has_a_line_of_its_own(app):
    """It shared a line with the model and speed boxes and was squeezed to a
    column a few words wide and ten lines tall."""
    from app.ui.chat_view import ChatView

    view = ChatView()
    view.resize(1280, 800)
    view.show_available(False, "The chat model is not downloaded yet.")
    view.show()
    app.processEvents()
    try:
        notice, speed = view.notice, view.speed
        assert notice.geometry().bottom() < speed.geometry().top() or \
            notice.mapTo(view, notice.rect().bottomLeft()).y() <= speed.mapTo(view, speed.rect().topLeft()).y()
        assert notice.width() > 3 * speed.width() or notice.width() > 400, notice.width()
    finally:
        view.shutdown()
        view.close()
        view.deleteLater()


@pytest.mark.parametrize("scheme", [theme.Theme.LIGHT, theme.Theme.DARK])
def test_the_preview_marks_words_as_the_results_list_does(app, scheme):
    """The list marked a searched word in soft yellow and the preview in the
    system's selection blue: one search, two colours."""
    from app.ui.widgets.search_marks import SearchMarks

    theme.stylesheet(scheme)                       # sets what `theme_colours` answers
    host = QWidget()
    view = QPlainTextEdit(host)
    view.setPlainText("the boiler quote from Dave")
    marks = SearchMarks(host, view)
    marks.show(["boiler", "dave"])
    drawn = view.extraSelections()
    assert len(drawn) == 2
    colours = theme.PALETTES[scheme]
    for mark in drawn:
        assert mark.format.background().color().name() == colours["mark"]
        assert mark.format.foreground().color().name() == colours["mark_text"]
    host.close()
    host.deleteLater()


def test_a_table_rules_its_rows_and_not_its_columns(app):
    from app.ui.widgets.result_table import ResultTable

    table = ResultTable(["Name", "Size"])
    assert table.showGrid() is False
    assert "QTableWidget::item {" in theme.stylesheet(theme.Theme.LIGHT)
    table.close()
    table.deleteLater()
