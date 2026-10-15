r"""IA-6 (UX review 2026-10-14): Photos and Contacts, linked both ways.

Layer: L5

From Photos, a person - in the People list or on the naming page - has "Open contact"
when linked to one and "Link to contact…" to pick one; the link is the row the contact's
own panel writes. From a contact, "See all photos" opens Photos on `who:` that person and
"See mail from them" opens Mail on `from:` the address.
"""

from __future__ import annotations

import os

import pytest

from app.ui.presenter.photo_people import (LINK_CONTACT, OPEN_CONTACT, choice_labels, fold,
                                           linked_contact, mail_box, person_menu, photos_box)


# -- the words, no Qt -----------------------------------------------------------------------

def test_the_person_menu_offers_open_only_when_linked():
    assert person_menu(True) == (OPEN_CONTACT, LINK_CONTACT)
    assert person_menu(False) == (LINK_CONTACT,)
    assert OPEN_CONTACT[0] == "Open contact" and LINK_CONTACT[0] == "Link to contact…"


def test_the_contact_to_open_is_the_first_linked():
    links = {3: [(7, "Ann Smith"), (9, "Ann S")]}
    assert linked_contact(links, 3) == 7
    assert linked_contact(links, 4) is None
    assert linked_contact({}, 3) is None and linked_contact(links, None) is None


def test_the_picker_tells_two_contacts_of_one_name_apart():
    labels = choice_labels([(1, "Ann Smith", "ann@acme.com"), (2, "Bob", ""),
                            (3, "Ann Smith", "ann@home.example"), (4, "", "cy@x.example"),
                            (5, "Bob", ""), (6, "", "")])
    assert labels == ["Ann Smith <ann@acme.com>", "Bob", "Ann Smith <ann@home.example>",
                      "cy@x.example", "Bob (2)", "(no name)"]
    assert len(set(labels)) == len(labels)


def test_the_switches_are_the_ones_every_box_reads():
    assert photos_box("Ann") == "who:Ann"
    assert photos_box("Ann Smith") == 'who:"Ann Smith"'
    assert photos_box("  ") == ""
    assert mail_box("ann@acme.com") == "from:ann@acme.com"
    assert mail_box("") == ""
    assert fold("  Ann   SMITH ") == "ann smith"


# -- the store side -------------------------------------------------------------------------

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.contacts import people                                          # noqa: E402
from app.contacts.hub import ContactHub                                  # noqa: E402
from tests.unit.test_contacts_index_panel import (                      # noqa: E402,F401
    ANN_EMAILS, panel, settle, store, world,
)


def _ann_pile(store):
    return store.conn.execute("SELECT id FROM piles WHERE name = 'Ann'").fetchone()[0]


def test_only_an_accepted_link_to_a_live_contact_counts(world, store):
    _, ids = world
    pile = _ann_pile(store)
    assert people.contacts_of_piles(store.conn) == {pile: [(ids["ann"], "Ann Smith")]}
    assert people.named_piles(store.conn)["ann"] == pile
    people.decline_pile_link(ContactHub(store), ids["ann"])
    assert people.contacts_of_piles(store.conn) == {}
    names = [name for _cid, name, _email in people.contact_choices(store.conn)]
    assert names == ["Ann Smith", "Bob Jones"]
    assert people.contact_choices(store.conn)[0][2] == "ann@acme.com"


# -- from a contact: See all photos, See mail from them ----------------------------------------

def test_a_linked_contact_offers_its_photos_and_its_mail(world, panel):
    _, ids = world
    asked = []
    panel.photos_requested.connect(lambda box: asked.append(("photos", box)))
    panel.mail_requested.connect(lambda box: asked.append(("mail", box)))
    panel.show_for(ids["ann"], ANN_EMAILS)
    settle()
    assert panel.see_buttons["photos"].text() == "See all photos"
    assert panel.see_buttons["mail"].text() == "See mail from them"
    assert all(b.toolTip() for b in (panel.see_buttons["photos"], panel.see_buttons["mail"]))
    panel.see_buttons["photos"].click()
    panel.see_buttons["mail"].click()
    assert asked == [("photos", "who:Ann"), ("mail", "from:ann@acme.com")]


def test_an_unlinked_contact_offers_its_mail_only_and_one_with_no_address_nothing(world, panel):
    _, ids = world
    panel.show_for(ids["bob"], ["bob@other.com"])
    settle()
    assert "mail" in panel.see_buttons and "photos" not in panel.see_buttons
    panel.show_for(ids["bob"], [])
    settle()
    assert "mail" not in panel.see_buttons


