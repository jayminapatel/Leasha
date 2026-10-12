"""Order 1s 9b: the causes the first precision check found, each pinned so it cannot come back.

Layer: L7

The check sampled 100 signature proposals from the owner's index: 82 right, 18 wrong.
Every wrong one had one of seven causes; each section below is one cause, shaped like
the cases found and rewritten with invented people, companies, numbers and addresses
(UK numbers from the 07700 900xxx and 020 7946 0xxx ranges set aside for fiction).
"""

from __future__ import annotations

import json
from calendar import timegm

import pytest

from app.contacts import phone as phones
from app.contacts.card import Card, Field
from app.contacts.enrich import measure, signatures
from app.contacts.enrich.consensus import Reading
from app.contacts.enrich.owner import is_sent_folder, learn_owner
from app.contacts.enrich.sigparse import Signature, parse_block
from app.contacts.enrich.sigtail import clean_tail, is_legal
from app.contacts.hub import ContactHub
from app.storage.sqlite_store import SqliteStore


def epoch(y, m, d):
    return timegm((y, m, d, 12, 0, 0))


@pytest.fixture()
def hub(tmp_path):
    store = SqliteStore(tmp_path / "precision.db").connect()
    yield ContactHub(store)
    store.close()


_n = [0]


def seed(hub, sender, body, *, sent, folder="Inbox", to=("someone@else.example",), subject=None):
    _n[0] += 1
    s = hub.store
    fid = s.upsert_file(f"mail/p{_n[0]}.eml", parent_dir="mail", ext="eml", size_bytes=10, mtime_ns=_n[0],
                        status="INDEXED", source_kind="eml")
    s.set_message(fid, subject=subject or f"Note {_n[0]}", sender=sender, recipients=json.dumps(list(to)),
                  sent_at=sent, folder_path=folder)
    s.replace_chunks(fid, [{"ordinal": 0, "text": f"Subject: x\nFrom: {sender}", "label": "Headers"},
                           {"ordinal": 1, "text": body, "label": "Body"}])
    return fid


def numbers(props):
    return {phones.normalise(p.new, "GB") for p in props if p.field_kind == "phone"}


# ---- cause 1: the owner's own details, from every employer he ever sent mail from ---------------------------------

OWNER_NOW = "tomas@reyes-home.example"
#: Three past employers: (address, Sent folder as each archive names it, signature).
PAST = [
    ("tomas.reyes@alderbank.example", "Alderbank.pst/Sent Items",
     "Tomas Reyes\nSolutions Architect\nAlderbank Systems Ltd\nM: 07700 900311\nDDI: 020 7946 0311"),
    ("t.reyes@brightwell.example", "brightwell/[Gmail]/Sent Mail",
     "Tomas Reyes\nSenior Consultant\nBrightwell Engineering Ltd\nM: 07700 900322\nD: 020 7946 0322\n"
     "E  t.reyes@brightwell.example\n4 Quayside Walk, Bristol BS1 4QX"),
    ("tomas@corvane.example", "Corvane/Verzonden items",
     "Tomas Reyes\nPartner\nCorvane Partners LLP\nMobile: 07700 900333\nDirect: 020 7946 0333"),
]
OWNER_NUMBERS = {"+447700900311", "+442079460311", "+447700900322", "+442079460322", "+447700900333",
                 "+442079460333"}
PRIYA = "priya.nair@lumenor.example"


def owner_history(hub):
    for k, (addr, folder, sig) in enumerate(PAST):
        for d in (1, 2, 3):
            seed(hub, f"Tomas Reyes <{addr}>", f"Hello, note {d}.\n\nBest regards,\n{sig}\n",
                 sent=epoch(2019 + 2 * k, 3, d), folder=folder, to=(PRIYA,))
    for d in (1, 2):
        seed(hub, f"Tomas Reyes <{OWNER_NOW}>", "Hi,\n\nThanks,\nTomas Reyes\nM: 07700 900399\n",
             sent=epoch(2026, 5, d), folder="Sent Items", to=(PRIYA,))


def priya(hub):
    return hub.create(Card(scalars={"given": "Priya", "family": "Nair"}, fields=[Field("email", PRIYA, "work")]))


