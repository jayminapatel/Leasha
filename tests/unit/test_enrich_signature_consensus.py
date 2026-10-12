"""Order 1s section 3h-3k and 4c: the signature reader over a real index - whose details, how sure, what changed.

Layer: L7

A real `SqliteStore` in `tmp_path`, seeded the way the indexer writes mail, and a
real `ContactHub`. Invented people, companies and numbers throughout. What is
pinned: the owner's own details are never offered to anyone (3h, the order's guard
fixture: a client reply quoting the owner's signature); a message the person did not
send never credits them (3i); values are agreed across messages, newest wins and older
ones become history (3j); a changed card value is an update that never starts ticked
(3k); a signature quoted inside somebody else's message is credited only to the
address in its own From line, within the window, at lower confidence (4c); and every
message is read through `base.full_text` (section 4's one door).
"""

from __future__ import annotations

import json
from calendar import timegm

import pytest

from app.accounts.store import AccountSettings
from app.contacts import phone as phones
from app.contacts.card import Card, Field
from app.contacts.enrich import base, signatures
from app.contacts.enrich.run import enrich_selection
from app.contacts.hub import ContactHub
from app.storage.sqlite_store import SqliteStore

OWNER = "me@home.example"
OWNER_MOBILE = "07700 900999"
ANN = "ann@acme-widgets.co.uk"


def epoch(y, m, d):
    return timegm((y, m, d, 12, 0, 0))


@pytest.fixture()
def hub(tmp_path):
    store = SqliteStore(tmp_path / "sig.db").connect()
    yield ContactHub(store)
    store.close()


_n = [0]


def seed(hub, sender, body, *, sent, subject=None, to=(OWNER,)):
    _n[0] += 1
    s = hub.store
    fid = s.upsert_file(f"mail/{_n[0]}.eml", parent_dir="mail", ext="eml", size_bytes=10, mtime_ns=_n[0],
                        status="INDEXED", source_kind="eml")
    s.set_message(fid, subject=subject or f"Note {_n[0]}", sender=sender, recipients=json.dumps(list(to)),
                  sent_at=sent)
    s.replace_chunks(fid, [{"ordinal": 0, "text": f"Subject: x\nFrom: {sender}", "label": "Headers"},
                           {"ordinal": 1, "text": body, "label": "Body"}])
    return fid


def sig(title="Senior Engineer", mobile="07700 900123", extra=""):
    lines = ["Kind regards,", "", "Ann Lee", title, "Acme Widgets Ltd", "T: 020 7946 0018"]
    if mobile:
        lines.append(f"M: {mobile}")
    return "Thanks for this.\n\n" + "\n".join(lines) + ("\n" + extra if extra else "") + "\n"


def ann(hub, **scalars):
    return hub.create(Card(scalars={"given": "Ann", "family": "Lee", **scalars},
                           fields=[Field("email", ANN, "work", primary=True)]))


OWNER_SIG = f"Jay Patel\nPrincipal Consultant\nHomefield Advisory Ltd\nM: {OWNER_MOBILE}\nT: 020 7946 0777"


def owner_mail(hub):
    seed(hub, f"Jay Patel <{OWNER}>", "Hello Ann, see attached.\n\nBest,\n" + OWNER_SIG, sent=epoch(2026, 8, 1),
         to=(ANN,))
    seed(hub, f"Jay Patel <{OWNER}>", "Morning, two points below.\n\nBest,\n" + OWNER_SIG, sent=epoch(2026, 8, 3),
         to=(ANN,))


def props_of(hub, cid, **kw):
    return signatures.signature_proposals(hub.store, cid, hub.get_card(cid), **kw)


def _all_numbers(props):
    return {phones.normalise(p.new) for p in props if p.field_kind == "phone"}


# ---- 3h never the owner's own details --------------------------------------------------------------------------

def test_a_client_reply_quoting_the_owners_signature_never_offers_the_owners_mobile(hub):
    owner_mail(hub)
    cid = hub.create(Card(scalars={"given": "Ravi", "family": "Menon"},
                          fields=[Field("email", "ravi@kestrel-foods.example", "work")]))
    quoted = (f"All fine, thanks.\n\nRavi\n\nFrom: Jay Patel <{OWNER}>\nSent: Monday, 7 September 2026 09:14\n"
              f"To: Ravi Menon <ravi@kestrel-foods.example>\nSubject: Plan\n\nRavi, the plan.\n\nBest,\n{OWNER_SIG}\n")
    seed(hub, "Ravi Menon <ravi@kestrel-foods.example>", quoted, sent=epoch(2026, 9, 7))
    props = props_of(hub, cid, owner_emails=[OWNER])
    assert "+447700900999" not in _all_numbers(props)
    assert not any("Principal Consultant" in str(p.new) or "Homefield" in str(p.new) for p in props)


