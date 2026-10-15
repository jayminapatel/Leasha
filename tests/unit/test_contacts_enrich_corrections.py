"""Corrections: misread email and web addresses offered as fixes in the enrichment review.

Layer: L7

Owner, 2026-10-15: misread values "need to be part of review for enrichment" - and do not change surnames.
A real `SqliteStore` and `ContactHub` in `tmp_path`; the cases are the ones found on the owner's book
(addresses at `schneider-elecmc.com`, `schneidef-electric.com`, `schneiderwlectric.com` beside many at
`schneider-electric.com`, and web addresses with words run on after `.com`), the people invented.

Pinned: each case is offered as an update (never ticked), with the reason in words; nothing changes until
it is accepted, and accepting it fixes the card; a common domain is never "corrected"; the thresholds
hold; names are never touched; the book's domains are counted once a run; the switch is on by default.
"""

from __future__ import annotations

import json
import socket

import pytest

from app.accounts.store import AccountSettings
from app.contacts.card import Card, Field
from app.contacts.enrich import apply_proposals, corrections, enrich_selection, run
from app.contacts.hub import ContactHub
from app.storage.sqlite_store import SqliteStore

RIGHT = "schneider-electric.com"
ONLY = ["correction"]


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("the corrections source opened a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture()
def hub(tmp_path):
    store = SqliteStore(tmp_path / "corrections.db").connect()
    yield ContactHub(store)
    store.close()


def person(hub, given, family, *emails, urls=()):
    fields = [Field("email", e, "work", primary=(i == 0)) for i, e in enumerate(emails)]
    fields += [Field("url", u, "work") for u in urls]
    return hub.create(Card(scalars={"given": given, "family": family}, fields=fields))


def colleagues(hub, n, domain=RIGHT):
    return [person(hub, f"Col{i}", f"League{i}", f"col{i}@{domain}") for i in range(n)]


def run_on(hub, ids, **settings):
    return enrich_selection(hub, AccountSettings(**settings), ids, sources=ONLY)


# ---- a) an email domain one misreading away from the company's ------------------------------------

def test_the_owners_three_misread_domains_are_offered_as_updates_with_the_reason(hub):
    colleagues(hub, 12)
    ahmed = person(hub, "Ahmed", "Fateen", "ahmed.fateen@schneider-elecmc.com")
    jim = person(hub, "Jim", "Chappell", "jim.chappell@schneidef-electric.com")
    veli = person(hub, "Veli-Matti", "Jarvinen", "veli-matti.jarvinen@schneiderwlectric.com")
    props = run_on(hub, [ahmed, jim, veli])
    got = {p.contact_id: p for p in props}
    assert set(got) == {ahmed, jim, veli} and len(props) == 3
    assert (got[ahmed].old, got[ahmed].new) == ("ahmed.fateen@schneider-elecmc.com", "ahmed.fateen@" + RIGHT)
    assert (got[jim].old, got[jim].new) == ("jim.chappell@schneidef-electric.com", "jim.chappell@" + RIGHT)
    assert got[veli].new == "veli-matti.jarvinen@" + RIGHT
    for p in props:
        assert p.source == "correction" and p.field_kind == "email" and p.field_label == "work"
        assert p.update is True                                    # never ticked by itself (3k, D4)
        assert 0.6 <= p.confidence < 0.85                          # shown at the defaults, below "tick from"
        assert f"looks like a misreading of {RIGHT}, which 12 contacts use" in p.receipt
        assert "@" not in p.receipt                                # domains only, never an address
        assert p.opens == ""                                       # a mailto: is not a place to open
    assert got[jim].confidence > got[ahmed].confidence           # one edit out is surer than three


def test_nothing_is_written_until_accepted_and_accepting_fixes_the_card(hub):
    colleagues(hub, 12)
    jim = person(hub, "Jim", "Chappell", "jim.chappell@schneidef-electric.com", "jim@home.example")
    before, journal = hub.get_card(jim).to_json(), len(hub.history(jim))
    props = run_on(hub, [jim])
    assert hub.get_card(jim).to_json() == before and len(hub.history(jim)) == journal
    assert hub.sources(jim) == {}
    result = apply_proposals(hub, props, range(len(props)))
    assert result.applied == [0]
    card = hub.get_card(jim)
    emails = [(f.value, f.label, f.primary) for f in card.values("email")]
    assert emails == [("jim.chappell@" + RIGHT, "work", True), ("jim@home.example", "work", False)]
    assert (card.get("given"), card.get("family")) == ("Jim", "Chappell")
    assert hub.sources(jim)["f:email:jim.chappell@" + RIGHT][0][0] == "enrichment:correction"
    assert run_on(hub, [jim]) == []                                # fixed: nothing more to offer


def test_when_the_card_already_has_the_right_address_accepting_removes_the_misread_one(hub):
    colleagues(hub, 12)
    ahmed = person(hub, "Ahmed", "Fateen", "ahmed.fateen@" + RIGHT, "ahmed.fateen@schneider-elecmc.com")
    props = run_on(hub, [ahmed])
    assert len(props) == 1
    p = props[0]
    assert (p.old, p.new, p.update) == ("ahmed.fateen@schneider-elecmc.com", "ahmed.fateen@" + RIGHT, True)
    assert "accepting removes the misread one" in p.receipt
    apply_proposals(hub, props, [0])
    assert [f.value for f in hub.get_card(ahmed).values("email")] == ["ahmed.fateen@" + RIGHT]


def test_a_correction_whose_old_value_has_gone_writes_nothing(hub):
    colleagues(hub, 12)
    jim = person(hub, "Jim", "Chappell", "jim.chappell@schneidef-electric.com")
    props = run_on(hub, [jim])
    card = hub.get_card(jim)
    card.fields = [Field("email", "jim@elsewhere.example", "work", primary=True)]
    hub.update(jim, card)
    result = apply_proposals(hub, props, [0])
    assert result.applied == [] and [f.value for f in hub.get_card(jim).values("email")] == ["jim@elsewhere.example"]


def test_a_common_domain_is_never_corrected_and_names_are_never_touched(hub):
    ids = colleagues(hub, 12)
    odd = person(hub, "Schneidef", "Electrik", "jim@schneidef-electric.com")    # a name like the domain
    props = run_on(hub, [*ids, odd])
    assert {p.contact_id for p in props} == {odd}
    assert all(p.field_kind in ("email", "url") for p in props)
    apply_proposals(hub, props, range(len(props)))
    assert (hub.get_card(odd).get("given"), hub.get_card(odd).get("family")) == ("Schneidef", "Electrik")


@pytest.mark.parametrize("common, odd_on, offered", [
    (10, 0, True),       # the odd domain on this one contact only: RATIO times one (and over COMMON_MIN)
    (9, 0, False),       # under RATIO times as many
    (20, 1, True),       # two contacts on the odd domain (RARE_MAX), 20 is 10x
    (19, 1, False),      # under RATIO times as many
    (40, 2, False),      # three contacts on the odd domain: not rare
])
def test_the_thresholds(hub, common, odd_on, offered):
    colleagues(hub, common)
    for i in range(odd_on):
        person(hub, f"Other{i}", "Odd", f"other{i}@schneidef-electric.com")
    jim = person(hub, "Jim", "Chappell", "jim.chappell@schneidef-electric.com")
    assert bool(run_on(hub, [jim])) is offered


@pytest.mark.parametrize("odd, common", [
    ("jim@abc.com", "abb.com"),                        # a short name: two companies, not a misreading
    ("jim@schneider-electric.fr", RIGHT),              # another public ending is another site
    ("jim@schneider-electric.com", RIGHT),             # the common one itself
    ("jim@siemens-energy.com", RIGHT),                 # far from it
])
def test_a_domain_that_is_not_a_near_miss_is_left_alone(hub, odd, common):
    colleagues(hub, 30, common)
    jim = person(hub, "Jim", "Chappell", odd)
    assert run_on(hub, [jim]) == []


def test_a_domain_mail_has_come_from_is_real_however_close(hub):
    colleagues(hub, 12, "acme-widgets.com")
    jim = person(hub, "Jim", "Chappell", "jim@acme-widgetz.com")
    assert run_on(hub, [jim])
    fid = hub.store.upsert_file("mail/1.eml", parent_dir="mail", ext="eml", size_bytes=1, mtime_ns=1,
                                status="INDEXED", source_kind="eml")
    hub.store.set_message(fid, subject="hi", sender="Jim <jim@acme-widgetz.com>",
                          recipients=json.dumps(["me@home.example"]), sent_at=1_700_000_000)
    assert run_on(hub, [jim]) == []


def test_the_edit_allowance_grows_with_the_name():
    assert corrections.edit_distance("schneider-elecmc", "schneider-electric") == 3
    assert corrections.edit_distance("schneidef-electric", "schneider-electric") == 1
    assert corrections.edit_distance("schneiderwlectric", "schneider-electric") == 2
    assert corrections.edit_distance("ab", "ba") == 1                       # a swap is one edit
    assert [corrections.allowed_edits(x) for x in ("abb", "acme", "siemens", "acme-widgets", RIGHT[:-4])] == \
        [0, 0, 1, 2, 3]


# ---- b) a web address with words run on after the real domain -----------------------------------

def test_the_owners_run_on_web_addresses_are_offered_trimmed_and_open_the_trimmed_site(hub):
    ann = person(hub, "Ann", "Lee", "ann@example.org",
                 urls=["http://www.schneider-electric.com.saicustomercarecentertoll",
                       "http://www.schneider-electric.com.saicustomercare", "https://www.acme.co.uk"])
    props = run_on(hub, [ann])
    assert [(p.old, p.new) for p in props] == [
        ("http://www.schneider-electric.com.saicustomercarecentertoll", "http://www.schneider-electric.com"),
        ("http://www.schneider-electric.com.saicustomercare", "http://www.schneider-electric.com")]
    for p in props:
        assert p.source == "correction" and p.field_kind == "url" and p.update is True and p.confidence >= 0.6
        assert "is run on after schneider-electric.com" in p.receipt
        assert p.opens == "http://www.schneider-electric.com"
    assert "saicustomercarecentertoll" in props[0].receipt
    apply_proposals(hub, props, [0])
    urls = [f.value for f in hub.get_card(ann).values("url")]
    assert urls == ["http://www.schneider-electric.com", "http://www.schneider-electric.com.saicustomercare",
                    "https://www.acme.co.uk"]
    again = run_on(hub, [ann])                                     # the second now removes its duplicate
    assert len(again) == 1 and "accepting removes this one" in again[0].receipt
    apply_proposals(hub, again, [0])
    assert [f.value for f in hub.get_card(ann).values("url")] == ["http://www.schneider-electric.com",
                                                                 "https://www.acme.co.uk"]


@pytest.mark.parametrize("url, want", [
    ("www.schneider-electric.com.saicustomercare/contact?x=1", "www.schneider-electric.com/contact?x=1"),
    ("https://www.acme.co.uk", ""),
    ("https://amazon.com.br", ""),                     # a country after .com is a real site
    ("https://acme.com.solutions", ""),                # a web ending the reader knows
    ("https://acme.com.abc", ""),                      # too short to be sure it is not an ending
    ("https://localhost.junkjunk", ""),
    ("", ""),
])
def test_only_a_run_on_that_is_no_web_ending_is_trimmed(url, want):
    assert corrections.trimmed_url(url) == want


# ---- running it ----------------------------------------------------------------------------------

def test_the_source_is_registered_on_by_default_and_its_switch_turns_it_off(hub):
    assert "correction" in run.LOCAL_SOURCES and run.SWITCHES["correction"] == "enrich_corrections"
    assert AccountSettings().enrich_corrections is True
    colleagues(hub, 12)
    jim = person(hub, "Jim", "Chappell", "jim.chappell@schneidef-electric.com")
    assert [p.source for p in enrich_selection(hub, AccountSettings(), [jim])].count("correction") == 1
    assert run_on(hub, [jim], enrich_corrections=False) == []


def test_the_books_domains_are_counted_once_a_run_not_once_a_contact(hub, monkeypatch):
    ids = colleagues(hub, 12)
    ids.append(person(hub, "Jim", "Chappell", "jim.chappell@schneidef-electric.com"))
    calls = []
    real = corrections.domain_counts
    monkeypatch.setattr(corrections, "domain_counts", lambda store: calls.append(1) or real(store))
    assert len(run_on(hub, ids)) == 1
    assert calls == [1]


def test_a_contact_in_the_bin_is_not_counted(hub):
    ids = colleagues(hub, 5)
    hub.delete(ids[0])
    jim = person(hub, "Jim", "Chappell", "jim.chappell@schneidef-electric.com")
    assert corrections.domain_counts(hub.store)[RIGHT] == 4
    assert run_on(hub, [jim]) == []
