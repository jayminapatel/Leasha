r"""The manage-faces window never freezes, however many faces a person has - 2026-10-26.

Layer: L5

Measured from the owner's logs on 2026-10-26: the lag monitor caught the window
unresponsive for 1.4 s, 2.0 s and 5.0 s inside `_ManageFacesDialog._faces_ready`,
which built a tile *and started a crop worker* for every one of a person's faces
in one go on the window's thread. "Change the cover photo…" opens the same
window, which is why the owner called it flaky.

These tests open the window on 5,000 faces and time every pass of the event loop
while it fills: no pass may block for longer than `BUDGET_S`. They also check that
every face arrives, that only a handful of crops are ever cut at once, and that
closing the window halfway through the fill is quiet.
"""

from __future__ import annotations

import gc
import time
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.qt, pytest.mark.slow, pytest.mark.timing]

FACES = 5_000
#: The longest one pass of the event loop may take while the window fills. The
#: lag monitor reports from 250 ms; a slice is sized for well under 50 ms, and
#: this leaves room for a busy machine without letting a whole fill through.
BUDGET_S = 0.120


class _Store:
    """Just what the window reads: one person's faces, and nobody else."""

    def __init__(self, count: int) -> None:
        self.faces = [(n + 1, f"/nowhere/face-{n}.jpg", (0.0, 0.0, 50.0, 50.0))
                      for n in range(count)]

    def faces_in_pile(self, _pile_id):
        return list(self.faces)

    def faces_seen_once(self):
        return list(self.faces)

    def piles_with_counts(self):
        return [SimpleNamespace(id=2, name="Bo")]


def _pump(qapp, until, timeout_s: float = 30.0) -> float:
    """Run the event loop until `until()`; the longest single pass, in seconds."""
    from PySide6.QtCore import QThreadPool

    worst = 0.0
    deadline = time.perf_counter() + timeout_s
    while not until():
        assert time.perf_counter() < deadline, "the window never finished filling"
        started = time.perf_counter()
        qapp.processEvents()
        worst = max(worst, time.perf_counter() - started)
        QThreadPool.globalInstance().waitForDone(1)
    return worst


def _crops_running(dialog) -> int:
    return len(dialog._crops._running)


def test_five_thousand_faces_fill_without_a_long_pass(qapp, monkeypatch):
    from app.ui.widgets import face_crops
    from app.ui.widgets.photo_tagger_page import _ManageFacesDialog

    started = []
    real = face_crops.cached_face_crop

    def counting(*args):
        started.append(args[0])
        return real(*args)

    monkeypatch.setattr("app.ui.widgets.photo_tagger_page.cached_face_crop", counting,
                        raising=False)
    monkeypatch.setattr(face_crops, "cached_face_crop", counting)

    # The first window of the run pays for icons and styles, whatever it holds.
    _ManageFacesDialog(_Store(3), 1, None, pile_name="Ann").deleteLater()
    qapp.processEvents()
    store = _Store(FACES)
    opened = time.perf_counter()
    dialog = _ManageFacesDialog(store, 1, None, pile_name="Ann", choosing_cover=True)
    building = time.perf_counter() - opened
    most_at_once = [0]

    def filled() -> bool:
        most_at_once[0] = max(most_at_once[0], _crops_running(dialog))
        return dialog._list.count() == FACES and not dialog._filling()

    worst = _pump(qapp, filled)
    print(f"\nmanage faces, {FACES:,} faces: worst pass {worst * 1000:.1f} ms, "
          f"window built in {building * 1000:.1f} ms")
    assert building < BUDGET_S
    assert worst < BUDGET_S, f"one pass of the event loop took {worst * 1000:.0f} ms"

    assert dialog._count.text() == f"{FACES:,} face(s)"
    from app.ui.widgets.photo_tagger_page import ROLE_FACE_ID

    ids = [dialog._list.item(row).data(ROLE_FACE_ID) for row in range(dialog._list.count())]
    assert ids == [face_id for face_id, _p, _b in store.faces]
    # Only the faces near the top are asked for, and only a few are cut at once.
    assert most_at_once[0] <= face_crops.AT_ONCE
    _pump(qapp, lambda: _crops_running(dialog) == 0)
    assert 0 < len(started) < FACES // 4
    dialog.deleteLater()
    qapp.processEvents()


