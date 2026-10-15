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
    # By the words: Alpha, Beta and More were three rows too, so a count proved nothing (2026-10-14).
    places = side.topLevelItem(1)
    assert [places.child(i).text(0) for i in range(places.childCount())] == ["Alpha", "Beta", "Gamma"]


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

@pytest.mark.slow
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
    assert tree[0] == (r"place:D:\Data", "", r"D:\Data", sum(c for _f, c in counts))
    by_parent: dict = {}
    for key, parent, _folder, total in tree[1:]:
        by_parent.setdefault(parent, []).append(total)
    assert all(v == sorted(v, reverse=True) for v in by_parent.values())          # largest first at each level
    assert len(by_parent[r"place:D:\Data"]) == 30 and max(len(v) for v in by_parent.values()) == 30
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


def _rows_made(side) -> int:
    stack = [side.topLevelItem(i) for i in range(side.topLevelItemCount())]
    made = 0
    while stack:
        item = stack.pop()
        made += 1
        stack.extend(item.child(i) for i in range(item.childCount()))
    return made


@pytest.mark.slow
def test_a_big_places_tree_makes_only_the_rows_that_can_be_seen(qtbot):
    """2026-10-14 06:56, the lag monitor: `set_entries` -> `_rebuild` -> `_item` held the window 5 s
    at start-up. Every folder three levels down was made a row - 19,533 for 18,750 folders - though
    only the roots and their children can be seen until one is opened."""
    from app.ui.presenter.side_entries import files_entries, folder_tree
    from app.ui.widgets.side_list import _ENTRY, SideList

    counts = [(rf"D:\Data\Company {a}\Project {b}\Docs {c}", 3)
              for a in range(30) for b in range(25) for c in range(25)]            # 18,750 folders
    tree = folder_tree([r"D:\Data"], counts)
    entries = files_entries([r"D:\Data"], counts, [], [("pdf", 3)], tree=tree)
    assert len(entries) > 19_000
    side = SideList("filesSide", accessible="t", shown=12)
    qtbot.addWidget(side)
    side.set_entries(entries)

    # All files, two headings, the root, its 30 companies, one kind: not nineteen thousand.
    assert _rows_made(side) < 100, f"{_rows_made(side)} rows made for a list showing about 35"
    root = side._by_key[r"place:D:\Data"]
    assert root.childCount() == 30                                    # the root still shows its arrow and children

    root.setExpanded(True)                                            # opened: the rows below its children appear
    company = root.child(0)
    assert company.childCount() == 25 and company.child(0).childCount() == 0   # Docs wait for Project's opening
    company.setExpanded(True)
    assert company.child(0).childCount() == 25
    company_key = company.data(0, _ENTRY).key

    # A deep folder chosen (a restore) is found, made with the rows above it, from today's entry.
    deep = next(e for e in entries if e.key.endswith(r"Company 29\Project 24\Docs 24"))
    recount = [e if e.key != deep.key else SideEntry(e.key, e.label, section=e.section, icon=e.icon,
                                                       count=77, parent=e.parent, value=e.value, tip=e.tip)
               for e in entries]
    side.set_entries(recount)                                         # a count moved: updated in place
    side.select(deep.key)
    assert side.currentItem() is not None and side.currentItem().text(1) == "77"

    # Opened folders stay open across a rebuild.
    grown = recount + [SideEntry("kind:txt", "Text", section="Kinds", icon="file", count=1)]
    side.set_entries(grown)
    assert side._by_key[r"place:D:\Data"].isExpanded()
    assert side._by_key[company_key].isExpanded()


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
        # None indexed: listed with 0 since 2026-10-14 (the owner asked for the types to be there).
        assert listed["vsdx"] == 2 and listed["mpp"] == 1 and listed["vsd"] == 0
        entries = files_entries([], [], [], side_counts.kind_counts(store, limit=3, always=["vsdx", "mpp"]))
        labels = {e.label for e in entries if e.section == "Kinds"}
        assert {"VSDX - Visio", "MPP - Microsoft Project", "PDF"} <= labels
        assert kind_label("pdf") == "PDF" and kind_label("VSDX") == "VSDX - Visio"


# -- "More... (N)" and the kinds always listed (the owner, 2026-10-14) ----------------------

def _labels_under(side, heading: str) -> list[str]:
    for i in range(side.topLevelItemCount()):
        top = side.topLevelItem(i)
        if top.text(0) == heading:
            return [top.child(n).text(0) for n in range(top.childCount())]
    raise AssertionError(f"no {heading} section")


