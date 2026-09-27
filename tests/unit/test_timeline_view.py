r"""Order 202626270602 (0n) section 4 - the Life Timeline window, driven like a person.

Layer: L5

pytest-qt scenarios (the order's own 5b convention: every acceptance sentence
gets a scenario that presses the keys). One `TimelineView` on a real store, the
real workers, offscreen:

- year -> month, and any two dates, both reach the same items
- "June 2015" holds the photograph, the letter, the offline drive's item (with
  its badge) and the message, in the order they happened
- a month of hundreds of photographs arrives a page at a time as the list is
  scrolled, never all at once, and the window keeps answering while it does
- a burst is one row that can be taken apart; an unreadable or absent picture
  is a placeholder, never an error
- what somebody types that is not a date is said plainly and changes nothing
"""

from __future__ import annotations

import os
import time
import pytest

pytest.importorskip("PyQt6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt, QTimer                      # noqa: E402

from app.reports import timeline_words as words                   # noqa: E402
from app.reports.timeline import Period                            # noqa: E402
from app.ui import timeline_view as tv_module                      # noqa: E402
from app.ui.presenter.timeline import photo_tip, row_text          # noqa: E402
from app.ui.timeline_view import TimelineView                      # noqa: E402
from app.storage.sqlite_store import SqliteStore                   # noqa: E402
from tests.unit.timeline_env import NS, add_file, add_mail, june_2015, noon  # noqa: E402

pytestmark = pytest.mark.gui


def double_click(widget, pos) -> None:
    """A real double-click event. `QTest.mouseDClick` delivers nothing to a
    list on the offscreen platform (checked against a plain `QListWidget`), so
    the press is sent the ordinary way and the double-click event is sent by hand."""
    from PyQt6.QtCore import QEvent, QPointF
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication

    QTest.mouseClick(widget, Qt.MouseButton.LeftButton, pos=pos)
    QApplication.sendEvent(widget, QMouseEvent(
        QEvent.Type.MouseButtonDblClick, QPointF(pos), QPointF(widget.mapToGlobal(pos)),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))


def loaded(view: TimelineView) -> bool:
    return not view._loading and view.list.block_count() > 0


def kinds(view: TimelineView) -> list[str]:
    return [view.list.block_at(row).kind for row in range(view.list.block_count())]


def entries(view: TimelineView) -> list:
    out = []
    for row in range(view.list.block_count()):
        for fold in view.list.block_at(row).folds:
            out.append(fold.head)
            out.extend(fold.older)
    return out


@pytest.fixture
def june(qtbot, tmp_path):
    store, ids = june_2015(tmp_path)
    view = TimelineView(store)
    qtbot.addWidget(view)
    view.resize(900, 640)
    view.show()
    view.refresh()
    qtbot.waitUntil(lambda: view._overview is not None, timeout=8000)
    yield view, ids, store
    from PyQt6.QtCore import QThreadPool
    QThreadPool.globalInstance().waitForDone(5000)
    store.close()


# ---------------------------------------------------------------------------
# Year -> month, and any two dates
# ---------------------------------------------------------------------------

def test_the_picker_lists_the_years_that_have_anything_and_says_how_much(june):
    view, _ids, _store = june
    labels = [view.picker.year_box.itemText(i) for i in range(view.picker.year_box.count())]
    assert [label.split()[0] for label in labels] == ["2015", "2019"]
    assert "(5)" in labels[0] and "(1)" in labels[1]
    assert view.summary.text() == "6 items from 2015 to 2019."


def test_choosing_a_year_shows_the_whole_year_and_only_months_with_something_are_offered(qtbot, june):
    view, ids, _store = june
    view.picker.year_box.setCurrentIndex(0)                       # 2015
    qtbot.waitUntil(lambda: loaded(view), timeout=8000)
    assert view.heading.text() == "2015"
    assert {e.file_id for e in entries(view)} == {ids["photo"], ids["offline"], ids["letter"],
                                                  ids["mail"], ids["july"]}
    enabled = [b.text() for b in view.picker.month_buttons if b.isEnabled()]
    assert enabled == ["Whole year", "Jun", "Jul"]
    assert "4 items" in view.picker.month_buttons[6].toolTip()


def test_a_month_shows_everything_from_it_oldest_first_across_every_source(qtbot, june):
    r"""The order's acceptance sentence: "June 2015" is a place you can go."""
    view, ids, _store = june
    view.picker.year_box.setCurrentIndex(0)
    qtbot.waitUntil(lambda: loaded(view), timeout=8000)
    view.picker.month_buttons[6].click()                          # Jun
    qtbot.waitUntil(lambda: view.heading.text() == "June 2015" and loaded(view)
                    and len(entries(view)) == 4, timeout=8000)
    assert [e.file_id for e in entries(view)] == [ids["photo"], ids["offline"], ids["letter"], ids["mail"]]
    # photographs are thumbnails in bands, the rest are rows, days are headings:
    assert kinds(view) == ["day", "band", "day", "band", "day", "row", "day", "row"]
    assert view.status.text() == "4 items - that is everything from this period."


def test_any_two_dates_reach_the_same_items_as_the_month(qtbot, june):
    view, ids, _store = june
    view.picker.range_from.setText("2015-06-01")
    view.picker.range_to.setText("2015-06-30")
    qtbot.mouseClick(view.picker.range_go, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: len(entries(view)) == 4, timeout=8000)
    assert {e.file_id for e in entries(view)} == {ids["photo"], ids["offline"], ids["letter"], ids["mail"]}


def test_a_range_can_be_open_ended(qtbot, june):
    view, ids, _store = june
    view.picker.range_from.setText("2019")
    qtbot.mouseClick(view.picker.range_go, Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: loaded(view), timeout=8000)
    assert [e.file_id for e in entries(view)] == [ids["later_photo"]]


def test_something_that_is_not_a_date_is_said_plainly_and_changes_nothing(qtbot, june):
    view, _ids, _store = june
    view.picker.year_box.setCurrentIndex(0)
    qtbot.waitUntil(lambda: loaded(view), timeout=8000)
    before = kinds(view)
    view.picker.range_from.setText("banana")
    qtbot.mouseClick(view.picker.range_go, Qt.MouseButton.LeftButton)
    assert view.status.text() == words.BAD_DATE
    assert kinds(view) == before and view.heading.text() == "2015"


def test_a_month_with_nothing_in_it_says_so_as_a_fact_about_the_records(qtbot, june):
    view, _ids, _store = june
    view.browse(Period.month(1999, 1))
    qtbot.waitUntil(lambda: "Nothing in Leasha's records" in view.status.text(), timeout=8000)
    assert view.status.text() == "Nothing in Leasha's records is dated January 1999."
    assert view.list.block_count() == 0


def test_the_kind_box_narrows_the_month_and_is_remembered(qtbot, june):
    view, ids, store = june
    view.browse(Period.month(2015, 6))
    qtbot.waitUntil(lambda: len(entries(view)) == 4, timeout=8000)
    view.picker.kind_box.setCurrentIndex(view.picker.kind_box.findData("mail"))
    qtbot.waitUntil(lambda: [e.file_id for e in entries(view)] == [ids["mail"]], timeout=8000)
    # Queued on the ordered state writer since the page-switch fix (bug 3a).
    from app.ui.state_writes import pool
    assert pool().waitForDone(5000)
    assert store.get_state("ui:timeline_kind", None) == "mail"
    assert TimelineView(store).picker.kind() == "mail"            # a fresh window starts there


# ---------------------------------------------------------------------------
# Offline items, folding, opening
# ---------------------------------------------------------------------------

def test_an_item_on_a_drive_in_a_drawer_is_there_with_its_badge_and_says_it_is_not_plugged_in(qtbot, june):
    view, ids, _store = june
    view.browse(Period.month(2015, 6))
    qtbot.waitUntil(lambda: len(entries(view)) == 4, timeout=8000)
    by_id = {e.file_id: e for e in entries(view)}
    offline = by_id[ids["offline"]]
    band = next(view.list.block_at(r) for r in range(view.list.block_count())
                if view.list.block_at(r).kind == "band" and view.list.block_at(r).folds[0].head is offline)
    tip = photo_tip(band.folds[0])
    assert "Old WD - not plugged in" in tip and "Taken" in tip
    assert words.badge_words(by_id[ids["photo"]]) == ""           # a local file needs no badge
    view.grab()                                                   # paints the placeholder without error
    assert view.list.picture_for(offline) is None                 # no path to read: never decoded


def test_a_row_says_how_its_date_was_arrived_at(qtbot, june):
    view, ids, _store = june
    view.browse(Period.month(2015, 6))
    qtbot.waitUntil(lambda: len(entries(view)) == 4, timeout=8000)
    rows = {view.list.block_at(r).folds[0].head.file_id: row_text(view.list.block_at(r).folds[0])
            for r in range(view.list.block_count()) if view.list.block_at(r).kind == "row"}
    assert rows[ids["letter"]].basis == "File date" and "copied" in rows[ids["letter"]].basis_tip
    assert rows[ids["mail"]].basis == "Sent" and rows[ids["mail"]].title == "Wedding plans"
    assert rows[ids["mail"]].detail.startswith("Message  -  from dave@example.com")


def test_enter_and_double_click_open_the_row_they_are_on(qtbot, june):
    view, ids, _store = june
    view.browse(Period.month(2015, 6))
    qtbot.waitUntil(lambda: len(entries(view)) == 4, timeout=8000)
    opened = []
    view.opened.connect(opened.append)
    letter_row = next(r for r in range(view.list.block_count())
                      if view.list.block_at(r).kind == "row"
                      and view.list.block_at(r).folds[0].head.file_id == ids["letter"])
    view.list.setCurrentIndex(view.list._model.index(letter_row, 0))
    qtbot.keyClick(view.list, Qt.Key.Key_Return)
    assert [e.file_id for e in opened] == [ids["letter"]]
    rect = view.list.visualRect(view.list._model.index(letter_row, 0))
    double_click(view.list.viewport(), rect.center())
    assert [e.file_id for e in opened] == [ids["letter"], ids["letter"]]
    assert hasattr(opened[0], "volume_id") and hasattr(opened[0], "relative_path")   # what the opener reads


def test_the_arrow_keys_move_along_a_band_of_photographs(qtbot, tmp_path):
    with SqliteStore(tmp_path / "t.db") as store:
        for i in range(3):
            add_file(store, rf"D:\p\a{i}.jpg", mtime=noon(2019, 1, 1), taken=noon(2015, 6, 10, i * 3000))
        view = TimelineView(store)
        qtbot.addWidget(view)
        view.picker.fold_box.setChecked(False)
        view.resize(900, 500)
        view.show()
        view.browse(Period.month(2015, 6))
        qtbot.waitUntil(lambda: loaded(view), timeout=8000)
        opened = []
        view.opened.connect(opened.append)
        band = next(r for r in range(view.list.block_count()) if view.list.block_at(r).kind == "band")
        view.list.setCurrentIndex(view.list._model.index(band, 0))
        qtbot.keyClick(view.list, Qt.Key.Key_Right)
        qtbot.keyClick(view.list, Qt.Key.Key_Right)
        qtbot.keyClick(view.list, Qt.Key.Key_Right)                # past the last: stays on it
        qtbot.keyClick(view.list, Qt.Key.Key_Left)
        qtbot.keyClick(view.list, Qt.Key.Key_Return)
        assert [e.name for e in opened] == ["a1.jpg"]
        from PyQt6.QtCore import QThreadPool
        QThreadPool.globalInstance().waitForDone(5000)


def _burst_store(tmp_path, count=12):
    store = SqliteStore(tmp_path / "burst.db").connect()
    base = 0x0F0F0F0F0F0F0F0F
    for i in range(count):
        add_file(store, rf"D:\p\burst{i}.jpg", mtime=noon(2019, 1, 1),
                 taken=noon(2015, 6, 10, i), phash=f"{base ^ (1 << (i % 6)):016x}")
    add_file(store, r"D:\p\single.jpg", mtime=noon(2019, 1, 1), taken=noon(2015, 6, 11),
             phash="ffff0000ffff0000")
    return store


def test_a_burst_is_one_line_that_can_be_taken_apart_without_losing_the_place(qtbot, tmp_path):
    store = _burst_store(tmp_path)
    view = TimelineView(store)
    qtbot.addWidget(view)
    view.resize(900, 500)
    view.show()
    view.browse(Period.month(2015, 6))
    qtbot.waitUntil(lambda: loaded(view), timeout=8000)
    folds = [f for r in range(view.list.block_count()) for f in view.list.block_at(r).folds]
    assert len(folds) < 13 and any(f.folded for f in folds)
    assert view.status.text().startswith("13 items")               # hidden ones still counted
    big = max(folds, key=lambda f: len(f.older))
    assert "similar photos" in big.label() and "right-click" in photo_tip(big)
    view.unfold(big)
    assert len(entries(view)) == 13
    assert all(not f.folded for r in range(view.list.block_count()) for f in view.list.block_at(r).folds
               if f.head in [big.head, *big.older])
    from PyQt6.QtCore import QThreadPool
    QThreadPool.globalInstance().waitForDone(5000)
    store.close()


def test_turning_grouping_off_shows_every_photograph_separately(qtbot, tmp_path):
    store = _burst_store(tmp_path, count=6)
    view = TimelineView(store)
    qtbot.addWidget(view)
    view.show()
    view.browse(Period.month(2015, 6))
    qtbot.waitUntil(lambda: loaded(view), timeout=8000)
    grouped = sum(len(view.list.block_at(r).folds) for r in range(view.list.block_count()))
    view.picker.fold_box.setChecked(False)
    qtbot.waitUntil(lambda: sum(len(view.list.block_at(r).folds)
                                for r in range(view.list.block_count())) == 7, timeout=8000)
    assert grouped < 7 and store.get_state("ui:timeline_fold", None) == "off"
    from PyQt6.QtCore import QThreadPool
    QThreadPool.globalInstance().waitForDone(5000)
    store.close()


# ---------------------------------------------------------------------------
# Thumbnails: decoded on a worker, and a missing one is a placeholder
# ---------------------------------------------------------------------------

def test_a_real_photograph_gets_its_thumbnail_and_a_missing_one_keeps_its_placeholder(qtbot, tmp_path):
    from PIL import Image

    real = tmp_path / "lake.jpg"
    Image.new("RGB", (400, 300), (30, 120, 200)).save(real)
    with SqliteStore(tmp_path / "t.db") as store:
        add_file(store, str(real), mtime=noon(2019, 1, 1), taken=noon(2015, 6, 10))
        add_file(store, str(tmp_path / "gone.jpg"), mtime=noon(2019, 1, 1), taken=noon(2015, 6, 10, 60))
        view = TimelineView(store)
        qtbot.addWidget(view)
        view.resize(900, 400)
        view.show()
        view.browse(Period.month(2015, 6))
        qtbot.waitUntil(lambda: loaded(view), timeout=8000)
        view.grab()                                               # the paint asks for the pictures
        qtbot.waitUntil(lambda: str(real) in view.list._pictures, timeout=8000)
        assert not view.list._pictures[str(real)].isNull()
        view.grab()                                               # and the missing one paints fine
        qtbot.wait(300)
        assert view.list._pictures.get(str(tmp_path / "gone.jpg")) is None
        from PyQt6.QtCore import QThreadPool
        QThreadPool.globalInstance().waitForDone(5000)


def test_reading_and_thumbnailing_never_change_a_users_file(qtbot, tmp_path):
    from PIL import Image

    real = tmp_path / "keep.jpg"
    Image.new("RGB", (64, 64), (1, 2, 3)).save(real)
    before = (real.read_bytes(), real.stat().st_mtime_ns)
    with SqliteStore(tmp_path / "t.db") as store:
        add_file(store, str(real), mtime=noon(2019, 1, 1), taken=noon(2015, 6, 10))
        view = TimelineView(store)
        qtbot.addWidget(view)
        view.show()
        view.browse(Period.month(2015, 6))
        qtbot.waitUntil(lambda: loaded(view), timeout=8000)
        view.grab()
        qtbot.waitUntil(lambda: str(real) in view.list._pictures, timeout=8000)
        from PyQt6.QtCore import QThreadPool
        QThreadPool.globalInstance().waitForDone(5000)
    assert (real.read_bytes(), real.stat().st_mtime_ns) == before
    assert sorted(p.name for p in tmp_path.iterdir() if p.suffix == ".jpg") == ["keep.jpg"]


# ---------------------------------------------------------------------------
# Density: a month of photographs arrives a page at a time
# ---------------------------------------------------------------------------

def _dense_store(tmp_path, photos: int):
    store = SqliteStore(tmp_path / "dense.db").connect()
    rows = [(rf"D:\Pictures\p{i:05d}.jpg", r"D:\Pictures", "jpg", 500, noon(2019, 1, 1), f"h{i}",
             "INDEXED", "file", None, None, f"{(i * 2654435761) % (1 << 64):016x}", 1_700_000_000 + i,
             noon(2015, 6, 1 + (i * 29) // photos, (i * 37) % 30_000), 0, None)
            for i in range(photos)]
    with store.write() as conn:
        conn.executemany(
            "INSERT INTO files (path, parent_dir, ext, size_bytes, mtime_ns, content_hash, status, "
            "source_kind, volume_id, relative_path, phash, indexed_at, taken_at_ns, taken_at_is_hint, "
            "repo_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return store


def test_a_month_of_photographs_arrives_a_page_at_a_time_as_the_list_is_scrolled(qtbot, tmp_path, monkeypatch):
    r"""4c: "a month with 4,000 photos paginates ... scrolling stays worker-fed"."""
    monkeypatch.setattr(tv_module, "PAGE_SIZE", 100)
    store = _dense_store(tmp_path, 1_000)
    view = TimelineView(store)
    qtbot.addWidget(view)
    view.resize(900, 500)
    view.show()
    view.picker.fold_box.setChecked(False)
    view.browse(Period.month(2015, 6))
    qtbot.waitUntil(lambda: loaded(view), timeout=8000)
    qtbot.wait(200)
    first = len(entries(view))
    assert first == 100, "only the first page was read, not the month"
    assert not view._done and "so far - scroll down for more" in view.status.text()
    bar = view.list.verticalScrollBar()
    for _ in range(40):                                            # keep scrolling to the bottom
        if view._done:
            break
        bar.setValue(bar.maximum())
        qtbot.wait(60)
    qtbot.waitUntil(lambda: view._done and not view._loading, timeout=20000)
    assert len(entries(view)) == 1_000
    assert len({e.file_id for e in entries(view)}) == 1_000        # none twice
    assert view.status.text() == "1,000 items - that is everything from this period."
    times = [e.when_ns for e in entries(view)]
    assert times == sorted(times)                                  # oldest first all the way down
    from PyQt6.QtCore import QThreadPool
    QThreadPool.globalInstance().waitForDone(5000)
    store.close()


def test_the_window_keeps_answering_while_a_dense_month_loads_and_scrolls(qtbot, tmp_path):
    """The `test_ui_never_blocks` promise, measured: a 5 ms heartbeat on the
    window thread across the whole load and scroll never misses by much."""
    store = _dense_store(tmp_path, 4_000)
    view = TimelineView(store)
    qtbot.addWidget(view)
    view.resize(900, 500)
    view.show()
    beats: list[float] = []
    clock = QTimer()
    clock.setInterval(5)
    clock.timeout.connect(lambda: beats.append(time.perf_counter()))
    clock.start()
    view.browse(Period.month(2015, 6))
    qtbot.waitUntil(lambda: loaded(view), timeout=8000)
    bar = view.list.verticalScrollBar()
    deadline = time.monotonic() + 60
    while not view._done and time.monotonic() < deadline:
        bar.setValue(bar.maximum())
        qtbot.wait(20)
    clock.stop()
    assert view._done and sum(len(e.folds) for e in [view.list.block_at(r) for r in range(view.list.block_count())]) > 0
    gaps = [b - a for a, b in zip(beats, beats[1:])]
    assert gaps, "the heartbeat never ran"
    assert max(gaps) < 0.75, f"the window stalled for {max(gaps) * 1000:.0f} ms while loading"
    from PyQt6.QtCore import QThreadPool
    QThreadPool.globalInstance().waitForDone(5000)
    store.close()


def test_a_page_that_arrives_after_another_month_was_picked_is_dropped(qtbot, tmp_path):
    with SqliteStore(tmp_path / "t.db") as store:
        add_file(store, r"D:\may.txt", mtime=noon(2015, 5, 5))
        add_file(store, r"D:\june.txt", mtime=noon(2015, 6, 5))
        view = TimelineView(store)
        qtbot.addWidget(view)
        view.show()
        view.browse(Period.month(2015, 5))
        view.browse(Period.month(2015, 6))                          # before May's page could land
        qtbot.waitUntil(lambda: loaded(view), timeout=8000)
        qtbot.wait(300)
        assert [e.name for e in entries(view)] == ["june.txt"]
        assert view.heading.text() == "June 2015"
        from PyQt6.QtCore import QThreadPool
        QThreadPool.globalInstance().waitForDone(5000)


# ---------------------------------------------------------------------------
# It never asks for a click it cannot honour
# ---------------------------------------------------------------------------

def test_the_timeline_has_no_search_box_of_its_own(june):
    r"""4d: "a browsing surface, not a search tab: no query box"."""
    from PyQt6.QtWidgets import QLineEdit

    view, _ids, _store = june
    boxes = view.findChildren(QLineEdit)
    assert {b.accessibleName() for b in boxes} == {"From date", "To date"}
    assert not [b for b in boxes if "search" in (b.placeholderText() + b.accessibleName()).lower()]


def test_the_list_can_be_read_by_a_screen_reader(qtbot, june):
    view, _ids, _store = june
    view.browse(Period.month(2015, 6))
    qtbot.waitUntil(lambda: len(entries(view)) == 4, timeout=8000)
    assert view.list.accessibleName() == "Timeline"
    spoken = [view.list._model.index(r, 0).data(int(Qt.ItemDataRole.AccessibleTextRole))
              for r in range(view.list.block_count())]
    assert spoken[0] == "Wednesday 10 June 2015" and all(spoken)
    assert "1 photograph, from lake.jpg" in spoken
