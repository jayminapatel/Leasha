r"""One list behaviour (`app/ui/widgets/list_keys.py`), UI phase B.

Layer: L5

Each list had its own keys, and some had two routes to the same action - a
double-click handler *and* a key handler - which is how one double-click once
opened the photo viewer twice. These tests drive real key presses and real
double-clicks into real lists and count what ran:

* double-click and Enter run the default action, once each, from one signal;
* Space shows the page's preview (and still ticks a tick box);
* Delete runs the destructive line through its own confirmation, F2 renames;
* Ctrl+C copies the path or paths, Ctrl+A selects everything;
* where a list allows several rows, Copy path and Show in folder act on all of
  the selected rows (Files, Mail, Code, Browse); the rest stay single-row.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, Qt                     # noqa: E402
from PySide6.QtGui import QKeyEvent, QKeySequence                 # noqa: E402
from PySide6.QtTest import QTest                                  # noqa: E402
from PySide6.QtWidgets import (                                   # noqa: E402
    QApplication, QMenu, QTableWidget, QTableWidgetItem, QWidget, QVBoxLayout,
)

from app.ui.widgets import list_keys                              # noqa: E402
from app.ui.widgets import menus                                  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def menus_seen(monkeypatch):
    """Every menu a right-click opens, without its modal loop."""
    seen: list = []
    monkeypatch.setattr(QMenu, "exec", lambda self, *a, **k: seen.append(self))
    monkeypatch.setattr(QMenu, "popup", lambda self, *a, **k: seen.append(self))
    return seen


@pytest.fixture()
def clipboard(monkeypatch):
    texts: list = []
    from app.ui.widgets import file_menu

    monkeypatch.setattr(file_menu, "_copy", texts.append)
    return texts


def _press(widget, key, mods=Qt.KeyboardModifier.NoModifier) -> None:
    for kind in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress):
        QApplication.sendEvent(widget, QKeyEvent(kind, key, mods))


def _double_click(view, index) -> None:
    """A double-click as a mouse gives one: a click, then the double. Qt's view
    answers a double only on the line its press landed on, and Qt 6's
    `mouseDClick` sends the double alone."""
    where = view.visualRect(index).center()
    for send in (QTest.mouseClick, QTest.mouseDClick):
        send(view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, where)


def _right_click(view, index) -> None:
    """A right-click as Qt delivers it: the point in viewport coordinates."""
    view.customContextMenuRequested.emit(view.visualRect(index).center())


def _lines(menu) -> list:
    return [a for a in menu.actions() if not a.isSeparator()]


def _line(menu, words):
    return next(a for a in _lines(menu) if a.text() == words)


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------

def _plain_table(rows: int = 4, checkable: bool = False) -> QTableWidget:
    table = QTableWidget(rows, 1)
    # As every list in the window: a double-click acts, it does not edit.
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    for row in range(rows):
        item = QTableWidgetItem(f"row {row}")
        if checkable:
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
        table.setItem(row, 0, item)
    table.resize(240, 240)
    table.show()
    QApplication.processEvents()
    return table


def test_enter_and_double_click_run_the_default_once_each(qapp) -> None:
    table = _plain_table()
    ran = []
    list_keys.install(table, default=lambda index: ran.append(index.row()))
    table.setCurrentCell(2, 0)
    _press(table, Qt.Key.Key_Return)
    _press(table, Qt.Key.Key_Enter, Qt.KeyboardModifier.KeypadModifier)
    _double_click(table, table.model().index(1, 0))
    assert ran == [2, 2, 1]
    table.deleteLater()


def test_space_delete_f2_and_ctrl_c_run_what_the_list_gave(qapp) -> None:
    table = _plain_table()
    ran = []
    list_keys.install(table, preview=lambda: ran.append("preview"),
                      delete=lambda: ran.append("delete"), rename=lambda: ran.append("rename"),
                      copy=lambda: ran.append("copy"))
    table.setCurrentCell(0, 0)
    for key, mods in ((Qt.Key.Key_Space, Qt.KeyboardModifier.NoModifier),
                      (Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier),
                      (Qt.Key.Key_F2, Qt.KeyboardModifier.NoModifier),
                      (Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)):
        _press(table, key, mods)
    assert ran == ["preview", "delete", "rename", "copy"]
    table.deleteLater()


def test_a_key_the_list_has_is_claimed_from_the_window(qapp) -> None:
    table = _plain_table()
    list_keys.install(table, delete=lambda: None)
    override = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_Delete,
                         Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(table, override)
    assert override.isAccepted()
    unused = QKeyEvent(QEvent.Type.ShortcutOverride, Qt.Key.Key_F2, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(table, unused)
    assert not unused.isAccepted(), "a key the list does not use must stay the window's"
    table.deleteLater()


def test_space_still_ticks_a_tick_box(qapp) -> None:
    table = _plain_table(checkable=True)
    ran = []
    list_keys.install(table, preview=lambda: ran.append("preview"))
    table.setCurrentCell(0, 0)
    _press(table, Qt.Key.Key_Space)
    assert ran == []
    assert table.item(0, 0).checkState() == Qt.CheckState.Checked
    table.deleteLater()


def test_page_preview_is_the_pages_own_toggle(qapp) -> None:
    page = QWidget()
    toggled = []
    page.view_button = SimpleNamespace(toggle_preview=lambda: toggled.append(1))
    layout = QVBoxLayout(page)
    table = _plain_table()
    layout.addWidget(table)
    list_keys.install(table, preview=list_keys.PAGE_PREVIEW)
    table.setCurrentCell(0, 0)
    _press(table, Qt.Key.Key_Space)
    assert toggled == [1]
    page.deleteLater()


def test_ctrl_a_selects_every_row(qapp) -> None:
    table = _plain_table()
    list_keys.install(table, copy=lambda: None)
    table.setCurrentCell(0, 0)
    QTest.keyClick(table, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert len(table.selectionModel().selectedRows()) == 4
    # QTest's Ctrl+A leaves Qt believing Ctrl is still held (2026-10-13); a later test's
    # `setCurrentIndex` would toggle its pick off. Let go of it, and prove it was let go.
    QTest.keyRelease(table, Qt.Key.Key_Control)
    assert QApplication.keyboardModifiers() == Qt.KeyboardModifier.NoModifier
    table.deleteLater()


# ---------------------------------------------------------------------------
# On the lists themselves
# ---------------------------------------------------------------------------

@pytest.fixture()
def store(tmp_path: Path):
    from app.storage.sqlite_store import SqliteStore

    s = SqliteStore(tmp_path / "index.db").connect()
    now = time.time_ns()
    for n in range(3):
        done = s.upsert_file(f"C:/docs/f{n}/report{n}.pdf", size_bytes=10 + n, mtime_ns=now - n)
        s.mark_indexed(done)
    for n in range(3):
        message = s.upsert_file(f"pst://box.pst/E{n}", size_bytes=5, mtime_ns=now,
                                source_kind="pst_message")
        s.mark_indexed(message)
        s.set_message(message, subject=f"Quote {n}", sender="dave@acme.com",
                      sent_at=1_700_000_000 + n)
    yield s
    s.close()


def _pump() -> None:
    from PySide6.QtCore import QThreadPool

    for _ in range(10):
        QThreadPool.globalInstance().waitForDone(5000)
        QApplication.processEvents()


def _filled(view, table) -> None:
    view.resize(900, 500)
    view.show()
    _pump()
    assert table.rowCount() >= 2, "the list never filled; this would prove nothing"


def test_files_opens_once_per_enter_and_once_per_double_click(qapp, store, monkeypatch) -> None:
    from app.ui import files_view

    opened = []
    monkeypatch.setattr(files_view, "open_row_async",
                        lambda _s, row, reveal=False, **_k: opened.append((row.path, reveal)))
    view = files_view.FilesView(store)
    _filled(view, view.results)
    view.results.setFocus()
    view.results.selectRow(0)
    _press(view.results, Qt.Key.Key_Return)
    _double_click(view.results, view.results.model().index(1, 0))
    assert len(opened) == 2 and not any(reveal for _p, reveal in opened)
    view.deleteLater()


def test_files_copy_path_and_show_in_folder_act_on_every_selected_row(
        qapp, store, monkeypatch, menus_seen, clipboard) -> None:
    from app.ui import files_view
    from app.ui.widgets import file_menu

    revealed = []
    monkeypatch.setattr(file_menu, "open_row_async",
                        lambda _s, row, reveal=False, **_k: revealed.append(row.path),
                        raising=False)
    monkeypatch.setattr(files_view, "open_row_async",
                        lambda _s, row, reveal=False, **_k: revealed.append(row.path))
    view = files_view.FilesView(store)
    _filled(view, view.results)
    view.results.selectAll()
    paths = sorted(str(view.results.row_object(r).path) for r in range(view.results.rowCount()))
    _right_click(view.results, view.results.model().index(0, 0))
    menu = menus_seen[-1]
    assert menu.property(menus.BUILT_PROPERTY) and _lines(menu)[0].text() == "Open"
    _line(menu, "Copy path").trigger()
    assert sorted(clipboard[-1].splitlines()) == paths
    _line(menu, "Show in folder").trigger()
    assert sorted(revealed) == paths
    # Ctrl+C on the list is the same as Copy path.
    clipboard.clear()
    _press(view.results, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert sorted(clipboard[-1].splitlines()) == paths
    view.deleteLater()


def test_a_right_click_acts_on_the_row_under_the_pointer(qapp, store, menus_seen, clipboard) -> None:
    """The viewport point goes to `rowAt` as it is: the old mapping took the row above."""
    from app.ui import files_view

    view = files_view.FilesView(store)
    _filled(view, view.results)
    view.results.clearSelection()
    _right_click(view.results, view.results.model().index(1, 0))
    assert view.results.currentRow() == 1
    _line(menus_seen[-1], "Copy path").trigger()
    assert clipboard[-1] == str(view.results.row_object(1).path)
    view.deleteLater()


def test_mail_opens_once_and_copies_every_selected_message(
        qapp, store, monkeypatch, menus_seen, clipboard) -> None:
    from app.ui import mail_view

    opened = []
    monkeypatch.setattr(mail_view, "open_row_async",
                        lambda _s, row, reveal=False, **_k: opened.append((row.path, reveal)))
    view = mail_view.MailView(store)
    _filled(view, view.results)
    view.results.setFocus()
    view.results.selectRow(0)
    _press(view.results, Qt.Key.Key_Return)
    _double_click(view.results, view.results.model().index(1, 0))
    assert len(opened) == 2
    view.results.selectAll()
    _right_click(view.results, view.results.model().index(0, 0))
    _line(menus_seen[-1], "Copy path").trigger()
    assert len(clipboard[-1].splitlines()) == view.results.rowCount()
    view.deleteLater()


def _code_results():
    from app.ui.presenter.code import RepoFileRow
    from app.ui.view_options import ViewPreferences
    from app.ui.widgets.code_results import CodeResults

    widget = CodeResults()
    rows = [RepoFileRow(name=f"m{n}.py", size="", kind="py", seen="", path=f"app/m{n}.py",
                        full_path=f"D:/repo/d{n}/m{n}.py", repo="leasha") for n in range(3)]
    widget.show_rows(rows, ViewPreferences())
    widget.resize(800, 400)
    widget.show()
    QApplication.processEvents()
    return widget


def test_code_reveals_and_copies_every_selected_row(qapp, menus_seen, clipboard) -> None:
    widget = _code_results()
    revealed = []
    widget.reveal_requested.connect(revealed.append)
    widget.table.selectAll()
    _right_click(widget.table, widget.table.model().index(0, 0))
    menu = menus_seen[-1]
    assert all(not a.icon().isNull() for a in _lines(menu))
    _line(menu, "Show in folder").trigger()
    _line(menu, "Copy path").trigger()
    # One Explorer window a folder; each of these is in a folder of its own.
    expected = sorted(f"D:/repo/d{n}/m{n}.py" for n in range(3))
    assert sorted(revealed) == expected
    assert sorted(clipboard[-1].splitlines()) == expected
    widget.deleteLater()


def test_one_folder_opens_once_however_many_of_its_files_are_selected(qapp) -> None:
    from app.ui.widgets.file_menu import REVEAL_AT_MOST, _reveal_each

    shown = []
    _reveal_each([("C:/a/1.txt", None), ("C:/a/2.txt", None), ("C:/b/3.txt", None)],
                 lambda path, _row: shown.append(path))
    assert shown == ["C:/a/1.txt", "C:/b/3.txt"]
    shown.clear()
    _reveal_each([(f"C:/f{n}/x.txt", None) for n in range(REVEAL_AT_MOST + 5)],
                 lambda path, _row: shown.append(path))
    assert len(shown) == REVEAL_AT_MOST


def test_code_opens_once_per_double_click(qapp) -> None:
    widget = _code_results()
    opened = []
    widget.open_requested.connect(opened.append)
    _double_click(widget.table, widget.table.model().index(0, 0))
    assert opened == ["D:/repo/d0/m0.py"]
    widget.deleteLater()


def _browse():
    from app.search.folding import Fold
    from app.ui.widgets.browse_table import BrowsePane

    pane = BrowsePane()
    entries = [SimpleNamespace(path=f"C:/p{n}/{n}.jpg", real_path=f"C:/p{n}/{n}.jpg", name=f"{n}.jpg",
                               size_bytes=1, when_ns=1_700_000_000_000_000_000 + n, kind="photo",
                               basis="taken", source_kind="file", file_id=n, reachable=True,
                               folder=f"C:/p{n}", source_name="", ext="jpg")
               for n in range(3)]
    pane.set_folds([Fold(head=entry) for entry in entries])
    pane.resize(800, 400)
    pane.show()
    QApplication.processEvents()
    return pane, entries


def test_browse_hands_every_selected_row_to_the_menu(qapp) -> None:
    pane, entries = _browse()
    asked = []
    pane.menu_requested.connect(lambda fold, where: asked.append(fold))
    pane.results.selectAll()
    _right_click(pane.results, pane.results.model().index(0, 0))
    assert asked and len(pane.selected_folds()) == 3
    pane.deleteLater()


def test_the_timeline_menu_shows_and_copies_every_selected_browse_row(
        qapp, monkeypatch, menus_seen, clipboard) -> None:
    from app.ui.timeline_view import TimelineView

    view = TimelineView(None)
    pane, entries = _browse()
    monkeypatch.setattr(view, "table", pane)
    revealed = []
    view.reveal_requested.connect(revealed.append)
    monkeypatch.setattr(view.stack, "currentIndex", lambda: 1)
    pane.results.selectAll()
    view._show_menu(pane.selected_folds()[0], QPoint(1, 1))
    _line(menus_seen[-1], "Show in folder").trigger()
    _line(menus_seen[-1], "Copy path").trigger()
    assert len(revealed) == 3
    assert sorted(clipboard[-1].splitlines()) == sorted(e.path for e in entries)
    pane.deleteLater()
    view.deleteLater()


def test_the_timeline_list_installs_the_list_keys(qapp) -> None:
    from app.ui.widgets.timeline_list import TimelineList

    timeline = TimelineList()
    assert timeline.property("listKeys") is True
    timeline.deleteLater()


def test_search_results_open_once_per_double_click_and_space_previews(qapp, monkeypatch) -> None:
    from app.ui.results_view import ResultsView

    from tests.unit.test_adoption_scenarios import _result

    page = QWidget()
    toggled = []
    page.view_button = SimpleNamespace(toggle_preview=lambda: toggled.append(1))
    layout = QVBoxLayout(page)
    view = ResultsView()
    layout.addWidget(view)
    page.resize(720, 300)
    page.show()
    view.show_results([_result(), _result(chunk_id=2, file_id=2, path="C:/work/other.pdf")],
                      ["safety"])
    QApplication.processEvents()
    opened = []
    view.opened.connect(opened.append)
    index = view._model.index(0, 0)
    view._list.setCurrentIndex(index)
    _double_click(view._list, index)
    _press(view._list, Qt.Key.Key_Return)
    assert len(opened) == 2
    _press(view._list, Qt.Key.Key_Space)
    assert toggled == [1]
    page.deleteLater()


def test_the_tidy_list_installs_the_list_keys(qapp) -> None:
    from app.ui.widgets.tidy_list_pane import TidyListPane

    pane = TidyListPane()
    assert pane.results.property("listKeys") is True
    pane.deleteLater()


def test_the_offline_list_installs_the_list_keys(qapp) -> None:
    from app.ui.offline_media_view import OfflineMediaView

    view = OfflineMediaView(None)
    assert view.tree.property("listKeys") is True
    view.deleteLater()


def test_every_listed_list_installs_the_behaviour() -> None:
    """A static check that each list asks for the behaviour; the tests above drive it.

    By the module that installs it: the Search list's is in `results_items.py`, the
    Offline list's in `offline_media_rows.py`, each beside the view it serves. The
    side lists are not here: a single click there is the action (it narrows the
    page), so a double-click must not run it a second time, and their Enter is
    already a click (`SideList.keyPressEvent`).
    """
    root = Path(__file__).resolve().parents[2] / "app" / "ui"
    lists = ["widgets/results_items.py", "files_view.py", "mail_view.py", "widgets/code_results.py",
             "widgets/browse_table.py", "widgets/thumbnail_grid.py", "widgets/photo_browser.py",
             "widgets/timeline_list.py", "widgets/tidy_list_pane.py", "widgets/tidy_set_pane.py",
             "widgets/offline_media_rows.py", "widgets/roots_box.py", "widgets/file_types.py",
             "widgets/photo_tagger_page.py"]
    missing = [name for name in lists
               if "list_keys" not in (root / name).read_text(encoding="utf-8")]
    assert not missing, missing


def test_name_people_renames_on_f2_and_forgets_on_delete(qapp, monkeypatch) -> None:
    from app.ui.widgets import photo_tagger_page as page_module

    asked = []
    monkeypatch.setattr(page_module.PhotoTaggerPage, "_rename",
                        lambda self, pile, label: asked.append(("rename", pile)))
    monkeypatch.setattr(page_module.PhotoTaggerPage, "_forget",
                        lambda self, pile, name: asked.append(("forget", pile)))
    monkeypatch.setattr(page_module.PhotoTaggerPage, "reload", lambda self: None)
    page = page_module.PhotoTaggerPage(SimpleNamespace())
    from PySide6.QtWidgets import QListWidgetItem

    item = QListWidgetItem("Person 1 — 3 photos")
    item.setData(page_module.ROLE_PILE_ID, 7)
    page._list.addItem(item)
    page._list.setCurrentItem(item)
    _press(page._list, Qt.Key.Key_F2)
    _press(page._list, Qt.Key.Key_Delete)
    assert asked == [("rename", 7), ("forget", 7)]
    page.deleteLater()


def test_a_menu_key_shortcut_matches_its_list_key(qapp) -> None:
    """The key a line shows is the key the list answers: Ctrl+C on Copy path."""
    assert QKeySequence("Ctrl+C").matches(QKeySequence(QKeySequence.StandardKey.Copy)) \
        != QKeySequence.SequenceMatch.NoMatch


def test_several_paths_with_a_catalogued_drive_among_them_are_resolved_together(
        qapp, monkeypatch) -> None:
    """A selection that includes a file on a catalogued drive copies its real path,
    as one line's Copy path does - all of them resolved first, then put on the
    clipboard at once (`workers.copy_paths_async`)."""
    from app.ui import tasks, workers
    from app.ui.widgets.file_menu import copy_paths

    monkeypatch.setattr(tasks, "resolve_open_path", lambda _store, row: "E:/Holiday/a.jpg")
    drive = SimpleNamespace(path="leasha-volume://3/Holiday/a.jpg", volume_id=3)
    plain = SimpleNamespace(path="C:/docs/b.pdf", volume_id=None)
    monkeypatch.setattr(workers, "_CONTEXT", SimpleNamespace(store=object()), raising=False)
    copy_paths([(drive.path, drive), (plain.path, plain)])
    deadline = time.time() + 5
    while QApplication.clipboard().text() != "E:/Holiday/a.jpg\nC:/docs/b.pdf" and time.time() < deadline:
        _pump()
    assert QApplication.clipboard().text() == "E:/Holiday/a.jpg\nC:/docs/b.pdf"