def test_the_owners_number_and_address_inside_a_clients_own_signature_are_removed(hub):
    owner_mail(hub)
    cid = ann(hub)
    seed(hub, f"Ann Lee <{ANN}>", sig(extra=f"Account lead: {OWNER_MOBILE}\nCopy: {OWNER}"), sent=epoch(2026, 9, 1))
    props = props_of(hub, cid, owner_emails=[OWNER])
    assert _all_numbers(props) == {"+442079460018", "+447700900123"}
    assert not any(OWNER in str(p.new) for p in props)


def test_a_run_passes_the_owners_addresses_to_the_reader(hub):
    owner_mail(hub)
    cid = ann(hub)
    seed(hub, f"Ann Lee <{ANN}>", sig(extra=f"Account lead: {OWNER_MOBILE}"), sent=epoch(2026, 9, 1))
    out = enrich_selection(hub, AccountSettings(enrich_max_months=0), [cid], owner_emails=[OWNER],
                           sources=["signature"])
    assert out and "+447700900999" not in _all_numbers(out)


# ---- 3i never from a message the person did not send ---------------------------------------------------------------

def test_a_message_sent_on_behalf_of_someone_is_skipped(hub):
    cid = ann(hub)
    seed(hub, f"Bob Ray on behalf of Ann Lee <{ANN}>", sig(), sent=epoch(2026, 9, 1))
    assert props_of(hub, cid) == []


# ---- 3j consensus --------------------------------------------------------------------------------------------------

def test_agreeing_messages_raise_confidence_and_an_older_disagreeing_value_becomes_history(hub):
    cid = ann(hub)
    seed(hub, ANN, sig(title="Junior Engineer"), sent=epoch(2024, 1, 10), subject="oldest")
    for i, m in enumerate((3, 5, 7)):
        seed(hub, f"Ann Lee <{ANN}>", sig(), sent=epoch(2026, m, 1), subject=f"new {i}")
    title = next(p for p in props_of(hub, cid) if p.field_kind == "title")
    assert title.new == "Senior Engineer" and "new 2" in title.receipt
    assert title.confidence == 0.97                       # 0.6 + 0.2 name line + 0.2 two agreeing, capped
    assert title.seen == 3 and title.first_seen == epoch(2026, 3, 1) and title.when == epoch(2026, 7, 1)
    assert title.history == "was Junior Engineer until January 2024"
    assert all(p.new != "Junior Engineer" for p in props_of(hub, cid))


def test_a_newer_message_without_the_field_lowers_its_confidence(hub):
    cid = ann(hub)
    seed(hub, ANN, sig(), sent=epoch(2026, 3, 1))
    seed(hub, ANN, sig(), sent=epoch(2026, 4, 1))
    seed(hub, ANN, sig(mobile=""), sent=epoch(2026, 5, 1))                  # the newest has no mobile
    by = {(p.field_kind, p.field_label): p for p in props_of(hub, cid)}
    assert by[("phone", "mobile")].confidence == 0.7                       # 0.6 + 0.2 + 0.1 - 0.2
    assert by[("phone", "work")].confidence == 0.97


def test_only_up_to_max_messages_are_read(hub):
    cid = ann(hub)
    for d in range(1, signatures.MAX_MESSAGES + 6):
        seed(hub, ANN, sig(), sent=epoch(2026, 1, d))
    title = next(p for p in props_of(hub, cid) if p.field_kind == "title")
    assert title.seen == signatures.MAX_MESSAGES


# ---- 3k changes, not only gaps -------------------------------------------------------------------------------------

def test_a_card_value_that_differs_from_a_sure_newer_one_is_an_update_never_pre_ticked(hub):
    cid = ann(hub, title="Engineer")
    for m in (3, 4, 5):
        seed(hub, ANN, sig(), sent=epoch(2026, m, 1))
    title = next(p for p in props_of(hub, cid) if p.field_kind == "title")
    assert (title.old, title.new, title.update) == ("Engineer", "Senior Engineer", True)


def test_a_change_seen_once_is_not_offered_over_what_the_card_holds(hub):
    cid = ann(hub, title="Engineer")
    seed(hub, ANN, sig(), sent=epoch(2026, 5, 1))
    assert not [p for p in props_of(hub, cid) if p.field_kind == "title"]
    gaps = [p for p in props_of(hub, cid) if p.field_kind == "company"]
    assert gaps and not gaps[0].update


