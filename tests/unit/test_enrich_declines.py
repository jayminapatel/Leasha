"""Order 1s 7c, D5: "Never suggest these" is remembered - schema 43's `enrich_declines`.

Layer: L7

A value the person turned down for a contact is not offered again for that contact by any
source; another contact, or another value, is unaffected; the memory can be undone one row at
a time or cleared. The value is kept as a salted SHA-256, so an email address turned down
cannot be read back out of the table. Looking (with nothing turned down) writes nothing.
"""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from app.accounts.store import AccountSettings
from app.contacts.card import Card, Field
from app.contacts.enrich import declines
from app.contacts.enrich.base import Proposal, drop_declined
from app.contacts.enrich.run import enrich_selection
from app.contacts.hub import ContactHub
from app.storage import migrations
from app.storage.sqlite_store import SqliteStore


@pytest.fixture()
def hub(tmp_path):
    store = SqliteStore(tmp_path / "d.db").connect()
    yield ContactHub(store)
    store.close()


def _person(hub, given, email, phone=""):
    card = Card(scalars={"given": given})
    card.fields.append(Field("email", email, "work"))
    if phone:
        card.fields.append(Field("phone", phone, "work"))
    return hub.create(card)


def test_schema_43_makes_the_declines_table():
    # A floor, not `== 43`: it broke the moment schema 44 (`piles.cover_face_id`) came,
    # the trap `test_phash_column_migration.py` records (2026-10-25).
    assert migrations.CURRENT_VERSION >= 43
    assert migrations.MIGRATIONS[43].__name__ == "_v43_enrich_declines"
    conn = sqlite3.connect(":memory:")
    migrations._v43_enrich_declines(conn)
    migrations._v43_enrich_declines(conn)                      # again: additive and idempotent
    cols = {r[1] for r in conn.execute("PRAGMA table_info(enrich_declines)")}
    assert {"contact_id", "field_key", "value_hash", "label", "shown", "source", "at"} <= cols


def test_a_turned_down_value_is_never_offered_again_for_that_contact_by_any_source(hub):
    a = _person(hub, "Ann", "ann@acme-widgets.co.uk", "020 7946 0018")
    b = _person(hub, "Bo", "bo@acme-widgets.co.uk")
    title = Proposal(a, "title", "Job title", "", "Senior Engineer", "signature", "Re: x, 3 May")
    phone = Proposal(a, "phone", "work", "", "+44 20 7946 0999", "signature", "Re: x, 3 May")
    assert declines.decline(hub.store, [title, phone], region=hub.region) == 2

    again = [Proposal(a, "title", "Job title", "", "senior  engineer", "wikidata", "https://x"),   # other source
             Proposal(a, "phone", "mobile", "", "020 7946 0999", "signature", "Re: y"),           # other format
             Proposal(a, "title", "Job title", "", "Director", "signature", "Re: y"),             # other value
             Proposal(b, "title", "Job title", "", "Senior Engineer", "signature", "Re: z")]      # other contact
    kept = drop_declined(hub.store, again, region=hub.region)
    assert [(p.contact_id, p.new) for p in kept] == [(a, "Director"), (b, "Senior Engineer")]


def test_the_run_leaves_out_what_was_turned_down(hub):
    a = _person(hub, "Ann", "ann@acme-widgets.co.uk")
    settings = AccountSettings()
    first = enrich_selection(hub, settings, [a], sources=["company"])
    assert [p.new for p in first] and first[0].field_kind == "company"
    declines.decline(hub.store, first, region=hub.region)
    assert enrich_selection(hub, settings, [a], sources=["company"]) == []


def test_an_email_turned_down_cannot_be_read_back_from_the_table(hub):
    a = _person(hub, "Ann", "ann@acme-widgets.co.uk")
    extra = Proposal(a, "email", "work", "", "Ann.Lee@Yasref.example", "signature", "Re: x")
    declines.decline(hub.store, [extra], region=hub.region)
    row = hub.store.conn.execute("SELECT * FROM enrich_declines").fetchone()
    dumped = " ".join(str(row[k]) for k in row.keys()).lower()
    assert "ann.lee" not in dumped and "yasref" not in dumped
    plain = hashlib.sha256(b"ann.lee@yasref.example").hexdigest()
    assert row["value_hash"] != plain                              # salted, so no lookup table finds it
    assert row["field_key"] == "f:email"                            # the kind, never the value
    assert drop_declined(hub.store, [extra], region=hub.region) == []


def test_looking_writes_nothing_when_nothing_was_turned_down(hub):
    a = _person(hub, "Ann", "ann@acme-widgets.co.uk")
    p = Proposal(a, "title", "Job title", "", "Buyer", "signature", "Re: x")
    before = hub.store.get_state(declines.SALT_KEY)
    assert drop_declined(hub.store, [p], region=hub.region) == [p]
    assert hub.store.get_state(declines.SALT_KEY) == before is None


def test_a_store_without_the_table_is_read_as_nothing_turned_down():
    class Bare:
        conn = sqlite3.connect(":memory:")

        def get_state(self, key, default=None):
            return "salt"

    p = Proposal(1, "title", "Job title", "", "Buyer", "signature", "Re: x")
    assert drop_declined(Bare(), [p]) == [p]


def test_undo_one_and_clear_all(hub):
    a = _person(hub, "Ann", "ann@acme-widgets.co.uk")
    b = _person(hub, "Bo", "bo@acme-widgets.co.uk")
    ps = [Proposal(a, "title", "Job title", "", "Buyer", "signature", "Re: x"),
          Proposal(a, "company", "Company", "", "Acme", "company", "domain"),
          Proposal(b, "title", "Job title", "", "Buyer", "signature", "Re: x")]
    declines.decline(hub.store, ps, region=hub.region)
    assert declines.count(hub.store) == 3
    rows = declines.for_contact(hub.store, a)
    assert sorted((r.label, r.shown) for r in rows) == [("Company", "Acme"), ("Job title", "Buyer")]
    title = next(r for r in rows if r.label == "Job title")
    assert declines.undo(hub.store, a, title.field_key, title.value_hash) == 1
    assert [p.new for p in drop_declined(hub.store, ps[:2], region=hub.region)] == ["Buyer"]
    assert declines.clear(hub.store) == 2 and declines.count(hub.store) == 0
