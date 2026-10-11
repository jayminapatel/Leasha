"""Order 1k: the master account and the mirror.

Layer: L7

The owner, 2026-10-11: one account is the master list, and what it deletes is deleted everywhere;
every contact is kept in every account; an add or an edit in any account reaches all of them.
"""

from __future__ import annotations

import pytest

from tests.unit.accounts_fakes import person
from tests.unit.test_contact_sync_engine import World, only


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


def _two_accounts_with_ann(world, *, mirror=True, master=True):
    g, m = world.account("Google"), world.account("Work")
    rid_g = world.servers[g].add(person("Ann", "Lee", email="ann@x.example"))
    world.servers[g].add(person("Bob", "Bell", email="bob@x.example"))
    world.accounts.update_settings(master_account_id=g if master else None, mirror_contacts=mirror)
    world.sync()
    world.sync()
    return g, m, rid_g


def _alive(world):
    return sorted(r.display_name for r in world.hub.list_rows())


def _names(server):
    return sorted(f"{c.get('given')} {c.get('family')}" for c in server.live().values())


def test_the_mirror_keeps_every_contact_in_every_account(world):
    g, m, _ = _two_accounts_with_ann(world)
    assert _names(world.servers[g]) == ["Ann Lee", "Bob Bell"]
    assert _names(world.servers[m]) == ["Ann Lee", "Bob Bell"]            # created in Work by the mirror


def test_an_add_in_any_account_reaches_all_of_them(world):
    g, m, _ = _two_accounts_with_ann(world)
    world.servers[m].add(person("Cy", "Cox", email="cy@x.example"))       # added in the NON-master account
    world.sync()
    world.sync()
    assert _alive(world) == ["Ann Lee", "Bob Bell", "Cy Cox"]
    assert _names(world.servers[g]) == _names(world.servers[m]) == ["Ann Lee", "Bob Bell", "Cy Cox"]


def test_an_edit_in_any_account_reaches_all_of_them(world):
    g, m, _ = _two_accounts_with_ann(world)
    rid_m = next(r for r, rec in world.servers[m].recs.items() if rec.card.get("given") == "Ann")
    card = world.servers[m].recs[rid_m].card.copy()
    card.scalars["company"] = "Acme"
    world.servers[m].edit(rid_m, card)
    world.sync()
    world.sync()
    for aid in (g, m):
        ann = next(c for c in world.servers[aid].live().values() if c.get("given") == "Ann")
        assert ann.get("company") == "Acme"


def test_what_the_master_deletes_is_deleted_everywhere(world):
    g, m, rid_g = _two_accounts_with_ann(world)
    world.servers[g].remove(rid_g)                                         # deleted in the master (Google)
    out = world.sync()
    world.sync()
    assert only(out, g).plan.deleted_in == 1
    assert _alive(world) == ["Bob Bell"]
    assert _names(world.servers[m]) == ["Bob Bell"]                        # and from the other account
    assert world.hub.count(include_bin=True) == 2                          # kept in the Bin: undoable


def test_what_a_non_master_account_deletes_is_only_flagged(world):
    g, m, _ = _two_accounts_with_ann(world)
    rid_m = next(r for r, rec in world.servers[m].recs.items() if rec.card.get("given") == "Ann")
    world.servers[m].remove(rid_m)
    out = world.sync()
    world.sync()
    assert only(out, m).plan.flagged_deleted == 1 and only(out, m).plan.deleted_in == 0
    assert _alive(world) == ["Ann Lee", "Bob Bell"]                        # still here
    assert _names(world.servers[g]) == ["Ann Lee", "Bob Bell"]            # and still in the master
    assert _names(world.servers[m]) == ["Bob Bell"]                        # and not recreated against its wish


def test_without_a_master_a_delete_stays_a_flag(world):
    g, m, rid_g = _two_accounts_with_ann(world, master=False)
    world.servers[g].remove(rid_g)
    out = world.sync()
    assert only(out, g).plan.flagged_deleted == 1 and _alive(world) == ["Ann Lee", "Bob Bell"]


def test_a_master_that_suddenly_returns_nothing_cannot_empty_the_book(world):
    """The delete brake: an outage or a revoked permission looks like deleting everything."""
    g, m = world.account("Google"), world.account("Work")
    ids = [world.servers[g].add(person(f"P{i}", "Q", email=f"p{i}@x.example")) for i in range(30)]
    world.accounts.update_settings(master_account_id=g, mirror_contacts=True)
    world.sync()
    world.sync()
    assert world.hub.count() == 30
    for rid in ids:
        world.servers[g].remove(rid)
    out = world.sync()
    assert only(out, g).status == "held" and world.hub.count() == 30       # nothing deleted
    assert len(world.servers[m].live()) == 30
    out = world.sync(allow_brake=True)                                     # the person says yes
    world.sync(allow_brake=True)
    assert world.hub.count() == 0 and len(world.servers[m].live()) == 0


class _KeepsTwoEmails:
    """An account like Outlook: it quietly holds fewer values than it is sent (here two emails,
    and no primary flag), without saying so."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def _trim(self, card):
        card = card.copy()
        emails = [f for f in card.fields if f.kind == "email"][:2]
        card.fields = [f for f in card.fields if f.kind != "email"] + emails
        for f in card.fields:
            f.primary = False
        return card

    def create(self, card):
        return self._inner.create(self._trim(card))

    def update(self, remote_id, version, card):
        return self._inner.update(remote_id, version, self._trim(card))


def test_a_value_the_account_quietly_cannot_hold_stays_in_leasha_and_is_not_rewritten(world):
    """Outlook keeps three emails. Leasha used to read the missing ones as 'deleted at the account'
    on the next pull and take them out of the book."""
    from app.contacts.card import Card, Field
    g, m = world.account("Google"), world.account("Work")
    world.providers[m] = _KeepsTwoEmails(world.providers[m])
    card = Card(scalars={"given": "Ann", "family": "Lee"})
    card.fields = [Field("email", f"ann{i}@x.example", "work", primary=(i == 0)) for i in range(4)]
    world.servers[g].add(card)
    world.accounts.update_settings(mirror_contacts=True)
    world.sync()
    for _ in range(3):
        world.sync()
    ann = world.hub.get_card(world.hub.list_rows()[0].id)
    assert len([f for f in ann.fields if f.kind == "email"]) == 4          # nothing was taken out of the book
    writes = len(world.servers[m].writes)
    world.sync()
    world.sync()
    assert len(world.servers[m].writes) == writes                          # and it is not rewritten each sync
