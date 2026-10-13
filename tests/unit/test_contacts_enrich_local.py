"""Order 1k section 7a-7e: enrichment from the owner's own index.

Layer: L7

A real `SqliteStore` in `tmp_path`, seeded the way the indexer writes mail
(`upsert_file`, `set_message`, `replace_chunks`), and a real `ContactHub`. No
network is touched and no network code is given a transport: these sources are
local. What is pinned: each source reads the index and proposes, a proposal
changes nothing until it is accepted, an accepted value records its source and
only its own, and the whole acceptance is one journal batch.
"""

from __future__ import annotations

import json
import time
from calendar import timegm

import pytest

from app.accounts.store import AccountSettings
from app.contacts.card import Card, Field
from app.contacts.enrich import (accept_suggestions, apply_proposals, contact_history, enrich_selection,
                                 refresh_last_contacted, suggest_contacts)
from app.contacts.enrich import company as company_mod
from app.contacts.enrich import signatures
from app.contacts.enrich.base import Proposal
from app.contacts.hub import ContactHub
from app.storage.sqlite_store import SqliteStore

OWNER = "me@home.example"

#: Order 1s D1 (2026-10-12): enrichment reads only the last six months of mail by default. These tests are
#: about the sources, not the window (test_enrich_window.py has that), and their mail is dated 2026 - it
#: would age out as the calendar moves - so they read mail of any age.
ANY_AGE = {"enrich_max_months": 0}


def epoch(y, m, d):
    return timegm((y, m, d, 12, 0, 0))


@pytest.fixture()
def hub(tmp_path):
    store = SqliteStore(tmp_path / "enrich.db").connect()
    yield ContactHub(store)
    store.close()


_n = [0]


def seed(hub, sender, body, *, subject="Hello", sent=None, to=(OWNER,), label_headers=True):
    """One indexed mail message, as the indexer leaves it (header passage, then body)."""
    _n[0] += 1
    s = hub.store
    fid = s.upsert_file(f"mail/{_n[0]}.eml", parent_dir="mail", ext="eml", size_bytes=10, mtime_ns=_n[0],
                        status="INDEXED", source_kind="eml")
    s.set_message(fid, subject=subject, sender=sender, recipients=json.dumps(list(to)), sent_at=sent)
    chunks = [{"ordinal": 0, "text": f"Subject: {subject}\nFrom: {sender}\nTo: {', '.join(to)}",
               "label": "Headers" if label_headers else None},
              {"ordinal": 1, "text": body, "label": "Body"}]
    s.replace_chunks(fid, chunks)
    return fid


SIGNED = """Hi Bob,

Please find the figures attached.

Kind regards,

Ann Lee
Senior Engineer
Acme Widgets Ltd
T: 020 7946 0018 | M: 07700 900123
Unit 4, Mill Road
Cambridge CB1 2AB
www.acme-widgets.co.uk
"""


def ann(hub, **extra):
    card = Card(scalars={"given": "Ann", "family": "Lee", **extra},
                fields=[Field("email", "ann@acme-widgets.co.uk", "work", primary=True)])
    return hub.create(card)


# ---- 7a signatures -----------------------------------------------------------

def test_a_signature_is_read_into_proposals_with_the_message_as_receipt(hub):
    cid = ann(hub)
    seed(hub, "Ann Lee <ann@acme-widgets.co.uk>", SIGNED, subject="Q3 figures", sent=epoch(2026, 9, 1))
    props = signatures.signature_proposals(hub.store, cid, hub.get_card(cid))
    by = {(p.field_kind, p.field_label): p for p in props}
    assert by[("phone", "work")].new == "020 7946 0018"
    assert by[("phone", "mobile")].new == "07700 900123"
    assert by[("title", "")].new == "Senior Engineer"
    assert by[("company", "")].new == "Acme Widgets Ltd"
    assert by[("url", "work")].new == "https://www.acme-widgets.co.uk"
    addr = by[("address", "work")]
    assert addr.data["postcode"] == "CB1 2AB" and addr.data["street"] == "Unit 4, Mill Road"
    assert addr.data["city"] == "Cambridge"
    for p in props:
        assert p.source == "signature" and p.contact_id == cid
        assert "Q3 figures" in p.receipt and "2026-09-01" in p.receipt
        assert 0 < p.confidence <= 1


