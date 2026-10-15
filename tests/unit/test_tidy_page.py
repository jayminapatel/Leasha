r"""Order 1q section 2 - the Tidy page: its list, its tabs, its marks, its one question.

Layer: L5

A real store and real files; the Recycle Bin is `test_tidy_engine.FakeBin`, put in
place of the real one, so no test here sends anything to the owner's bin.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QThreadPool  # noqa: E402

from app.storage.sqlite_store import SqliteStore  # noqa: E402
from tests.unit.test_tidy_engine import FakeBin, real  # noqa: E402

NS = 1_000_000_000


@pytest.fixture
def page(qtbot, tmp_path, monkeypatch):
    from app.core.osbridge import recycle
    from app.ui.tidy_view import TidyView

    bin = FakeBin(tmp_path / "bin")
    monkeypatch.setattr(recycle, "RecycleBin", lambda: bin)
    with SqliteStore(tmp_path / "index.db") as store:
        kept = real(store, tmp_path / "Documents", "r.pdf", b"report", mtime=5 * NS)
        spare = real(store, tmp_path / "Downloads", "r.pdf", b"report", mtime=1 * NS)
        lone = real(store, tmp_path / "Documents", "lone.txt", b"only here")
        view = TidyView(store)
        qtbot.addWidget(view)
        view.refresh()
        qtbot.waitUntil(lambda: view._snapshot is not None, timeout=15000)
        yield view, store, {"kept": kept, "spare": spare, "lone": lone}, bin
        QThreadPool.globalInstance().waitForDone(10_000)


def wait_for_workers(qtbot):
    QThreadPool.globalInstance().waitForDone(10_000)
    qtbot.wait(50)


@pytest.mark.slow
def test_the_list_has_the_five_sections_with_icons_and_counts(page):
    view, _store, _files, _bin = page
    entries = view.list.entries()
    assert [e.section for e in entries] == ["Overview", "Clean up", "Clean up", "Clean up", "Clean up",
                                            "Keep safe", "Report", "History"]
    assert all(e.icon for e in entries)
    copies = view.list.entry("copies")
    assert copies.count == 1 and "gives back" in copies.tip


def test_a_set_has_five_tabs_each_with_an_icon(page):
    view, *_ = page
    view.show_kind("copies")
    view.retint({"text_dim": "#777777"})
    tabs = view.sets.tabs
    segments = [tabs.button(i) for i in range(tabs.count())]
    assert [b.text() for b in segments] == ["Compare", "Copies", "Details", "Keep rule", "List"]
    assert all(not b.icon().isNull() for b in segments)
    tabs.setCurrentIndex(1)
    assert view.sets.views.currentWidget() is view.sets.copies


def test_leasha_picks_marks_the_downloads_copy_and_the_queue_says_so(page):
    view, _store, files, _bin = page
    view.show_kind("copies")
    assert view.marks.marked == {str(files["spare"])}
    assert view.queue.text().startswith("Marked: 1") and view.review.isEnabled()
    assert "keeping r.pdf" in view.sets.why.text()


def test_ill_choose_marks_nothing(page):
    view, *_ = page
    view._rule_chosen("manual")
    view.show_kind("copies")
    assert view.marks.marked == set() and not view.review.isEnabled()
    assert "Nothing is marked" in view.sets.why.text()


def test_keep_this_one_swaps_what_stays(page):
    view, _store, files, _bin = page
    view.show_kind("copies")
    view._keep_only(str(files["spare"]))
    assert view.marks.marked == {str(files["kept"])}


def test_nothing_moves_without_a_yes(page, qtbot):
    view, _store, files, bin = page
    view.show_kind("copies")
    view.ask = lambda *_a: False
    view._review()
    wait_for_workers(qtbot)
    assert files["spare"].exists() and bin.sent == []


@pytest.mark.slow
def test_a_yes_moves_the_marked_copy_and_history_can_put_it_back(page, qtbot):
    view, _store, files, bin = page
    view.show_kind("copies")
    questions = []
    view.ask = lambda title, text: questions.append(title) or True
    view._review()
    qtbot.waitUntil(lambda: not files["spare"].exists(), timeout=10000)
    qtbot.waitUntil(lambda: view.history.batches.count() == 1, timeout=15000)
    assert questions == ["Move to the Recycle Bin"] and files["kept"].exists()
    assert "moved to the Recycle Bin" in view.progress.text() or view.history.batches.count() == 1
    view.history.batches.setCurrentRow(0)
    assert view.history.button.isEnabled()
    view.history._put_back()
    qtbot.waitUntil(lambda: files["spare"].exists(), timeout=10000)


def test_find_copies_of_this_opens_the_files_set_with_nothing_marked(page, qtbot):
    view, _store, files, _bin = page
    view.open_file(str(files["kept"]))
    qtbot.waitUntil(lambda: view._kind == "file", timeout=10000)
    assert view.sets.current() is not None and len(view.sets.current().files) == 2
    assert view.marks.marked == set()
    view._loaded(view._snapshot)                     # a refresh landing keeps the file's sets
    assert view._kind == "file" and len(view.sets.current().files) == 2


def test_the_files_menu_offers_find_copies_for_a_file_on_disk(qtbot, tmp_path):
    from PySide6.QtWidgets import QWidget

    from app.ui.widgets.file_menu import FileActions, build_menu

    on_disk = tmp_path / "a.txt"
    on_disk.write_text("x")
    parent = QWidget()
    qtbot.addWidget(parent)
    asked = []
    menu = build_menu(parent, str(on_disk), FileActions(find_copies=lambda: asked.append(1)))
    (action,) = [a for a in menu.actions() if a.text() == "Find copies of this…"]
    action.trigger()
    assert asked == [1]
    archived = build_menu(parent, "pst://box/2108548", FileActions(find_copies=lambda: None))
    assert "Find copies of this…" not in [a.text() for a in archived.actions()]


def test_a_saved_rail_order_naming_reports_keeps_tidys_place():
    from app.ui.widgets.rail_order import DEFAULT_ORDER, RENAMED

    assert "Tidy" in DEFAULT_ORDER and "Reports" not in DEFAULT_ORDER
    assert RENAMED["Reports"] == "Tidy"


def test_the_page_moves_nothing_itself():
    from pathlib import Path

    from tests.unit.test_reports_read_only import source_offences

    root = Path(__file__).resolve().parents[2]
    for name in ("app/ui/tidy_view.py", "app/ui/widgets/tidy_set_pane.py", "app/ui/widgets/tidy_parts.py",
                 "app/ui/presenter/tidy.py"):
        assert source_offences(root / name) == [], name
    assert os.path.isfile(root / "app" / "tidy" / "remove.py")


def test_the_marks_are_plain_python(tmp_path):
    """`TidyMarks` holds the page's logic, so it is tested without a window."""
    from app.tidy.find import TidyFile, TidySet
    from app.ui.presenter.tidy_marks import TidyMarks

    files = (TidyFile(1, r"C:\Docs\r.pdf", 9, 5, "h"), TidyFile(2, r"C:\Downloads\r.pdf", 9, 1, "h"))
    copies = TidySet("copies:h", "copies", files)
    marks = TidyMarks("leasha")
    assert marks.apply_rule(copies) and marks.marked == {r"C:\Downloads\r.pdf"}
    assert not marks.apply_rule(copies), "a settled set is never marked twice"
    marks.keep_only(copies, r"C:\Downloads\r.pdf")
    assert marks.marked == {r"C:\Docs\r.pdf"}
    marks.choose_rule("manual", copies)
    assert marks.marked == set() and marks.rule == "manual"
    photos = TidySet("similar:1", "similar", files)
    leasha = TidyMarks("leasha")
    assert leasha.needs_quality(photos) and not leasha.apply_rule(photos)
    leasha.set_quality("similar:1", {r"C:\Docs\r.pdf": (100, 1.0), r"C:\Downloads\r.pdf": (400, 1.0)})
    assert leasha.apply_rule(photos) and leasha.marked == {r"C:\Docs\r.pdf"}
    assert TidyMarks("nonsense").rule == "leasha"


