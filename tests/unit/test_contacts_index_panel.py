"""Order 1k 6f: "From your index" in the contact detail, and 2b's search hit.

Layer: L5

The facts are `app.contacts.activity`, the reading the MCP tool `contact_activity` shares, so the
first tests hold the two to the same answer. The panel then lists the latest mail (with its
direction and, for live mail, its account), the files exchanged, the photos and the first and
last contact; every item opens through one route; and the photo link is offered, accepted and
refused here. The last tests hold the Search -> Contacts hop and 2b: a contact is found by its
name, company, title, notes, email and phone.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThreadPool                                  # noqa: E402
from PySide6.QtWidgets import QApplication                              # noqa: E402

from app.accounts.store import AccountStore                             # noqa: E402
from app.contacts import activity                                       # noqa: E402
from app.contacts.hub import ContactHub                                 # noqa: E402
from app.storage.sqlite_store import SqliteStore                        # noqa: E402
from tests.unit.test_mcp_contacts import (                              # noqa: E402
    ATTACHMENT, MESSAGE_FROM, MESSAGE_TO, PHOTO, _card, seed, tools_for,
)


def settle(rounds: int = 40) -> None:
    app = QApplication.instance()
    for _ in range(rounds):
        QThreadPool.globalInstance().waitForDone(5000)
        app.processEvents()


@pytest.fixture()
def world(tmp_path):
    from app.core.config import load_settings
    from tests.unit.test_cli_wiring import env_file

    settings = load_settings(Path(env_file(tmp_path)))
    settings.fts_db.parent.mkdir(parents=True, exist_ok=True)
    ids = seed(settings.fts_db)
    return settings, ids


@pytest.fixture()
def store(world, qapp):
    settings, _ = world
    s = SqliteStore(settings.fts_db).connect()
    yield s
    s.close()


@pytest.fixture()
def panel(store, qapp):
    from app.ui.contacts.index_panel import IndexPanel

    p = IndexPanel()
    p.configure(ContactHub(store))
    opened: list[str] = []
    p.opener = opened.append
    p.opened = opened
    p.resize(480, 600)
    p.show()
    yield p
    p.close()


ANN_EMAILS = ["ann@acme.com"]


# -- one reading, two readers -------------------------------------------------------------------

def test_the_panels_facts_are_the_tools_facts(world, store):
    settings, ids = world
    answer = tools_for(settings).contact_activity(ids["ann"], 10)
    found = activity.gather(store, ids["ann"], ANN_EMAILS, 10)
    drop = lambda entries: [{k: v for k, v in e.items() if k != "sent_at"} for e in entries]  # noqa: E731
    assert drop(found["messages"]) == answer["messages"]
    assert found["first_contact"] == answer["first_contact"] and found["last_contact"] == answer["last_contact"]
    assert [{k: v for k, v in f.items() if k != "message"} for f in found["files_exchanged"]] == \
           [{k: v for k, v in f.items() if k != "message"} for f in answer["files_exchanged"]]
    assert found["photos"] == answer["photos"] and found["photo_match"] == answer["photo_match"] == "accepted"
    assert [m["path"] for m in found["messages"]] == [MESSAGE_TO, MESSAGE_FROM]
    assert all(isinstance(m["sent_at"], (int, float)) for m in found["messages"])


def test_the_tool_still_answers_as_before_for_a_live_mail_account(world, store):
    """`contact_activity` has never named the account of a live message; the panel's read does."""
    settings, ids = world
    path = f"account://{ids['work']}/INBOX/abc"
    fid = store.upsert_file(path, size_bytes=1, mtime_ns=1, ext="eml", source_kind="account_message",
                            status="INDEXED")
    store.set_message(fid, store_path="", entry_id="abc", subject="Live note", sender="Ann <ann@acme.com>",
                      recipients="me@example.invalid", sent_at=1_730_000_000, has_attach=0)
    named = activity.gather(store, ids["ann"], ANN_EMAILS, 10, {ids["work"]: "Work"})
    live = next(m for m in named["messages"] if m["path"] == path)
    assert live["account"] == "Work" and live["direction"] == "from them"
    tool = tools_for(settings).contact_activity(ids["ann"], 10)
    assert next(m for m in tool["messages"] if m["path"] == path)["account"] == "an account that is no longer set up"


# -- the panel ----------------------------------------------------------------------------------

def _texts(panel):
    return [b.text() for b in panel.items]