def test_nothing_changes_until_a_proposal_is_accepted(hub):
    cid = ann(hub)
    seed(hub, "Ann Lee <ann@acme-widgets.co.uk>", SIGNED, sent=epoch(2026, 9, 1))
    before = hub.get_card(cid).to_json()
    journal_before = len(hub.history(cid))
    props = enrich_selection(hub, AccountSettings(**ANY_AGE), [cid])
    assert props
    assert hub.get_card(cid).to_json() == before and len(hub.history(cid)) == journal_before
    assert hub.sources(cid) == {}


def test_the_newest_signature_wins(hub):
    cid = ann(hub)
    old = SIGNED.replace("Senior Engineer", "Junior Engineer")
    seed(hub, "ann@acme-widgets.co.uk", old, sent=epoch(2024, 1, 1), subject="old")
    seed(hub, "Ann Lee <ann@acme-widgets.co.uk>", SIGNED, sent=epoch(2026, 1, 1), subject="new")
    seed(hub, "ann@acme-widgets.co.uk", old, sent=epoch(2023, 1, 1), subject="older")
    title = [p for p in signatures.signature_proposals(hub.store, cid, hub.get_card(cid))
             if p.field_kind == "title"][0]
    assert title.new == "Senior Engineer" and "new" in title.receipt


def test_only_mail_from_that_exact_address_is_read(hub):
    cid = ann(hub)
    seed(hub, "Joann <joann@acme-widgets.co.uk>", SIGNED.replace("Ann Lee", "Joann Lee"), sent=epoch(2026, 1, 1))
    seed(hub, "Bob <bob@x.example>", SIGNED, sent=epoch(2026, 2, 1), to=("ann@acme-widgets.co.uk",))
    assert signatures.signature_proposals(hub.store, cid, hub.get_card(cid)) == []


def test_values_already_on_the_card_are_not_offered_again(hub):
    card = Card(scalars={"given": "Ann", "family": "Lee", "title": "Senior Engineer"},
                fields=[Field("email", "ann@acme-widgets.co.uk"), Field("phone", "+44 20 7946 0018", "work")])
    cid = hub.create(card)
    seed(hub, "ann@acme-widgets.co.uk", SIGNED, sent=epoch(2026, 1, 1))
    kinds = [(p.field_kind, p.field_label) for p in signatures.signature_proposals(hub.store, cid, hub.get_card(cid))]
    assert ("title", "") not in kinds and ("phone", "work") not in kinds     # same number, other format
    assert ("phone", "mobile") in kinds


@pytest.mark.parametrize("body", [
    "Thanks, see you Tuesday.",                                   # no signature at all
    "Regards,\n\nAnn\nsent from a phone",                         # a sign-off with nothing readable
    "Kind regards,\nAnn Lee\nTel: 12345",                         # not a real phone number
])
def test_a_message_with_nothing_readable_gives_nothing(hub, body):
    cid = ann(hub)
    seed(hub, "ann@acme-widgets.co.uk", body, sent=epoch(2026, 1, 1))
    assert signatures.signature_proposals(hub.store, cid, hub.get_card(cid)) == []


def test_a_website_in_an_email_address_or_a_social_link_is_not_a_website():
    block = ["Ann Lee", "Ann@acme.example", "https://www.linkedin.com/in/ann",
             "https://eur01.safelinks.protection.outlook.com/?url=x"]
    sig = signatures.parse_block(block, name_tokens=["ann", "lee"])
    assert "website" not in sig


def test_a_title_needs_a_title_word_and_a_company_a_suffix_or_the_domain():
    sig = signatures.parse_block(["Ann Lee", "Thanks for everything", "Some Words Here"],
                                 name_tokens=["ann", "lee"], sender_email="ann@zzz.example")
    assert "title" not in sig and "company" not in sig
    sig = signatures.parse_block(["Ann Lee", "Head of Sales", "Acme Widgets"], name_tokens=["ann"],
                                 sender_email="ann@acme-widgets.co.uk")
    assert sig["title"] == "Head of Sales" and sig["company"] == "Acme Widgets"      # agrees with the domain


# ---- accepting -----------------------------------------------------------------

