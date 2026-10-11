"""An account that has room for less than Leasha holds: the rest stays here, is never read as deleted there,
and never makes the account look changed. (The owner, 2026-10-12: "have them in our contacts too so those issues
don't arise - we have locally what works everywhere".)"""

from __future__ import annotations

from app.accounts.providers.graph_contacts import GRAPH_CAPS
from app.contacts.card import ALL_CAPS, Card, Field, merge, project, units


def phones(n: int, primary_at: int = -1) -> list[Field]:
    return [Field("phone", f"+44 20 7946 {i:04d}", "work", {}, i == primary_at) for i in range(n)]


def card(fields: list[Field]) -> Card:
    return Card(scalars={"given": "Ann"}, fields=fields)


def test_the_account_holds_five_numbers_the_default_first():
    held = project(card(phones(8, primary_at=6)), GRAPH_CAPS)
    values = [f.value for f in held.fields]
    assert len(values) == 5 and "+44 20 7946 0006" in values            # the default always fits


def test_numbers_over_the_room_do_not_make_the_account_look_changed():
    local = card(phones(8))
    remote = project(local, GRAPH_CAPS)                                 # what the account holds after a write
    r = merge(remote, local, remote, GRAPH_CAPS)                        # base == remote: nothing changed there
    assert not r.push_needed and r.conflicts == []
    assert len([f for f in r.card.fields if f.kind == "phone"]) == 8    # and nothing was lost here


def test_the_account_deleting_one_it_holds_frees_a_slot_that_the_next_waiting_number_takes():
    local = card(phones(8))
    base = project(local, GRAPH_CAPS)
    remote = Card(scalars={"given": "Ann"}, fields=base.fields[1:])      # deleted one at the account
    r = merge(base, local, remote, GRAPH_CAPS)
    kept = [f.value for f in r.card.fields if f.kind == "phone"]
    assert base.fields[0].value not in kept                             # the delete went through
    assert len(kept) == 7                                               # the three that never fitted are still here


def test_kinds_without_a_limit_are_untouched():
    c = card(phones(8))
    assert len(units(c, ALL_CAPS)) == 1 + 8


def test_related_people_outlook_has_no_slot_for_are_not_compared():
    c = Card(fields=[Field("related", "Bo", "friend"), Field("related", "Cy", "spouse")])
    held = project(c, GRAPH_CAPS)
    assert [f.value for f in held.fields] == ["Cy"]
