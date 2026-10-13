"""Order 1s section 2: fresh details only - the age window (D1, the owner's: a hard cut, six months).

Layer: L7

Mail sent before the window is not read, not offered and not counted; an undated
message is outside every window but "any age"; a run that found nothing because of
the window says so, with how many contacts have older mail; every suggestion says
when its message was sent.
"""

from __future__ import annotations

import json
import time

import pytest

from app.accounts.store import AccountSettings
from app.contacts.card import Card, Field
from app.contacts.enrich import signatures
from app.contacts.enrich.history import contact_history
from app.contacts.enrich.run import enrich_selection
from app.contacts.hub import ContactHub
from app.storage.sqlite_store import SqliteStore

SIGNED = """Thanks,

Kind regards,

Ann Lee
Senior Engineer
Acme Widgets Ltd
T: 020 7946 0018
"""

DAY = 86400
NOW = int(time.time())


@pytest.fixture()
def hub(tmp_path):
    store = SqliteStore(tmp_path / "w.db").connect()
    yield ContactHub(store)
    store.close()


_n = [0]


def _mail(hub, body, sent, sender="Ann Lee <ann@acme-widgets.co.uk>"):
    _n[0] += 1
    s = hub.store
    fid = s.upsert_file(f"m/{_n[0]}.eml", parent_dir="m", ext="eml", size_bytes=1, mtime_ns=_n[0],
                        status="INDEXED", source_kind="eml")
    s.set_message(fid, subject=f"Re {_n[0]}", sender=sender, recipients=json.dumps(["me@home.example"]),
                  sent_at=sent)
    s.replace_chunks(fid, [{"ordinal": 0, "text": "Subject: x", "label": "Headers"},
                           {"ordinal": 1, "text": body, "label": "Body"}])
    return fid


def _ann(hub):
    return hub.create(Card(scalars={"given": "Ann", "family": "Lee"},
                           fields=[Field("email", "ann@acme-widgets.co.uk", "work", primary=True)]))


def _settings(months):
    s = AccountSettings()
    s.enrich_max_months = months
    s.enrich_company = s.enrich_faces = s.enrich_history = s.enrich_suggest = False
    return s


def test_mail_before_the_window_is_not_read(hub):
    cid = _ann(hub)
    _mail(hub, SIGNED, NOW - 400 * DAY)
    since = NOW - 180 * DAY
    assert signatures.signature_proposals(hub.store, cid, hub.get_card(cid), since=since) == []
    assert signatures.signature_proposals(hub.store, cid, hub.get_card(cid))       # any age


def test_an_undated_message_is_outside_every_window_but_any_age(hub):
    cid = _ann(hub)
    _mail(hub, SIGNED, None)
    assert signatures.signature_proposals(hub.store, cid, hub.get_card(cid), since=NOW - 180 * DAY) == []
    assert signatures.signature_proposals(hub.store, cid, hub.get_card(cid))


def test_every_suggestion_says_when_its_message_was_sent(hub):
    cid = _ann(hub)
    sent = NOW - 20 * DAY
    _mail(hub, SIGNED, sent)
    props = signatures.signature_proposals(hub.store, cid, hub.get_card(cid), since=NOW - 180 * DAY)
    assert props and all(p.when == sent for p in props)


def test_a_run_that_found_nothing_because_of_the_window_says_so(hub):
    cid = _ann(hub)
    _mail(hub, SIGNED, NOW - 400 * DAY)
    notes: list[str] = []
    assert enrich_selection(hub, _settings(6), [cid], notes=notes) == []
    said = " ".join(notes)
    assert "No suggestions from mail since" in said and "1 contact has older mail" in said


def test_with_any_age_the_same_run_finds_it_and_says_nothing_of_a_window(hub):
    cid = _ann(hub)
    _mail(hub, SIGNED, NOW - 400 * DAY)
    notes: list[str] = []
    assert enrich_selection(hub, _settings(0), [cid], notes=notes)
    assert not any("since" in n for n in notes)


def test_history_counts_only_the_window_and_says_which(hub):
    cid = _ann(hub)
    _mail(hub, "hello", NOW - 400 * DAY)
    _mail(hub, "hello again", NOW - 10 * DAY)
    since = NOW - 180 * DAY
    h = contact_history(hub.store, cid, hub=hub, since=since)
    assert h.messages == 1 and h.since == since and h.first >= since
    assert contact_history(hub.store, cid, hub=hub).messages == 2


def test_the_when_column_gives_the_date_and_the_age():
    from app.ui.contacts_tools.enrich_dialog import when_words

    sent = int(time.mktime((2026, 3, 12, 12, 0, 0, 0, 0, -1)))
    words = when_words(sent, now=sent + 21 * DAY)
    assert words.startswith("12 Mar 2026, ") and words.endswith("ago")
    assert when_words(None) == "" and when_words(0) == ""
