"""Order 1k: a person arriving from a second account joins the one in the master.

Layer: L7

The master is the local contact book. A contact pulled from an account that is certainly someone the
book already holds (same email or mobile, names that agree) becomes another link of that person;
anything less certain becomes a new contact for the Duplicates screen. Shared landlines, shared
numbers and different names never join two people.
"""

from __future__ import annotations

import pytest

from tests.unit.accounts_fakes import person
from tests.unit.test_contact_sync_engine import World

MOBILE = "07911 123456"
OFFICE = "020 7946 0018"


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


def _names(world):
    return sorted(r.display_name for r in world.hub.list_rows())


def _link_count(world, name):
    row = next(r for r in world.hub.list_rows() if r.display_name == name)
    return sorted(row.accounts)


def test_the_same_email_in_two_accounts_is_one_contact_with_two_links(world):
    g, m = world.account("Google"), world.account("Work")
    world.servers[g].add(person("Ann", "Lee", email="ann@x.example"))
    world.servers[m].add(person("A.", "Lee", email="ANN@x.example", phone=MOBILE))
    world.sync()
    assert world.hub.count() == 1
    assert _link_count(world, "Ann Lee") == sorted([g, m])
    assert world.sync() and world.hub.count() == 1                      # and a second sync stays one


def test_the_same_mobile_with_a_matching_name_joins_too(world):
    g, m = world.account("Google"), world.account("Work")
    world.servers[g].add(person("Bob", "Bell", phone=MOBILE))
    world.servers[m].add(person("Bob", "Bell", phone="+44 7911 123456"))
    world.sync()
    assert world.hub.count() == 1 and len(_link_count(world, "Bob Bell")) == 2


def test_an_office_landline_never_joins_different_people(world):
    g, m = world.account("Google"), world.account("Work")
    world.servers[g].add(person("Alaa", "Arawi", phone=OFFICE))
    world.servers[m].add(person("Stephen", "Tagg", phone=OFFICE))
    world.sync()
    assert _names(world) == ["Alaa Arawi", "Stephen Tagg"]


def test_the_same_name_on_a_landline_only_is_left_for_the_duplicates_screen(world):
    g, m = world.account("Google"), world.account("Work")
    world.servers[g].add(person("Ann", "Lee", phone=OFFICE))
    world.servers[m].add(person("Ann", "Lee", phone=OFFICE))
    world.sync()
    assert _names(world) == ["Ann Lee", "Ann Lee"]


def test_different_names_on_one_mobile_stay_separate(world):
    g, m = world.account("Google"), world.account("Work")
    world.servers[g].add(person("Ann", "Lee", phone=MOBILE))
    world.servers[m].add(person("Joe", "Lee", phone=MOBILE))
    world.sync()
    assert _names(world) == ["Ann Lee", "Joe Lee"]


def test_a_number_marked_shared_does_not_join_even_matching_names(world):
    g, m = world.account("Google"), world.account("Work")
    world.hub.mark_shared({"phone:+447911123456"})
    world.servers[g].add(person("Ann", "Lee", phone=MOBILE))
    world.servers[m].add(person("Ann", "Lee", phone=MOBILE))
    world.sync()
    assert _names(world) == ["Ann Lee", "Ann Lee"]


def test_two_possible_people_are_a_doubt_so_nothing_is_joined(world):
    g, m = world.account("Google"), world.account("Work")
    world.servers[g].add(person("Ann", "Lee", email="ann@x.example"))
    world.servers[g].add(person("Ann", "Lee", email="ann@x.example"))      # a duplicate inside one account
    world.servers[m].add(person("Ann", "Lee", email="ann@x.example"))
    world.sync()
    assert world.hub.count() == 3