def test_a_sent_folder_is_known_by_its_last_part_in_any_archive_and_several_languages():
    for path in ("Sent Items", "x.pst/Sent Items", "[Gmail]/Sent Mail", "Sent", "Mailbox/Sent Messages",
                 "Archief/Verzonden items", "Postfach\\Gesendete Elemente"):
        assert is_sent_folder(path), path
    for path in ("Inbox", "Sent Items/Old", "Clients/Sentinel", ""):
        assert not is_sent_folder(path), path


def test_the_owner_is_learned_from_every_sent_folder_at_any_age(hub):
    owner_history(hub)
    owner = learn_owner(hub.store, [OWNER_NOW])
    assert {a for a, _, _ in PAST} | {OWNER_NOW} <= set(owner.emails)
    assert OWNER_NUMBERS | {"+447700900399"} <= {phones.normalise(n, "GB") or n for n in owner.phones}


def test_a_client_reply_quoting_the_owners_old_signature_never_offers_any_of_it(hub):
    owner_history(hub)
    cid = priya(hub)
    # A Dutch reply header: not a marker the own-part cut knows, so the owner's old block reaches the reader.
    body = ("Thanks Tomas, agreed.\n\nRegards,\nPriya Nair\nOptical Design Lead\nLumenor Optics Ltd\n"
            "T: 020 7946 0450\n\nVan: Tomas Reyes\nVerzonden: 3 mei 2026\nAan: Priya Nair\nOnderwerp: Plan\n\n"
            f"Priya, the plan.\n\nMet vriendelijke groet,\n{PAST[1][2]}\n")
    seed(hub, f"Priya Nair <{PRIYA}>", body, sent=epoch(2026, 5, 4), to=(OWNER_NOW,))
    props = signatures.signature_proposals(hub.store, cid, hub.get_card(cid), owner_emails=[OWNER_NOW])
    assert not numbers(props) & OWNER_NUMBERS
    offered = " ".join(str(p.new) for p in props)
    for owner_bit in ("Senior Consultant", "Brightwell", "Quayside", "t.reyes@"):
        assert owner_bit not in offered


def test_a_quoted_block_holding_an_old_owner_address_is_dropped_whole(hub):
    owner_history(hub)
    cid = priya(hub)
    # Priya's message, quoted by her colleague, carries the owner's old block with only his address line in it:
    # the direct line and title beside that address are the owner's too.
    quoted = ("Tomas, see Priya's note.\n\nBob\n\nFrom: Priya Nair <" + PRIYA + ">\n"
              "Sent: Monday, 7 September 2026 09:14\nTo: Bob Ray <bob@lumenor.example>\nSubject: Plan\n\n"
              "Bob, forwarding what Tomas sent me.\n\nKind regards,\nTomas Reyes\nPartner\n"
              "Direct: 020 7946 0377\nE  tomas@corvane.example\n")
    seed(hub, "Bob Ray <bob@lumenor.example>", quoted, sent=epoch(2026, 9, 8), to=(OWNER_NOW, PRIYA))
    props = signatures.signature_proposals(hub.store, cid, hub.get_card(cid), owner_emails=[OWNER_NOW])
    assert "+442079460377" not in numbers(props)
    assert not any(p.new == "Partner" for p in props)


def test_a_namesake_of_the_owner_outside_the_sent_folders_is_not_the_owner(hub):
    owner_history(hub)
    other = "tomas.reyes@quillfield.example"
    cid = hub.create(Card(scalars={"given": "Tomas", "family": "Reyes"}, fields=[Field("email", other, "work")]))
    seed(hub, f"Tomas Reyes <{other}>", "Hi.\n\nRegards,\nTomas Reyes\nPlant Manager\nQuillfield Mills Ltd\n"
         "M: 07700 900488\n", sent=epoch(2026, 6, 1), to=(OWNER_NOW,))
    owner = learn_owner(hub.store, [OWNER_NOW])
    assert other not in owner.emails                       # a display name alone is never enough (3h b)
    props = signatures.signature_proposals(hub.store, cid, hub.get_card(cid), owner=owner)
    assert "+447700900488" in numbers(props)


# ---- cause 2: legal text is not an address ------------------------------------------------------------------------

