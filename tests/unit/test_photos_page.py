r"""The Photos tab: one library, narrowed like every tab, shown four ways.

Layer: L2/L5

2026-10-05, the owner: "a chip just for pictures designed to view find and deal
with pictures including namings", "list, small thumbnail or normal thumbnail
etc design a system", "make it like a professional photo management/viewer",
"like other tabs where i can narrow by year name location etc etc .. also have
/ commands", "all that functionality should be also available on photos page".
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import pytest

from app.index import face_clustering as fc
from app.search.query import parse_query
from app.search.run import words_of
from app.storage.sqlite_store import PhotoRow, SqliteStore
from app.ui.presenter import photos as pp


def _row(n, *, people=(), faces=0, place=None, when=dt.datetime(2022, 6, 1, 12),
         ext="jpg", described=False, has_text=False, page_like=False, tags=(),
         path=None, size=1000, hint=False):
    ns = int(when.timestamp() * 1e9) if when else None
    return PhotoRow(file_id=n, path=path or f"D:/Photos/{when.year if when else 'x'}/p{n}.{ext}",
                    ext=ext, size_bytes=size, mtime_ns=ns or 0, taken_at_ns=ns,
                    taken_is_hint=hint, place=place, people=tuple(people),
                    faces=max(faces, len(people)), described=described, has_text=has_text,
                    page_like=page_like, status="SKIPPED", scanned=True, tags=tuple(tags))


ROWS = [
    _row(1, people=("Jason",), place="London", tags=("beach", "dog")),
    _row(2, people=("Jason", "Sarita"), faces=3, when=dt.datetime(2019, 3, 2)),
    _row(3, faces=1, place="Leeds", ext="heic", described=True),
    _row(4, when=dt.datetime(2019, 8, 9), has_text=True, path="D:/Shots/Screenshot 1.png",
         ext="png", size=5_000_000),
    _row(5, when=None),
]


def _narrow(text):
    parsed = parse_query(text)
    return [r.file_id for r in pp.narrow(ROWS, parsed, words_of(parsed))]


# --- the box, read the way every tab reads it -------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("who:Jason", [1, 2]),
    ("who:jas", [1, 2]),                          # a name by its start, any case
    ("who:Jason,Sarita", [1, 2]),
    ("-who:Sarita who:Jason", [1]),
    ("date:2019", [2, 4]),
    ("after:2019-06 before:2019-12", [4]),
    ("place:london", [1]),
    ("-place:leeds", [1, 2, 4, 5]),
    ("type:heic", [3]),
    ("only:named", [1, 2]),
    ("only:unnamed", [2, 3]),
    ("only:no-faces", [4, 5]),
    ("only:described", [3]),
    ("only:text", [4]),
    ("only:screenshots", [4]),
    ("-only:no-faces", [1, 2, 3]),
    ("shows:dog", [1]),
    ("beach", [1]),
    ("london -dog", []),
    ("name:screenshot", [4]),
    ("path:Shots", [4]),
    ("size:>1mb", [4]),
    ("who:Jason date:2019", [2]),
])
def test_the_box_narrows_the_library(text, expected):
    assert _narrow(text) == expected


def test_a_side_list_click_is_the_same_as_typing_it_and_a_second_click_takes_it_out():
    text = pp.toggle_in_box("beach", "who", "Sarita Patel")
    assert text == 'beach who:"Sarita Patel"'
    assert parse_query(text).who == ("sarita patel",)
    assert pp.box_has(text, "who", "sarita patel")
    assert pp.toggle_in_box(text, "who", "Sarita Patel") == "beach"
    assert pp.toggle_in_box("", "date", 2019) == "date:2019"
    assert pp.toggle_in_box("date:2019 only:unnamed", "only", "unnamed") == "date:2019"


def test_the_counts_beside_each_entry():
    found = pp.facets(ROWS)
    assert found["people"] == {"Jason": 2, "Sarita": 1}
    assert found["years"][2019] == 2 and found["years"][2022] == 2
    assert found["places"] == {"London": 1, "Leeds": 1}
    assert found["kinds"]["unnamed"] == 2 and found["kinds"]["screenshots"] == 1


def test_sorting_and_the_words_on_screen():
    assert [r.file_id for r in pp.sort_rows(ROWS, "oldest")][:2] == [5, 2]
    assert [r.file_id for r in pp.sort_rows(ROWS, "size")][0] == 4
    assert pp.people_text(ROWS[1]) == "Jason, Sarita + 1 not named"
    assert pp.people_text(ROWS[2]) == "1 face(s), not named"
    assert pp.summary(10, 10) == "10 photo(s)"
    assert pp.summary(3, 10, 2) == "3 of 10 photo(s) · 2 selected"
    assert pp.month_heading(ROWS[0].when_ns) == "June 2022"
    guessed = _row(9, when=dt.datetime(2022, 1, 1, tzinfo=dt.timezone.utc), hint=True)
    assert pp.date_text(guessed) == "about 2022", "a folder's year, not an hour"
    assert pp.size_text(5_000_000) == "4.8 MB"


# --- the store: one read for the whole library ------------------------------------------

@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def _photo(store, name, **kwargs):
    return store.upsert_file(path=f"/photos/{name}", size_bytes=10, mtime_ns=5,
                             source_kind="file", **kwargs)


def test_the_library_reads_people_descriptions_text_and_tags_at_once(store):
    jason_photo = _photo(store, "a.jpg")
    _photo(store, "b.heic")
    store.upsert_file(path="/docs/report.pdf", size_bytes=1, mtime_ns=1, source_kind="file")
    pile = store.split_pile([store.add_face(jason_photo, (0, 0, 1, 1), fc.to_bytes([1.0, 0]))])
    store.add_face(jason_photo, (0, 0, 1, 1), fc.to_bytes([0, 1.0]))
    store.rename_pile(pile, "Jason")
    store.add_caption_chunk(jason_photo, "A boy on a beach", label="AI description")
    store.add_caption_chunk(jason_photo, "HAPPY BIRTHDAY", label="Text read from the image")
    store.conn.execute("INSERT INTO file_tags (file_id, tag) VALUES (?, 'beach')",
                       (jason_photo,))
    store.conn.commit()

    rows = {Path(r.path).name: r for r in store.photo_library(["jpg", ".HEIC"])}
    assert set(rows) == {"a.jpg", "b.heic"}, "pictures only"
    first = rows["a.jpg"]
    assert first.people == ("Jason",) and first.faces == 2
    assert first.described and first.has_text and first.tags == ("beach",)
    assert not rows["b.heic"].described and rows["b.heic"].faces == 0
    details = store.photo_details(jason_photo)
    assert details["AI description"] == "A boy on a beach"
    assert details["Text read from the image"] == "HAPPY BIRTHDAY"
    assert store.photo_library([]) == []


def test_only_narrows_every_tab_the_same_way(store):
    """`/only` is a shared switch: the Files tab's SQL agrees with the page."""
    from app.storage.filters import file_filter_sql

    named = _photo(store, "named.jpg")
    unnamed = _photo(store, "unnamed.jpg")
    blank = _photo(store, "blank.jpg")
    _photo(store, "Screenshot 2024.png")
    store.upsert_file(path="/docs/a.pdf", size_bytes=1, mtime_ns=1, source_kind="file")
    pile = store.split_pile([store.add_face(named, (0, 0, 1, 1), fc.to_bytes([1.0, 0]))])
    store.rename_pile(pile, "Jason")
    store.split_pile([store.add_face(unnamed, (0, 0, 1, 1), fc.to_bytes([0, 1.0]))])
    for file_id in (named, unnamed, blank):
        store.conn.execute("INSERT INTO face_scans (file_id, scanned_at) VALUES (?, 1)",
                           (file_id,))
    store.conn.commit()

    def names(text):
        where, params = file_filter_sql(parse_query(text))
        rows = store.conn.execute(f"SELECT f.path FROM files f WHERE 1 = 1 {where}", params)
        return sorted(Path(r[0]).name for r in rows)

    assert names("only:named") == ["named.jpg"]
    assert names("only:unnamed") == ["unnamed.jpg"]
    assert names("only:no-faces") == ["blank.jpg"]
    assert names("only:screenshots") == ["Screenshot 2024.png"]
    assert "a.pdf" in names("-only:named")


