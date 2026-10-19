"""Back and forward keep the contact picked on Contacts (IA-10, 2026-10-19).

Layer: L5.

Search -> a contact -> Mail -> Back used to land on Contacts but not on that person when
somebody else had been opened since. The page's sub-state is now the contact picked
(`presenter/page_history.contact_sub`): written as Contacts is left, picked again on Back and
Forward from the rows already read - and a contact deleted meanwhile leaves just the page, with
no dialog. The Qt-free half is in `test_page_history.py`.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.unit.conftest import gui_pump  # noqa: E402
from tests.unit.test_contacts_view_world import card, settle, world  # noqa: E402,F401


def _page_title(window) -> str:
    return window.rail.tabText(window.rail.currentIndex())


def _goto(window, title: str) -> None:
    index = window.nav.index_of(title)
    assert index >= 0, f"no page called {title!r}"
    window.rail.setCurrentIndex(index)


@pytest.fixture
def win(gui_mainwindow):
    app, window, _store, _engine = gui_mainwindow
    window.show()
    gui_pump(app)
    yield app, window
    _goto(window, "Search")
    gui_pump(app)


def _people(window, *names):
    hub = window.contacts_view.hub
    return [hub.create(card(*name.split())) for name in names]


def test_back_returns_to_the_same_contact(win):
    app, window = win
    ann, bob = _people(window, "Ann Lee", "Bob Bell")
    page = window.contacts_view.page
    nav = window.nav
    _goto(window, "Search")
    nav.reset()
    window._open_contact(ann)
    settle(page)
    assert page.current_contact_id() == ann
    _goto(window, "Mail")
    window._open_contact(bob)                   # somebody else, opened since
    settle(page)
    assert page.current_contact_id() == bob
    _goto(window, "Files")
    assert nav.back() and _page_title(window) == "Contacts"
    settle(page)
    assert page.current_contact_id() == bob
    assert nav.back() and _page_title(window) == "Mail"
    assert nav.back() and _page_title(window) == "Contacts"
    settle(page)
    assert page.current_contact_id() == ann, "back on the person the page was left on"
    assert nav.forward() and nav.forward() and _page_title(window) == "Contacts"
    settle(page)
    assert page.current_contact_id() == bob


def test_a_contact_deleted_meanwhile_returns_to_the_page_only(win, monkeypatch):
    app, window = win
    cy, di = _people(window, "Cy Day", "Di Dee")
    page = window.contacts_view.ensure_built()
    asked = []
    monkeypatch.setattr(page, "confirm", lambda *a, **k: asked.append(a) or False)
    monkeypatch.setattr(page, "_ask_discard", lambda *a, **k: asked.append(a) or False)
    nav = window.nav
    _goto(window, "Search")
    nav.reset()
    window._open_contact(cy)
    settle(page)
    _goto(window, "Mail")
    window._open_contact(di)
    settle(page)
    _goto(window, "Mail")
    window.contacts_view.hub.delete(cy)         # to the bin, while out of sight
    assert nav.back() and _page_title(window) == "Contacts"
    settle(page)
    assert nav.back() and _page_title(window) == "Mail"
    assert nav.back() and _page_title(window) == "Contacts"
    settle(page)
    assert page.current_contact_id() != cy
    assert cy not in page.model.ids()
    assert not asked, "no dialog"


def test_the_page_picks_from_rows_already_read_and_waits_for_a_read_in_flight(world):
    ann = world.add(card("Ann", "Lee"))
    bob = world.add(card("Bob", "Bell"))
    page = world.page()
    page.show_contact_id(ann)
    assert page.current_contact_id() == ann
    page.refresh()                               # a read in flight would put back the old pick
    page.show_contact_id(bob)
    settle(page)
    assert page.current_contact_id() == bob
    page.show_contact_id(None)
    assert page.current_contact_id() == bob


def test_a_binned_contact_is_not_picked_and_nothing_is_read_for_it(world, monkeypatch):
    from app.ui.contacts import loader

    ann = world.add(card("Ann", "Lee"))
    bob = world.add(card("Bob", "Bell"))
    page = world.page()
    page.select_ids([bob])
    settle(page)
    world.hub.delete(ann)
    page.refresh()
    settle(page)
    reads = []
    real = loader.run_task
    monkeypatch.setattr(loader, "run_task", lambda *a, **k: reads.append(a) or real(*a, **k))
    page.show_contact_id(ann)
    assert page.current_contact_id() == bob and not reads
