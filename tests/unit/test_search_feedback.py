r"""The Search page says when it is working, and when it found nothing.

Layer: L5

Review 2026-10-14 (`docs/REVIEW-2026-10-14-ux-architecture.md`):

* MS-1 - a busy line over the results while a search is out and rows are on
  screen, from `BUSY_DELAY_MS` to that search's finished answer (or failure);
* MS-7 - an answer of nothing over kept rows dims them and says so; the full
  tier's own "nothing" empties the list; the status line never contradicts it;
* MS-8 - a search superseded before it started is taken back from the pool;
* MS-2 - with the photo grid in front, the box's Down/Up move the grid;
* MS-5 - the notice bar offers the grid when most rows are pictures;
* IA-4 - "Photos only" searches documents with `type:image`.

A real `SearchView` over a bare stand-in engine; the searches are stand-in
workers the test lets go of, so nothing here depends on timing it cannot hold.
"""

from __future__ import annotations

import threading
import time

import pytest
from PySide6.QtCore import QEvent, QRunnable, Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import QApplication

from app.search.engine import SearchResponse, SearchResult
from app.ui.presenter import search_feedback as fb
from app.ui.presenter.search import Tier
from app.ui.search_view import SearchView
from app.ui.workers import WorkerSignals


class FakeEngine:
    store = None


def result(file_id: int, *, ext: str = "pdf") -> SearchResult:
    return SearchResult(chunk_id=file_id, file_id=file_id, path=rf"D:\Archive\file{file_id}.{ext}",
                        text="pump station", score=0.9, rank=file_id, ext=ext,
                        mtime_ns=1_700_000_000_000_000_000)


#: Every stand-in search made, let go of after each test so none holds a thread.
_GATES: list = []


class Gate(QRunnable):
    """A search that answers when the test lets it go."""

    def __init__(self, generation: int, response: SearchResponse | None = None, *,
                 fail: bool = False) -> None:
        super().__init__()
        self.setAutoDelete(False)
        _GATES.append(self)
        self.generation = generation
        self.signals = WorkerSignals()
        self.response = response or SearchResponse(results=[result(9)])
        self.release = threading.Event()
        self.started = threading.Event()
        self.ran = False
        self.fail = fail

    def run(self) -> None:
        self.started.set()
        self.release.wait(5)
        self.ran = True
        if self.fail:
            self.signals.failed.emit(RuntimeError("no"))
        else:
            self.signals.finished.emit((self.generation, self.response))
        self.signals.done.emit()


def _settle(until, tries: int | None = None, *, ceiling_s: float = 30.0) -> bool:
    """Pump events until `until()` holds. With `tries`, exactly that many 10 ms turns -
    a pause, as `_settle(lambda: False, tries=10)` is. Without, up to `ceiling_s`.

    2026-10-25: the wait was 200 turns of 10 ms; it ran out in a full local run with
    four parts loading the laptop, and the same test passed alone 3 of 3. A ceiling,
    not a budget - what each test checks is unchanged."""
    if tries is not None:
        for _ in range(tries):
            QApplication.processEvents()
            if until():
                return True
            time.sleep(0.01)
        return False
    deadline = time.monotonic() + ceiling_s
    while True:
        QApplication.processEvents()
        if until():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


@pytest.fixture()
def view(qtbot):
    widget = SearchView(FakeEngine())
    qtbot.addWidget(widget)
    widget.resize(900, 600)
    widget.show()
    widget.results.show_results([result(1), result(2), result(3)], ["pump"])
    widget._shown_anything = True
    yield widget
    widget.flight._take_back()


@pytest.fixture(autouse=True)
def _let_go():
    """No stand-in search outlives its test or holds a search thread."""
    yield
    from PySide6.QtCore import QThreadPool

    for gate in _GATES:
        gate.release.set()
    QThreadPool.globalInstance().waitForDone(5_000)
    for pool in _POOLS:
        pool.waitForDone(5_000)
    QApplication.processEvents()
    _GATES.clear()
    _POOLS.clear()


#: Small pools made by a test, waited for after it.
_POOLS: list = []


# ---------------------------------------------------------------------------
# The presenter's decisions
# ---------------------------------------------------------------------------

def test_only_the_latest_finished_answer_ends_the_busy_line():
    assert fb.busy_ends(5, 5, early=False)
    assert not fb.busy_ends(5, 5, early=True), "the reranked order is still coming"
    assert not fb.busy_ends(4, 5, early=False), "a stale answer: the newer search is out"


def test_nothing_is_kept_only_while_a_better_answer_is_coming():
    assert fb.after_nothing(shown_anything=True, early=True, interim=False) == fb.KEEP
    assert fb.after_nothing(shown_anything=True, early=False, interim=True) == fb.KEEP
    assert fb.after_nothing(shown_anything=True, early=False, interim=False) == fb.EMPTY
    assert fb.after_nothing(shown_anything=False, early=True, interim=True) == fb.EMPTY