def test_reading_again_mid_fill_starts_over_cleanly(qapp):
    from app.ui.widgets.photo_tagger_page import _ManageFacesDialog

    store = _Store(FACES)
    dialog = _ManageFacesDialog(store, 1, None, pile_name="Ann")
    _pump(qapp, lambda: 0 < dialog._list.count() < FACES)
    store.faces = store.faces[:300]                  # a move took most of them away
    dialog._load()
    worst = _pump(qapp, lambda: dialog._list.count() == 300 and not dialog._filling())
    assert worst < BUDGET_S
    for _ in range(20):                              # nothing from the first fill lands
        qapp.processEvents()
    assert dialog._list.count() == 300
    dialog.deleteLater()
    qapp.processEvents()


def test_closing_mid_fill_stops_the_fill_and_the_crops(qapp):
    from PySide6.QtCore import QThreadPool

    from app.ui.widgets.photo_tagger_page import _ManageFacesDialog

    store = _Store(FACES)
    dialog = _ManageFacesDialog(store, 1, None, pile_name="Ann")
    _pump(qapp, lambda: 0 < dialog._list.count() < FACES)
    count = dialog._list.count()
    dialog.close()
    assert not dialog._filling()                     # closed: the fill stopped there
    for _ in range(10):
        qapp.processEvents()
    assert dialog._list.count() == count
    dialog.deleteLater()
    dialog = None
    gc.collect()
    for _ in range(10):                              # late crops land on nothing, quietly
        QThreadPool.globalInstance().waitForDone(2_000)
        qapp.processEvents()


def test_a_thousand_people_tiles_go_in_without_a_long_pass(qapp, tmp_path):
    r"""The People grid: the same fault, found while fixing the window. Every tile
    was found by walking the grid and every face had its worker made up front -
    2,000 piles measured 7.2 s in `_piles_ready` and 5.8 s more as the faces
    landed. Now a tile is a lookup and the faces are cut `AT_ONCE` at a time."""
    from PySide6.QtCore import QThreadPool

    from app.storage.sqlite_store import SqliteStore
    from app.ui.widgets import face_crops
    from app.ui.widgets.photo_tagger_page import ROLE_PILE_ID, PhotoTaggerPage

    piles = [SimpleNamespace(id=n + 1, name=None, face_count=2_000 - n, last_found=0,
                             samples=[SimpleNamespace(path=f"/nowhere/pile-{n}.jpg",
                                                      bbox=(0.0, 0.0, 50.0, 50.0),
                                                      face_id=n + 1)])
             for n in range(1_000)]
    with SqliteStore(tmp_path / "index.db") as store:
        page = PhotoTaggerPage(store)
        for _ in range(10):                          # its own first read, of nothing
            QThreadPool.globalInstance().waitForDone(2_000)
            qapp.processEvents()
        started = time.perf_counter()
        page._piles_ready(piles, page._generation)
        took = time.perf_counter() - started
        most_at_once = [0]

        def all_cut() -> bool:
            most_at_once[0] = max(most_at_once[0], len(page._crops._running))
            return not page._crops.busy()

        worst = _pump(qapp, all_cut)
        print(f"\npeople grid, 1,000 piles: in {took * 1000:.1f} ms, "
              f"worst pass after {worst * 1000:.1f} ms")
        assert took < BUDGET_S and worst < BUDGET_S
        assert most_at_once[0] <= face_crops.AT_ONCE
        assert [page._list.item(r).data(ROLE_PILE_ID) for r in range(1_000)] == \
            [pile.id for pile in piles]
        assert page._item_for(500) is page._list.item(499)
        page._list.takeItem(499)                     # taken out behind the lookup's back
        assert page._item_for(500) is None and page._item_for(501) is page._list.item(499)
        page.deleteLater()
        qapp.processEvents()