# --- thumbnails ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


def test_a_thumbnail_is_made_once_kept_on_disk_and_read_back(qapp, tmp_path):
    from PIL import Image

    from app.ui.widgets import photo_thumbs

    photo = tmp_path / "wide.jpg"
    Image.new("RGB", (1600, 900), (200, 40, 40)).save(photo)
    cache = tmp_path / "thumbs"
    first = photo_thumbs.photo_thumbnail(str(photo), 1, 2, cache)
    assert max(first.width(), first.height()) == photo_thumbs.CACHE_EDGE
    kept = list(cache.glob("*.jpg"))
    assert len(kept) == 1
    photo.unlink()                                 # read from the cache now
    again = photo_thumbs.photo_thumbnail(str(photo), 1, 2, cache)
    assert again is not None and again.width() == first.width()
    assert photo_thumbs.cache_name("a", 1, 2) != photo_thumbs.cache_name("a", 1, 3), \
        "a changed photo gets a new thumbnail"
    from PyQt6.QtGui import QPixmap

    square = photo_thumbs._square(QPixmap.fromImage(first))
    assert square.width() == square.height()


def test_the_loader_makes_what_was_asked_for_last_first(qapp, tmp_path, monkeypatch):
    from app.ui.widgets import photo_thumbs

    loader = photo_thumbs.ThumbLoader(tmp_path)
    started = []
    monkeypatch.setattr(photo_thumbs, "AT_ONCE", 0)
    for n in range(5):
        loader.request(f"p{n}", 1, 1)
    monkeypatch.setattr(photo_thumbs, "AT_ONCE", 1)
    monkeypatch.setattr("app.ui.workers.run", lambda pool, worker: started.append(worker))
    loader._next()
    assert list(loader._running) == ["p4"]