def test_accepted_proposals_are_written_with_their_source_and_only_theirs(hub):
    cid = ann(hub, notes="Met at the fair.")
    seed(hub, "Ann Lee <ann@acme-widgets.co.uk>", SIGNED, subject="Q3 figures", sent=epoch(2026, 9, 1))
    props = signatures.signature_proposals(hub.store, cid, hub.get_card(cid))
    pick = [i for i, p in enumerate(props) if p.field_kind in ("title", "phone")]
    res = apply_proposals(hub, props, pick)
    assert sorted(res.applied) == sorted(pick) and res.changed_contacts == [cid]
    card = hub.get_card(cid)
    assert card.get("title") == "Senior Engineer"
    assert {f.value for f in card.values("phone")} == {"020 7946 0018", "07700 900123"}
    assert card.get("company") == ""                                    # not accepted: not written
    src = hub.sources(cid)
    ours = {k: v for k, v in src.items() if any(s == "enrichment:signature" for s, _ in v)}
    assert set(ours) == {"s:title", "f:phone:+442079460018", "f:phone:+447700900123"}
    (source, ref), = ours["s:title"]
    assert source == "enrichment:signature" and "Q3 figures" in ref and "2026-09-01" in ref
    # the person's own values were not given a receipt they do not deserve
    assert "s:given" not in ours and "f:email:ann@acme-widgets.co.uk" not in ours


def test_an_acceptance_is_one_journal_batch_that_undoes_in_one_step(hub):
    cid = ann(hub)
    seed(hub, "Ann Lee <ann@acme-widgets.co.uk>", SIGNED, sent=epoch(2026, 9, 1))
    props = signatures.signature_proposals(hub.store, cid, hub.get_card(cid))
    res = apply_proposals(hub, props, range(len(props)))
    entries = [e for e in hub.history(cid) if e.actor == "enrich"]
    assert entries and {e.batch_id for e in entries} == {res.batch_id}
    hub.undo_batch(res.batch_id)
    card = hub.get_card(cid)
    assert card.get("title") == "" and card.values("phone") == []


def test_accepting_twice_or_a_value_already_there_writes_nothing_more(hub):
    cid = ann(hub)
    props = [Proposal(cid, "title", "", "", "Chief", "signature", "r"),
             Proposal(cid, "title", "", "", "Boss", "signature", "r2")]
    res = apply_proposals(hub, props, [0, 1])
    assert res.applied == [0] and res.skipped == [1]                    # one value per place
    assert hub.get_card(cid).get("title") == "Chief"
    n = len(hub.history(cid))
    assert apply_proposals(hub, props, [0]).applied == []
    assert len(hub.history(cid)) == n


# ---- 7b company ------------------------------------------------------------------

def test_company_comes_from_a_work_domain_never_from_free_mail(hub):
    work = hub.create(Card(scalars={"given": "A"}, fields=[Field("email", "a@mail.acme-widgets.co.uk", "work")]))
    free = hub.create(Card(scalars={"given": "B"}, fields=[Field("email", "b@gmail.com")]))
    has = hub.create(Card(scalars={"given": "C", "company": "Mine"}, fields=[Field("email", "c@acme.com")]))
    got = lambda cid: company_mod.company_proposals(cid, hub.get_card(cid))          # noqa: E731
    p = got(work)
    assert [(x.field_kind, x.new, x.source) for x in p] == [("company", "Acme Widgets", "company")]
    assert "acme-widgets.co.uk" in p[0].receipt
    assert got(free) == [] and got(has) == []


def test_the_fallback_guess_agrees_with_the_shared_one():
    from app.contacts import cleanup
    for e in ("a@acme-widgets.co.uk", "b@gmail.com", "c@mail.acme.com", "d@hotmail.co.uk", "nope", "e@acme"):
        assert company_mod._fallback_guess(e) == cleanup.company_guess_from_email(e), e


def test_colleagues_are_grouped_by_company(hub):
    a = hub.create(Card(scalars={"given": "A", "company": "Acme Ltd"}))
    b = hub.create(Card(scalars={"given": "B"}, fields=[Field("email", "b@acme.com")]))
    c = hub.create(Card(scalars={"given": "C", "company": "ACME"}))
    d = hub.create(Card(scalars={"given": "D"}, fields=[Field("email", "d@gmail.com")]))
    e = hub.create(Card(scalars={"given": "E", "company": "Other"}))
    groups = company_mod.group_by_company(hub)
    assert sorted(map(sorted, groups.values())) == sorted([[a, b, c], [e]])
    assert d not in sum(groups.values(), [])
    pairs = list(company_mod.group_by_company(hub, min_members=2).values())
    assert len(pairs) == 1 and sorted(pairs[0]) == [a, b, c]


