r"""The Photo Tagger page's suggestion strip - work order 0j section 2c's UI
half: "Is this Daddy?" as an actual clickable chip, not just the storage
layer `test_photo_tagger.py::test_pending_suggestions_*` already proves.

Layer: L5.

Offscreen `pytest-qt`-style widget test, same idiom `test_window_opens.py`
already uses for a `CallableWorker`-driven refresh: start the page, run the
real `QThreadPool`, then assert on what landed.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QThreadPool                             # noqa: E402
from PyQt6.QtWidgets import QApplication                         # noqa: E402

from app.index import face_clustering as fc                      # noqa: E402
from app.storage.sqlite_store import SqliteStore                 # noqa: E402
from app.ui.widgets.photo_tagger_page import PhotoTaggerPage      # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def _vec(x, y):
    return fc.to_bytes([x, y])


def _photo(store, name="a.jpg"):
    return store.upsert_file(
        path=f"/photos/{name}", size_bytes=100, mtime_ns=1, source_kind="file")


def _settle(qapp):
    r"""Drains a chain of workers, not just one round of them.

    `_on_suggestion_decided` now schedules its own `CallableWorker`
    (non-negotiable #5, closing `test_ui_never_blocks.py`'s guard for this
    file) whose `finished` signal calls `reload()`, which schedules two
    more. A single `waitForDone()` + `processEvents()` only drains the
    first level - `waitForDone()` returns before the second-level workers
    even exist, since `reload()` only runs once `processEvents()` delivers
    the first worker's queued signal. Interleaving the two, repeatedly,
    drains however many levels deep the chain goes.
    """
    for _ in range(5):
        QThreadPool.globalInstance().waitForDone(5_000)
        qapp.processEvents()


def test_the_strip_is_hidden_when_there_is_nothing_to_ask(qapp, store):
    page = PhotoTaggerPage(store)
    _settle(qapp)

    assert page._suggestions_holder.isHidden() is True


def test_a_suggestion_against_a_named_pile_shows_one_chip(qapp, store):
    file_id = _photo(store, "maybe.jpg")
    daddy = store.create_pile(name="Daddy")
    face_id = store.add_face(file_id, (0, 0, 1, 1), _vec(0.6, 0.8))
    store.suggest_face(face_id, daddy)

    page = PhotoTaggerPage(store)
    _settle(qapp)

    assert page._suggestions_holder.isHidden() is False
    # One chip plus the trailing stretch this module's own layout keeps.
    assert page._suggestions_row.count() == 2


def test_a_suggestion_against_an_unnamed_pile_shows_no_chip(qapp, store):
    r"""Mirrors `test_photo_tagger.py::
    test_pending_suggestions_excludes_a_suggestion_against_an_unnamed_pile`
    at the widget level - the page must not invent a name to ask about."""
    file_id = _photo(store)
    unnamed = store.create_pile()
    face_id = store.add_face(file_id, (0, 0, 1, 1), _vec(0.6, 0.8))
    store.suggest_face(face_id, unnamed)

    page = PhotoTaggerPage(store)
    _settle(qapp)

    assert page._suggestions_holder.isHidden() is True


def test_saying_yes_assigns_the_face_and_clears_the_chip(qapp, store):
    file_id = _photo(store, "maybe.jpg")
    daddy = store.create_pile(name="Daddy")
    face_id = store.add_face(file_id, (0, 0, 1, 1), _vec(0.6, 0.8))
    store.suggest_face(face_id, daddy)

    page = PhotoTaggerPage(store)
    _settle(qapp)

    page._on_suggestion_decided(face_id, True)
    _settle(qapp)

    assert page._suggestions_holder.isHidden() is True
    assert store.faces_for_file(file_id)[0].pile_id == daddy
    assert "People: Daddy" in [c.text for c in store.chunks_for_file(file_id)]


def test_saying_no_returns_the_face_to_the_pool_and_clears_the_chip(qapp, store):
    file_id = _photo(store, "maybe.jpg")
    daddy = store.create_pile(name="Daddy")
    face_id = store.add_face(file_id, (0, 0, 1, 1), _vec(0.6, 0.8))
    store.suggest_face(face_id, daddy)

    page = PhotoTaggerPage(store)
    _settle(qapp)

    page._on_suggestion_decided(face_id, False)
    _settle(qapp)

    assert page._suggestions_holder.isHidden() is True
    face = store.faces_for_file(file_id)[0]
    assert face.pile_id is None
    assert face.suggested_pile_id is None


def test_every_yes_no_button_states_its_effect_in_its_tooltip(qapp, store):
    r"""The standing §6a rule this whole codebase holds every control to -
    `photo_tagger_page.py`'s own docstring names it explicitly."""
    from app.storage.sqlite_store import PendingSuggestion
    from app.ui.widgets.photo_tagger_page import _SuggestionChip

    suggestion = PendingSuggestion(
        face_id=1, file_id=1, path="/photos/a.jpg", bbox=(0, 0, 1, 1),
        pile_id=1, pile_name="Daddy")
    chip = _SuggestionChip(suggestion)

    from PyQt6.QtWidgets import QPushButton

    yes_no = chip.findChildren(QPushButton)
    assert len(yes_no) == 2
    for button in yes_no:
        assert button.toolTip(), f"{button.text()!r} button has no tooltip"
        assert "Daddy" in button.toolTip() or "guess" in button.toolTip().lower()


def test_twenty_suggestions_scroll_and_never_widen_the_window(qapp, store):
    """2026-10-05, the owner: "that window is not maximizing or scaling properly
    it is not scrolling on the bottom too". Twenty chips in a plain row made
    the window ~1,900 px wide - past the screen - so its bottom fell off it."""
    from app.storage.sqlite_store import PendingSuggestion
    from app.ui.widgets.photo_tagger_page import CHIP_BUTTON_MIN, PhotoTaggerPage
    from app.ui.widgets.photo_tagger_window import PhotoTaggerWindow

    window = PhotoTaggerWindow(store)
    page = window.page
    suggestions = [PendingSuggestion(face_id=n, file_id=n, path=f"/photos/{n}.jpg",
                                     bbox=(0, 0, 1, 1), pile_id=1, pile_name="Sarita")
                   for n in range(20)]
    page._suggestions_ready(suggestions, page._generation)
    window.show()
    qapp.processEvents()

    assert window.minimumSizeHint().width() < 1000, "the chips scroll, not the window"
    holder = page._suggestions_holder
    from app.ui.widgets.photo_tagger_page import _SuggestionChip

    chip = page._suggestions_strip.findChildren(_SuggestionChip)[0]
    assert holder.height() >= chip.sizeHint().height(), "no chip cut off - not a sliver"
    from PyQt6.QtWidgets import QPushButton

    for button in page._suggestions_strip.findChildren(QPushButton):
        assert button.width() >= CHIP_BUTTON_MIN, f"{button.text()!r} squeezed to a blob"
    window.close()
    window.deleteLater()