LEGAL_TAIL = """Regards,
Ravi Menon
Procurement Lead
Kestrel Foods Ltd
T: 020 7946 0555
Kestrel Foods Ltd (company no. 01234567) is located at 1 Mill Lane, London EC1A 9ZZ
Registered Office: 1 Mill Lane, London EC1A 9ZZ
VAT No: GB 123 4567 89 Registered in England No: 01234567
All e-mails sent and received by Kestrel Foods are monitored
Issued pursuant to licence by the Food Standards Board
***** Email Confidentiality Notice *****
Suite 9, Ferry House, 2 Dock Road, Hull HU1 9ZZ
Anything at all after the notice
"""


def test_clean_tail_drops_legal_and_paragraph_lines_and_everything_after_a_notice():
    assert clean_tail(LEGAL_TAIL).splitlines() == [
        "Regards,", "Ravi Menon", "Procurement Lead", "Kestrel Foods Ltd", "T: 020 7946 0555"]


@pytest.mark.parametrize("line", [
    "This email originates from Kestrel Foods Ltd, authorised and regulated by the Food Authority",
    "Registered in England and Wales No 01234567",
    "Kestrel Foods is a trademark of Kestrel Holdings",
    "If you are not the intended recipient please delete it",
    "We accept no liability for viruses",
    "Our firm. It is the best one in the whole wide county.",
])
def test_legal_or_paragraph_lines_are_recognised(line):
    assert is_legal(line) or clean_tail(line) == ""


def test_the_address_reader_rejects_legal_words_and_overlong_candidates():
    legal = parse_block(["Ravi Menon", "Registered Office: 1 Mill Lane, London EC1A 9ZZ"], name_tokens=["ravi"])
    assert "address" not in legal
    long_street = "Unit 4, " + ", ".join(f"Block {i} of the Long Industrial Estate" for i in range(4))
    assert "address" not in parse_block(["Ravi Menon", long_street, "Leeds LS1 9ZZ"], name_tokens=["ravi"])


# ---- cause 3: separators are not part of a value ---------------------------------------------------------------------

def test_clean_tail_splits_a_line_on_its_separators():
    assert clean_tail("48 Orchard Ridge Drive | Fairview, PA 17025\n\u2022 Knowledge Park, Block 9, Dubai\n"
                      "T: 020 7946 0018 \u00b7 M: 07700 900018").splitlines() == [
        "48 Orchard Ridge Drive", "Fairview, PA 17025", "Knowledge Park, Block 9, Dubai", "T: 020 7946 0018",
        "M: 07700 900018"]


def _sig_of(text, tokens, email="x@zz.example"):
    return parse_block(clean_tail(text).splitlines(), name_tokens=tokens, sender_email=email)


def test_a_us_address_keeps_its_state_with_the_zip_and_no_separator():
    sig = _sig_of("Dana Brook\nPlant Engineer\n48 Orchard Ridge Drive | Fairview, PA 17025", ["dana"])
    assert sig["address_text"] == "48 Orchard Ridge Drive, Fairview, PA 17025"
    assert sig["address"]["region"] == "PA" and sig["address"]["postcode"] == "17025"


def test_a_bulleted_address_has_no_bullet():
    sig = _sig_of("Hana Saleh\n\u2022 Knowledge Park, Block 9, Dubai", ["hana"])
    assert "\u2022" not in sig["address_text"] and sig["address_text"].startswith("Knowledge Park, Block 9")


def test_a_name_title_or_department_is_never_part_of_an_address():
    sig = _sig_of("Lena Ortiz | Business Operations | Process Solutions Centre |, Doha, Qatar", ["lena"])
    assert "Lena" not in sig.get("address_text", "") and "Operations" not in sig.get("address_text", "")
    for value in sig.values():
        assert "|" not in str(value)


def test_two_complete_addresses_are_never_joined_the_first_is_offered():
    sig = _sig_of("Omar Haddad\nOffice 4, Harbour House, Muscat, Oman | 12 Mill Lane, Leeds LS1 9ZZ", ["omar"])
    assert sig["address_text"] == "Office 4, Harbour House, Muscat, Oman"


# ---- cause 4: a web address cut by a line wrap --------------------------------------------------------------------

