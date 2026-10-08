r"""Settings › Search: "Offer recent searches" turned off hides them. 2026-10-08.

Layer: L5.

**The bug.** The window pushes the Search tab a *dictionary* of search
preferences (`policy.preferences`), not `Settings`. The switch is not a
search behaviour, so the dictionary never carried it, and `first_contact.
rows_for` read it with `getattr` - which on a dictionary always falls back to
the default. Turned off, the switch did nothing: the empty box went on listing
the person's own past questions, on a screen anyone can see.

These drive the real route: the settings controller's push, the Search tab
built for real, its empty box's sections.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication                        # noqa: E402

from app.ui.first_contact import RECENT_HEADING, offer_recent, rows_for, sections  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def _headings(found) -> list:
    return [heading for heading, _rows in found]


def _pushed(settings, overrides=None) -> dict:
    """What `_apply_search_preferences` hands the Search tab, captured."""
    from app.ui.controllers.settings_controller import SettingsController

    seen: dict = {}
    window = SimpleNamespace(
        _settings=settings, _settings_overrides=dict(overrides or {}),
        search_view=SimpleNamespace(set_search_preferences=seen.update))
    SettingsController._apply_search_preferences(SimpleNamespace(_w=window))
    return seen


def _settings(offer: bool):
    from app.core.config import Settings

    fields = {name: field.default for name, field in Settings.model_fields.items()
              if name.startswith("search_")}
    return SimpleNamespace(**{**fields, "search_offer_recent": offer})


def test_the_preferences_the_search_tab_is_given_carry_the_switch():
    r"""After a restart: `.env` says off, so what reaches the Search tab says
    off, and its empty box offers no recent searches."""
    rows = [{"query": "leeds site survey"}]
    pushed = _pushed(_settings(False))
    assert rows_for(rows, pushed) == ()
    assert RECENT_HEADING not in _headings(sections(rows, (), pushed))
    assert rows_for(rows, _pushed(_settings(True))) == ("leeds site survey",)


def test_a_switch_changed_in_this_session_applies_at_once():
    """Turned off in Settings a moment ago - an override, a raw `.env` string."""
    rows = [{"query": "leeds site survey"}]
    assert rows_for(rows, _pushed(_settings(True), {"search_offer_recent": "false"})) == ()
    assert rows_for(rows, _pushed(_settings(False), {"search_offer_recent": "true"})) == (
        "leeds site survey",)


@pytest.mark.parametrize(("given", "offered"), [
    ({"search_offer_recent": False}, False), ({"search_offer_recent": "false"}, False),
    ({"search_offer_recent": True}, True), ({}, True), (None, True),
    (SimpleNamespace(search_offer_recent=False), False),
])
def test_the_switch_is_read_from_settings_or_the_dictionary(given, offered):
    assert offer_recent(given) is offered


def test_the_search_tab_hides_them_and_brings_them_back(qapp):
    r"""The real Search tab over a real history. Off: its empty box offers no
    recent searches. On again: they come back without a restart."""
    from app.search.engine import SearchEngine
    from app.storage.sqlite_store import SqliteStore
    from app.ui.search_view import SearchView

    store = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "recent.db").connect()
    store.log_search("leeds site survey", hits=1)

    class _NoVectors:
        def search(self, *_a, **_k):
            return []

    class _NoModel:
        def embed(self, _t):
            raise RuntimeError("no model")

    engine = SearchEngine(store, _NoVectors(), _NoModel())
    view = SearchView(engine)

    def wait_for(check, seconds=5.0):
        end = time.time() + seconds
        while time.time() < end and not check():
            qapp.processEvents()
            time.sleep(0.01)
        return check()

    try:
        assert wait_for(lambda: RECENT_HEADING in _headings(view.saved.sections()))
        view.set_search_preferences(_pushed(_settings(False)))
        assert RECENT_HEADING not in _headings(view.saved.sections())

        # A read made while it is off holds nothing - the fetch honours it too.
        view.saved.forget_recent()
        wait_for(lambda: False, seconds=0.5)
        assert view.saved._recent == ()
        view.set_search_preferences(_pushed(_settings(True)))
        assert wait_for(lambda: RECENT_HEADING in _headings(view.saved.sections())), (
            "switched back on, the recent searches did not come back")
    finally:
        if hasattr(view, "shutdown"):
            view.shutdown()
        engine.close()
        store.close()