# ---- 7c faces ----------------------------------------------------------------------

def _photo_with_faces(hub, tmp_path, pile, boxes):
    from PIL import Image
    path = tmp_path / "party.jpg"
    Image.new("RGB", (400, 300), (200, 120, 60)).save(path)
    fid = hub.store.upsert_file(str(path), parent_dir=str(tmp_path), ext="jpg", size_bytes=1, mtime_ns=1,
                                status="INDEXED", source_kind="file")
    ids = [hub.store.add_face(fid, b, b"\0" * 8) for b in boxes]
    for i in ids:
        hub.store.assign_face(i, pile)
    return path


def _link(hub, cid, pile, state="accepted"):
    with hub.store.write() as conn:
        conn.execute("INSERT INTO contact_pile_links (contact_id, pile_id, state) VALUES (?,?,?)", (cid, pile, state))


def test_a_photo_is_proposed_from_the_linked_persons_best_face(hub, tmp_path):
    from PIL import Image
    import io
    cid = ann(hub)
    pile = hub.store.create_pile("Ann Lee")
    path = _photo_with_faces(hub, tmp_path, pile, [(10, 10, 20, 20), (100, 60, 120, 120)])
    _link(hub, cid, pile)
    props = enrich_selection(hub, AccountSettings(), [cid], sources=["face"])
    assert len(props) == 1 and props[0].field_kind == "photo" and props[0].source == "face"
    assert path.name in props[0].receipt and "Ann Lee" in props[0].receipt
    im = Image.open(io.BytesIO(props[0].new.data))
    assert im.size == (256, 256) and props[0].new.mime == "image/jpeg"
    assert hub.get_card(cid).photo is None                              # proposed, not applied
    apply_proposals(hub, props, [0])
    assert hub.get_card(cid).photo is not None
    assert hub.sources(cid)["photo"][0][0] == "enrichment:face"


def test_no_photo_for_a_suggested_link_a_missing_file_or_a_card_that_has_one(hub, tmp_path):
    cid, other = ann(hub), hub.create(Card(scalars={"given": "Z"}))
    pile = hub.store.create_pile("Ann Lee")
    path = _photo_with_faces(hub, tmp_path, pile, [(100, 60, 120, 120)])
    _link(hub, cid, pile, "suggested")
    assert enrich_selection(hub, AccountSettings(), [cid], sources=["face"]) == []
    with hub.store.write() as conn:
        conn.execute("UPDATE contact_pile_links SET state = 'accepted'")
    path.unlink()
    assert enrich_selection(hub, AccountSettings(), [cid], sources=["face"]) == []      # photo gone
    assert enrich_selection(hub, AccountSettings(), [other], sources=["face"]) == []    # not linked


# ---- 7d history ------------------------------------------------------------------------

def test_history_counts_dates_and_who_else_is_on_the_thread(hub):
    cid = ann(hub)
    seed(hub, "ann@acme-widgets.co.uk", "x", sent=epoch(2025, 1, 10), to=(OWNER, "Cy <cy@x.example>"))
    seed(hub, "ann@acme-widgets.co.uk", "x", sent=epoch(2025, 3, 10), to=(OWNER, "cy@x.example", "di@x.example"))
    seed(hub, OWNER, "x", sent=epoch(2026, 1, 10), to=("Ann <ann@acme-widgets.co.uk>", "cy@x.example"))
    seed(hub, "stranger@x.example", "x", sent=epoch(2026, 2, 1))
    h = contact_history(hub.store, cid, hub=hub, owner_emails=[OWNER])
    assert (h.messages, h.from_them, h.to_them) == (3, 2, 1)
    assert h.first == epoch(2025, 1, 10) and h.last == epoch(2026, 1, 10)
    assert h.with_others[0] == ("cy@x.example", 3) and ("di@x.example", 1) in h.with_others
    assert all(a != OWNER for a, _ in h.with_others)
    assert h.often                                                      # a sentence, not empty