@pytest.mark.slow
def test_the_window_opens_photos_and_mail_on_those_switches(gui_mainwindow):
    from tests.unit.conftest import gui_pump

    app, window, _store, _engine = gui_mainwindow
    window.show_photos_for('who:"Ann Smith"')
    gui_pump(app, 2)
    assert window.rail.currentIndex() == window._tab_index[window.photos_view]
    assert window.photos_view.input.text() == 'who:"Ann Smith"'
    window.show_mail_for("from:ann@acme.com")
    gui_pump(app, 2)
    assert window.rail.currentIndex() == window._tab_index[window.mail_view]
    assert window.mail_view.input.text() == "from:ann@acme.com"
    # The contact panel's two signals reach these through the Contacts page's host.
    page = window.contacts_view.ensure_built()
    page.editor.index_panel.photos_requested.emit("who:Ann")
    gui_pump(app, 2)
    assert window.photos_view.input.text() == "who:Ann"
    page.editor.index_panel.mail_requested.emit("from:bob@other.com")
    gui_pump(app, 2)
    assert window.mail_view.input.text() == "from:bob@other.com"
    window.photos_view.input.clear()
    window.mail_view.input.clear()
    window._show(window.search_view)


# -- from Photos: Open contact, Link to contact… -------------------------------------------------

@pytest.fixture()
def photos(store, qapp):
    from app.ui.photos_view import PhotosView

    view = PhotosView(store)
    settle()
    yield view
    view.shutdown()
    view.deleteLater()


def _person(view, name):
    return next(e for e in view.sidebar.entries() if e.value == ("who", name))


def test_the_people_list_offers_open_contact_for_a_linked_person(world, store, photos):
    from app.ui.widgets.photo_contacts import person_action
    from app.ui.widgets.photo_page_parts import library

    _, ids = world
    _rows, waiting, counts = library(store)
    photos.sidebar.fill(counts, waiting)
    ann = _person(photos, "Ann")
    assert [line[0] for line in ann.menu] == ["Open contact", "Link to contact…"]
    opened = []
    photos.contact_requested.connect(opened.append)
    person_action(photos, ann, "open_contact")
    assert opened == [ids["ann"]]


def test_link_to_contact_picks_from_the_book_and_links_at_once(world, store, photos, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    from app.ui.widgets.photo_contacts import pick_and_link

    _, ids = world
    pile = store.create_pile("Bobby")
    asked = []

    def choose(_parent, _title, question, labels, *_a, **_k):
        asked.append((question, list(labels)))
        return "Bob Jones", True

    monkeypatch.setattr(QInputDialog, "getItem", staticmethod(choose))
    done = []
    pick_and_link(photos, store, pile, "Bobby", done=done.append)
    settle()
    assert asked == [("Which contact is Bobby?", ["Ann Smith", "Bob Jones"])]
    assert done == [ids["bob"]]
    link = people.pile_link(ContactHub(store), ids["bob"])
    assert (link.pile_id, link.state) == (pile, "accepted")


def test_a_cancelled_pick_links_nothing(world, store, photos, monkeypatch):
    from PySide6.QtWidgets import QInputDialog

    from app.ui.widgets.photo_contacts import pick_and_link

    _, ids = world
    pile = store.create_pile("Bobby")
    monkeypatch.setattr(QInputDialog, "getItem", staticmethod(lambda *a, **k: ("", False)))
    pick_and_link(photos, store, pile, "Bobby")
    settle()
    assert people.pile_link(ContactHub(store), ids["bob"]) is None


def test_the_naming_page_menu_has_open_and_link_contact(world, store, qapp, monkeypatch):
    from app.ui.widgets import menus
    from app.ui.widgets.photo_tagger_page import ROLE_PILE_ID, PhotoTaggerPage

    _, ids = world
    page = PhotoTaggerPage(store)
    page.resize(800, 600)
    page.show()
    page.reload()
    settle()
    pile = _ann_pile(store)
    built = []
    monkeypatch.setattr(menus, "show", lambda menu, *a, **k: built.append(menu))
    item = next(page._list.item(n) for n in range(page._list.count())
                if page._list.item(n).data(ROLE_PILE_ID) == pile)
    page._on_context_menu(page._list.visualItemRect(item).center())
    lines = {a.text(): a for a in built[-1].actions() if a.text()}
    assert lines["Open contact"].isEnabled()
    assert "Link to contact…" in lines
    opened = []
    page.contact_requested.connect(opened.append)
    lines["Open contact"].trigger()
    assert opened == [ids["ann"]]
    page.hide()
    page.deleteLater()
