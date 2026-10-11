"""Order 1k 2c: a contact linked to a named person on the photo side.

Layer: L7
"""

from __future__ import annotations

import pytest

from app.contacts import people
from app.contacts.hub import ContactHub
from app.storage.sqlite_store import SqliteStore
from tests.unit.accounts_fakes import person


@pytest.fixture
def hub(tmp_path):
    store = SqliteStore(tmp_path / "k.db").connect()
    yield ContactHub(store)
    store.close()


def test_a_matching_name_is_only_suggested_and_a_no_is_remembered(hub):
    pid = hub.store.create_pile("Ann Lee")
    hub.store.create_pile("Someone Else")
    a = hub.create(person("Ann", "Lee"))
    hub.create(person("Zed", "Moon"))
    assert people.suggest_pile_links(hub) == 1
    link = people.pile_link(hub, a)
    assert link.state == "suggested" and link.pile_id == pid
    assert people.faces_of_contact(hub, a) == []                  # not linked until accepted
    people.decline_pile_link(hub, a)
    assert people.suggest_pile_links(hub) == 0                    # not suggested again
    assert people.pile_link(hub, a).state == "declined"


def test_accepting_links_the_contact_to_the_persons_faces(hub):
    pid = hub.store.create_pile("Ann Lee")
    a = hub.create(person("Ann", "Lee"))
    people.suggest_pile_links(hub)
    people.accept_pile_link(hub, a)
    assert people.pile_link(hub, a).state == "accepted"
    people.unlink(hub, a)
    assert people.pile_link(hub, a) is None
    people.link_to_pile(hub, a, pid)
    assert people.pile_link(hub, a).state == "accepted"
