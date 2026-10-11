"""Order 1k: undoing a merge gives each person their own account records back.

Layer: L7

Found on the owner's index (2026-10-11): undoing one folded contact raised
`UNIQUE constraint failed: contact_links.account_id, contact_links.remote_id`, because the merge
had moved the folded contact's account link to the survivor and the undo tried to insert it again.
"""

from __future__ import annotations

import pytest

from tests.unit.accounts_fakes import person
from tests.unit.test_contact_sync_engine import World


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


def _two_people_in_two_accounts(world):
    g, m = world.account("Google"), world.account("Work")
    world.servers[g].add(person("Alaa", "Arawi", email="alaa@x.com"))
    world.servers[m].add(person("Stephen", "Tagg", email="stephen@y.com"))
    world.sync()
    rows = {r.display_name: r.id for r in world.hub.list_rows()}
    return g, m, rows["Alaa Arawi"], rows["Stephen Tagg"]


def _links(world, cid):
    return sorted((r["account_id"], r["state"]) for r in world.store.conn.execute(
        "SELECT account_id, state FROM contact_links WHERE contact_id = ?", (cid,)))


def test_undoing_a_folded_contact_returns_its_account_record_to_it(world):
    g, m, alaa, stephen = _two_people_in_two_accounts(world)
    world.hub.merge(stephen, [alaa])
    assert _links(world, stephen) == [(g, "synced"), (m, "synced")]          # the survivor inherited Google's
    folded = world.store.conn.execute(
        "SELECT id FROM contact_journal WHERE contact_id = ? AND action = 'merged'", (alaa,)).fetchone()[0]
    world.hub.undo_entry(folded)                                              # used to raise IntegrityError
    assert _links(world, alaa) == [(g, "synced")]
    assert _links(world, stephen) == [(m, "synced")]
    assert world.hub.get_row(alaa).display_name == "Alaa Arawi"
    assert world.hub.get_row(alaa) is not None and world.hub.count() == 2


def _group_of_three(world):
    """Yarob (the survivor), Pradeep (a wrong fold: only a landline in common) and Yarob's own
    second copy (a right fold: same email)."""
    g, m = world.account("Google"), world.account("Work")
    world.servers[g].add(person("Yarob", "Alkassar", email="yarob@x.com", phone="07700 900111"))
    world.servers[m].add(person("Yarob", "Alkassar", email="yarob@x.com", phone="07700 900222"))
    world.servers[g].add(person("Pradeep", "Vishvanathan", email="pradeep@y.com", phone="07700 900333"))
    world.sync()
    rows = [(r.id, r.display_name) for r in world.hub.list_rows()]
    ids = {n: i for i, n in rows}
    yarob_ids = sorted(i for i, n in rows if n == "Yarob Alkassar")
    return g, m, yarob_ids[0], yarob_ids[1], ids["Pradeep Vishvanathan"]


def _merge(world, survivor, others):
    """What the Duplicates tool does: one card from all of them, written to the survivor."""
    from app.contacts import dupes
    cards = [world.hub.get_card(i) for i in [survivor, *others]]
    world.hub.merge(survivor, others, card=dupes.merge_cards(cards).card)


def _phones(world, cid):
    return sorted(f.value for f in world.hub.get_card(cid).fields if f.kind == "phone")


def test_unmerge_gives_the_wrong_person_back_and_keeps_what_the_right_merge_brought(world):
    g, m, yarob, yarob_copy, pradeep = _group_of_three(world)
    _merge(world, yarob, [yarob_copy, pradeep])
    assert _phones(world, yarob) == ["07700 900111", "07700 900222", "07700 900333"]
    res = world.hub.unmerge(pradeep)
    assert res.restored == pradeep and res.survivor == yarob and res.survivor_rebuilt
    assert _phones(world, yarob) == ["07700 900111", "07700 900222"]        # Pradeep's number left
    assert _phones(world, pradeep) == ["07700 900333"]
    assert world.hub.get_row(pradeep).display_name == "Pradeep Vishvanathan"
    assert world.hub.count() == 2 and _links(world, pradeep) == [(g, "synced")]
    assert _links(world, yarob) == [(g, "synced"), (m, "synced")]            # the right fold's record stays


def test_unmerge_leaves_a_survivor_edited_since_the_merge_alone(world):
    g, m, yarob, yarob_copy, pradeep = _group_of_three(world)
    _merge(world, yarob, [yarob_copy, pradeep])
    card = world.hub.get_card(yarob)
    card.scalars["notes"] = "my own note, written after the merge"
    world.hub.update(yarob, card)
    res = world.hub.unmerge(pradeep)
    assert res.restored == pradeep and not res.survivor_rebuilt
    assert world.hub.get_card(yarob).scalars["notes"] == "my own note, written after the merge"
    assert _phones(world, pradeep) == ["07700 900333"]


def test_unmerge_of_a_contact_that_was_never_folded_does_nothing(world):
    g, m, yarob, yarob_copy, pradeep = _group_of_three(world)
    assert world.hub.unmerge(pradeep).restored == 0


def test_giving_back_two_people_one_after_the_other_leaves_neither_in_the_survivor(world):
    """Seen live: the second give-back found the survivor changed by the first rebuild and left
    the other person's data behind."""
    g, m = world.account("Google"), world.account("Work")
    world.servers[g].add(person("Yarob", "Alkassar", email="yarob@x.com", phone="07700 900111"))
    world.servers[g].add(person("Pradeep", "Vishvanathan", phone="07700 900333"))
    world.servers[m].add(person("Pradeep", "Vishvanathan", phone="07700 900333"))
    world.sync()
    rows = {}
    for r in world.hub.list_rows():
        rows.setdefault(r.display_name, []).append(r.id)
    yarob, (p1, p2) = rows["Yarob Alkassar"][0], rows["Pradeep Vishvanathan"]
    _merge(world, yarob, [p1, p2])
    assert _phones(world, yarob) == ["07700 900111", "07700 900333"]
    world.hub.unmerge(p1)
    world.hub.unmerge(p2)
    assert _phones(world, yarob) == ["07700 900111"]
    assert world.hub.rebuild_merged(yarob) in (True, False)          # asking again changes nothing
    assert _phones(world, yarob) == ["07700 900111"]


def test_not_a_duplicate_teaches_leasha_which_numbers_are_shared(world):
    from app.contacts import dupes
    world.account("Google")
    cid = [world.hub.create(person("Ann", "Lee", phone="07911 123456")),
           world.hub.create(person("Ann", "Lee", phone="07911 123456"))]
    items = world.hub.all_cards()
    assert dupes.scan_duplicates(items, set())[0].safe                      # looks like one person
    world.hub.reject_pair(*cid)
    assert world.hub.shared_values() == {"phone:+447911123456"}
    again = dupes.scan_duplicates(items, world.hub.rejected_pairs(), world.hub.region, world.hub.shared_values())
    assert again == []
    world.hub.unmark_shared({"phone:+447911123456"})
    assert world.hub.shared_values() == set()