def test_the_kept_words_name_the_query_and_stay_on_one_line():
    assert fb.kept_words("  pump   station ") == \
        "No matches for “pump station” - showing the previous results"
    assert len(fb.kept_words("x" * 500)) < 120


def test_the_grid_is_offered_when_most_rows_are_pictures():
    assert fb.grid_offer([True] * 5 + [False], grid_on=False)
    assert not fb.grid_offer([True] * 5 + [False], grid_on=True), "already on"
    assert not fb.grid_offer([True, True, False, False, False], grid_on=False), "not most"
    assert not fb.grid_offer([True] * 3, grid_on=False), "too few for a grid"
    offer = fb.grid_offer([True] * 4, grid_on=False)[0]
    assert offer.code == fb.NOTICE_GRID_OFFER and f'href="{fb.GRID_HREF}"' in offer.message


# ---------------------------------------------------------------------------
# MS-1: the busy line
# ---------------------------------------------------------------------------

@pytest.mark.timing
def test_the_busy_line_shows_after_the_delay_and_goes_on_the_finished_answer(view, qtbot):
    view._generation = 1
    worker = view.flight.launch(Gate(1))
    assert not view.flight.busy(), "not at once - a warm answer usually beats it"
    qtbot.wait(fb.BUSY_DELAY_MS + 80)
    assert view.flight.busy()
    assert view.flight.bar.geometry().top() == 0, "over the top edge of the results"
    worker.signals.progress.emit((1, worker.response))
    QApplication.processEvents()
    assert view.flight.busy(), "an early answer: the better order is still coming"
    worker.release.set()
    assert _settle(lambda: not view.flight.busy())


@pytest.mark.slow
def test_a_stale_answer_does_not_end_the_newer_searchs_busy_line(view, qtbot):
    first, second = Gate(1), Gate(2)
    view.flight.launch(first)
    view.flight.launch(second)
    qtbot.wait(fb.BUSY_DELAY_MS + 80)
    first.release.set()
    assert _settle(lambda: first.ran)
    _settle(lambda: False, tries=10)            # let its answer land
    assert view.flight.busy()
    second.release.set()
    assert _settle(lambda: not view.flight.busy())


def test_a_failed_search_ends_the_busy_line(view, qtbot):
    worker = view.flight.launch(Gate(1, fail=True))
    qtbot.wait(fb.BUSY_DELAY_MS + 80)
    assert view.flight.busy()
    worker.release.set()
    assert _settle(lambda: not view.flight.busy())


def test_a_search_that_answers_after_its_page_is_gone_says_nothing(qtbot):
    """2026-10-15, CI on main: a search still running when its page was destroyed
    answered into the deleted busy timer - `RuntimeError ... QTimer already deleted`
    in the next test's event loop. The answer now finds nothing to update."""
    from app.ui import qtsip

    widget = SearchView(FakeEngine())
    widget.resize(900, 600)
    widget.show()
    widget.results.show_results([result(1)], ["pump"])
    widget._shown_anything = True
    flight = widget.flight
    worker = flight.launch(Gate(1, fail=True))
    assert _settle(lambda: worker.started.is_set())
    timer = flight._busy_timer
    qtsip.delete(widget)
    QApplication.processEvents()
    assert qtsip.isdeleted(timer)
    errors = []
    worker.signals.failed.connect(lambda _e: None)
    try:
        import sys
        old = sys.excepthook
        sys.excepthook = lambda *a: errors.append(a[1])
        worker.release.set()
        _settle(lambda: worker.ran, tries=100)
        _settle(lambda: False, tries=10)
    finally:
        sys.excepthook = old
    assert not errors, errors


def test_an_empty_list_gets_its_skeleton_not_the_busy_line(qtbot):
    widget = SearchView(FakeEngine())
    qtbot.addWidget(widget)
    widget.show()
    worker = widget.flight.launch(Gate(1))
    qtbot.wait(fb.BUSY_DELAY_MS + 80)
    assert not widget.flight.busy()
    worker.release.set()


def test_emptying_the_box_puts_the_busy_line_away(view, qtbot):
    view.input.blockSignals(True)
    view.input.setText("pump")
    view.input.blockSignals(False)
    worker = view.flight.launch(Gate(1))
    qtbot.wait(fb.BUSY_DELAY_MS + 80)
    view.input.setText("")
    assert not view.flight.busy()
    worker.release.set()


# ---------------------------------------------------------------------------
# MS-8: stale work taken back
# ---------------------------------------------------------------------------

def test_a_search_superseded_before_it_started_is_taken_back(view):
    """On a pool with every thread taken - here one thread, so the test can fill it."""
    from PySide6.QtCore import QThreadPool

    pool = QThreadPool()
    pool.setMaxThreadCount(1)
    _POOLS.append(pool)
    view.flight._pool = pool
    busy = [Gate(1)]
    for worker in busy:
        view.flight.launch(worker)
        assert worker.started.wait(3), "the thread was free"
    waiting, newest = Gate(3), Gate(4)
    view.flight.launch(waiting)                    # both threads busy: it queues
    view.flight.launch(newest)                     # ... and is taken back here
    for worker in (*busy, waiting, newest):
        worker.release.set()
    assert _settle(lambda: newest.ran)
    _settle(lambda: False, tries=20)
    assert not waiting.ran, "the superseded search still ran"


