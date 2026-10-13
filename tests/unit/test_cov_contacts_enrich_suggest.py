"""People who mail often and are in no contact: who is suggested, who never is, and accepting them.

Order 1u section 4d. `app.contacts.enrich.suggest` is reached through the enrichment window and switches, but no
test imported the module itself (static scan 2026-10-13). These pin `suggest_contacts`, `looks_automatic`
and `accept_suggestions` against a real store.
"""

from __future__ import annotations

import json

import pytest

from app.contacts.card import Card, Field
from app.contacts.enrich import suggest
from app.contacts.hub import ContactHub
from app.storage.sqlite_store import SqliteStore


@pytest.fixture()
def hub(tmp_path):
    store = SqliteStore(tmp_path / "s.db").connect()
    yield ContactHub(store)
    store.close()


_n = [0]


def _mail(hub, sender, times=1, sent=1_790_000_000):
    for _ in range(times):
        _n[0] += 1
        s = hub.store
        fid = s.upsert_file(f"mail/{_n[0]}.eml", parent_dir="mail", ext="eml", size_bytes=1, mtime_ns=_n[0],
                            status="INDEXED", source_kind="eml")
        s.set_message(fid, subject="hi", sender=sender, recipients=json.dumps(["me@home.example"]),
                      sent_at=sent + _n[0])


@pytest.mark.parametrize("address, name", [
    ("no-reply@shop.example", ""),
    ("noreply@shop.example", ""),
    ("do_not_reply@bank.example", ""),
    ("mailer-daemon@mx.example", ""),
    ("alerts@bank.example", ""),
    ("news@shop.example", "Shop Newsletter"),
    ("ann@bounces.shop.example", ""),
    ("hello@shop.example", "Shop (No Reply)"),
])
def test_automatic_senders_are_recognised(address, name):
    assert suggest.looks_automatic(address, name)


@pytest.mark.parametrize("address, name", [
    ("ann.lee@acme.example", "Ann Lee"),
    ("robert@acme.example", "Robert"),
    ("bottomley@acme.example", "Jo Bottomley"),      # "bot" at the start of a name is not a robot
    ("newsome@acme.example", "Kate Newsome"),
])
def test_people_are_not_mistaken_for_automatic_senders(address, name):
    assert not suggest.looks_automatic(address, name)


def test_a_frequent_sender_on_no_contact_is_suggested_with_their_count_and_most_used_name(hub):
    _mail(hub, "Ann Lee <Ann.Lee@acme.example>", 3)
    _mail(hub, "'Annie' <ann.lee@acme.example>", 1)
    _mail(hub, "Bob <bob@acme.example>", 2)                          # under the minimum

    found = suggest.suggest_contacts(hub.store)

    assert [(s.email, s.name, s.count) for s in found] == [("ann.lee@acme.example", "Ann Lee", 4)]
    assert found[0].last_seen is not None


def test_a_contact_the_owner_and_an_automatic_sender_are_never_suggested(hub):
    binned = hub.create(Card(scalars={"given": "Cy"}, fields=[Field("email", "cy@acme.example")]))
    hub.delete(binned)                                                # chosen to delete: stays unsuggested
    _mail(hub, "Cy <cy@acme.example>", 5)
    _mail(hub, "Me <me@home.example>", 5)
    _mail(hub, "Shop <noreply@shop.example>", 5)
    _mail(hub, "Dee <dee@acme.example>", 3)

    found = suggest.suggest_contacts(hub.store, owner_emails=["ME@home.example"])

    assert [s.email for s in found] == ["dee@acme.example"]


def test_most_messages_first_and_the_limit_and_minimum_are_kept(hub):
    _mail(hub, "A <a@x.example>", 3)
    _mail(hub, "B <b@x.example>", 5)
    _mail(hub, "C <c@x.example>", 4)

    assert [s.email for s in suggest.suggest_contacts(hub.store)] == ["b@x.example", "c@x.example", "a@x.example"]
    assert [s.email for s in suggest.suggest_contacts(hub.store, limit=1)] == ["b@x.example"]
    assert [s.email for s in suggest.suggest_contacts(hub.store, min_count=5)] == ["b@x.example"]


def test_accepting_makes_one_batch_of_contacts_named_and_sourced_and_never_twice(hub):
    found = [suggest.SuggestedContact("Smith, Dave", "dave@x.example", 4, 1_790_000_000),
             suggest.SuggestedContact("Mary Ann Jones", "mary@x.example", 3, None)]

    made = suggest.accept_suggestions(hub, found)

    assert len(made) == 2
    dave, mary = (hub.get_card(c) for c in made)
    assert (dave.get("given"), dave.get("family")) == ("Dave", "Smith")
    assert (mary.get("given"), mary.get("additional"), mary.get("family")) == ("Mary", "Ann", "Jones")
    assert [f.value for f in dave.fields] == ["dave@x.example"]
    batches = {hub.history(c)[0].batch_id for c in made}
    assert len(batches) == 1 and None not in batches
    sources = hub.sources(made[0])
    assert sources and all(src.endswith("suggest") for vals in sources.values() for src, _ref in vals)
    assert "4 messages from dave@x.example" in next(iter(sources.values()))[0][1]

    assert suggest.accept_suggestions(hub, found) == [], "accepting the same list again makes nobody"
    assert hub.count() == 2
