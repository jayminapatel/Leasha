"""'Merge all safe matches' on a real book: what it merges, what it refuses, that nothing is lost, and its undo.

Order 1u §4d. `app.contacts.book_actions.plan_safe_merges` and `merge_safe` had no test that imported them
(static scan 2026-10-13). These run them against a real `ContactHub` on a scratch SQLite store, so the
journal, the account links and the sources are the ones the app writes.
"""

from __future__ import annotations

import pytest

from app.accounts.store import AccountStore
from app.contacts import book_actions as ba
from app.contacts.card import Card, Field
from app.contacts.dupes import DuplicateGroup
from app.contacts.hub import ContactHub
from app.storage.sqlite_store import SqliteStore


@pytest.fixture()
def hub(tmp_path):
    store = SqliteStore(tmp_path / "book.db").connect()
    yield ContactHub(store)
    store.close()


def _card(given, family="", *, email="", phone="", company="", groups=()):
    fields = []
    if email:
        fields.append(Field("email", email, "work"))
    if phone:
        fields.append(Field("phone", phone, "mobile"))
    scalars = {"given": given}
    if family:
        scalars["family"] = family
    if company:
        scalars["company"] = company
    return Card(scalars=scalars, fields=fields, groups=list(groups))


def _emails(card):
    return sorted(f.value for f in card.fields if f.kind == "email")


def _phones(card):
    return sorted(f.value for f in card.fields if f.kind == "phone")


def _live(hub):
    return {r[0] for r in hub.conn.execute("SELECT id FROM contacts WHERE deleted_at IS NULL")}


# ---------------------------------------------------------------- what is merged

def test_two_cards_with_the_same_email_are_planned_and_merged_into_the_fuller_one(hub):
    thin = hub.create(_card("Ann", "Lee", email="ann@example.com"))
    full = hub.create(_card("Ann", "Lee", email="ann@example.com", phone="+447700900123", company="Acme"))
    scan = ba.scan_book(hub, use_cache=False)

    steps = ba.plan_safe_merges(scan.groups, scan.cards)

    assert len(steps) == 1
    step = steps[0]
    assert step.survivor == full, "the card holding the most keeps its id"
    assert step.others == [thin]
    assert set(step.reasons) <= ba.SAFE_REASONS

    summary = ba.merge_safe(hub, steps)

    assert summary.groups == 1 and summary.contacts == 2 and summary.batch_id
    assert _live(hub) == {full}
    folded = hub.conn.execute("SELECT deleted_at, merged_into FROM contacts WHERE id = ?", (thin,)).fetchone()
    assert folded["deleted_at"] is not None and folded["merged_into"] == full


def test_every_group_goes_under_one_batch_so_one_undo_puts_the_whole_action_back(hub):
    a1 = hub.create(_card("Ann", "Lee", email="ann@example.com"))
    a2 = hub.create(_card("Ann", "Lee", email="ann@example.com"))
    b1 = hub.create(_card("Bob", "Ray", email="bob@example.com"))
    b2 = hub.create(_card("Bob", "Ray", email="bob@example.com"))
    scan = ba.scan_book(hub, use_cache=False)
    steps = ba.plan_safe_merges(scan.groups, scan.cards)
    assert len(steps) == 2

    summary = ba.merge_safe(hub, steps)

    assert summary.groups == 2 and summary.contacts == 4
    batches = {r[0] for r in hub.conn.execute(
        "SELECT DISTINCT batch_id FROM contact_journal WHERE action IN ('merge', 'merged')")}
    assert batches == {summary.batch_id}
    assert len(_live(hub)) == 2

    res = hub.undo_batch(summary.batch_id)

    assert res.undone == 4 and res.skipped == 0
    assert _live(hub) == {a1, a2, b1, b2}
    for cid in (a1, a2, b1, b2):
        assert hub.conn.execute("SELECT merged_into FROM contacts WHERE id = ?", (cid,)).fetchone()[0] is None


def test_a_caller_batch_id_is_used_and_nothing_to_merge_offers_no_undo(hub):
    a1 = hub.create(_card("Ann", email="ann@example.com"))
    a2 = hub.create(_card("Ann", email="ann@example.com"))
    scan = ba.scan_book(hub, use_cache=False)
    steps = ba.plan_safe_merges(scan.groups, scan.cards)

    assert ba.merge_safe(hub, steps, batch_id="mine").batch_id == "mine"
    assert {r[0] for r in hub.conn.execute(
        "SELECT batch_id FROM contact_journal WHERE contact_id IN (?, ?) AND action != 'create'",
        (a1, a2))} == {"mine"}

    empty = ba.merge_safe(hub, [])
    assert (empty.batch_id, empty.groups, empty.contacts) == (None, 0, 0)


def test_a_rescan_after_the_merge_is_fresh_and_offers_the_merged_pair_no_more(hub):
    # A merge changes the book, so the held scan is not served again (`book_version`, and `forget_scan`).
    hub.create(_card("Ann", email="ann@example.com"))
    hub.create(_card("Ann", email="ann@example.com"))
    first = ba.scan_book(hub)
    ba.merge_safe(hub, ba.plan_safe_merges(first.groups, first.cards))

    again = ba.scan_book(hub)

    assert not again.cached
    assert ba.plan_safe_merges(again.groups, again.cards) == []


# ---------------------------------------------------------------- what is refused

@pytest.mark.parametrize("reasons", [
    ("same name",),                         # a name-only group a caller marked safe by mistake
    ("same email", "same name"),            # a safe reason with an unsafe one beside it
    (),                                     # no reason at all
])
def test_a_group_marked_safe_without_only_exact_email_or_phone_reasons_is_refused(hub, reasons):
    a = hub.create(_card("Ann", "Lee", email="ann@example.com"))
    b = hub.create(_card("Ann", "Lee", email="ann.lee@example.org"))
    cards = {a: hub.get_card(a), b: hub.get_card(b)}
    forged = DuplicateGroup((a, b), reasons, 0.9, True)

    assert ba.plan_safe_merges([forged], cards) == []