def test_refresh_last_contacted_is_a_derived_write_with_no_journal(hub):
    a = ann(hub)
    b = hub.create(Card(scalars={"given": "B"}, fields=[Field("email", "B@Other.example")]))
    none = hub.create(Card(scalars={"given": "N"}, fields=[Field("email", "n@none.example")]))
    seed(hub, "ann@acme-widgets.co.uk", "x", sent=epoch(2025, 1, 1))
    seed(hub, OWNER, "x", sent=epoch(2026, 5, 5), to=("ann@acme-widgets.co.uk",))
    seed(hub, "b@other.example", "x", sent=epoch(2024, 5, 5))
    rev = hub.get_row(a), hub.conn.execute("SELECT rev, edited_at FROM contacts WHERE id = ?", (a,)).fetchone()
    journal = hub.conn.execute("SELECT COUNT(*) FROM contact_journal").fetchone()[0]
    assert refresh_last_contacted(hub) == 2
    ms = lambda cid: hub.get_row(cid).last_contacted_at                         # noqa: E731
    assert ms(a) == epoch(2026, 5, 5) * 1000 and ms(b) == epoch(2024, 5, 5) * 1000 and ms(none) is None
    after = hub.conn.execute("SELECT rev, edited_at FROM contacts WHERE id = ?", (a,)).fetchone()
    assert tuple(after) == tuple(rev[1])
    assert hub.conn.execute("SELECT COUNT(*) FROM contact_journal").fetchone()[0] == journal
    assert refresh_last_contacted(hub) == 0                                     # nothing new: nothing written


# ---- 7e suggested contacts --------------------------------------------------------------

def test_frequent_unknown_senders_are_suggested_and_added_only_when_accepted(hub):
    ann(hub)
    for i in range(3):
        seed(hub, "Dave Smith <dave@x.example>", "x", sent=epoch(2026, 1, 1 + i))
    for i in range(2):
        seed(hub, "Rare <rare@x.example>", "x", sent=epoch(2026, 1, 1))
    for i in range(5):
        seed(hub, "ann@acme-widgets.co.uk", "x", sent=epoch(2026, 1, 1))                # already a contact
        seed(hub, "No Reply <no-reply@shop.example>", "x")
        seed(hub, "mailer-daemon@x.example", "x")
        seed(hub, "Weekly <newsletter@news.example>", "x")
        seed(hub, f"Me <{OWNER}>", "x")
    before = hub.count()
    got = suggest_contacts(hub.store, owner_emails=[OWNER])
    assert [(s.name, s.email, s.count) for s in got] == [("Dave Smith", "dave@x.example", 3)]
    assert got[0].last_seen == epoch(2026, 1, 3)
    assert hub.count() == before                                                # listed, not created
    assert len(suggest_contacts(hub.store, min_count=2, owner_emails=[OWNER])) == 2
    made = accept_suggestions(hub, got)
    assert len(made) == 1 and hub.count() == before + 1
    card = hub.get_card(made[0])
    assert (card.get("given"), card.get("family")) == ("Dave", "Smith")
    assert hub.find_by_email("dave@x.example") == made
    assert hub.sources(made[0])["f:email:dave@x.example"][0][0] == "enrichment:suggest"
    assert accept_suggestions(hub, got) == [] and hub.count() == before + 1     # twice makes no second one
    assert suggest_contacts(hub.store, owner_emails=[OWNER]) == []              # now a contact


def test_a_contact_in_the_bin_is_not_suggested_again(hub):
    cid = hub.create(Card(scalars={"given": "Dave"}, fields=[Field("email", "dave@x.example")]))
    for _ in range(4):
        seed(hub, "dave@x.example", "x")
    hub.delete(cid)
    assert suggest_contacts(hub.store) == []


# ---- switches --------------------------------------------------------------------------------

def test_a_local_source_that_is_switched_off_is_not_run(hub):
    cid = ann(hub)
    seed(hub, "Ann Lee <ann@acme-widgets.co.uk>", SIGNED, sent=epoch(2026, 1, 1))
    off = AccountSettings(enrich_signatures=False, enrich_company=False, enrich_faces=False)
    assert enrich_selection(hub, off, [cid]) == []
    only_sig = AccountSettings(enrich_company=False, enrich_faces=False, **ANY_AGE)
    assert {p.source for p in enrich_selection(hub, only_sig, [cid])} == {"signature"}


def test_the_bin_is_not_enriched(hub):
    cid = ann(hub)
    seed(hub, "Ann Lee <ann@acme-widgets.co.uk>", SIGNED, sent=epoch(2026, 1, 1))
    hub.delete(cid)
    assert enrich_selection(hub, AccountSettings(), [cid]) == []