def test_it_lists_the_mail_files_and_photos_and_the_first_and_last_contact(world, panel):
    _, ids = world
    panel.show_for(ids["ann"], ANN_EMAILS)
    settle()
    assert "First in touch" in panel.summary.text() and "last in touch" in panel.summary.text()
    texts = _texts(panel)
    assert len(texts) == 4                                   # two messages, one attachment, one photo
    mail = [t for t in texts if t.startswith("About")]
    assert len(mail) == 2 and any("to them" in t for t in mail) and any("from them" in t for t in mail)
    assert any(t.startswith("quote.pdf") and "About 1" in t for t in texts)
    assert any(t.startswith("ann on the beach.jpg") for t in texts)
    assert "Photos are of Ann." in [lbl.text() for lbl in panel.findChildren(type(panel.summary))]
    assert not panel.isHidden()


def test_every_item_opens_through_the_one_route(world, panel):
    _, ids = world
    panel.show_for(ids["ann"], ANN_EMAILS)
    settle()
    for button in panel.items:
        button.click()
    assert sorted(panel.opened) == sorted([MESSAGE_FROM, MESSAGE_TO, ATTACHMENT, PHOTO])
    assert all(b.toolTip() and b.accessibleName() for b in panel.items)


def test_the_default_route_is_the_windows_open_route(panel, monkeypatch):
    import app.ui.workers as workers

    seen = []
    monkeypatch.setattr(workers, "open_row_async", lambda store, row, **k: seen.append((row.path, k)))
    from app.ui.contacts.index_panel import IndexPanel

    IndexPanel._open_path(panel, "account://1/INBOX/abc")
    assert seen == [("account://1/INBOX/abc", {})]


def test_a_contact_without_an_email_says_so_and_a_quiet_one_says_that(world, panel, store):
    _, ids = world
    panel.show_for(ids["bob"], [])
    settle()
    assert "no email address" in panel.summary.text() and panel.items == []
    quiet = ContactHub(store).create(_card("Cy", "Nobody", email="cy@nowhere.example"))
    panel.show_for(quiet, ["cy@nowhere.example"])
    settle()
    assert "No mail with this person" in panel.summary.text() and panel.items == []


def test_a_slower_older_read_never_overwrites_a_newer_one(world, panel):
    _, ids = world
    panel.show_for(ids["ann"], ANN_EMAILS)
    panel.show_for(ids["bob"], ["bob@other.com"])
    settle()
    assert panel.cid == ids["bob"]
    assert any("About 4" in t for t in _texts(panel)) and not any("About 1" in t for t in _texts(panel))


def test_clearing_hides_the_panel(world, panel):
    _, ids = world
    panel.show_for(ids["ann"], ANN_EMAILS)
    settle()
    panel.clear()
    assert panel.isHidden() and panel.items == []


# -- the photo link (2c) ------------------------------------------------------------------------

def _name_bob_in_photos(store):
    photo = store.upsert_file("C:/photos/bob.jpg", size_bytes=10, mtime_ns=1_600_000_000 * 10**9, ext="jpg",
                              source_kind="file", status="INDEXED")
    pile = store.create_pile("Bob Jones")
    store.assign_face(store.add_face(photo, (0.1, 0.1, 0.2, 0.2), b"x" * 8), pile)
    return pile


def test_suggest_link_then_accept_shows_the_persons_photos(world, panel, store):
    from app.contacts import people

    _, ids = world
    _name_bob_in_photos(store)
    panel.show_for(ids["bob"], ["bob@other.com"])
    settle()
    assert set(panel.link_buttons) == {"suggest"} and panel.link_buttons["suggest"].text() == "Suggest link"
    assert not any("bob.jpg" in t for t in _texts(panel))
    panel.link_buttons["suggest"].click()
    settle()
    assert people.pile_link(panel.hub, ids["bob"]).state == "suggested"
    assert set(panel.link_buttons) == {"accept", "decline"}
    assert not any("bob.jpg" in t for t in _texts(panel))               # a suggestion shows no photos
    panel.link_buttons["accept"].click()
    settle()
    assert people.pile_link(panel.hub, ids["bob"]).state == "accepted"
    assert any(t.startswith("bob.jpg") for t in _texts(panel))
    panel.items[[b.text().startswith("bob.jpg") for b in panel.items].index(True)].click()
    assert panel.opened == ["C:/photos/bob.jpg"]


