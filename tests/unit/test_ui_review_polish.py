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


def test_the_people_window_with_no_piles_keeps_its_words_together(app, tmp_path):
    """With nothing to name, its four lines were spread down an empty window."""
    from PyQt6.QtWidgets import QLabel

    from app.storage.sqlite_store import SqliteStore
    from app.ui.widgets.photo_tagger_window import PhotoTaggerWindow

    store = SqliteStore(tmp_path / "knowledge.db").connect()
    window = PhotoTaggerWindow(store, None)
    window.resize(900, 600)
    window.show()
    app.processEvents()
    try:
        # The piles are read on a worker; "none" has arrived once the grid is
        # hidden. Measuring before that measured the page still loading, with
        # the empty grid holding the room (seen once in a whole-suite run).
        import time

        from app.ui.widgets.photo_tagger_page import PhotoTaggerPage

        page = window.findChild(PhotoTaggerPage)
        end = time.monotonic() + 10
        while page._list.isVisible() and time.monotonic() < end:
            app.processEvents()
            time.sleep(0.02)
        assert not page._list.isVisible(), "the piles never arrived"
        app.processEvents()
        labels = [label for label in window.findChildren(QLabel)
                  if label.isVisible() and label.text()]
        bottoms = [label.mapTo(window, label.rect().bottomLeft()).y() for label in labels]
        assert len(labels) >= 2                     # the title and the sentence under it
        assert max(bottoms) < 300, bottoms          # all in the top half
    finally:
        window.close()
        window.deleteLater()
        app.processEvents()
        store.close()


def _filled_after_an_empty_apply(app, widths):
    """A three-column table the way Files builds it: its preferences applied
    while it is still empty, then its rows, then applied again."""
    from PyQt6.QtWidgets import QTableWidgetItem

    from app.ui.view_options import ViewPreferences, apply_to_table
    from app.ui.widgets.result_table import ResultTable

    columns = [("name", "Name"), ("size", "Size"), ("folder", "Folder")]
    order = [key for key, _heading in columns]
    table = ResultTable([heading for _key, heading in columns])
    table.resize(900, 400)
    table.show()
    app.processEvents()
    prefs = ViewPreferences(widths=widths)
    apply_to_table(table, prefs, columns=columns, available=order)
    table.setRowCount(2)
    for row, name in enumerate(("boiler-service-notes-2025-final.md", "boiler-quote-dave.txt")):
        for column, text in enumerate((name, "126 B", "D:/Demo/leasha-guide/documents")):
            table.setItem(row, column, QTableWidgetItem(text))
    apply_to_table(table, prefs, columns=columns, available=order)
    app.processEvents()
    return table


def test_one_saved_width_does_not_leave_the_other_columns_at_their_headings(app):
    """With a width saved for any one column, the table's one fit was spent
    while it was still empty, so every other column opened as wide as its
    heading and names were cut to "boiler-..." - on every start, for anybody
    who had ever dragged a column. Seen in the user guide's own picture of
    Files (the demonstration store has `folder=221` saved)."""
    from app.ui.view_options import _available_width, column_cap

    table = _filled_after_an_empty_apply(app, {"folder": 221})
    try:
        content = table.sizeHintForColumn(0)
        cap = column_cap(_available_width(table))
        assert content > table.horizontalHeader().sectionSizeHint(0)
        assert table.columnWidth(0) >= min(content, cap) - 1, (table.columnWidth(0), content, cap)
        assert table.columnWidth(2) == 221, "the width somebody chose is the one kept"
    finally:
        table.close()
        table.deleteLater()


def test_advanced_is_closed_until_asked_for_and_says_when_it_was_clicked(app):
    from PyQt6.QtWidgets import QGroupBox

    from app.ui.widgets.advanced_fold import TITLE, AdvancedFold

    page = QWidget()
    box = QGroupBox("File types", page)
    fold = AdvancedFold([box], page)
    clicks = []
    fold.toggled.connect(clicks.append)
    page.show()
    app.processEvents()
    try:
        assert fold.header.text() == TITLE
        assert not fold.is_open and not box.isVisible(), "closed to begin with"
        fold.header.click()
        assert fold.is_open and box.isVisible() and clicks == [True]
        fold.set_open(False)
        assert not box.isVisible() and clicks == [True], "restoring is not a click"
        # The filter looks inside whatever the heading says, and puts it back.
        fold.reveal(True)
        assert box.isVisible() and not fold.header.isVisible()
        fold.reveal(False)
        assert not box.isVisible() and fold.header.isVisible()
    finally:
        page.close()
        page.deleteLater()


def test_settings_folds_the_expert_groups_and_the_filter_still_finds_them(app, tmp_path):
    """Six groups most people never change sit under "Advanced", closed. Each
    is still the same box, in its own category, and typing in the filter shows
    a folded setting exactly as it shows any other."""
    from tools import grab_ui

    from app.ui.widgets.settings_shelves import ADVANCED_BOXES

    _app, window, closers = grab_ui.build_window(tmp_path, theme="light", show=True)
    try:
        view = window.settings_view
        grab_ui._reach(app, window, grab_ui.SURFACES["settings-search"])
        assert len(view._folds) == 3, "What's indexed, Search, Models & AI"
        for attr in ADVANCED_BOXES:
            box = getattr(view, attr)
            assert any(fold.body.isAncestorOf(box) for fold in view._folds), attr
        assert not view.search_box.isVisible() and not view.editor_box.isVisible()
        assert view.search_behaviour.isVisible(), "what was not folded is where it was"
        # A group whose only control moved to Indexing is not left as an empty card.
        from PyQt6.QtWidgets import QGroupBox

        grab_ui._reach(app, window, grab_ui.SURFACES["settings-whats-indexed"])
        empty = [box.title() for box in view.findChildren(QGroupBox)
                 if box.isVisible() and not box.findChildren(QWidget)]
        assert not empty, empty
        grab_ui._reach(app, window, grab_ui.SURFACES["settings-search"])

        view._folds[0].header.click()
        app.processEvents()
        assert all(fold.is_open for fold in view._folds), "one click opens them all"
        assert view.search_box.isVisible()
        view._folds[0].header.click()
        app.processEvents()
        assert not view.search_box.isVisible()

        view.filter_box.setText("rerank")
        app.processEvents()
        assert view.search_box.isVisible(), "the filter looks under Advanced"
        view.filter_box.setText("")
        app.processEvents()
        assert not view.search_box.isVisible(), "and puts it back as it was"
    finally:
        window.close()
        from PyQt6.QtCore import QThreadPool

        QThreadPool.globalInstance().waitForDone(10_000)
        app.processEvents()
        for close in closers:
            close()
