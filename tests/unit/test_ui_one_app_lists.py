r"""The lists behave as one application: one press, one action, the same keys everywhere.

Layer: L5

2026-10-12, an audit of the lists after order 1p, and the owner: "do the recommended to make
it world class and friendly and do it all". Each test here failed before its fix:

- a double-click on a contact opened it twice (both `activated` and `doubleClicked` were wired);
- Enter did nothing in the left-hand lists, the Browse table and the Tidy list;
- a filter taken away by a second click stayed highlighted;
- the side list's accent fell back to a colour of its own instead of the theme's;
- Ctrl+F was bound twice window-wide while a preview pane was showing, so Qt fired neither;
- the end-of-list line counted passages, not the results shown after grouping by file;
- the Code menu offered "Copy full path" beside "Copy path", the same text twice;
- "Search inside this file" on a message, and on a repository it searches whole;
- the Contacts question box said its title twice and pressed the destructive button on Enter;
- a failed as-you-type read in Mail, Files or Code raised a modal dialog mid-keystroke.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                                            # noqa: E402
from PySide6.QtGui import QKeySequence, QShortcut                        # noqa: E402
from PySide6.QtTest import QTest                                         # noqa: E402
from PySide6.QtWidgets import QLabel, QMessageBox, QWidget               # noqa: E402

from app.ui.presenter.side_entry import SideEntry                       # noqa: E402
from tests.unit.test_contacts_view_world import card, settle, world     # noqa: E402,F401

ROOT = Path(__file__).resolve().parents[2]


# -- 1. Contacts: a double-click opens a contact once ------------------------------------

@pytest.mark.slow
def test_a_double_click_on_a_contact_opens_it_once(world, qapp):
    cid = world.add(card("Ann", "Lee"))
    page = world.page()
    page.select_ids([cid])
    settle(page)
    opened = []
    page.editor.focus_first = lambda: opened.append(1)
    page.set_view("table")
    qapp.processEvents()
    assert page.table.isVisible() and page.editor.isEnabled()
    rect = page.table.visualRect(page.model.index(0, 0))
    # What Windows delivers for a double-click: press, release, then a double-click and its release.
    QTest.mouseClick(page.table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    QTest.mouseDClick(page.table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    qapp.processEvents()
    # QTest's double-click leaves Qt believing the left button is still down (checked on PySide6,
    # 2026-10-13); a later test that tells a key from a click by `mouseButtons()` then failed -
    # the Contacts sidebar's arrow-key test, whenever it ran after this file.
    QTest.mouseRelease(page.table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center())
    qapp.processEvents()
    assert len(opened) == 1, f"one double-click opened the contact {len(opened)} times"


# -- 2. The left-hand list: Enter is a click, a removed filter lets go, the theme's accent -

def _side(qtbot):
    from app.ui.widgets.side_list import SideList

    side = SideList("t", accessible="Test")
    qtbot.addWidget(side)
    side.set_entries([
        SideEntry("all", "All", icon="folder", kind="all"),
        SideEntry("kind:pdf", "pdf", section="Kinds", icon="file", value=("type", "pdf"), checked=False),
        SideEntry("kind:txt", "txt", section="Kinds", icon="file", value=("type", "txt"), checked=False),
        SideEntry("dupes", "Duplicates", section="Tools", icon="tag", kind="door"),
    ])
    side.show()
    return side


def test_enter_in_a_side_list_does_what_a_click_does(qtbot):
    side = _side(qtbot)
    picked, doors = [], []
    side.picked.connect(lambda e: picked.append(e.key))
    side.door.connect(lambda e: doors.append(e.key))
    side.setFocus()
    side.setCurrentItem(side._by_key["kind:pdf"])
    QTest.keyClick(side, Qt.Key.Key_Return)
    side.setCurrentItem(side._by_key["dupes"])
    QTest.keyClick(side, Qt.Key.Key_Enter)
    assert picked == ["kind:pdf"] and doors == ["dupes"]


@pytest.mark.slow
def test_arrow_keys_in_a_side_list_only_move(qtbot):
    r"""A door opens a dialog: walking past one with the arrows must not open it."""
    side = _side(qtbot)
    heard = []
    side.picked.connect(heard.append)
    side.door.connect(heard.append)
    side.setFocus()
    side.setCurrentItem(side._by_key["all"])
    for _ in range(6):
        QTest.keyClick(side, Qt.Key.Key_Down)
    assert heard == []


def test_a_filter_taken_away_by_a_second_click_lets_go_of_its_highlight(qtbot):
    side = _side(qtbot)
    row = side._by_key["kind:pdf"]
    side.setCurrentItem(row)
    side._clicked(row, 0)                                             # first click: the page ticks it
    side.set_entries([e if e.key != "kind:pdf" else SideEntry(
        "kind:pdf", "pdf", section="Kinds", icon="file", value=("type", "pdf"), checked=True)
        for e in side.entries()])
    assert side.current_key() == "kind:pdf"
    side._clicked(side._by_key["kind:pdf"], 0)                        # second click: the page unticks it
    side.set_entries([e if e.key != "kind:pdf" else SideEntry(
        "kind:pdf", "pdf", section="Kinds", icon="file", value=("type", "pdf"), checked=False)
        for e in side.entries()])
    assert side.current_key() == ""
    assert not side._by_key["kind:pdf"].isSelected()
    assert side.currentItem() is None


def test_a_ticked_entry_takes_the_themes_accent_and_no_colour_of_its_own(qtbot, monkeypatch):
    from app.ui import theme
    from app.ui.widgets import icons

    asked = []
    real = icons.icon
    monkeypatch.setattr(icons, "icon", lambda name, colour, **kw: (asked.append(colour), real(name, colour, **kw))[1])
    side = _side(qtbot)
    side.retint({"text_dim": "#111111", "text_faint": "#222222"})    # a palette that names no accent
    side.set_entries([SideEntry("kind:pdf", "pdf", section="Kinds", icon="file", checked=True)])
    assert asked[-1] == theme.theme_colours()["accent"]
    source = (ROOT / "app/ui/widgets/side_list.py").read_text(encoding="utf-8")
    assert "#5b4bd5" not in source


# -- 3. Enter opens a line of the Browse table and of the Tidy list -------------------------

def test_enter_in_the_browse_table_opens_the_line(qtbot):
    from app.ui.widgets.browse_table import BrowsePane

    pane = BrowsePane()
    qtbot.addWidget(pane)
    head = SimpleNamespace(name="a.jpg", path="C:/p/a.jpg", real_path="", source_name="", when_ns=0, ext="jpg",
                           size_bytes=1, kind="photos", basis="saved", source_kind="file")
    pane.set_folds([SimpleNamespace(head=head, older=(), folded=False)])
    pane.show()
    opened = []
    pane.opened.connect(opened.append)
    pane.results.setFocus()
    pane.results.setCurrentCell(0, 0)
    QTest.keyClick(pane.results, Qt.Key.Key_Return)
    assert opened == [head]


def test_enter_in_the_tidy_list_does_what_a_double_click_does(qtbot):
    from app.ui.widgets.tidy_list_pane import TidyListPane

    pane = TidyListPane()
    qtbot.addWidget(pane)
    files = [SimpleNamespace(path=f"C:/p/{n}.jpg", name=f"{n}.jpg", size_bytes=1, mtime_ns=0) for n in "ab"]
    pane.set_rows([SimpleNamespace(files=files, size_bytes=2, kind="copies")], lambda _p: False)
    pane.show()
    kept = []
    pane.keep_only.connect(kept.append)
    pane.results.setFocus()
    pane.results.setCurrentCell(0, 1)
    QTest.keyClick(pane.results, Qt.Key.Key_Return)
    assert len(kept) == 1 and kept[0] in {f.path for f in files}


# -- 4. Ctrl+F: never two window-wide bindings ----------------------------------------------

def _find_shortcuts(widget):
    find = QKeySequence(QKeySequence.StandardKey.Find)
    return [s for s in widget.findChildren(QShortcut) if s.key() == find]


@pytest.mark.slow
def test_the_in_app_preview_binds_no_window_wide_ctrl_f(qapp):
    r"""The main window binds Ctrl+F (Search); two window-wide Ctrl+F and Qt fires neither."""
    from app.ui.widgets.preview import PreviewPane

    pane = PreviewPane()
    assert all(s.context() != Qt.ShortcutContext.WindowShortcut for s in _find_shortcuts(pane))
    shell = (ROOT / "app/ui/shell.py").read_text(encoding="utf-8")
    assert 'bind("Ctrl+F", self._find_or_search)' in shell         # the window's one, which asks the pane first


def test_ctrl_f_inside_a_preview_opens_its_find_bar(qapp):
    from app.ui.widgets.find_bar import focused_find
    from app.ui.widgets.preview import PreviewPane

    host = QWidget()
    outside = QLabel("x", host)
    pane = PreviewPane(host)
    host.show()
    pane.show()
    pane.text.setFocus()
    qapp.processEvents()
    assert focused_find(pane.text) is pane.find
    assert focused_find(outside) is None
    assert focused_find(None) is None
    # CodeRabbit on #68: the bar searches the text view, so on an EPUB chapter it would search text that is
    # not on screen - Ctrl+F goes to Search there.
    pane.stack.setCurrentWidget(pane.epub)
    assert focused_find(pane.epub) is None


def test_a_window_of_its_own_keeps_its_ctrl_f(qapp):
    from PySide6.QtWidgets import QTextBrowser

    from app.ui.widgets.find_bar import attach_find

    host = QWidget()
    attach_find(host, QTextBrowser(host))
    assert [s.context() for s in _find_shortcuts(host)] == [Qt.ShortcutContext.WindowShortcut]


# -- 5. The end of the list counts what is shown --------------------------------------------

def test_the_end_of_the_list_counts_files_not_passages(qtbot):
    from app.search.engine import SearchResult
    from app.ui.result_delegate import ROLE_PAYLOAD
    from app.ui.results_view import ResultsView

    view = ResultsView()
    qtbot.addWidget(view)
    passages = [SearchResult(chunk_id=i, file_id=1, path="C:/w/report.pdf", text=f"safety {i}", score=0.5,
                             rank=i, ext="pdf", mtime_ns=1_700_000_000_000_000_000) for i in range(3)]
    view.show_results(passages, ["safety"])
    model = view._model
    last = model.item(model.rowCount() - 1).data(ROLE_PAYLOAD)
    assert last.text == "That's all \u2014 1 result."


# -- 6 and 8. The menus: no copy twice, words that fit the row ------------------------------

def _labels(menu):
    return [a.text() for a in menu.actions() if a.text()]


def test_the_code_menu_copies_the_path_once_and_searches_the_repository(qtbot, monkeypatch):
    from app.ui.widgets import code_results, file_menu

    built = {}
    monkeypatch.setattr(code_results, "show_for", lambda w, p, path, actions: built.update(
        menu=file_menu.build_menu(w, path, actions)))
    view = code_results.CodeResults()
    qtbot.addWidget(view)
    row = SimpleNamespace(full_path="C:/r/app.py", repo="leasha", line_no=0)
    monkeypatch.setattr(view.table, "current_row", lambda: row)
    view._on_context_menu(view.table.rect().center())
    labels = _labels(built["menu"])
    assert "Copy full path" not in labels and labels.count("Copy path") == 1
    assert "Search this repository" in labels and "Search inside this file" not in labels


def test_the_menu_on_a_message_says_message(qapp):
    from app.ui.presenter.rows import MailRow
    from app.ui.widgets.file_menu import FileActions, build_menu

    message = MailRow(file_id=1, sender="a", recipients="b", sent="", subject="s", attachment="", size="",
                      path="pst://box/1")
    parent = QWidget()
    mail = _labels(build_menu(parent, message.path, FileActions(search_inside=lambda: None, row=message)))
    plain = _labels(build_menu(parent, "C:/w/a.txt", FileActions(search_inside=lambda: None)))
    assert "Search inside this message" in mail and "Search inside this file" not in mail
    assert "Search inside this file" in plain


# -- 9. The Contacts question box ------------------------------------------------------------

@pytest.mark.slow
def test_the_contacts_question_says_its_title_once_and_cancel_is_the_default(world, monkeypatch):
    page = world.page()
    seen = {}

    def look(box):
        seen.update(title=box.windowTitle(), text=box.text(), detail=box.informativeText(),
                    default=box.defaultButton().text() if box.defaultButton() else None)
        return 0

    monkeypatch.setattr(QMessageBox, "exec", look)
    assert page.confirm("Empty the bin?", "Every contact in the bin is deleted for good.", "Empty the bin") is False
    assert seen["text"] == "Empty the bin?" and seen["detail"].startswith("Every contact")
    assert seen["title"] != seen["text"], "the question showed twice"
    assert seen["default"] == "Cancel"


# -- 10. Typing never raises a modal ---------------------------------------------------------

def test_a_failed_read_while_typing_is_said_on_the_pages_own_line(qapp):
    from app.ui.widgets.chips import say_failed

    error = SimpleNamespace(code="X", message="The index could not be read.", suggestion="Try again.")
    view = SimpleNamespace(summary=QLabel(""))
    say_failed(view, error)
    assert error.message in view.summary.text() and "Try again." in view.summary.text()


def test_a_failure_from_a_superseded_read_never_overwrites_the_newer_result(qapp):
    """CodeRabbit on #68: a late success was dropped by its generation, a late failure was not."""
    from app.ui.widgets.chips import say_failed

    error = SimpleNamespace(code="X", message="The index could not be read.", suggestion="")
    view = SimpleNamespace(summary=QLabel("12 messages"), _generation=5)
    say_failed(view, error, 4)
    assert view.summary.text() == "12 messages"
    say_failed(view, error, 5)
    assert error.message in view.summary.text()


@pytest.mark.parametrize("name", ["mail_view.py", "files_view.py", "code_view.py"])
def test_the_typed_lists_send_a_failed_read_to_their_line_not_a_dialog(name):
    source = (ROOT / "app/ui" / name).read_text(encoding="utf-8")
    assert "worker.signals.failed.connect(self.error.emit)" not in source
    assert "say_failed(v, e, g)" in source, "the failure must carry its read's generation"


@pytest.mark.parametrize("name", ["files_view.py", "code_view.py"])
def test_a_skipped_query_drops_an_older_read_still_on_its_way(name, qapp):
    """CodeRabbit on #68: "keep typing" and "press Enter for history" left the generation alone, so an older
    read landing afterwards overwrote the sentence."""
    from app.ui.widgets.chips import say_failed, say_skipped

    source = (ROOT / "app/ui" / name).read_text(encoding="utf-8")
    assert "say_skipped(self," in source
    view = SimpleNamespace(summary=QLabel(""), _generation=3)
    say_skipped(view, "Keep typing")
    say_failed(view, SimpleNamespace(code="X", message="late", suggestion=""), 3)
    assert view.summary.text() == "Keep typing" and view._generation == 4