# ---- every message goes through full_text --------------------------------------------------------------------------

def test_the_reader_reads_through_full_text_with_the_originals_switch(hub, monkeypatch):
    cid = hub.create(Card(scalars={"given": "Liam", "family": "Byrne"},
                          fields=[Field("email", "liam@corrib-build.example", "work")]))
    seed(hub, "Liam Byrne <liam@corrib-build.example>", "Please see the revised schedule.", sent=epoch(2026, 9, 1))
    seen = []

    def fake(store, file_id, *, read_original=False):
        seen.append(read_original)
        return base.body_text(store, file_id) + "\n-- \nLiam Byrne\nSite Manager\nCorrib Build Ltd\nM: 07700 900142"

    monkeypatch.setattr(base, "full_text", fake)
    settings = AccountSettings(enrich_max_months=0, enrich_read_originals=True)
    out = enrich_selection(hub, settings, [cid], sources=["signature"])
    assert {p.new for p in out if p.field_kind == "title"} == {"Site Manager"}
    assert seen and all(seen)


# ---- 4c quoted signatures ------------------------------------------------------------------------------------------

def quoted_from_ann(sent_line="Monday, 7 September 2026 09:14", sender=f"Ann Lee <{ANN}>"):
    return (f"Jay, see Ann's note below.\n\nBob\n\nFrom: {sender}\nSent: {sent_line}\nTo: Bob Ray "
            f"<bob@acme-widgets.co.uk>\nCc: {OWNER}\nSubject: Plan\n\nBob, the drawings are late.\n\n"
            + sig().split("\n\n", 1)[1])


def test_a_signature_quoted_in_someone_elses_message_is_credited_to_its_own_sender(hub):
    cid = ann(hub)                                            # Ann has never mailed the owner herself
    seed(hub, "Bob Ray <bob@acme-widgets.co.uk>", quoted_from_ann(), sent=epoch(2026, 9, 8),
         to=(OWNER, ANN), subject="Fw: Plan")
    props = props_of(hub, cid, since=epoch(2026, 4, 12))
    title = next(p for p in props if p.field_kind == "title")
    assert title.new == "Senior Engineer" and title.source == "signature"
    assert title.when == timegm((2026, 9, 7, 9, 14, 0)) and "Fw: Plan" in title.receipt and "quoted" in title.receipt
    assert title.confidence == round(0.6 + 0.2 - 0.15, 2)


def test_a_quoted_block_is_never_credited_to_anyone_but_its_own_from_line(hub):
    cid = ann(hub)
    seed(hub, "Bob Ray <bob@acme-widgets.co.uk>", quoted_from_ann(sender="Carl Ode <carl@acme-widgets.co.uk>"),
         sent=epoch(2026, 9, 8), to=(OWNER, ANN))
    assert props_of(hub, cid) == []


def test_a_quoted_message_sent_before_the_window_is_not_read(hub):
    cid = ann(hub)
    seed(hub, "Bob Ray <bob@acme-widgets.co.uk>", quoted_from_ann("Tuesday, 3 June 2025 14:22"),
         sent=epoch(2026, 9, 8), to=(OWNER, ANN))
    assert props_of(hub, cid, since=epoch(2026, 4, 12)) == []
    assert props_of(hub, cid)                                   # any age reads it


def test_a_direct_message_outranks_the_same_value_quoted(hub):
    cid = ann(hub)
    seed(hub, f"Ann Lee <{ANN}>", sig(), sent=epoch(2026, 9, 1))
    seed(hub, "Bob Ray <bob@acme-widgets.co.uk>", quoted_from_ann(), sent=epoch(2026, 9, 8), to=(OWNER, ANN))
    title = next(p for p in props_of(hub, cid) if p.field_kind == "title")
    assert title.seen == 2 and "quoted" not in title.receipt


def test_a_quoted_copy_of_a_message_read_directly_does_not_agree_with_itself(hub):
    # The owner's reply quotes Ann's message in the owner's local time (an hour ahead of UTC in summer):
    # it is the same message, so it must not count as a second one agreeing (3j).
    cid = ann(hub)
    seed(hub, f"Ann Lee <{ANN}>", sig(), sent=timegm((2026, 9, 7, 8, 14, 0)))
    seed(hub, f"Jay Patel <{OWNER}>", quoted_from_ann("Monday, 7 September 2026 09:14"), sent=epoch(2026, 9, 8),
         to=(ANN,))
    title = next(p for p in props_of(hub, cid) if p.field_kind == "title")
    assert title.seen == 1 and "quoted" not in title.receipt
