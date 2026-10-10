r"""The Photos tab stays still while you work in it.

Layer: L5

2026-10-05, the owner: "the photos flash folder then the picture etc even when
tagging, can the ui be slick world class and smooth like i have in google
photos" and "can all photos be same size in the view too?". Each test pins one
cause of the flash, found in the code before it was fixed:

- the naming page cleared its list and rebuilt it, with a folder icon, on
  every name given - now a pile still there keeps its tile;
- every face was cut again from its full photo each time - now once;
- every Yes or No rebuilt every "Is this ...?" chip - now only the answered
  one goes;
- the photo grid reset whenever the library was read again - now not when the
  same photos come back;
- a landscape photo was a thin strip in its tile - now every tile is filled.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image                                             # noqa: E402
from PySide6.QtCore import QThreadPool                              # noqa: E402
from PySide6.QtWidgets import QApplication                          # noqa: E402

from app.index import face_clustering as fc                       # noqa: E402
from app.storage.sqlite_store import SqliteStore                  # noqa: E402
from app.ui import thumbnail_loader                               # noqa: E402
from app.ui.widgets import face_crops                             # noqa: E402
from app.ui.widgets.photo_tagger_page import (                    # noqa: E402
    ROLE_PILE_ID, PhotoTaggerPage, _SuggestionChip)


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


@pytest.fixture(autouse=True)
def _fresh_memory():
    face_crops._PIXMAPS.clear()
    yield
    face_crops._PIXMAPS.clear()


def _settle(qapp):
    for _ in range(6):
        QThreadPool.globalInstance().waitForDone(5_000)
        qapp.processEvents()


def _photo(store, tmp_path, name, size=(800, 600)):
    path = tmp_path / name
    Image.new("RGB", size, (30, 140, 200)).save(path)
    file_id = store.upsert_file(path=str(path), size_bytes=path.stat().st_size,
                                mtime_ns=1, source_kind="file")
    return file_id, path


def _person(store, tmp_path, name, *, faces=1, pile_name=None):
    file_id, _path = _photo(store, tmp_path, name)
    pile = store.create_pile(name=pile_name)
    for n in range(faces):
        face = store.add_face(file_id, (100 + n, 100, 200, 260), fc.to_bytes([0.6, 0.8]))
        store.assign_face(face, pile)
    return pile


def _decodes(monkeypatch):
    calls = []
    real = thumbnail_loader.decode_face_crop

    def counting(*args, **kwargs):
        calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(thumbnail_loader, "decode_face_crop", counting)
    return calls


def _items(page):
    return [page._list.item(row) for row in range(page._list.count())]


# -- the naming page --------------------------------------------------------------


def test_a_reload_keeps_every_tile_and_its_picture(qapp, store, tmp_path):
    _person(store, tmp_path, "a.jpg", faces=3)
    _person(store, tmp_path, "b.jpg", faces=1)
    page = PhotoTaggerPage(store)
    _settle(qapp)
    before = _items(page)
    icons = [item.icon().cacheKey() for item in before]
    assert len(before) == 2

    page.reload()
    _settle(qapp)

    after = _items(page)
    assert [id(i) for i in after] == [id(i) for i in before], "the tiles were rebuilt"
    assert [item.icon().cacheKey() for item in after] == icons, "a picture was redrawn"


def test_no_tile_ever_shows_a_folder(qapp, store, tmp_path):
    _person(store, tmp_path, "a.jpg")
    page = PhotoTaggerPage(store)
    _settle(qapp)
    page._list.clear()                         # as a first read finds it
    folder = page.style().standardIcon(page.style().StandardPixmap.SP_DirIcon).pixmap(16)
    page._piles_ready(store.piles_with_counts(), page._generation)
    items = _items(page)                       # the tiles are in; their faces are not yet
    assert items
    for item in items:
        assert item.icon().pixmap(16).toImage() != folder.toImage()
    _settle(qapp)


def test_each_face_is_cut_from_its_photo_once(qapp, store, tmp_path, monkeypatch):
    calls = _decodes(monkeypatch)
    _person(store, tmp_path, "a.jpg", faces=2)
    _person(store, tmp_path, "b.jpg")
    page = PhotoTaggerPage(store)
    _settle(qapp)
    first = len(calls)
    assert first == 2                           # one face per pile

    for _ in range(3):
        page.reload()
        _settle(qapp)
    assert len(calls) == first, "a reload cut the faces again"

    face_crops._PIXMAPS.clear()                 # a new session: read from disk
    again = PhotoTaggerPage(store)
    _settle(qapp)
    assert len(calls) == first
    assert all(not item.icon().isNull() for item in _items(again))
    assert list((tmp_path / "thumbs" / "faces").glob("*.jpg")), "nothing kept on disk"


def test_a_new_name_shows_before_the_store_answers(qapp, store, tmp_path, monkeypatch):
    pile = _person(store, tmp_path, "a.jpg", faces=2)
    page = PhotoTaggerPage(store)
    _settle(qapp)
    monkeypatch.setattr("app.ui.workers.run", lambda pool, worker: None)   # never answers

    page._name_checked(pile, "Sarita", None)

    assert _items(page)[0].text().startswith("Sarita")


def test_a_combine_shows_before_the_store_answers(qapp, store, tmp_path, monkeypatch):
    big = _person(store, tmp_path, "a.jpg", faces=3)
    small = _person(store, tmp_path, "b.jpg", faces=1)
    page = PhotoTaggerPage(store)
    _settle(qapp)
    monkeypatch.setattr("app.ui.workers.run", lambda pool, worker: None)

    page._show_combined(small, big)

    items = _items(page)
    assert [item.data(ROLE_PILE_ID) for item in items] == [big]
    assert "4 photo(s)" in items[0].text()


def test_a_combine_then_its_reload_keeps_the_tile_that_stayed(qapp, store, tmp_path):
    big = _person(store, tmp_path, "a.jpg", faces=3)
    small = _person(store, tmp_path, "b.jpg", faces=1)
    page = PhotoTaggerPage(store)
    _settle(qapp)
    kept = page._item_for(big)

    store.combine_piles(small, big)
    page.reload()
    _settle(qapp)

    assert _items(page) == [kept]
    assert "4 photo(s)" in kept.text()


# -- the "Is this ...?" strip ----------------------------------------------------------


def _suggest(store, tmp_path, name, pile):
    file_id, _path = _photo(store, tmp_path, name)
    face = store.add_face(file_id, (50, 50, 100, 120), fc.to_bytes([0.6, 0.8]))
    store.suggest_face(face, pile)
    return face


def test_answering_one_question_leaves_the_others_as_they_are(qapp, store, tmp_path):
    daddy = _person(store, tmp_path, "d.jpg", pile_name="Daddy")
    first = _suggest(store, tmp_path, "s1.jpg", daddy)
    second = _suggest(store, tmp_path, "s2.jpg", daddy)
    page = PhotoTaggerPage(store)
    _settle(qapp)
    chips = {c.face_id: c for c in page._suggestions_strip.findChildren(_SuggestionChip)}
    assert set(chips) == {first, second}

    page._on_suggestion_decided(first, True)
    assert chips[first].isHidden(), "the answered chip should go at once"
    _settle(qapp)

    left = [c for c in page._suggestions_strip.findChildren(_SuggestionChip)
            if not c.isHidden()]
    assert left == [chips[second]], "the other chip was rebuilt"


# -- the photo grid ---------------------------------------------------------------------


def _rows(paths, people=()):
    from app.storage.sqlite_store import PhotoRow

    return [PhotoRow(file_id=n, path=p, ext="jpg", size_bytes=1, mtime_ns=1,
                     taken_at_ns=n, taken_is_hint=False, place=None, people=tuple(people),
                     faces=len(people), described=False, has_text=False, page_like=False,
                     status="SKIPPED", scanned=True, tags=())
            for n, p in enumerate(paths)]


def test_the_same_photos_read_again_do_not_reset_the_grid(qapp, tmp_path):
    from app.ui.widgets.photo_browser import PhotoModel
    from app.ui.widgets.photo_thumbs import ThumbLoader

    model = PhotoModel(ThumbLoader(tmp_path))
    resets, changes = [], []
    model.modelReset.connect(lambda: resets.append(1))
    model.dataChanged.connect(lambda *_a: changes.append(1))
    model.set_rows(_rows(["a.jpg", "b.jpg"]))
    assert len(resets) == 1

    model.set_rows(_rows(["a.jpg", "b.jpg"], people=("Jason",)))     # named meanwhile
    assert len(resets) == 1, "the grid reset for the same photos"
    assert changes and model.row_at(0).people == ("Jason",)

    model.set_rows(_rows(["a.jpg", "c.jpg"]))                         # a different list
    assert len(resets) == 2


def test_every_tile_is_the_same_square_filled_edge_to_edge(qapp):
    from PySide6.QtGui import QColor, QPixmap

    from app.ui.widgets.photo_thumbs import CACHE_EDGE, _square

    # A landscape, a portrait, and a panorama whose short side is far below
    # the Large view's 256 - it came out a small tile among full ones.
    for width, height in ((320, 180), (180, 320), (320, 96), (320, 320)):
        picture = QPixmap(width, height)
        picture.fill(QColor(200, 40, 40))
        tile = _square(picture)
        assert tile.width() == tile.height() == CACHE_EDGE
        image = tile.toImage()
        for x, y in ((0, 0), (tile.width() - 1, tile.height() - 1)):
            assert image.pixelColor(x, y).alpha() == 255, "a clear band - not filled"


# -- fading in, gliding, and asking ahead (Option B items 1 and 2) ------------------------


class _Thumbs:
    """A `ThumbLoader` stand-in: records what was asked for, in order."""

    def __init__(self):
        from PySide6.QtCore import QObject, Signal

        class _Signals(QObject):
            ready = Signal(str)

        self._signals = _Signals()
        self.ready = self._signals.ready
        self.asked = []
        self.made = {}

    def pixmap(self, path):
        return self.made.get(path)

    def request(self, path, size, mtime):
        self.asked.append(path)

    def forget_waiting(self):
        pass


def test_a_picture_fades_in_rather_than_popping(qapp, monkeypatch):
    import time

    from app.ui.widgets import photo_browser

    model = photo_browser.PhotoModel(_Thumbs())
    model.set_rows(_rows(["a.jpg"]))
    assert model.fade_of("a.jpg") == 1.0, "nothing arriving - nothing fading"

    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    model._thumb_ready("a.jpg")
    assert model.fade_of("a.jpg") == 0.0
    clock[0] += photo_browser.FADE_MS / 2000
    assert 0.4 < model.fade_of("a.jpg") < 0.6
    clock[0] += photo_browser.FADE_MS / 1000
    model._fade_step()
    assert model.fade_of("a.jpg") == 1.0
    assert not model._fading.isActive(), "the fade timer runs only while something fades"


def test_a_half_faded_tile_paints(qapp, tmp_path):
    from PySide6.QtGui import QColor, QPixmap

    from app.ui.widgets.photo_browser import PhotoBrowser

    thumbs = _Thumbs()
    picture = QPixmap(320, 320)
    picture.fill(QColor(200, 40, 40))
    thumbs.made["a.jpg"] = picture
    browser = PhotoBrowser(thumbs)
    browser.resize(400, 300)
    browser.set_rows(_rows(["a.jpg"]))
    browser.model._thumb_ready("a.jpg")
    browser.show()
    qapp.processEvents()
    assert not browser.grab().isNull()
    browser.model._fading.stop()


def test_a_wheel_notch_glides_the_grid(qapp):
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtTest import QTest

    from app.ui.widgets import photo_browser

    browser = photo_browser.PhotoBrowser(_Thumbs())
    browser.resize(400, 300)
    browser.set_rows(_rows([f"p{n}.jpg" for n in range(400)]))
    browser.show()
    qapp.processEvents()
    bar = browser.grid.verticalScrollBar()
    assert bar.maximum() > photo_browser.GLIDE_PX
    assert browser.grid.verticalScrollMode() == browser.grid.ScrollMode.ScrollPerPixel

    wheel = QWheelEvent(QPointF(50, 50), QPointF(50, 50), QPoint(0, 0), QPoint(0, -120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(browser.grid.viewport(), wheel)
    qapp.processEvents()
    assert bar.value() < photo_browser.GLIDE_PX, "it jumped, not glided"
    QTest.qWait(photo_browser.GLIDE_MS + 150)
    assert bar.value() == photo_browser.GLIDE_PX


def test_the_next_screenful_is_asked_for_ahead(qapp):
    from app.ui.widgets.photo_browser import PhotoBrowser

    thumbs = _Thumbs()
    browser = PhotoBrowser(thumbs)
    browser.resize(400, 300)
    browser.set_rows(_rows([f"p{n}.jpg" for n in range(400)]))
    browser.show()
    qapp.processEvents()
    view = browser.grid.viewport().rect()
    on_screen = [n for n in range(400)
                 if browser.grid.visualRect(browser.model.index(n, 0)).intersects(view)]
    thumbs.asked.clear()

    browser._ask_ahead()

    ahead = [int(p[1:-4]) for p in thumbs.asked]
    # The screenful straight after the last picture on screen - not nothing,
    # which is what a probe landing in the margin right of the last column gave.
    assert sorted(ahead) == list(range(max(on_screen) + 1,
                                       max(on_screen) + 1 + len(on_screen)))
    assert ahead[-1] == min(ahead), "the nearest is asked for last, so it is made first"


# -- blur-up and round faces (Option B items 3 and 4) -------------------------------------


def test_the_blurred_previews_survive_a_restart(qapp, tmp_path):
    from app.ui.widgets import photo_thumbs

    photo = tmp_path / "wide.jpg"
    Image.new("RGB", (1600, 900), (200, 40, 40)).save(photo)
    cache = tmp_path / "thumbs"
    loader = photo_thumbs.ThumbLoader(cache)
    loader.request(str(photo), 1, 2)
    _settle(qapp)
    assert loader.pixmap(str(photo)) is not None
    soft = loader.tiny(str(photo), 1, 2)
    assert soft is not None and soft.width() == photo_thumbs.TINY_EDGE

    loader.save_tiny()
    _settle(qapp)
    assert (cache / photo_thumbs.TINY_PACK).exists()

    again = photo_thumbs.ThumbLoader(cache)          # a new session
    _settle(qapp)
    assert again.pixmap(str(photo)) is None, "the sharp one is not in memory yet"
    assert again.tiny(str(photo), 1, 2) is not None, "the blurred one should be"
    assert again.tiny(str(photo), 1, 3) is None, "a changed photo has no preview"


def test_a_damaged_pack_is_an_empty_one(tmp_path):
    from app.ui.widgets import photo_thumbs

    pack = tmp_path / "tiny.pack"
    photo_thumbs.write_tiny_pack(pack, {"a" * 40 + ".jpg": b"xyz", "b" * 40 + ".jpg": b"12"})
    assert photo_thumbs.read_tiny_pack(pack) == {"a" * 40 + ".jpg": b"xyz",
                                                 "b" * 40 + ".jpg": b"12"}
    pack.write_bytes(pack.read_bytes()[:-1])         # cut short
    assert list(photo_thumbs.read_tiny_pack(pack)) == ["a" * 40 + ".jpg"]
    pack.write_bytes(b"rubbish")
    assert photo_thumbs.read_tiny_pack(pack) == {}
    assert photo_thumbs.read_tiny_pack(tmp_path / "none.pack") == {}


def test_a_waiting_tile_shows_its_blurred_preview_not_grey(qapp):
    from PySide6.QtGui import QColor, QPixmap

    from app.ui.widgets.photo_browser import PhotoBrowser

    class _WithTiny(_Thumbs):
        def tiny(self, path, size, mtime):
            soft = QPixmap(24, 24)
            soft.fill(QColor(0, 200, 0))
            return soft

    browser = PhotoBrowser(_WithTiny())
    browser.resize(300, 260)
    browser.set_rows(_rows(["a.jpg"]))
    browser.show()
    qapp.processEvents()
    tile = browser.grid.visualRect(browser.model.index(0, 0)).center()
    colour = browser.grid.viewport().grab().toImage().pixelColor(tile.x(), tile.y() - 10)
    assert colour.green() > 150 and colour.red() < 80, f"grey, not the preview: {colour.name()}"


def test_people_are_round(qapp, store, tmp_path):
    from app.ui.widgets.photo_tagger_page import ROLE_FACE_KEY  # noqa: F401 - imported to pin

    _person(store, tmp_path, "a.jpg")
    page = PhotoTaggerPage(store)
    _settle(qapp)
    image = _items(page)[0].icon().pixmap(64).toImage()
    assert image.pixelColor(1, 1).alpha() == 0, "the corner should be clear - a circle"
    assert image.pixelColor(32, 32).alpha() == 255


def test_a_tick_repaints_a_run_of_fading_tiles_once(qapp, monkeypatch):
    """2026-10-09: a tick sent one change per fading tile, and a few hundred
    arriving together stalled the window. Neighbouring tiles now share one."""
    import time

    from app.ui.widgets import photo_browser

    names = [f"p{n}.jpg" for n in range(300)]
    model = photo_browser.PhotoModel(_Thumbs())
    model.set_rows(_rows(names))
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    for name in names:
        model._arrived[name] = clock[0]          # all arrived in this tick
    emitted = []
    model.dataChanged.connect(lambda first, last, *_a: emitted.append((first.row(), last.row())))
    model._fade_step()
    assert emitted == [(0, 299)], emitted       # one change for the whole run

    assert photo_browser.fade_runs([5, 3, 4, 9, 10, 10, 20]) == [(3, 5), (9, 10), (20, 20)]
    assert photo_browser.fade_runs([]) == []


def test_a_landed_thumbnail_waits_for_the_tick_that_repaints_it(qapp, monkeypatch):
    """A thumbnail that lands sends no repaint of its own: the fade tick repaints
    everything that landed meanwhile, in one change per run."""
    import time

    from app.ui.widgets import photo_browser

    names = [f"q{n}.jpg" for n in range(200)]
    model = photo_browser.PhotoModel(_Thumbs())
    model.set_rows(_rows(names))
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    emitted = []
    model.dataChanged.connect(lambda first, last, *_a: emitted.append((first.row(), last.row())))
    for name in names:
        model._thumb_ready(name)
    assert emitted == [], "nothing is repainted until the tick"
    model._fade_step()
    assert emitted == [(0, 199)], emitted


# -- the waiting picture is cheap to paint (2026-10-10, A5) ---------------------------------


def test_a_blurred_preview_is_scaled_once_per_tile_size(qapp):
    """A waiting tile is painted on every frame of a scroll; its 24-pixel preview was
    filtered up to the tile on every one of those paints."""
    from collections import OrderedDict

    from PySide6.QtGui import QColor, QPixmap

    from app.ui.widgets import photo_browser

    soft = QPixmap(24, 24)
    soft.fill(QColor(90, 120, 200))
    cache = OrderedDict()
    first = photo_browser.scaled_preview(cache, soft, 160, 160)
    again = photo_browser.scaled_preview(cache, soft, 160, 160)
    assert first.cacheKey() == again.cacheKey(), "scaled again for the same tile"
    assert (first.width(), first.height()) == (160, 160)
    larger = photo_browser.scaled_preview(cache, soft, 256, 256)
    assert (larger.width(), larger.height()) == (256, 256)
    assert len(cache) == 2


def test_a_preview_is_made_at_device_pixels_on_a_scaled_screen(qapp):
    from collections import OrderedDict

    from PySide6.QtGui import QColor, QPixmap

    from app.ui.widgets import photo_browser

    soft = QPixmap(24, 24)
    soft.fill(QColor(90, 120, 200))
    made = photo_browser.scaled_preview(OrderedDict(), soft, 160, 160, 1.5)
    assert (made.width(), made.height()) == (240, 240)
    assert made.devicePixelRatio() == 1.5


def test_the_scaled_previews_kept_are_bounded(qapp, monkeypatch):
    from collections import OrderedDict

    from PySide6.QtGui import QColor, QPixmap

    from app.ui.widgets import photo_browser

    monkeypatch.setattr(photo_browser, "KEEP_SCALED", 3)
    cache = OrderedDict()
    for _ in range(5):
        soft = QPixmap(24, 24)                     # a new pixmap each: a new cacheKey
        soft.fill(QColor(10, 10, 10))
        photo_browser.scaled_preview(cache, soft, 96, 96)
    assert len(cache) == 3


def test_a_waiting_tile_with_a_preview_paints_and_scales_it_once(qapp):
    from PySide6.QtGui import QColor, QPixmap

    from app.ui.widgets.photo_browser import PhotoBrowser

    soft = QPixmap(24, 24)
    soft.fill(QColor(90, 120, 200))

    class _WithPreview(_Thumbs):
        def tiny(self, path, size, mtime):
            return soft

    browser = PhotoBrowser(_WithPreview())
    browser.resize(400, 300)
    browser.set_rows(_rows(["a.jpg"]))
    browser.show()
    qapp.processEvents()
    assert not browser.grab().isNull()
    assert not browser.grab().isNull()             # painted twice
    delegate = browser.grid.itemDelegate()
    assert len(delegate._scaled) == 1, "the preview was scaled again on the second paint"


def test_a_cache_name_is_worked_out_once():
    from app.ui.widgets.photo_thumbs import cache_name

    cache_name.cache_clear()
    first = cache_name("D:/Photos/a.jpg", 10, 20)
    assert cache_name("D:/Photos/a.jpg", 10, 20) == first
    assert cache_name.cache_info().hits >= 1
    assert cache_name("D:/Photos/a.jpg", 10, 21) != first