# -- the List tab: every file of every set in the table every other list page has ----------------------

def _list(view):
    view.show_kind("copies")
    view.sets.tabs.setCurrentIndex(4)
    return view.sets.list_pane


def test_the_list_tab_shows_every_file_with_the_standard_table_and_view_menu(page):
    view, _store, files, _bin = page
    pane = _list(view)
    assert view.sets.views.currentWidget() is pane
    heads = [pane.results.horizontalHeaderItem(i).text() for i in range(pane.results.columnCount() - 1)]
    assert heads == ["Can go", "Name", "Folder", "Size", "Modified", "Set", "Type"]
    assert pane.results.rowCount() == 2                                   # both copies, one set
    names = {pane.results.item(r, 1).text() for r in range(2)}
    assert names == {"r.pdf"}
    assert pane.view_button is not None and pane.view_button.text() == "View"      # columns, density, text size
    assert pane.preview is not None and pane.results.isSortingEnabled()
    assert "2 files in 1 sets" in pane.summary.text() and "1 marked to go" in pane.summary.text()


@pytest.mark.slow
def test_a_tick_in_the_list_marks_that_file_in_its_own_set(page):
    from PySide6.QtCore import Qt

    view, _store, files, _bin = page
    view._rule_chosen("manual")
    pane = _list(view)
    assert view.marks.marked == set()
    row = next(r for r in range(pane.results.rowCount())
               if pane.results.row_object(r).path == str(files["spare"]))
    pane.results.item(row, 0).setCheckState(Qt.CheckState.Checked)
    assert view.marks.marked == {str(files["spare"])}
    assert view.queue.text().startswith("Marked: 1") and view.review.isEnabled()
    assert "1 marked to go" in pane.summary.text()
    pane.results.item(row, 0).setCheckState(Qt.CheckState.Unchecked)
    assert view.marks.marked == set()