def test_a_group_not_flagged_safe_is_refused_even_with_safe_reasons(hub):
    a = hub.create(_card("Ann", email="ann@example.com"))
    b = hub.create(_card("Ann", email="ann@example.com"))
    cards = {a: hub.get_card(a), b: hub.get_card(b)}

    assert ba.plan_safe_merges([DuplicateGroup((a, b), ("same email",), 1.0, False)], cards) == []


def test_a_group_with_fewer_than_two_live_cards_is_left_out(hub):
    a = hub.create(_card("Ann", email="ann@example.com"))
    group = DuplicateGroup((a, 9999), ("same email",), 1.0, True)

    assert ba.plan_safe_merges([group], {a: hub.get_card(a)}) == []


def test_two_people_sharing_only_a_name_are_never_merged_by_the_bulk_action(hub):
    hub.create(_card("Sam", "Smith", email="sam1@example.com"))
    hub.create(_card("Sam", "Smith", email="sam2@example.com"))
    scan = ba.scan_book(hub, use_cache=False)

    assert scan.groups, "the scan still suggests them for the person to look at"
    assert ba.plan_safe_merges(scan.groups, scan.cards) == []
    assert len(_live(hub)) == 2


# ---------------------------------------------------------------- nothing is lost

def test_the_survivor_keeps_every_value_of_every_card_it_absorbs(hub):
    hub.create(_card("Ann", "Lee", email="ann@example.com", phone="+447700900123", groups=["Family"]))
    b = hub.create(_card("Ann", "Lee", email="ann@example.com", company="Acme", groups=["Work"]))
    extra = hub.get_card(b)
    extra.fields.append(Field("email", "ann.home@example.net", "home"))
    hub.update(b, extra)
    scan = ba.scan_book(hub, use_cache=False)
    steps = ba.plan_safe_merges(scan.groups, scan.cards)

    ba.merge_safe(hub, steps)

    survivor = steps[0].survivor
    card = hub.get_card(survivor)
    assert _emails(card) == ["ann.home@example.net", "ann@example.com"]
    assert _phones(card) == ["+447700900123"]
    assert card.get("company") == "Acme"
    assert sorted(card.groups) == ["Family", "Work"]


def test_the_folded_contact_keeps_its_own_card_sources_and_journal_in_the_bin(hub):
    a = hub.create(_card("Ann", "Lee", email="ann@example.com", phone="+447700900123"))
    b = hub.create(_card("Ann", "Lee", email="ann@example.com"), source="import", source_ref="old.csv")
    before_b = hub.get_card(b).to_json()
    sources_b = hub.sources(b)
    assert sources_b, "the import credited its values"
    scan = ba.scan_book(hub, use_cache=False)
    steps = ba.plan_safe_merges(scan.groups, scan.cards)
    assert steps[0].survivor == a and steps[0].others == [b]

    summary = ba.merge_safe(hub, steps)

    assert hub.get_card(b).to_json() == before_b
    assert hub.sources(b) == sources_b
    actions_b = [e.action for e in reversed(hub.history(b))]
    assert actions_b == ["create", "merged"]
    merged = hub.history(b)[0]
    assert merged.batch_id == summary.batch_id
    assert merged.detail["into"] == a
    assert merged.before["card"] == before_b, "the journal holds what the folded card was"
    survivor_entry = hub.history(a)[0]
    assert survivor_entry.action == "merge" and survivor_entry.detail["folded"] == [b]


def test_an_account_link_moves_to_the_survivor_and_a_surplus_one_is_queued_to_leave(hub):
    accounts = AccountStore(hub.store)
    home = accounts.add("google", "me@gmail.example")
    work = accounts.add("microsoft", "me@work.example")
    a = hub.create(_card("Ann", "Lee", email="ann@example.com", phone="+447700900123"))
    b = hub.create(_card("Ann", "Lee", email="ann@example.com"))
    hub.set_link(a, home, remote_id="g-a")
    hub.set_link(b, home, remote_id="g-b")
    hub.set_link(b, work, remote_id="w-b")
    scan = ba.scan_book(hub, use_cache=False)
    steps = ba.plan_safe_merges(scan.groups, scan.cards)
    assert steps[0].survivor == a

    summary = ba.merge_safe(hub, steps)

    assert hub.link(a, work) is not None and hub.link(a, work).remote_id == "w-b"
    assert hub.link(b, home).state == "pending_remove", "the duplicate leaves the account it doubled in"

    hub.undo_batch(summary.batch_id)

    assert hub.link(b, work) is not None and hub.link(b, work).remote_id == "w-b"
    assert hub.link(a, work) is None
    assert hub.link(a, home).remote_id == "g-a"


def test_undo_gives_back_each_card_exactly_as_it_was(hub):
    a = hub.create(_card("Ann", "Lee", email="ann@example.com", phone="+447700900123"))
    b = hub.create(_card("Ann", "Lee", email="ann@example.com", company="Acme"))
    before = {a: hub.get_card(a).to_json(), b: hub.get_card(b).to_json()}
    scan = ba.scan_book(hub, use_cache=False)
    summary = ba.merge_safe(hub, ba.plan_safe_merges(scan.groups, scan.cards))
    assert hub.get_card(a).to_json() != before[a] or hub.get_card(b).to_json() != before[b]

    hub.undo_batch(summary.batch_id)

    assert {a: hub.get_card(a).to_json(), b: hub.get_card(b).to_json()} == before
    assert _live(hub) == {a, b}
