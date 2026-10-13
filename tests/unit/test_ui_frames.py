r"""UI phase B, the shared frames (owner, 2026-10-13): *"every screen ... look and feel like using
different apps not one app"*.

* One page frame (`widgets/page_frame.py`): every main page is laid out in it - one set of
  margins, the 8 px rhythm, the title drawn one way, the side list recorded on it.
* One dialog frame (`widgets/dialog_frame.py`): title, body, the button row with the primary on
  the right, Enter the default, Esc Cancel - and Cancel the default where the primary destroys.
* Remembered splitters (`widgets/splitter_memory.py`): a dragged divider is where it was left
  after a restart; a pane that was away keeps its width.
* One empty state and one busy line (`widgets/page_states.py`), used by the lists that had
  their own.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QMessageBox, QSplitter,  # noqa: E402
                               QWidget)

pytestmark = pytest.mark.gui

ROOT = Path(__file__).resolve().parents[2]


class _Store:
    """The two calls a remembered choice makes, in memory."""

    def __init__(self) -> None:
        self.rows: dict[str, str] = {}

    def get_state(self, key, default=""):
        return self.rows.get(key, default)

    def set_state(self, key, value):
        self.rows[key] = value


def _settle(app, rounds: int = 10) -> None:
    from app.ui import state_writes

    for _ in range(rounds):
        app.processEvents()
    state_writes.pool().waitForDone(3000)
    for _ in range(rounds):
        app.processEvents()


def _sheeted_parent(qapp):
    """A widget carrying the app's sheet, as the main window does, for a dialog to inherit."""
    from app.ui import theme

    parent = QWidget()
    parent.setStyleSheet(theme.stylesheet("light"))
    return parent


# ---------------------------------------------------------------------------
# One page frame
# ---------------------------------------------------------------------------

def _pages(window):
    return {"Search": window.search_view, "Files": window.files_view, "Mail": window.mail_view,
            "Code": window.code_view, "Photos": window.photos_view, "Browse": window.timeline_view,
            "Tidy": window.tidy_view, "Indexing": window.indexing_view}


def test_every_main_page_is_laid_out_in_the_one_frame(gui_mainwindow) -> None:
    """**Before:** Search 16/12 and 8 apart, Files, Mail, Code and Photos Qt's 9 and 6, Browse 0
    and 2, Indexing 9 - the page moved a few pixels at every rail click."""
    from app.ui.widgets.page_frame import MARGINS, SPACING, PageFrame

    _app, window, *_ = gui_mainwindow
    for name, view in _pages(window).items():
        frame = view.layout()
        assert isinstance(frame, PageFrame), f"{name}: {type(frame).__name__}"
        margins = frame.contentsMargins()
        got = (margins.left(), margins.top(), margins.right(), margins.bottom())
        assert got == MARGINS, f"{name}: {got}"
        assert frame.spacing() == SPACING, f"{name}: {frame.spacing()}"
    # Chat runs across (its strip sits on the window's edge), in the same margins otherwise.
    row = window.chat_view.layout()
    margins = row.contentsMargins()
    assert (margins.left(), margins.top(), margins.bottom()) == (MARGINS[0], MARGINS[1], MARGINS[3])


def test_a_list_page_takes_its_box_row_as_the_toolbar_and_records_its_side_list(gui_mainwindow) -> None:
    from app.ui.widgets.page_frame import SPACING, frame_of

    _app, window, *_ = gui_mainwindow
    for view in (window.files_view, window.mail_view, window.code_view, window.photos_view):
        frame = frame_of(view)
        assert frame.toolbar is not None and frame.toolbar.indexOf(view.input) >= 0
        assert frame.toolbar.spacing() == SPACING
    for view in (window.files_view, window.code_view, window.timeline_view):
        assert frame_of(view).side is view.side_list


def test_browse_draws_its_period_as_the_page_title(gui_mainwindow) -> None:
    from app.ui.widgets.page_frame import TITLE_SCALE, page_title

    _app, window, *_ = gui_mainwindow
    heading = window.timeline_view.heading
    assert heading.property("pageTitle") is True and heading.font().bold()
    plain = QLabel("")
    assert page_title(QLabel("")).font().pointSizeF() == pytest.approx(
        max(9.0, plain.font().pointSizeF()) * TITLE_SCALE)


# ---------------------------------------------------------------------------
# One dialog frame
# ---------------------------------------------------------------------------

def _x(button) -> int:
    return button.mapTo(button.window(), button.rect().topLeft()).x()