@pytest.mark.gui
def test_more_shows_every_hidden_entry_when_clicked(qapp, qtbot):
    """**Before:** "More... (2)" under Files > Kinds and "More... (44)" under Photos > Years did nothing.
    The click asked the list to draw itself again, but `set_entries` saw the same entries as last time
    and only restyled the ticks, so the hidden rows never came. A real mouse click, not a call."""
    from PySide6.QtCore import Qt

    from app.ui.widgets.side_list import SideList

    side = SideList("t", accessible="Test", shown=2)
    qtbot.addWidget(side)
    side.resize(300, 400)
    side.show()
    side.set_entries([SideEntry(f"k{n}", f"Kind {n}", section="Kinds", icon="file", count=10 - n)
                      for n in range(5)])
    assert _labels_under(side, "Kinds") == ["Kind 0", "Kind 1", "More... (3)"]
    kinds = side.topLevelItem(0)
    more = kinds.child(kinds.childCount() - 1)
    assert not more.icon(0).isNull(), "a side list line carries an icon - More too"
    qtbot.mouseClick(side.viewport(), Qt.MouseButton.LeftButton,
                     pos=side.visualItemRect(more).center())
    assert _labels_under(side, "Kinds") == [f"Kind {n}" for n in range(5)]
    side.set_entries(side.entries())                              # a redraw keeps them shown
    assert _labels_under(side, "Kinds") == [f"Kind {n}" for n in range(5)]


@pytest.mark.gui
def test_more_from_the_keyboard_and_on_the_photos_years(qapp, qtbot):
    from PySide6.QtCore import Qt

    from app.ui.widgets.photo_sidebar import SHOWN, PhotoSidebar

    side = PhotoSidebar()
    qtbot.addWidget(side)
    side.fill({"years": {year: 3 for year in range(1990, 1990 + SHOWN + 5)}}, 0)
    years = _labels_under(side, "Years")
    assert years[-1] == "More... (5)" and len(years) == SHOWN + 1
    heading = next(side.topLevelItem(i) for i in range(side.topLevelItemCount())
                   if side.topLevelItem(i).text(0) == "Years")
    side.setCurrentItem(heading.child(heading.childCount() - 1))
    qtbot.keyClick(side, Qt.Key.Key_Return)
    assert len(_labels_under(side, "Years")) == SHOWN + 5 and "More..." not in " ".join(_labels_under(side, "Years"))


@pytest.mark.gui
def test_the_kinds_always_listed_are_shown_past_the_cap_with_their_count_even_none(qapp, tmp_path):
    """**Before:** Visio and Project were added after the twelve commonest kinds, and the Files list shows
    twelve to a section - so they were exactly the "More... (2)" the owner saw; `.vsd`, with no plain file
    in the index, was dropped altogether. Now each type named in `FILE_KINDS_ALWAYS` is on the list, with
    its count, 0 if none, and never behind More."""
    from app.storage import side_counts
    from app.storage.sqlite_store import SqliteStore
    from app.ui.widgets.side_list import SideList

    with SqliteStore(tmp_path / "k.db") as store:
        kinds = [f"x{n:02d}" for n in range(12)]
        n = 0
        for rank, ext in enumerate(kinds):
            for _ in range(30 - rank):
                n += 1
                store.upsert_file(path=f"/d/f{n}.{ext}", size_bytes=1, mtime_ns=1, status="INDEXED",
                                  source_kind="file")
        for ext in ("vsdx", "vsdx", "mpp"):
            n += 1
            store.upsert_file(path=f"/d/f{n}.{ext}", size_bytes=1, mtime_ns=1, status="INDEXED",
                              source_kind="file")
        always = ("vsdx", "vsd", "mpp")
        counted = side_counts.kind_counts(store, limit=12, always=always)
    assert dict(counted)["vsd"] == 0 and dict(counted)["vsdx"] == 2 and dict(counted)["mpp"] == 1
    side = SideList("filesSide", accessible="Kinds", shown=12)
    side.set_entries(files_entries([], [], [], counted, always=always))
    shown = _labels_under(side, "Kinds")
    assert not any(label.startswith("More...") for label in shown), shown
    for label in ("VSDX - Visio", "VSD - Visio", "MPP - Microsoft Project"):
        assert label in shown, shown
    vsd = next(e for e in side.entries() if e.key == "kind:vsd")
    assert vsd.count == 0