def test_refusing_a_suggestion_is_remembered_and_offers_nothing_more(world, panel, store):
    from app.contacts import people

    _, ids = world
    _name_bob_in_photos(store)
    panel.show_for(ids["bob"], ["bob@other.com"])
    settle()
    panel.link_buttons["suggest"].click()
    settle()
    panel.link_buttons["decline"].click()
    settle()
    assert people.pile_link(panel.hub, ids["bob"]).state == "declined"
    assert panel.link_buttons == {}
    people.suggest_pile_links(panel.hub)
    assert people.pile_link(panel.hub, ids["bob"]).state == "declined"


def test_suggest_link_with_no_named_person_says_so(world, panel):
    _, ids = world
    panel.show_for(ids["bob"], ["bob@other.com"])
    settle()
    panel.link_buttons["suggest"].click()
    settle()
    assert "No person you have named in Photos has this name." in panel.summary.text()


# -- inside the Contacts page -------------------------------------------------------------------

def test_the_editor_shows_the_panel_for_the_picked_contact_and_hides_it_when_nothing_is(tmp_path, qapp):
    from tests.unit.test_contacts_view_world import World, card, settle as page_settle

    w = World(tmp_path)
    try:
        cid = w.add(card("Ann", "Lee", email="ann@acme.com"))
        page = w.page()
        assert page.editor.index_panel.hub is w.hub and page.editor.index_panel.isHidden()
        page.select_ids([cid])
        page_settle(page)
        page_settle()
        assert page.editor.index_panel.cid == cid and not page.editor.index_panel.isHidden()
        assert "From your index" in [lbl.text() for lbl in page.editor.index_panel.findChildren(type(page.editor.heading))]
        page.editor.clear()
        assert page.editor.index_panel.isHidden()
    finally:
        w.close()


def test_show_contact_clears_the_filter_and_picks_the_contact(tmp_path, qapp):
    from tests.unit.test_contacts_view_world import World, card, settle as page_settle

    w = World(tmp_path)
    try:
        w.add(card("Bob", "Bell"))
        cid = w.add(card("Ann", "Lee", email="ann@acme.com"))
        page = w.page()
        page.toolbar.filter.setText("Bell")
        page.reload(full=False)                              # what the filter's pause does
        page_settle(page)
        assert page.model.rowCount() == 1
        page.show_contact(cid)
        page_settle(page)
        page_settle()
        assert page.model.rowCount() == 2 and page.selected_ids() == [cid]
        assert page.editor.cid == cid and page.toolbar.filter.text() == ""
    finally:
        w.close()


def test_the_window_hop_goes_to_the_contacts_tab_on_that_person(tmp_path, qapp, monkeypatch):
    from app.ui.contacts_view import ContactsView
    from app.ui.shell import MainWindow

    shown, picked = [], []
    fake = type("W", (), {"contacts_view": type("V", (), {"show_contact": lambda s, c: picked.append(c)})(),
                          "_show": lambda s, v: shown.append(v)})()
    MainWindow._open_contact(fake, 42)
    assert shown == [fake.contacts_view] and picked == [42]
    view = ContactsView.__new__(ContactsView)
    calls = []
    view.ensure_built = lambda: type("P", (), {"show_contact": lambda s, c: calls.append(c)})()
    ContactsView.show_contact(view, "7")
    assert calls == [7]


# -- 2b: contacts are in the search ------------------------------------------------------------

@pytest.mark.parametrize("text, who", [
    ("Ann", "Ann Smith"),                    # name
    ("Smith", "Ann Smith"),
    ("Acme", "Ann Smith"),                   # company
    ("Chief Engineer", "Ann Smith"),         # title
    ("trade fair", "Ann Smith"),             # notes
    ("ann@acme.com", "Ann Smith"),           # email
    ("7700 900123", "Ann Smith"),            # phone
])
def test_a_search_by_name_company_title_notes_email_or_phone_finds_the_contact(tmp_path, qapp, text, who):
    s = SqliteStore(tmp_path / "find.db").connect()
    try:
        hub = ContactHub(s)
        card = _card("Ann", "Smith", email="ann@acme.com", phone="+44 7700 900123", company="Acme",
                     notes="Met at the trade fair")
        card.scalars["title"] = "Chief Engineer"
        hub.create(card)
        hub.create(_card("Bob", "Jones", email="bob@other.com", company="Other Ltd"))
        assert [h.display_name for h in hub.search(text)] == [who]
        assert hub.search("nothing like that") == []
    finally:
        s.close()
