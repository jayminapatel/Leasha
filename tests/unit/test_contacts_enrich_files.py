"""Order 1s section 5: enrichment from the person's own files - vCards, a LinkedIn export, invitations.

Layer: L7

A real `SqliteStore` and `ContactHub` in `tmp_path`, and real files on disk registered in `files` the
way the indexer leaves them (`ext` without the dot, `status='INDEXED'`, `source_kind='file'`). Every
name, address and number here is invented. No socket may be opened by any test in this file (the
guard is autouse): these sources are local, and 9e asks for proof.

Pinned: each source offers only what the contact lacks, with the file and its date as the receipt;
the age window applies to the file's modified date; a missing file is skipped quietly; a source that
is switched off is not called; documents (5e) offer nothing.
"""

from __future__ import annotations

import os
import socket
from calendar import timegm

import pytest

from app.accounts.store import AccountSettings
from app.contacts.card import Card, Field
from app.contacts.enrich import enrich_selection, suggestions_for
from app.contacts.enrich import documents, invitations, linkedin, localfiles, run, vcards
from app.contacts.hub import ContactHub
from app.storage.sqlite_store import SqliteStore

ANY_AGE = {"enrich_max_months": 0}
ONLY_FILES = ["vcard", "linkedin", "calendar", "document"]


def epoch(y, m, d):
    return timegm((y, m, d, 12, 0, 0))