def test_a_wrapped_web_address_is_joined_again():
    sig = parse_block(["Ravi Menon", "www.kestrelfoods.co m"], name_tokens=["ravi"],
                      sender_email="ravi@kestrelfoods.com")
    assert sig["website"] == "https://www.kestrelfoods.com"
    sig = parse_block(["Ravi Menon", "www.kestrelfoods.co .uk"], name_tokens=["ravi"],
                      sender_email="ravi@kestrelfoods.co.uk")
    assert sig["website"] == "https://www.kestrelfoods.co.uk"


def test_a_truncated_web_address_is_never_offered():
    sig = parse_block(["Ravi Menon", "www.kestrelfoods.co"], name_tokens=["ravi"], sender_email="ravi@kestrelfoods.com")
    assert "website" not in sig


def _reading(sig, *, when, subject="m", block=()):
    row = type("Row", (), {"subject": subject, "sent_at": when})()
    return Reading(row, when, sig, list(block), False)


def test_a_website_unlike_the_email_domain_with_no_known_tld_needs_two_messages():
    card = Card(scalars={"given": "Ravi"}, fields=[Field("email", "ravi@mailbox.example", "work")])

    def one(when):
        block = ["Ravi Menon", "www.kestrel-group.example"]
        return _reading(parse_block(block, name_tokens=["ravi"], sender_email="ravi@mailbox.example"),
                        when=when, block=block)

    assert not [p for p in signatures.proposals_from(1, card, [one(epoch(2026, 5, 1))]) if p.field_kind == "url"]
    twice = signatures.proposals_from(1, card, [one(epoch(2026, 5, 1)), one(epoch(2026, 6, 1))])
    assert [p.new for p in twice if p.field_kind == "url"] == ["https://www.kestrel-group.example"]


# ---- cause 5: a country glued to a company -------------------------------------------------------------------------

def test_a_leading_country_or_city_is_not_part_of_a_company_or_department():
    sig = parse_block(["Salim Rawas", "Senior Engineer", "Oman, Sohar Qasr Petrochem Refining (SQPR)",
                       "Doha, Process Engineering Department"], name_tokens=["salim"], sender_email="salim@sqpr.om")
    assert sig["company"] == "Sohar Qasr Petrochem Refining (SQPR)"
    assert sig["department"] == "Process Engineering Department"


# ---- cause 6: an address is the whole run of its lines, or nothing ----------------------------------------------------

def test_the_lines_between_a_floor_and_the_city_are_kept():
    sig = parse_block(["Hana Saleh", "Project Manager", "Level 2", "Gate Village Building 5", "Dubai, UAE"],
                      name_tokens=["hana"])
    assert sig["address"]["street"] == "Level 2, Gate Village Building 5"
    assert sig["address_text"] == "Level 2, Gate Village Building 5, Dubai, United Arab Emirates"
    sig = parse_block(["Hana Saleh", "Level 2", "Palm View Tower, Dubai"], name_tokens=["hana"])
    assert sig["address_text"] == "Level 2, Palm View Tower, Dubai"


def test_a_business_unit_is_not_taken_for_the_street():
    sig = parse_block(["Greta Holm", "Applied Digital, Controls, and Integration Services Business Line, "
                                     "7 Wharf Square,", "London W2 9ZZ"], name_tokens=["greta"])
    assert sig["address"]["street"] == "7 Wharf Square" and sig["address"]["postcode"] == "W2 9ZZ"
    assert "Business Line" not in sig["address_text"]


def test_an_unclosed_or_dangling_address_is_not_offered():
    assert "address" not in parse_block(["Ann Lee", "Unit 4 (Rear, Mill Road", "Leeds LS1 9ZZ"], name_tokens=["ann"])
    assert "address" not in parse_block(["Ann Lee", "Floor 3,"], name_tokens=["ann"])


def test_a_place_address_needs_a_street_a_number_or_a_po_box():
    assert "address" not in parse_block(["Ann Lee", "Process Solutions Centre", "Doha, Qatar"], name_tokens=["ann"])


# ---- cause 7: the receipt and the sample show the block the value was read from ---------------------------------------