def test_a_dialog_puts_its_primary_on_the_right_and_enter_and_esc_do_what_they_say(qapp) -> None:
    """**Before:** Qt's Windows order - OK left of Cancel - in one dialog, Close at the right of
    a hand-built row in the next."""
    from PySide6.QtTest import QTest

    from app.ui.widgets.dialog_frame import MARGINS, SPACING
    from app.ui.widgets.offline_media_dialogs import ScanNameDialog

    parent = _sheeted_parent(qapp)
    dialog = ScanNameDialog("/media/usb", parent)
    try:
        dialog.show()
        qapp.processEvents()
        margins = dialog.layout().contentsMargins()
        assert (margins.left(), margins.top(), margins.right(), margins.bottom()) == MARGINS
        assert dialog.layout().spacing() == SPACING
        go = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        cancel = dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        assert go.text() == "Scan" and _x(go) > _x(cancel), (_x(go), _x(cancel))
        assert go.isDefault() and not cancel.isDefault()
        dialog.name.setText("Projects 2019")
        QTest.keyClick(dialog.name, Qt.Key.Key_Return)
        assert dialog.result() == QDialog.DialogCode.Accepted
        dialog.show()
        qapp.processEvents()
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        assert dialog.result() == QDialog.DialogCode.Rejected
    finally:
        dialog.deleteLater()
        parent.deleteLater()


def test_a_dialog_that_destroys_puts_enter_on_cancel(qapp) -> None:
    from PySide6.QtTest import QTest

    from app.ui.widgets.offline_media_dialogs import DeleteVolumeDialog, RenameSuggestionDialog

    parent = _sheeted_parent(qapp)
    dialog = DeleteVolumeDialog("Holiday disk", 12, parent)
    ask = RenameSuggestionDialog("Old NAS", parent)
    try:
        dialog.show()
        qapp.processEvents()
        go = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        cancel = dialog.buttons.button(QDialogButtonBox.StandardButton.Cancel)
        assert go.text() == "Delete" and _x(go) > _x(cancel)
        assert cancel.isDefault() and not go.isDefault()
        QTest.keyClick(dialog, Qt.Key.Key_Return)
        assert dialog.result() == QDialog.DialogCode.Rejected
        ask.show()
        qapp.processEvents()
        yes = ask.buttons.button(QDialogButtonBox.StandardButton.Yes)
        no = ask.buttons.button(QDialogButtonBox.StandardButton.No)
        assert yes.text().startswith("Yes") and no.text() == "No — catalogue as new"
        assert _x(yes) > _x(no)
    finally:
        for widget in (dialog, ask, parent):
            widget.deleteLater()


def test_every_question_box_puts_the_verb_on_the_right_too(qapp, monkeypatch) -> None:
    """`confirm` (phase A) is a `QMessageBox`; the sheet's order reaches it as well."""
    from app.ui.widgets import confirm as confirm_module

    parent = _sheeted_parent(qapp)
    seen: dict = {}

    def fake_exec(box):
        box.show()
        qapp.processEvents()
        buttons = {b.text(): b for b in box.buttons()}
        seen["order"] = _x(buttons["Delete"]) > _x(buttons["Cancel"])
        seen["default"] = box.defaultButton().text()
        box.hide()
        return 0

    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    try:
        assert confirm_module.confirm(parent, "Delete this model?", "It goes from the disk.", "Delete") is False
        assert seen == {"order": True, "default": "Cancel"}
    finally:
        parent.deleteLater()


#: Dialogs that keep a frame of their own, and why.
_OWN_FRAME = {
    "app/ui/widgets/about_dialog.py": "the brand's clear space, the L's height (36/24)",
    "app/ui/widgets/photo_viewer.py": "a photo filling a window, not a dialog with a body",
}
#: Every Contacts dialog is converted now (2026-10-13); the set stays so a new one is noticed.
_CONTACTS_TO_DO: set[str] = set()


