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


# ---------------------------------------------------------------- order 1s round 3: what the index really holds
#
# On the owner's index every .vcf and .ics on disk was PARTIAL (text indexed, vectors still to come in a
# text-first run), and most vCards and invitations are attachments, not files on disk. Both were skipped.

ARCHIVE = "C:/mail/archive.pst"


def put_partial(hub, path, text, *, when=None):
    """A file on disk the way a text-first run leaves it: read, its vectors not made yet."""
    put(hub, path, text, when=when)
    hub.store.conn.execute("UPDATE files SET status = 'PARTIAL' WHERE path = ?", (str(path),))
    hub.store.conn.commit()
    return path


def _chunks_of(text):
    """The passages the indexer would store for `text`, with their offsets - the real chunker's."""
    from app.extract.base import normalise_whitespace
    from app.extract.chunker import chunk_text

    return [{"ordinal": i, "text": c.text, "char_start": c.char_start, "char_end": c.char_end}
            for i, c in enumerate(chunk_text(normalise_whitespace(text)))]


_mid = [400]


def attach(hub, name, text=None, *, sent, subject="Contact details", status="PARTIAL", chunks=None):
    """A message in an Outlook archive with `name` attached, indexed as the direct reader leaves it."""
    import json

    _mid[0] += 1
    store = hub.store
    key = f"pst://{ARCHIVE}/{_mid[0]}"
    mid = store.upsert_file(key, parent_dir=ARCHIVE, ext="msg", size_bytes=10, mtime_ns=1, status="INDEXED",
                            source_kind="pst_message")
    store.set_message(mid, subject=subject, sender="Avery Quill <avery.quill@northwind.example>",
                      recipients=json.dumps(["me@home.example"]), sent_at=sent, store_path=ARCHIVE,
                      entry_id=str(_mid[0]), folder_path="Inbox", folder_index=3)
    fid = store.upsert_file(f"{key}/attachments/{name}", parent_dir=key, ext=name.rsplit(".", 1)[-1],
                            size_bytes=len(text or ""), mtime_ns=epoch(2026, 10, 1) * 1_000_000_000,
                            status=status, source_kind="pst_message")
    store.replace_chunks(fid, chunks if chunks is not None else _chunks_of(text))
    return fid


def test_a_partial_vcard_on_disk_is_read(hub, tmp_path):
    put_partial(hub, tmp_path / "avery.vcf", CARD)
    cid = person(hub, email="avery.quill@northwind.example")
    assert any(p.field_kind == "title" for p in
               enrich_selection(hub, AccountSettings(**ANY_AGE), [cid], sources=["vcard"]))


def test_a_partial_invitation_on_disk_is_read(hub, tmp_path):
    put_partial(hub, tmp_path / "a.ics", INVITE)
    cid = person(hub, email="rowan@vale.example")
    assert {p.new for p in enrich_selection(hub, AccountSettings(**ANY_AGE), [cid], sources=["calendar"])} == \
        {"Rowan", "Vale"}


def test_a_vcard_attachment_is_read_from_its_indexed_text_with_the_message_as_receipt(hub):
    attach(hub, "avery.vcf", CARD, sent=epoch(2026, 8, 14), subject="My new details")
    cid = person(hub, email="avery.quill@northwind.example")
    props = enrich_selection(hub, AccountSettings(**ANY_AGE), [cid], sources=["vcard"])
    assert ("title", "Commissioning Lead") in {(p.field_kind, p.new) for p in props}
    for p in props:
        assert "My new details" in p.receipt and "2026-08-14" in p.receipt and "avery.vcf" in p.receipt
        assert p.when == epoch(2026, 8, 14)


def test_the_age_window_applies_to_the_attachments_message_date(hub):
    attach(hub, "avery.vcf", CARD, sent=epoch(2019, 2, 1))       # the attachment row itself is new
    cid = person(hub, email="avery.quill@northwind.example")
    assert enrich_selection(hub, AccountSettings(enrich_max_months=6), [cid], sources=["vcard"]) == []
    assert enrich_selection(hub, AccountSettings(**ANY_AGE), [cid], sources=["vcard"])


def _many_cards(n=60):
    return "".join(f"BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Person{i} Example\r\nN:Example;Person{i};;;\r\n"
                   f"ORG:Firm {i} Ltd\r\nTITLE:Senior Engineer {i}\r\nTEL;TYPE=CELL:+44 7700 900{i:03d}\r\n"
                   f"EMAIL:p{i}@firm{i}.example\r\nEND:VCARD\r\n" for i in range(n))


