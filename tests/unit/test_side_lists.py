r"""Order 1p: one left-hand list for every page, drawn the Contacts way.

Layer: L2 and L5

2026-10-11, the owner: "i like the look of contacts panel with icons etc can that be
standardised ?" - then "yes write the order and build it for all".
"""

from __future__ import annotations

import os

import pytest

from app.storage import side_counts
from app.storage.sqlite_store import SqliteStore
from app.ui.presenter.side_entries import (browse_entries, clear_from_box, code_entries,
                                           files_entries, folder_tree)
from app.ui.presenter.side_entry import SideEntry


# -- the counts and the entries (no Qt) ---------------------------------------------

@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "s.db") as store:
        for path, ext in ((r"D:\Data\School\a.pdf", "pdf"), (r"D:\Data\School\Maths\b.pdf", "pdf"),
                          (r"D:\Data\Photos\c.jpg", "jpg"), (r"D:\Data\notes.txt", "txt"),
                          (r"D:\Elsewhere\d.txt", "txt")):
            store.upsert_file(path, parent_dir=path.rsplit("\\", 1)[0], ext=ext, size_bytes=1,
                              mtime_ns=1, status="INDEXED", source_kind="file")
        store.set_state("ui:roots", "D:/Data")
        yield store


def test_the_counts_are_one_grouped_read_each(store):
    assert side_counts.saved_roots(store) == ["D:/Data"]
    assert dict(side_counts.folder_counts(store))[r"D:\Data\School"] == 1
    assert side_counts.kind_counts(store)[:2] == [("pdf", 2), ("txt", 2)]
    assert side_counts.drive_counts(store) == []
    assert side_counts.repo_counts(store) == []


def test_places_are_a_tree_of_the_indexed_folders_with_counts_that_add_up(store):
    tree = folder_tree(side_counts.saved_roots(store), side_counts.folder_counts(store))
    by_key = {key: (parent, count) for key, parent, _folder, count in tree}
    assert by_key[r"place:D:\Data"] == ("", 4)                    # Elsewhere is not indexed
    assert by_key[r"place:D:\Data\School"] == (r"place:D:\Data", 2)
    assert by_key[r"place:D:\Data\School\Maths"] == (r"place:D:\Data\School", 1)
    keys = [key for key, *_rest in tree]
    assert keys.index(r"place:D:\Data\School") < keys.index(r"place:D:\Data\School\Maths")


def test_files_entries_tick_what_the_box_holds_and_all_clears_it(store):
    entries = files_entries(side_counts.saved_roots(store), side_counts.folder_counts(store),
                            [("Old WD", "offline", 3)], side_counts.kind_counts(store),
                            box='type:pdf path:"D:\\Data\\School"')
    by_key = {e.key: e for e in entries}
    assert by_key["all"].kind == "all" and by_key["all"].count == 4
    assert by_key["kind:pdf"].checked and not by_key["kind:txt"].checked
    assert by_key[r"place:D:\Data\School"].checked
    assert by_key["drive:Old WD"].icon == "hard-drive" and "not plugged in" in by_key["drive:Old WD"].tip
    assert {e.section for e in entries} == {"", "Places", "Drives", "Kinds"}
    assert clear_from_box('boiler type:pdf on:"Old WD" /newest', ("type", "on")) == "boiler /newest"


def test_code_and_browse_entries():
    entries = code_entries([("leasha", r"D:\Local\GitHub\Leasha", 900)], [("py", 700)], box="repo:leasha")
    assert [e.section for e in entries] == ["", "Repositories", "Languages"]
    assert entries[1].checked and entries[1].value == ("repo", "leasha")

    class Overview:
        years = ((2015, 7), (2016, 1))

        @staticmethod
        def months_of(year):
            return {6: 7} if year == 2015 else {1: 1}

    shown = browse_entries(Overview(), "photos", [("Everything", "everything"), ("Photos", "photos")])
    keys = [e.key for e in shown]
    assert keys[:4] == ["year:2016", "month:2016-01", "year:2015", "month:2015-06"]
    june = next(e for e in shown if e.key == "month:2015-06")
    assert june.parent == "year:2015" and june.label == "June" and june.count == 7
    assert next(e for e in shown if e.key == "show:photos").checked


# -- the widget -------------------------------------------------------------------------