def test_every_dialog_is_built_in_the_one_frame() -> None:
    """A new `QDialog` laid out by hand fails here, not in somebody's hands."""
    missing = []
    for path in sorted((ROOT / "app" / "ui").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if not re.search(r"\(QDialog\)|= QDialog\(", text):
            continue
        if rel in _OWN_FRAME or rel in _CONTACTS_TO_DO:
            continue
        if "dialog_frame import" not in text or not re.search(r"\bframe\(", text):
            missing.append(rel)
    assert not missing, f"dialogs not built in dialog_frame: {missing}"


# ---------------------------------------------------------------------------
# Remembered splitters
# ---------------------------------------------------------------------------

def _page_with_split(store, sizes=(300, 500)):
    page = QWidget()
    page._store = store
    page.resize(800, 400)
    split = QSplitter(Qt.Orientation.Horizontal, page)
    split.setObjectName("testSplit")
    for _ in sizes:
        split.addWidget(QLabel("x"))
    split.resize(800, 400)
    return page, split


def test_a_dragged_divider_is_where_it_was_left_after_a_restart(qapp) -> None:
    """**Before:** every divider but Chat's side panel went back to its built width at each start."""
    from app.ui.widgets.splitter_memory import KEY_PREFIX, remember_all

    store = _Store()
    page, split = _page_with_split(store)
    assert remember_all(page, "Test") == 1
    page.show()
    _settle(qapp)
    split.setSizes([200, 600])
    split.splitterMoved.emit(200, 1)                      # what a drag of the handle sends
    qapp.processEvents()
    split._size_memory.save()                             # the settle timer, without the wait
    # Saved once, not again `SETTLE_MS` later: the timer left running fired into a later test's
    # patched state writer (`test_ui_one_app_settings_b`) after this page had been put away.
    assert not split._size_memory._timer.isActive()
    _settle(qapp)
    assert store.rows[KEY_PREFIX + "Test:testSplit"] == ",".join(str(s) for s in split.sizes())
    page.hide()
    page.deleteLater()

    again, split2 = _page_with_split(store)
    remember_all(again, "Test")
    again.show()
    _settle(qapp)
    assert split2.sizes()[0] < split2.sizes()[1] and abs(split2.sizes()[0] - 200) <= 10, split2.sizes()
    again.hide()
    again.deleteLater()


def test_a_size_qt_arrived_at_is_not_saved_and_a_hidden_pane_keeps_its_width(qapp) -> None:
    from app.ui.widgets.splitter_memory import merged, restored, sizes_from_text

    store = _Store()
    page, split = _page_with_split(store)
    from app.ui.widgets.splitter_memory import remember

    remember(split, "Test:quiet")
    page.show()
    _settle(qapp)
    split.setSizes([100, 700])                             # a page's own call, not a drag
    _settle(qapp)
    assert store.rows == {}
    page.hide()
    page.deleteLater()
    # The pane that was away (0 wide) keeps the width it had; only shown panes are put back.
    assert merged([220, 600, 320], [260, 740, 0], [True, True, False]) == [260, 740, 320]
    assert restored([260, 740, 320], [220, 900, 0], [True, True, False]) == [260, 740, 0]
    assert restored([1, 2], [220, 900, 0], [True, True, False]) is None
    assert sizes_from_text("12,x") == [] and sizes_from_text("") == []


def test_every_splitter_on_every_page_of_the_window_has_a_memory(gui_mainwindow) -> None:
    _app, window, *_ = gui_mainwindow
    window._remember_splitters()
    forgotten = []
    for view in window._tab_index:
        for split in view.findChildren(QSplitter):
            if split.property("rememberSizes") is False:
                continue
            if not split.property("sizesRemembered"):
                forgotten.append(f"{type(view).__name__}:{split.objectName() or '?'}")
    assert not forgotten, forgotten


# ---------------------------------------------------------------------------
# One empty state, one busy line
# ---------------------------------------------------------------------------

def test_the_empty_state_is_an_icon_one_sentence_and_an_optional_action(qapp) -> None:
    from app.ui.widgets.page_states import EmptyState

    empty = EmptyState("Nothing here yet.", glyph="folder")
    assert empty.text() == "Nothing here yet." and not empty.icon.pixmap().isNull()
    empty.setText("Still nothing.")
    assert empty.label.text() == "Still nothing." and empty.action is None
    pressed = []
    button = empty.set_action("Index now", lambda: pressed.append(1), glyph="play", tip="Start reading")
    button.click()
    assert pressed == [1] and button.toolTip() == "Start reading" and not button.icon().isNull()
    links = []
    empty.linkActivated.connect(links.append)
    empty.label.linkActivated.emit("index")
    assert links == ["index"]
    empty.deleteLater()


def test_the_busy_line_moves_while_working_and_stops_with_the_sentence(qapp) -> None:
    from app.ui.widgets.page_states import BusyLine

    line = BusyLine()
    line.start("Looking through what Leasha has from 2019...")
    assert line.busy() and line.bar.busy() and line.text().startswith("Looking")
    line.setText("12 items - that is everything from this period.")
    assert not line.busy() and line.text().startswith("12 items")
    line.start("Finding duplicates...")
    line.stop()
    assert not line.busy() and line.text() == "Finding duplicates..."
    line.deleteLater()


def test_the_lists_that_had_their_own_use_the_shared_states(gui_mainwindow) -> None:
    from app.ui.widgets.page_states import BusyLine, EmptyState

    _app, window, *_ = gui_mainwindow
    assert isinstance(window.code_view.empty, EmptyState)
    assert isinstance(window.offline_media_view.empty, EmptyState)
    assert isinstance(window.photos_view.info.empty, EmptyState)
    assert isinstance(window.tidy_view.history.empty, EmptyState)
    assert isinstance(window.chat_view.sources.empty, EmptyState)
    assert isinstance(window.timeline_view.status, BusyLine)
    assert isinstance(window.tidy_view.progress, BusyLine)
    assert isinstance(window.tidy_view.report.progress_label, BusyLine)


def test_a_divider_dragged_just_before_closing_is_saved(qapp, tmp_path):
    """Review of #78: closing within the settle time of a drag lost it; `flush_all` saves it first."""
    from PySide6.QtWidgets import QLabel, QSplitter, QWidget

    from app.ui.widgets import splitter_memory

    saved = []
    root = QWidget()
    split = QSplitter(root)
    split.addWidget(QLabel("a"))
    split.addWidget(QLabel("b"))
    memory = splitter_memory.remember(split, "test:split", store=object())
    memory.save = lambda: saved.append(split.sizes())
    memory._timer.start()                                   # a drag, still settling
    assert splitter_memory.flush_all(root) == 1 and saved
    assert not memory._timer.isActive()
    assert splitter_memory.flush_all(root) == 0             # nothing settling: nothing saved again