def test_a_proposal_carries_the_newest_block_that_holds_its_value():
    card = Card(scalars={"given": "Ann"}, fields=[Field("email", "ann@acme-widgets.example", "work")])
    holds = ["Ann Lee", "Senior Engineer", "Acme Widgets Ltd"]
    lacks = ["Ann Lee", "Acme Widgets Ltd"]
    sig = Signature(title="Senior Engineer", company="Acme Widgets Ltd", name_matched="1")
    # The newest message's reading claims the title, but its block does not hold it: the older one does.
    readings = [_reading(Signature(sig), when=epoch(2026, 6, 1), subject="newest", block=lacks),
                _reading(Signature(sig), when=epoch(2026, 5, 1), subject="older", block=holds)]
    props = {p.field_kind: p for p in signatures.proposals_from(1, card, readings)}
    assert "older" in props["title"].receipt and props["title"].block == holds
    assert "newest" in props["company"].receipt and props["company"].block == lacks


def test_a_value_in_no_block_is_never_offered():
    card = Card(scalars={"given": "Ann"}, fields=[Field("email", "ann@acme-widgets.example", "work")])
    ghost = Signature(title="Chief Ghost Officer", phone=[("mobile", "07700 900777")], name_matched="1")
    readings = [_reading(ghost, when=epoch(2026, 6, 1), block=["Ann Lee", "Senior Engineer"])]
    assert signatures.proposals_from(1, card, readings) == []


def test_the_sample_prints_the_block_of_the_proposals_own_receipt(tmp_path):
    card = Card(scalars={"given": "Ann"}, fields=[Field("email", "ann@acme-widgets.example", "work")])
    holds = ["Ann Lee", "Senior Engineer"]
    readings = [_reading(Signature(title="Senior Engineer", name_matched="1"), when=epoch(2026, 6, 1),
                         subject="newest", block=["Ann Lee"]),
                _reading(Signature(title="Senior Engineer", name_matched="1"), when=epoch(2026, 5, 1),
                         subject="older", block=holds)]
    props = signatures.proposals_from(1, card, readings)
    fun = measure.Funnel()
    measure._count(fun, 1, card, props, {}, True)
    out = tmp_path / "s.csv"
    measure.write_sample(fun, 5, out)
    text = out.read_text(encoding="utf-8")
    assert "Senior Engineer" in text.split("older", 1)[1]


# ---- HTML entities ---------------------------------------------------------------------------------------------------

def test_html_entities_are_decoded_before_reading():
    assert clean_tail("T: &#43;44 20 7946 0018\nAcme &amp; Sons Ltd&nbsp;\n&quot;Quoted&quot; &lt;x&gt;") == \
        'T: +44 20 7946 0018\nAcme & Sons Ltd\n"Quoted" <x>'


# =====================================================================================================================
# Round 2 of 9b: the second sample (84/100). Same rules: invented people, numbers and addresses.
# =====================================================================================================================

# ---- r2 cause 1: a firm's footer list of countries is not part of the address -----------------------------------------

def test_an_address_ends_at_its_first_country_and_a_list_of_countries_is_dropped():
    sig = _sig_of("Pieter Smit\nSenior Engineer\nKorenmolen 13D | 4817 ZX Breda | The Netherlands | Belgium | "
                  "France | Ireland | Luxembourg", ["pieter"])
    assert sig["address_text"] == "Korenmolen 13D, 4817 ZX Breda, Netherlands"


def test_a_country_list_on_the_address_line_keeps_only_the_first_country():
    sig = parse_block(["Niran Chai", "Soi Ruam 4, Bangkok 10250, Thailand, Belgium, France, Ireland"],
                      name_tokens=["niran"])
    assert sig["address_text"].endswith("Thailand") and "Belgium" not in sig["address_text"]


def test_a_list_of_countries_is_never_joined_to_an_address():
    sig = parse_block(["Ann Lee", "Unit 4, Harbour Road", "Dubai", "Belgium", "France"], name_tokens=["ann"])
    assert sig["address_text"] == "Unit 4, Harbour Road, Dubai"


# ---- r2 cause 2: a meeting invitation is not a signature --------------------------------------------------------------

def test_clean_tail_stops_at_a_meeting_invitation():
    text = ("Regards,\nAnn Lee\nT: 020 7946 0101\n\nJoin Microsoft Teams Meeting\n"
            "Or call in (audio only) | +91 22 5555 0128,,,,125254186#    India, Mumbai | Phone Conference ID: "
            "125 254 186# | Find a local number | Reset PIN | Meeting options\n12 Mill Lane, Leeds LS1 9ZZ")
    assert clean_tail(text).splitlines() == ["Regards,", "Ann Lee", "T: 020 7946 0101"]