def test_the_view_dispatches_on_the_flight_not_the_global_pool(view, monkeypatch):
    started: list = []
    monkeypatch.setattr(view.flight, "launch", started.append)
    view.input.blockSignals(True)
    view.input.setText("pump")
    view.input.blockSignals(False)
    view._dispatch(Tier.FULL)
    assert len(started) == 1 and started[0].generation == view._generation


# ---------------------------------------------------------------------------
# MS-7: nothing found over kept rows
# ---------------------------------------------------------------------------

def _answer(view, generation, response, *, early=False):
    view._on_results((generation, response), early=early)


def test_nothing_over_kept_rows_dims_them_and_says_so(view):
    view.input.blockSignals(True)
    view.input.setText("pumpx")
    view.input.blockSignals(False)
    _answer(view, 2, SearchResponse(results=[], interim=True))
    assert view.flight.dimmed()
    assert view.results._list.graphicsEffect() is not None
    assert view.status.text() == fb.kept_words("pumpx")
    assert len(view.results._rows) == 3, "the rows somebody was reading stay"


def test_the_full_tiers_nothing_empties_the_list(view):
    view.input.blockSignals(True)
    view.input.setText("pumpx")
    view.input.blockSignals(False)
    _answer(view, 2, SearchResponse(results=[], interim=True))
    _answer(view, 3, SearchResponse(results=[]))
    assert not view.flight.dimmed()
    assert view.results._rows == []
    assert view.status.text() != fb.kept_words("pumpx"), "the line no longer claims kept rows"
    assert not view._shown_anything


def test_an_early_nothing_keeps_the_rows_until_the_finished_answer(view):
    _answer(view, 2, SearchResponse(results=[]), early=True)
    assert view.flight.dimmed() and len(view.results._rows) == 3


def test_rows_arriving_take_the_dimming_away(view):
    _answer(view, 2, SearchResponse(results=[], interim=True))
    _answer(view, 3, SearchResponse(results=[result(7)]))
    assert not view.flight.dimmed()
    assert view.results._list.graphicsEffect() is None


# ---------------------------------------------------------------------------
# MS-2: the box's keys reach the photo grid
# ---------------------------------------------------------------------------

def _key(key):
    return QKeyEvent(QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier)


def test_with_the_grid_in_front_down_moves_the_grid_not_the_hidden_list(view):
    photos = [result(n, ext="jpg") for n in (11, 12, 13)]
    view.results.show_results(photos, [])
    view.split.switches.boxes["grid"].setChecked(True)
    QApplication.processEvents()
    grid = view.split.grid
    assert view.eventFilter(view.input, _key(Qt.Key.Key_Down)) is True
    assert grid.current_row() is not None
    assert view.results.current_row() is None, "the hidden list did not move"
    opened: list = []
    grid.opened.connect(lambda row, siblings: opened.append(row))
    assert view.eventFilter(view.input, _key(Qt.Key.Key_Return)) is True
    assert opened == [grid.current_row()]
    view.split.switches.boxes["grid"].setChecked(False)


def test_with_the_list_in_front_down_still_moves_the_list(view):
    assert view.eventFilter(view.input, _key(Qt.Key.Key_Down)) is True
    assert view.results.current_row() is not None


# ---------------------------------------------------------------------------
# MS-5: the grid offered
# ---------------------------------------------------------------------------

def test_mostly_pictures_offers_the_grid_and_the_link_turns_it_on(view):
    box = view.split.switches.boxes["grid"]
    box.setChecked(False)
    response = SearchResponse(results=[result(n, ext="jpg") for n in range(20, 26)])
    assert [n.code for n in view.flight.offers(response)] == [fb.NOTICE_GRID_OFFER]
    view.notices.chosen.emit(fb.GRID_HREF)
    assert box.isChecked()
    assert view.flight.offers(response) == []
    box.setChecked(False)


# ---------------------------------------------------------------------------
# IA-4: "Photos only"
# ---------------------------------------------------------------------------

def test_photos_only_searches_documents_with_type_image(view, monkeypatch):
    started: list = []
    monkeypatch.setattr(view.flight, "launch", started.append)
    view.input.blockSignals(True)
    view.input.setText("beach")
    view.input.blockSignals(False)
    view.scope.blockSignals(True)
    view.set_scope("photos")
    view.scope.blockSignals(False)
    assert view.current_scope() == "photos"
    view._dispatch(Tier.FULL)
    worker = started[0]
    assert worker._query == "beach type:image"
    assert worker._options["scope"] == "documents"