@pytest.mark.slow
def test_the_list_follows_marks_made_in_the_other_tabs_and_keeps_one_when_asked(page):
    view, _store, files, _bin = page
    pane = _list(view)
    marked = {pane.results.row_object(r).path: pane.results.item(r, 0).checkState().value
              for r in range(pane.results.rowCount())}
    assert marked[str(files["spare"])] == 2 and marked[str(files["kept"])] == 0       # Leasha's rule: Downloads goes
    pane.keep_only.emit(str(files["spare"]))                                           # "Keep this one" on the other
    assert view.marks.marked == {str(files["kept"])}
    now = {pane.results.row_object(r).path: pane.results.item(r, 0).checkState().value
           for r in range(pane.results.rowCount())}
    assert now[str(files["kept"])] == 2 and now[str(files["spare"])] == 0


@pytest.mark.slow
def test_the_list_filters_by_name_or_folder_and_says_how_many(page):
    view, *_ = page
    pane = _list(view)
    pane.filter.setText("downloads")
    shown = [r for r in range(pane.results.rowCount()) if not pane.results.isRowHidden(r)]
    assert len(shown) == 1 and "Downloads" in pane.results.item(shown[0], 2).text()
    assert pane.summary.text().startswith("Showing 1 of 2 files")
    pane.filter.setText("")
    assert not any(pane.results.isRowHidden(r) for r in range(pane.results.rowCount()))


@pytest.mark.slow
def test_the_list_sorts_sizes_as_numbers_and_columns_fit_what_they_hold(page):
    view, *_ = page
    pane = _list(view)
    pane.results.sortItems(3)
    assert pane.results.rowCount() == 2
    widths = [pane.results.columnWidth(i) for i in range(pane.results.columnCount() - 1)
              if not pane.results.isColumnHidden(i)]
    assert all(w > 0 for w in widths) and len(set(widths)) > 1             # fitted, not one width for all


@pytest.mark.slow
def test_large_files_and_leftovers_open_on_the_list(page):
    view, *_ = page
    view.show_kind("large")
    assert view.sets.views.currentWidget() is view.sets.list_pane