# --- the tab ---------------------------------------------------------------------------------

def _library_store(store, tmp_path):
    from PIL import Image

    paths = []
    for n, colour in enumerate(((200, 0, 0), (0, 200, 0), (0, 0, 200))):
        path = tmp_path / f"photo{n}.jpg"
        Image.new("RGB", (64, 48), colour).save(path)
        paths.append(path)
        store.upsert_file(path=str(path), size_bytes=path.stat().st_size,
                          mtime_ns=path.stat().st_mtime_ns, source_kind="file")
    first = store.conn.execute("SELECT id FROM files WHERE path = ?", (str(paths[0]),)
                               ).fetchone()[0]
    pile = store.split_pile([store.add_face(first, (0, 0, 1, 1), fc.to_bytes([1.0, 0]))])
    store.rename_pile(pile, "Jason")
    return paths


def test_the_tab_shows_narrows_switches_view_and_opens_naming(qapp, qtbot, store, tmp_path):
    from app.ui.photos_view import PhotosView

    paths = _library_store(store, tmp_path)
    view = PhotosView(store)
    qtbot.addWidget(view)
    qtbot.waitUntil(lambda: view.browser.model.rowCount() == 3, timeout=15000)
    assert view.summary.text().startswith("3 photo(s)")

    view._toggle("who", "Jason")                   # what a side-list click does
    assert view.input.text() == "who:Jason"
    qtbot.waitUntil(lambda: view.browser.model.rowCount() == 1, timeout=15000)
    assert view.browser.model.row_at(0).path == str(paths[0])
    assert "1 of 3" in view.summary.text()

    for mode in ("details", "small", "large", "medium"):
        view.set_mode(mode, remember=False)
        assert view.browser.mode == mode
    view.browser.select_path(str(paths[0]))
    assert view.browser.current_row().path == str(paths[0])

    view.show_naming()
    assert view.centre.currentIndex() == 1 and view._naming is not None
    assert hasattr(view._naming, "accept_all"), "Accept all is on the Photos tab too"
    view.show_photos()
    assert view.centre.currentWidget() is view.browser

    from PyQt6.QtWidgets import QMenu

    menu = QMenu()
    view.view_button.menu_for(menu)
    labels = [a.text() for a in menu.actions() if a.text()]
    assert labels[:4] == ["Details", "Small thumbnails", "Medium thumbnails",
                          "Large thumbnails"]


def test_the_viewer_steps_through_the_photos_as_ordered(qapp, qtbot, store, tmp_path):
    from app.ui.photos_view import PhotosView
    from app.ui.widgets.photo_viewer import PhotoViewer, caption

    _library_store(store, tmp_path)
    view = PhotosView(store)
    qtbot.addWidget(view)
    qtbot.waitUntil(lambda: view.browser.model.rowCount() == 3, timeout=15000)
    first = view.browser.model.row_at(0)
    view.browser.select_path(str(first.path))
    viewer = PhotoViewer(first, view.browser.step, view._position, view)
    qtbot.addWidget(viewer)
    viewer.go(1)
    assert viewer._row.path == view.browser.model.row_at(1).path
    assert "(2 of 3)" in viewer.line.text()
    viewer.go(-1)
    assert viewer._row.path == first.path
    assert caption(first, 1, 3).endswith("(1 of 3)")


def test_writing_names_from_the_tab_uses_sidecars_by_default(qapp, store, tmp_path):
    import threading

    from app.index.photo_metadata import SIDECAR, read_xmp
    from app.index.photo_metadata import run_write
    from app.ui.widgets.photo_write_dialog import WriteNamesDialog

    paths = _library_store(store, tmp_path)
    dialog = WriteNamesDialog(store, tmp_path, selected=[1])
    assert dialog.where_sidecar.isChecked() and not dialog.backup_row.isVisible()
    assert dialog.chosen_ids() == [1]
    before = paths[0].read_bytes()
    progress = {}
    result = run_write(store, None, where=SIDECAR, backup_root=tmp_path / "b",
                       progress=progress, stop=threading.Event())
    assert result.sidecars == 1 and progress == {"total": 1, "done": 1}
    assert paths[0].read_bytes() == before, "the photo itself is not touched"
    assert b"Jason" in read_xmp(paths[0])
    dialog.deleteLater()