def test_a_long_attachment_is_rebuilt_from_overlapping_passages_by_their_offsets(hub):
    """The chunker's passages overlap and end mid-line: joined in order they repeat text and break lines;
    stitched by their offsets they are the extracted text exactly, and every card in it is read."""
    from app.extract.base import normalise_whitespace

    text = _many_cards()
    chunks = _chunks_of(text)
    assert len(chunks) > 2 and "".join(c["text"] for c in chunks) != normalise_whitespace(text)
    fid = attach(hub, "team.vcf", text, sent=epoch(2026, 9, 1), chunks=chunks)
    assert localfiles.indexed_text(hub.store, fid, "BEGIN:VCARD", "END:VCARD") == normalise_whitespace(text)
    for i in (0, 31, 59):
        cid = person(hub, email=f"p{i}@firm{i}.example")
        titles = {p.new for p in enrich_selection(hub, AccountSettings(**ANY_AGE), [cid], sources=["vcard"])
                  if p.field_kind == "title"}
        assert titles == {f"Senior Engineer {i}"}


def test_passages_with_a_gap_or_no_offsets_are_not_trusted(hub):
    text = _many_cards(30)
    chunks = _chunks_of(text)
    gap = [c for k, c in enumerate(chunks) if k != 1]
    fid = attach(hub, "gap.vcf", text, sent=epoch(2026, 9, 1), chunks=gap)
    assert localfiles.indexed_text(hub.store, fid, "BEGIN:VCARD", "END:VCARD") is None
    bare = [{"ordinal": c["ordinal"], "text": c["text"]} for c in chunks]
    fid = attach(hub, "bare.vcf", text, sent=epoch(2026, 9, 1), chunks=bare)
    assert localfiles.indexed_text(hub.store, fid, "BEGIN:VCARD", "END:VCARD") is None


def test_an_attachment_indexed_by_name_only_is_read_again_from_its_archive(hub, monkeypatch):
    from app.extract import pst_attachment

    calls = []

    def reader(archive, entry_id, name, **kw):
        calls.append((archive, entry_id, name, kw.get("search"), kw.get("folder_index")))
        return CARD.encode()

    monkeypatch.setattr(pst_attachment, "read_attachment", reader)
    attach(hub, "avery.vcf", sent=epoch(2026, 9, 2), chunks=[{"ordinal": 0, "text": "avery.vcf"}])
    cid = person(hub, email="avery.quill@northwind.example")
    props = enrich_selection(hub, AccountSettings(**ANY_AGE), [cid], sources=["vcard"])
    assert any(p.field_kind == "title" for p in props)
    assert calls and calls[0][0] == ARCHIVE and calls[0][2] == "avery.vcf" and calls[0][3] is False


def test_an_attachment_its_archive_cannot_give_back_is_skipped_quietly(hub, monkeypatch):
    from app.core.errors import AppErrorException, make_error
    from app.extract import pst_attachment

    def reader(*a, **k):
        raise AppErrorException(make_error("ERR_ATTACHMENT_OPEN", "test", path=ARCHIVE, name="x"))

    monkeypatch.setattr(pst_attachment, "read_attachment", reader)
    attach(hub, "avery.vcf", sent=epoch(2026, 9, 2), chunks=[{"ordinal": 0, "text": "avery.vcf"}])
    cid = person(hub, email="avery.quill@northwind.example")
    assert enrich_selection(hub, AccountSettings(**ANY_AGE), [cid], sources=["vcard"]) == []


def test_an_invitation_attachment_names_a_contact_with_its_message_as_receipt(hub):
    attach(hub, "invite.ics", INVITE, sent=epoch(2026, 9, 9), subject="Pump trial review")
    cid = person(hub, email="rowan@vale.example")
    props = enrich_selection(hub, AccountSettings(**ANY_AGE), [cid], sources=["calendar"])
    assert {p.new for p in props} == {"Rowan", "Vale"}
    assert all("Pump trial review" in p.receipt and p.when == epoch(2026, 9, 9) for p in props)


def test_an_attachments_card_of_someone_else_is_still_not_used_by_phone(hub):
    attach(hub, "avery.vcf", CARD, sent=epoch(2026, 9, 2))
    person(hub, "Avery", "Quill", email="avery.quill@northwind.example")
    shared_line = person(hub, "Front", "Desk", phone="+44 7700 900123")
    assert enrich_selection(hub, AccountSettings(**ANY_AGE), [shared_line], sources=["vcard"]) == []


def test_the_files_are_listed_once_a_run_not_once_a_contact(hub, tmp_path, monkeypatch):
    """9c (2026-10-13): listing the vCards and invitations for every contact cost the owner's index 1.08 s a
    contact. The listing is reused within a run, and a file indexed since is listed again at once."""
    put(hub, tmp_path / "avery.vcf", CARD)
    calls = []
    real = localfiles._list_files
    monkeypatch.setattr(localfiles, "_list_files", lambda *a: calls.append(a) or real(*a))
    for _ in range(5):
        assert len(localfiles.indexed_files(hub.store, vcards.EXTS)) == 1
    assert len(calls) == 1
    put(hub, tmp_path / "other.vcf", CARD.replace("Avery", "Blake"))
    assert len(localfiles.indexed_files(hub.store, vcards.EXTS)) == 2 and len(calls) == 2
    monkeypatch.setattr(localfiles, "LIST_SECONDS", 0.0)
    localfiles.indexed_files(hub.store, vcards.EXTS)
    assert len(calls) == 3