@pytest.fixture
def qapp():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.mark.gui
def test_the_widget_draws_sections_icons_counts_and_nesting(qapp):
    from app.ui.widgets.side_list import SideList

    side = SideList("t", accessible="Test", shown=2)
    side.set_entries([
        SideEntry("all", "All", icon="folder", kind="all"),
        SideEntry("a", "Alpha", section="Places", icon="folder", count=1200),
        SideEntry("a1", "Inside", parent="a", icon="folder", count=3),
        SideEntry("b", "Beta", section="Places", icon="folder", count=2),
        SideEntry("c", "Gamma", section="Places", icon="folder", count=1),
        SideEntry("d", "Duplicates", section="Tools", icon="tag", kind="door"),
    ])
    tops = [side.topLevelItem(i).text(0) for i in range(side.topLevelItemCount())]
    assert tops == ["All", "Places", "Tools"]
    places = side.topLevelItem(1)
    assert places.font(0).bold()
    alpha = places.child(0)
    assert (alpha.text(0), alpha.text(1)) == ("Alpha", "1,200") and not alpha.icon(0).isNull()
    assert alpha.child(0).text(0) == "Inside"
    assert places.child(places.childCount() - 1).text(0) == "More... (1)"   # Gamma waits

    heard, doors = [], []
    side.picked.connect(heard.append)
    side.door.connect(doors.append)
    side._clicked(alpha, 0)
    side._clicked(side.topLevelItem(2).child(0), 0)
    assert [e.key for e in heard] == ["a"] and [e.key for e in doors] == ["d"]
    assert side.current_key() == "a", "a door moved the highlight"
    side._clicked(places.child(places.childCount() - 1), 0)               # More...
    assert side.topLevelItem(1).childCount() == 3


@pytest.mark.gui
def test_a_right_click_offers_the_entrys_own_actions(qapp):
    from PySide6.QtCore import QPoint

    from app.ui.widgets.side_list import SideList

    side = SideList("t", accessible="Test")
    side.set_entries([SideEntry("g", "Family", section="Groups", icon="users",
                                menu=(("Rename this group...", "pencil", "rename"),))])
    side.resize(240, 200)
    side.show()
    qapp.processEvents()
    chosen = []
    side.menu_chosen.connect(lambda entry, action: chosen.append((entry.key, action)))
    rect = side.visualItemRect(side.topLevelItem(0).child(0))
    side._menu(QPoint(rect.center()))
    [action] = side.last_menu.actions()
    action.trigger()
    assert chosen == [("g", "rename")]
    side.last_menu.close()


# -- every page, in the window ------------------------------------------------------------

@pytest.mark.gui
def test_every_list_page_has_its_list_its_switch_and_its_view_entry(gui_mainwindow):
    from tests.unit.conftest import gui_pump

    app, window, _store, _engine = gui_mainwindow
    gui_pump(app, 6)
    for view, words in ((window.files_view, "Places"), (window.code_view, "Repositories"),
                        (window.timeline_view, "Years and months"), (window.mail_view, "Folders")):
        assert getattr(view, "side_list", None) is not None, type(view).__name__
        assert "side" in view.toggles and view.toggles["side"].text() == words
    for view in (window.files_view, window.code_view, window.mail_view):
        menu = view.view_button.menu_for(window)
        assert any(a.text() == view.toggles["side"].text() for a in menu.actions()), type(view).__name__


@pytest.mark.gui
def test_a_files_click_writes_into_the_box_and_all_takes_it_away(gui_mainwindow):
    from app.ui.presenter.side_entry import SideEntry
    from tests.unit.conftest import gui_pump

    app, window, _store, _engine = gui_mainwindow
    view = window.files_view
    view.input.setText("boiler")
    side = view.side_list
    side.picked.emit(SideEntry("kind:pdf", "PDF", section="Kinds", value=("type", "pdf")))
    assert view.input.text() == "boiler type:pdf"
    side.picked.emit(SideEntry("all", "All files", kind="all"))
    assert view.input.text() == "boiler"
    view.toggles["side"].click()
    assert side.isHidden()
    view.show_side(True, remember=False)
    view.input.setText("")
    gui_pump(app, 2)


@pytest.mark.gui
def test_a_browse_click_opens_that_month(gui_mainwindow, monkeypatch):
    from app.ui.presenter.side_entry import SideEntry

    _app, window, _store, _engine = gui_mainwindow
    view = window.timeline_view
    opened = []
    monkeypatch.setattr(view, "browse", lambda period: opened.append(period))
    view.side_list.picked.emit(SideEntry("month:2015-06", "June", value=("month", (2015, 6))))
    from app.reports.timeline import Period

    assert opened == [Period.month(2015, 6)]
