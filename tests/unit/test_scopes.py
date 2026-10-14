r"""One scope table for the Search page and the quick search - review 2026-10-14, IA-4.

Layer: L5

The two boxes had two vocabularies, and only the quick search could narrow to
photos. Both read `presenter.scopes.SCOPES` now; neither box's existing words
changed; the Search page gains "Photos only"; Shift+Enter from the quick search
carries its chip to the Search page.
"""

from __future__ import annotations

import os

import pytest

from app.ui.presenter import scopes


def test_the_search_pages_words_are_unchanged_and_photos_only_is_added():
    from app.ui.widgets.search_bar import SCOPES

    assert SCOPES == (("Everything", "all"), ("Mail only", "mail"),
                      ("Documents only", "documents"), ("Photos only", "photos"),
                      ("Code only", "code"))


def test_the_quick_searchs_chips_are_unchanged_in_words_and_order():
    from app.ui.presenter.quick_search import SCOPE_CHIPS, chip_tip

    assert SCOPE_CHIPS == ((None, "All", "layout-grid"), ("files", "Files", "file-text"),
                           ("mail", "Mail", "mail"), ("photos", "Photos", "image"),
                           ("code", "Code", "code"))
    assert chip_tip("photos") == "Show only photos and pictures"
    assert chip_tip(None) == "Show every kind of result"


def test_both_boxes_cover_the_same_set():
    from app.ui.presenter.quick_search import SCOPE_CHIPS
    from app.ui.widgets.search_bar import SCOPES

    search = {value for _label, value in SCOPES}
    quick = {scopes.key_for_bucket(bucket) for bucket, _label, _icon in SCOPE_CHIPS}
    assert search == quick


@pytest.mark.parametrize(("bucket", "expected"), [
    (None, ("all", "beach")), ("mail", ("mail", "beach")), ("files", ("documents", "beach")),
    ("photos", ("documents", "beach type:image")), ("code", ("code", "beach")),
])
def test_the_quick_searchs_engine_request_is_the_tables(bucket, expected):
    from app.ui.presenter.quick_search import scope_request

    assert scope_request(bucket, "beach") == expected


def test_a_typed_type_wins_over_photos():
    assert scopes.engine_request(scopes.by_key("photos"), "beach type:pdf") == \
        ("documents", "beach type:pdf")
    assert scopes.engine_request(scopes.by_key("photos"), "") == ("documents", "type:image")


def test_an_unknown_value_is_everything():
    assert scopes.by_key("nonsense").key == "all"
    assert scopes.key_for_bucket("nonsense") == "all"


# ---------------------------------------------------------------------------
# Shift+Enter carries the chip
# ---------------------------------------------------------------------------

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_shift_enter_hands_the_chosen_scope_to_the_search_page(qtbot):
    from app.ui.widgets.mini_search import MiniSearch

    box = MiniSearch(engine=None)
    qtbot.addWidget(box)
    seen: list = []
    plain: list = []
    box.expanded_to.connect(lambda query, scope: seen.append((query, scope)))
    box.expanded.connect(plain.append)
    box._active_chip = "photos"
    box.box.setText("beach")
    box._expand()
    assert seen == [("beach", "photos")]
    assert plain == ["beach"], "the one-argument signal still fires"


def test_the_window_selects_the_scope_it_is_handed(qtbot, monkeypatch):
    """`shell._search_from_mini`, run against a stand-in window."""
    from types import SimpleNamespace

    from app.ui import shell
    from app.ui.search_view import SearchView

    class Engine:
        store = None

    view = SearchView(Engine())
    qtbot.addWidget(view)
    monkeypatch.setattr(view, "search_now", lambda: None)
    monkeypatch.setattr(shell, "bring_forward", lambda _w: None)
    window = SimpleNamespace(search_view=view, _show=lambda _v: None)
    shell.MainWindow._search_from_mini(window, "beach", "photos")
    assert view.input.text() == "beach" and view.current_scope() == "photos"
    view.flight._take_back()