@pytest.mark.parametrize("marker", [
    "Or call in (audio only)", "Phone Conference ID: 125 254 186#", "Join by phone", "Meeting options",
    "Global call-in numbers", "United Kingdom Toll", "Join from a video system or application", "Reset PIN",
    "+44-20-7946-0149,,1374429198#54775552#", "Join Microsoft Teams Meeting"])
def test_every_meeting_marker_ends_the_tail(marker):
    assert clean_tail(f"Ann Lee\n{marker}\nUnit 4, Mill Road, Leeds LS1 9ZZ") == "Ann Lee"


@pytest.mark.parametrize("line", ["+44-20-7946-0149,,1374429198# United Kingdom, London",
                                  "SAUDI ARABIA     800-8-110078", "Dial 125254186# then Doha, Qatar"])
def test_a_dial_string_or_toll_number_is_never_an_address(line):
    assert "address" not in parse_block(["Ann Lee", "Unit 4, Mill Road", line], name_tokens=["ann"])
    assert "address" not in parse_block(["Ann Lee", line], name_tokens=["ann"])


# ---- r2 cause 3: a phone line is not an address line ---------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "Customer Care  920 055 101, Qamar Street, Riyadh",
    "Kingdom of Bahrain, Bahrain : Mobile: (+973) 3355 0123, United Arab Emirates",
    "M UK: +44 7700 900591, Saudi Arabia",
    "Office: 4455 0101, Tower 2, Doha, Qatar",
    "Unit 4, Mill Road, 0113 496 0555, Leeds LS1 9ZZ"])
def test_a_line_holding_a_phone_is_never_part_of_an_address(line):
    sig = parse_block(["Fahad Ali", line], name_tokens=["fahad"])
    assert "address" not in sig


def test_a_po_box_or_postcode_number_is_not_a_phone():
    assert parse_block(["Fahad Ali", "P.O. Box 1234567, Doha, Qatar"], name_tokens=["fahad"])["address"][
        "street"] == "P.O. Box 1234567"
    assert "address" in parse_block(["Dana Brook", "48 Orchard Ridge Drive", "Fairview, PA 17025-1234"],
                                    name_tokens=["dana"])


# ---- r2 cause 4: the replacement character ------------------------------------------------------------------------------

def test_a_replacement_character_standing_for_a_bullet_is_a_separator():
    assert clean_tail("\ufffd Korenstraat 1, \ufffd 2000 Antwerpen").splitlines() == ["Korenstraat 1",
                                                                                        "2000 Antwerpen"]


def test_a_value_still_holding_a_replacement_character_is_refused():
    sig = parse_block(["Ann Lee", "Senior Engin\ufffder", "Molen\ufffdstraat 1", "2000 Antwerpen, Belgium"],
                      name_tokens=["ann"])
    assert all("\ufffd" not in str(v) for v in sig.values())
    assert "address" not in sig and "title" not in sig


# ---- r2 cause 5: a title is not a department, a department is not an address ------------------------------------------

def test_the_line_after_the_name_with_a_title_word_is_the_title_not_a_department():
    sig = parse_block(["Omar Haddad", "Tendering Team Leader", "Sahil Trading W.L.L."], name_tokens=["omar"])
    assert sig["title"] == "Tendering Team Leader" and "department" not in sig


def test_a_department_line_is_never_part_of_an_address_even_ending_in_a_country():
    sig = parse_block(["Layla Aziz", "Private & Corporate Lending (PCL), Bahrain, P.O. Box 5701, Manama"],
                      name_tokens=["layla"])
    assert "Lending" not in sig.get("address_text", "") and "PCL" not in sig.get("address_text", "")


# ---- r2 cause 6: a postcode line keeps its city and postcode ---------------------------------------------------------

def test_an_indian_pin_after_its_city_is_kept_with_the_city():
    sig = _sig_of("Ravi Kumar\nKestrel Infotech Limited | Plot No.48, Phase III, Kharadi, Pune 411014 India",
                  ["ravi"])
    assert sig["address_text"] == "Plot No.48, Phase III, Kharadi, Pune 411014, India"
    assert sig["address"]["city"] == "Pune" and sig["address"]["postcode"] == "411014"
    assert sig["address"]["country"] == "India"
