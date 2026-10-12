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


# -- speed: the Files list froze for seconds at a click (2026-10-11, the owner) -----------------

def test_the_folder_tree_of_a_big_index_builds_at_once_and_in_the_right_order():
    import time

    from app.ui.presenter.side_entries import folder_tree

    counts = [(rf"D:\Data\Company {a}\Project {b}\Docs {c}", 3)
              for a in range(30) for b in range(25) for c in range(25)]            # 18,750 folders
    counts += [(r"D:\Data", 5), (r"D:\Data\Company 0", 2)]
    began = time.perf_counter()
    tree = folder_tree([r"D:\Data"], counts)
    assert time.perf_counter() - began < 1.5                                        # it was 7 s at this size
    assert tree[0] == ("place:D:\Data", "", "D:\Data", sum(c for _f, c in counts))
    by_parent: dict = {}
    for key, parent, _folder, total in tree[1:]:
        by_parent.setdefault(parent, []).append(total)
    assert all(v == sorted(v, reverse=True) for v in by_parent.values())          # largest first at each level
    assert len(by_parent["place:D:\Data"]) == 30 and max(len(v) for v in by_parent.values()) == 30
    assert tree.index(next(r for r in tree if r[2] == r"D:\Data\Company 0")) < tree.index(
        next(r for r in tree if r[2] == r"D:\Data\Company 0\Project 0"))               # parents before children


def test_a_tick_change_restyles_the_rows_in_place_and_keeps_folders_open(qtbot):
    from app.ui.presenter.side_entry import SideEntry
    from app.ui.widgets.side_list import SideList

    def make(ticked):
        return [SideEntry("a", "A", section="Places", icon="folder", count=3, value=("path", "A"), checked=ticked == "a"),
                SideEntry("a/b", "B", section="Places", icon="folder", count=2, parent="a", value=("path", "A/B"),
                          checked=ticked == "b")]

    side = SideList("t", accessible="t")
    qtbot.addWidget(side)
    side.set_entries(make(""))
    row_a, row_b = side._by_key["a"], side._by_key["a/b"]
    row_a.setExpanded(True)
    side.set_entries(make("b"))                                              # only the tick moved
    assert side._by_key["a"] is row_a and side._by_key["a/b"] is row_b      # the same rows: nothing rebuilt
    assert row_a.isExpanded() and row_b.font(0).bold() and not row_a.font(0).bold()
    from PySide6.QtCore import Qt

    assert "chosen" in row_b.data(0, Qt.ItemDataRole.AccessibleTextRole)
    changed = make("b")
    changed[1] = SideEntry("a/b", "B", section="Places", icon="folder", count=9, parent="a", value=("path", "A/B"),
                           checked=True)                                    # a count moved
    side.set_entries(changed)
    assert side._by_key["a"] is row_a and row_b.text(1) == "9"                # updated where it stands: no flash
    assert row_a.isExpanded()
    grown = changed + [SideEntry("a/c", "C", section="Places", icon="folder", count=1, parent="a",
                                 value=("path", "A/C"))]                      # a row added: the structure changed
    side.set_entries(grown)
    assert side._by_key["a"] is not row_a and side._by_key["a"].isExpanded()   # rebuilt, and still open


def test_a_tree_follows_the_index_only_while_it_is_on_screen_and_only_when_it_moved(qtbot):
    """"Make sure this is on all trees" (the owner, 2026-10-12): Mail, Files, Code, Browse and Photos."""
    from PySide6.QtWidgets import QWidget

    from app.ui.widgets.side_panel import follow_index

    class Store:
        generation = 1

    view = QWidget()
    view._store = Store()
    qtbot.addWidget(view)
    calls = []
    clock = follow_index(view, lambda: calls.append(1), interval_ms=10_000)
    tick = clock.timeout
    clock.stop()
    from app.ui.widgets import side_panel

    seen = {}
    assert side_panel.moved(view._store, seen) and not side_panel.moved(view._store, seen)
    view._store.generation = 2
    assert side_panel.moved(view._store, seen)
    assert tick is not None and calls == []                      # hidden: a tick does nothing, whatever the counter


def test_the_files_kinds_list_also_lists_the_types_the_person_always_wants(tmp_path):
    """"Visio and Microsoft Project file types missing" (the owner, 2026-10-12): the commonest twelve left them out."""
    from app.storage import side_counts
    from app.storage.sqlite_store import SqliteStore
    from app.ui.presenter.side_entries import files_entries, kind_label

    with SqliteStore(tmp_path / "k.db") as store:
        for n, ext in enumerate(["pdf"] * 6 + ["docx"] * 5 + ["jpg"] * 4 + ["vsdx"] * 2 + ["mpp"]):
            store.upsert_file(path=f"/d/f{n}.{ext}", size_bytes=1, mtime_ns=1, status="INDEXED", source_kind="file")
        plain = dict(side_counts.kind_counts(store, limit=3))
        assert "vsdx" not in plain and "mpp" not in plain                      # the three commonest only
        listed = dict(side_counts.kind_counts(store, limit=3, always=["vsdx", "mpp", "vsd"]))
        assert listed["vsdx"] == 2 and listed["mpp"] == 1 and "vsd" not in listed   # none indexed: not listed
        entries = files_entries([], [], [], side_counts.kind_counts(store, limit=3, always=["vsdx", "mpp"]))
        labels = {e.label for e in entries if e.section == "Kinds"}
        assert {"VSDX - Visio", "MPP - Microsoft Project", "PDF"} <= labels
        assert kind_label("pdf") == "PDF" and kind_label("VSDX") == "VSDX - Visio"
