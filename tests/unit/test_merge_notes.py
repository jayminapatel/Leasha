"""Notes changed in two places are joined, not picked between: nothing is lost, so nothing is a clash."""

from __future__ import annotations

from app.contacts.card import ALL_CAPS, Card, merge


def card(notes: str) -> Card:
    return Card(scalars={"given": "Ann", "notes": notes})


def test_notes_edited_on_both_sides_keep_both():
    r = merge(card("met in 2019"), card("met in 2019\nlikes tea"), card("met in 2019\nowes me lunch"), ALL_CAPS,
              local_time=2, remote_time=1)
    assert r.conflicts == []
    notes = r.card.get("notes")
    assert "likes tea" in notes and "owes me lunch" in notes and r.hub_changed and r.push_needed


def test_a_note_that_contains_the_other_just_stands():
    r = merge(card("a"), card("a b"), card("a b c"), ALL_CAPS)
    assert r.card.get("notes") == "a b c" and r.conflicts == []


def test_a_real_clash_in_another_field_is_still_decided_and_recorded():
    local, remote = Card(scalars={"given": "Ann", "company": "Acme"}), Card(scalars={"given": "Ann", "company": "Zeta"})
    r = merge(Card(scalars={"given": "Ann", "company": "Old"}), local, remote, ALL_CAPS, local_time=1, remote_time=2)
    assert r.card.get("company") == "Zeta" and [c.unit for c in r.conflicts] == ["s:company"]