@pytest.fixture(autouse=True)
def _no_sockets(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("a local enrichment source opened a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    localfiles.clear_cache()
    yield
    localfiles.clear_cache()


@pytest.fixture()
def hub(tmp_path):
    store = SqliteStore(tmp_path / "files.db").connect()
    yield ContactHub(store)
    store.close()


def put(hub, path, text, *, when=None):
    """Write a file and index it as the indexer would."""
    path.write_text(text, encoding="utf-8")
    t = when or epoch(2026, 9, 1)
    os.utime(path, (t, t))
    hub.store.upsert_file(str(path), parent_dir=str(path.parent), ext=path.suffix.lstrip(".").lower(),
                          size_bytes=path.stat().st_size, mtime_ns=t * 1_000_000_000, status="INDEXED",
                          source_kind="file")
    return path


def person(hub, given="", family="", *, email="", phone="", company="", title=""):
    scalars = {k: v for k, v in (("given", given), ("family", family), ("company", company), ("title", title)) if v}
    fields = []
    if email:
        fields.append(Field("email", email, "work", primary=True))
    if phone:
        fields.append(Field("phone", phone, "work"))
    return hub.create(Card(scalars=scalars, fields=fields))


CARD = """BEGIN:VCARD
VERSION:3.0
N:Quill;Avery;;;
FN:Avery Quill
ORG:Northwind Pumps
TITLE:Commissioning Lead
EMAIL;TYPE=WORK:avery.quill@northwind.example
TEL;TYPE=CELL:+44 7700 900123
URL:https://northwind.example
END:VCARD
"""


# ---------------------------------------------------------------- 5a vCards

def test_a_vcard_matched_by_email_offers_only_what_the_contact_lacks(hub, tmp_path):
    f = put(hub, tmp_path / "avery.vcf", CARD, when=epoch(2026, 9, 3))
    cid = person(hub, "Avery", "Quill", email="Avery.Quill@northwind.example", company="Northwind Pumps")
    props = enrich_selection(hub, AccountSettings(**ANY_AGE), [cid], sources=["vcard"])
    got = {(p.field_kind, str(p.new)) for p in props}
    assert ("title", "Commissioning Lead") in got and ("url", "https://northwind.example") in got
    assert any(k == "phone" for k, _ in got)
    assert not any(k in ("company", "given", "family", "email") for k, _ in got)   # already on the card
    for p in props:
        assert p.source == "vcard" and str(f) in p.receipt and "2026-09-03" in p.receipt
        assert p.when == epoch(2026, 9, 3) and p.confidence <= 0.9


def test_a_vcard_matches_by_phone_then_by_name_with_a_shared_company(hub, tmp_path):
    put(hub, tmp_path / "avery.vcf", CARD)
    by_phone = person(hub, phone="07700 900123")
    by_name = person(hub, "Avery", "Quill", company="Northwind Pumps Ltd")
    namesake = person(hub, "Avery", "Quill", company="Southwind Fans")
    props = enrich_selection(hub, AccountSettings(**ANY_AGE), [by_phone, by_name, namesake], sources=["vcard"])
    assert {p.contact_id for p in props} == {by_phone, by_name}
    phone_conf = {p.confidence for p in props if p.contact_id == by_phone}
    assert phone_conf and max(phone_conf) <= 0.8
    assert any(p.field_kind == "email" for p in props if p.contact_id == by_phone)


def test_a_vcard_whose_address_is_another_contacts_is_not_used_by_phone(hub, tmp_path):
    put(hub, tmp_path / "avery.vcf", CARD)
    person(hub, "Avery", "Quill", email="avery.quill@northwind.example")
    shared_line = person(hub, "Front", "Desk", phone="+44 7700 900123")
    assert enrich_selection(hub, AccountSettings(**ANY_AGE), [shared_line], sources=["vcard"]) == []


def test_the_age_window_applies_to_the_vcards_modified_date(hub, tmp_path):
    put(hub, tmp_path / "old.vcf", CARD, when=epoch(2019, 1, 1))
    cid = person(hub, email="avery.quill@northwind.example")
    assert enrich_selection(hub, AccountSettings(enrich_max_months=6), [cid], sources=["vcard"]) == []
    assert enrich_selection(hub, AccountSettings(**ANY_AGE), [cid], sources=["vcard"])


def test_a_missing_vcard_is_skipped_quietly(hub, tmp_path):
    f = put(hub, tmp_path / "gone.vcf", CARD)
    f.unlink()
    cid = person(hub, email="avery.quill@northwind.example")
    assert enrich_selection(hub, AccountSettings(**ANY_AGE), [cid], sources=["vcard"]) == []


def test_a_vcard_is_read_not_written(hub, tmp_path):
    f = put(hub, tmp_path / "avery.vcf", CARD)
    before = (f.read_bytes(), f.stat().st_mtime_ns)
    cid = person(hub, email="avery.quill@northwind.example")
    enrich_selection(hub, AccountSettings(**ANY_AGE), [cid], sources=["vcard"])
    assert (f.read_bytes(), f.stat().st_mtime_ns) == before


# ---------------------------------------------------------------- 5c LinkedIn

EXPORT = """Notes:
"When exporting your connection data, you may notice that some of the email addresses are missing."

First Name,Last Name,URL,Email Address,Company,Position,Connected On
Avery,Quill,https://www.linkedin.example/in/avery,,Northwind Pumps,Head of Commissioning,14 Mar 2021
Rowan,Vale,https://www.linkedin.example/in/rowan,rowan@vale.example,Vale Instruments,Sales Engineer,02 Aug 2025
"""


def test_a_linkedin_export_with_notes_before_its_header_is_read(tmp_path):
    p = tmp_path / "Connections.csv"
    p.write_text(EXPORT, encoding="utf-8")
    rows = linkedin.read_export(str(p))
    assert [(r.name, r.company, r.position) for r in rows] == [
        ("Avery Quill", "Northwind Pumps", "Head of Commissioning"),
        ("Rowan Vale", "Vale Instruments", "Sales Engineer")]
    assert rows[0].connected == epoch(2021, 3, 14)


def test_linkedin_matches_by_email_else_by_name_and_company(hub, tmp_path):
    p = tmp_path / "Connections.csv"
    p.write_text(EXPORT, encoding="utf-8")
    by_email = person(hub, email="rowan@vale.example")
    by_name = person(hub, "Avery", "Quill", company="Northwind Pumps")
    name_only = person(hub, "Avery", "Quill")
    s = AccountSettings(enrich_linkedin_file=str(p), **ANY_AGE)
    props = enrich_selection(hub, s, [by_email, by_name, name_only], sources=["linkedin"])
    got = {(p_.contact_id, p_.field_kind, p_.new) for p_ in props}
    assert got == {(by_email, "company", "Vale Instruments"), (by_email, "title", "Sales Engineer"),
                   (by_name, "title", "Head of Commissioning")}
    for x in props:
        assert x.source == "linkedin" and str(p) in x.receipt and x.when is not None
        assert x.confidence <= (0.9 if x.contact_id == by_email else 0.8)


def test_linkedin_with_no_file_or_a_missing_file_offers_nothing(hub, tmp_path):
    cid = person(hub, email="rowan@vale.example")
    assert enrich_selection(hub, AccountSettings(), [cid], sources=["linkedin"]) == []
    s = AccountSettings(enrich_linkedin_file=str(tmp_path / "nowhere.csv"))
    assert enrich_selection(hub, s, [cid], sources=["linkedin"]) == []


# ---------------------------------------------------------------- 5d invitations

INVITE = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
SUMMARY:Pump trial review
ORGANIZER;CN="Juniper Holt":mailto:juniper@holt.example
ATTENDEE;CN=Rowan Vale;ROLE=REQ-PARTICIPANT:mailto:rowan@vale.example
ATTENDEE;CN=Board Room 2;CUTYPE=ROOM:mailto:room2@holt.example
ATTENDEE;CN=me@home.example:mailto:me@home.example
ATTENDEE;CN="Sage Mar
 low":mailto:sage@marlow.example
END:VEVENT
END:VCALENDAR
"""


def test_people_in_an_invitation_skip_rooms_and_unfold_names():
    people = {p.address: (p.role, p.name) for p in invitations.people_in(INVITE.encode())}
    assert people["juniper@holt.example"] == ("organizer", "Juniper Holt")
    assert people["sage@marlow.example"] == ("attendee", "Sage Marlow")
    assert people["me@home.example"] == ("attendee", "")           # an address is not a name
    assert "room2@holt.example" not in people


def test_an_invitation_names_a_contact_that_has_no_name_and_nothing_else(hub, tmp_path):
    f = put(hub, tmp_path / "a.ics", INVITE, when=epoch(2026, 9, 5))
    put(hub, tmp_path / "b.ics", INVITE, when=epoch(2026, 9, 6))
    nameless = person(hub, email="rowan@vale.example")
    named = person(hub, "R", "V", email="sage@marlow.example")
    props = enrich_selection(hub, AccountSettings(**ANY_AGE), [nameless, named], sources=["calendar"])
    assert {(p.contact_id, p.field_kind, p.new) for p in props} == {(nameless, "given", "Rowan"),
                                                                  (nameless, "family", "Vale")}
    for p in props:
        assert p.source == "calendar" and p.confidence <= 0.9 and p.when == epoch(2026, 9, 6)
        assert "b.ics" in p.receipt and "1 more invitation" in p.receipt
    assert f.exists()


def test_organisers_and_frequent_attendees_are_suggested_contacts(hub, tmp_path):
    for i in range(3):
        put(hub, tmp_path / f"m{i}.ics", INVITE, when=epoch(2026, 9, 1 + i))
    put(hub, tmp_path / "once.ics", INVITE.replace("sage@marlow.example", "once@marlow.example"),
        when=epoch(2026, 9, 9))
    person(hub, "Rowan", "Vale", email="rowan@vale.example")              # already a contact
    got = {s.email: s for s in invitations.calendar_suggestions(hub.store, owner_emails=["me@home.example"])}
    assert set(got) == {"juniper@holt.example", "sage@marlow.example"}       # organiser; three invitations
    assert got["juniper@holt.example"].source == "calendar" and got["juniper@holt.example"].count == 4
    via_run = {s.email for s in suggestions_for(hub, AccountSettings(**ANY_AGE))}
    assert {"juniper@holt.example", "sage@marlow.example"} <= via_run
    off = {s.email for s in suggestions_for(hub, AccountSettings(enrich_calendar=False, **ANY_AGE))}
    assert "juniper@holt.example" not in off


# ---------------------------------------------------------------- switches, 5e, the guard

@pytest.mark.parametrize("source,module,func", [
    ("vcard", vcards, "vcard_proposals"), ("linkedin", linkedin, "linkedin_proposals"),
    ("calendar", invitations, "calendar_proposals"), ("document", documents, "document_proposals")])
def test_a_source_that_is_switched_off_is_not_called(hub, monkeypatch, source, module, func):
    called = []
    monkeypatch.setattr(module, func, lambda *a, **k: called.append(source) or [])
    cid = person(hub, email="rowan@vale.example")
    s = AccountSettings(enrich_linkedin_file="x.csv", **ANY_AGE)
    setattr(s, run.SWITCHES[source], False)
    enrich_selection(hub, s, [cid], sources=ONLY_FILES)
    assert called == []
    setattr(s, run.SWITCHES[source], True)
    enrich_selection(hub, s, [cid], sources=ONLY_FILES)
    assert called == [source]


def test_documents_are_off_by_default_and_offer_nothing(hub):
    assert AccountSettings().enrich_documents is False
    cid = person(hub, "Avery", "Quill", email="avery.quill@northwind.example")
    assert documents.document_proposals(hub.store, cid, hub.get_card(cid)) == []
    assert enrich_selection(hub, AccountSettings(**ANY_AGE), [cid], sources=["document"]) == []


def test_the_new_local_sources_are_registered_and_on_by_default():
    s = AccountSettings()
    for source in ("vcard", "linkedin", "calendar"):
        assert source in run.LOCAL_SOURCES and run._on(s, source)
    assert "document" in run.LOCAL_SOURCES and not run._on(s, "document")


def test_every_new_local_source_runs_with_no_socket(hub, tmp_path):
    """9e: the guard fixture turns any connection into a failure; every source runs over real files."""
    put(hub, tmp_path / "avery.vcf", CARD)
    put(hub, tmp_path / "a.ics", INVITE)
    p = tmp_path / "Connections.csv"
    p.write_text(EXPORT, encoding="utf-8")
    cid = person(hub, email="rowan@vale.example")
    other = person(hub, email="avery.quill@northwind.example")
    s = AccountSettings(enrich_linkedin_file=str(p), enrich_documents=True, **ANY_AGE)
    props = enrich_selection(hub, s, [cid, other], sources=ONLY_FILES)
    assert {p_.source for p_ in props} == {"vcard", "linkedin", "calendar"}
    suggestions_for(hub, s)


# ---------------------------------------------------------------- 5c the settings button

def test_the_linkedin_button_stores_the_chosen_file_and_shows_its_name(qtbot, tmp_path):
    from PySide6.QtWidgets import QFormLayout, QPushButton, QWidget

    from app.ui.accounts import enrich_settings

    host = QWidget()
    qtbot.addWidget(host)
    saved: list[dict] = []
    ctl = enrich_settings.build(QFormLayout(host), lambda **v: saved.append(v))
    button = host.findChild(QPushButton, "enrich_linkedin_file")
    assert button is ctl.linkedin_button and button.text() == "Use a LinkedIn export..."
    chosen = tmp_path / "Connections.csv"
    ctl.pick_linkedin = lambda: str(chosen)
    button.click()
    assert saved[-1] == {"enrich_linkedin_file": str(chosen)}
    assert "Connections.csv" in ctl.linkedin_label.text()
    n = len(saved)
    ctl.pick_linkedin = lambda: ""                       # cancelled: nothing saved, nothing changed
    button.click()
    assert len(saved) == n and "Connections.csv" in ctl.linkedin_label.text()
    s = AccountSettings(enrich_linkedin_file="")
    enrich_settings.load(ctl, s)
    assert len(saved) == n and "Connections.csv" not in ctl.linkedin_label.text()
