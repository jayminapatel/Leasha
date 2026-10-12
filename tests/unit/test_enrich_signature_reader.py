"""Order 1s section 3a-3g: the signature reader, rebuilt - the pure half.

Layer: L7

Text in, lines or values out; no store. The golden texts are the five shapes the
order's opening found on the owner's index (no sign-off, a sign-off the reader did
not know, Outlook's doubled lines, junk after the signature, a signature the index
cut), rewritten with invented people, companies and numbers. Numbers are from the
ranges set aside for fiction where a country has one (UK 07700 900xxx, 020 7946 0xxx).
"""

from __future__ import annotations

import pytest

from app.contacts import phone as phones
from app.contacts.enrich import sigblock, signatures
from app.contacts.enrich.sigparse import parse_block
from app.contacts.enrich.sigtail import clean_tail, own_part

# ---- 3a: clean before reading --------------------------------------------------------------------------------

JUNK_AFTER = """Kind regards,
Omar Haddad
Commercial Manager
Sahil Trading W.L.L.
T: +974 4455 0101
E: omar.haddad@sahil-trading.example<mailto:omar.haddad@sahil-trading.example>
www.sahil-trading.example<http://www.sahil-trading.example/>
[cid:image001.png@01DA1234.56789AB0]
https://protect-eu.mimecast.com/s/AbCdEfGhIj?domain=sahil-trading.example
https://eur02.safelinks.protection.outlook.com/?url=https%3A%2F%2Fwww.sahil-trading.example&data=05
[LinkedIn] [Twitter] [Facebook]
LinkedIn | Instagram | YouTube
Follow us on LinkedIn
http://t.sidekickopen07.com/s1t/c/5/f18dQhb0S7lC8dDMPbW2n0x6l2B9nMJW7t5XZs4WJfgqW
http://sahil-trading.us1.list-manage.com/track/click?u=abc&id=def
"""


def test_clean_tail_drops_link_annotations_tracking_links_icons_and_follow_us():
    assert clean_tail(JUNK_AFTER).splitlines() == [
        "Kind regards,", "Omar Haddad", "Commercial Manager", "Sahil Trading W.L.L.", "T: +974 4455 0101",
        "E: omar.haddad@sahil-trading.example", "www.sahil-trading.example"]


def test_clean_tail_collapses_runs_of_blank_lines_and_invisible_spaces():
    text = "Thanks\n\n\n\n\u00a0\nRegards\n \n\u200b\n\nAnn"
    assert clean_tail(text) == "Thanks\n\nRegards\n\nAnn"


def test_a_company_home_page_on_its_own_line_is_kept():
    # 3a drops lines that are only links of the tracking or rewritten kind; a home page is the website (7a)
    assert clean_tail("Ann Lee\nhttps://www.acme-widgets.co.uk/") == "Ann Lee\nhttps://www.acme-widgets.co.uk/"
    assert clean_tail("Ann Lee\nhttps://acme.example/news/2026/09/our-new-plant?utm_source=sig") == "Ann Lee"


def test_the_own_part_stops_at_a_quoted_chain_and_at_a_confidentiality_notice():
    text = ("Fine by me.\n\nAnn Lee\nM: 07700 900123\n\nThis email is confidential and intended only for you.\n"
            "From: Jay Patel <me@home.example>\nSent: Monday, 7 September 2026 09:14\nTo: Ann\n\nJay Patel\n"
            "M: 07700 900999")
    own = own_part(text)
    assert "07700 900123" in own and "confidential" not in own and "900999" not in own


# ---- the five shapes, end to end through the pure reader --------------------------------------------------------

FYI = """FYI

Nadia Farouk
Senior Planning Engineer
Qasr Refining Company LLC
T: +971 2 555 0134
M: +971 50 555 0172
E: nadia.farouk@qasr-refining.ae
"""

MY_BEST = """Hi team, the drawings are attached.

My Best,
Pieter Van Damme
Lead Process Engineer
Brugwater Engineering BV
Direct: +32 3 555 01 23
Mobile: 0470 55 01 99
"""

DOUBLED = ("Thanks Bob, that works.\n\n\n\nRegards\n\n\u00a0\n\nGreta Holm\n\n\n\nProject Director\n\n\n\n"
           "Nordlys Consulting Ltd\n\n\n\nMob: 07700 900456\n\n\n\nTel: 020 7946 0321\n\n\n\n"
           "12 Harbour Street\n\n\n\nLeith EH6 6QN\n\n\n\nwww.nordlys-consulting.example\n\n")


def _read(text, tokens, email, region="GB"):
    lines = sigblock.tail_lines(clean_tail(own_part(text)))
    block = sigblock.anchored_block(lines, name_tokens=tokens, emails=[email])
    return parse_block(block, name_tokens=tokens, sender_email=email, region=region)


def test_shape_1_no_sign_off_the_name_line_is_the_anchor():
    sig = _read(FYI, ["nadia", "farouk"], "nadia.farouk@qasr-refining.ae")
    assert sig["title"] == "Senior Planning Engineer" and sig["company"] == "Qasr Refining Company LLC"
    nums = {label: phones.normalise(n, "AE") for label, n in sig["phone"]}
    assert nums == {"work": "+97125550134", "mobile": "+971505550172"}


def test_shape_2_a_sign_off_the_reader_did_not_know():
    sig = _read(MY_BEST, ["pieter", "damme"], "pieter@brugwater.be")
    assert sig["title"] == "Lead Process Engineer" and sig["company"] == "Brugwater Engineering BV"
    nums = {label: phones.normalise(n, "GB") for label, n in sig["phone"]}
    # 3d: the mobile has no + and the domain is Belgian, so it is read as a Belgian number, and kept as one
    assert nums == {"direct": "+3235550123", "mobile": "+32470550199"}


def test_shape_3_outlooks_doubled_lines_still_fit_the_block():
    sig = _read(DOUBLED, ["greta", "holm"], "greta@nordlys-consulting.example")
    assert sig["title"] == "Project Director" and sig["company"] == "Nordlys Consulting Ltd"
    assert {label for label, _ in sig["phone"]} == {"mobile", "work"}
    assert sig["address"]["postcode"] == "EH6 6QN" and sig["website"] == "https://www.nordlys-consulting.example"


def test_shape_4_junk_after_the_signature_is_not_read_and_does_not_push_it_out():
    text = "Please see below.\n\n" + JUNK_AFTER + "\n".join(f"https://protect-eu.mimecast.com/s/x{i}" for i in range(30))
    sig = _read(text, ["omar", "haddad"], "omar.haddad@sahil-trading.example")
    assert sig["title"] == "Commercial Manager" and sig["company"] == "Sahil Trading W.L.L."
    assert sig["website"] == "https://www.sahil-trading.example"


def test_shape_5_a_signature_under_a_delimiter_is_read_when_it_reaches_the_reader():
    # The index cut it (strip_quoted, at "-- "); section 4a/4b hand it back through full_text.
    text = "Please see the revised schedule.\n-- \nLiam Byrne\nSite Manager\nCorrib Build Ltd\nM: +353 87 555 0142"
    sig = _read(text, ["liam", "byrne"], "liam@corrib-build.ie")
    assert sig["title"] == "Site Manager" and phones.normalise(sig["phone"][0][1]) == "+353875550142"


# ---- 3b: the block is what repeats ------------------------------------------------------------------------------

def _two(body_a, body_b, sig):
    return [body_a + "\n" + sig, body_b + "\n" + sig]


RAVI = "Ravi Menon\nProcurement Lead\nKestrel Foods Ltd\nTel: 020 7946 0555"


def test_the_longest_run_shared_by_two_messages_is_the_signature():
    texts = _two("Hello,\nThe order is late again.\nSpeak soon", "Hi,\nInvoice attached.\nTa", RAVI)
    blocks = sigblock.find_blocks([sigblock.tail_lines(clean_tail(t)) for t in texts], name_tokens=[], emails=[])
    assert blocks[0] == RAVI.splitlines() and blocks[1] == RAVI.splitlines()
    assert all(b.how == "repeat" for b in blocks)


def test_a_title_that_changed_between_messages_is_still_inside_the_block():
    new = RAVI.replace("Procurement Lead", "Head of Procurement")
    tails = [sigblock.tail_lines(t) for t in ("Ok.\n" + new, "Sure thing.\n" + RAVI)]
    blocks = sigblock.find_blocks(tails, name_tokens=["ravi", "menon"], emails=[])
    assert blocks[0][:2] == ["Ravi Menon", "Head of Procurement"]


def test_one_message_falls_back_to_the_anchors():
    blocks = sigblock.find_blocks([sigblock.tail_lines(MY_BEST)], name_tokens=["pieter"], emails=[])
    assert blocks[0].how == "anchor" and blocks[0][0] == "Pieter Van Damme"


def test_a_shared_disclaimer_is_not_taken_for_the_signature():
    notice = ("Kestrel Foods Ltd is registered in England and Wales under company number 01234567 and its "
              "registered office is at 1 Mill Lane, which is where all of our post should be sent please.")
    tails = [sigblock.tail_lines(f"Body {i}.\nThanks,\nRavi Menon\nM: 07700 900{i}11\n{notice}") for i in (1, 2)]
    blocks = sigblock.find_blocks(tails, name_tokens=["ravi"], emails=[])
    assert blocks[0][0] == "Ravi Menon"


# ---- 3c: anchors -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sign_off", ["My best", "Best,", "BR", "Br,", "Thanks,", "Regards", "KIND REGARDS",
                                      "Cordialement", "Mit freundlichen Gr\u00fc\u00dfen", "Saludos", "Groeten",
                                      "Met vriendelijke groet", "Shukran"])
def test_every_sign_off_on_the_wider_list_starts_a_block_in_any_case(sign_off):
    lines = sigblock.tail_lines(f"Body text here.\n{sign_off}\nX Y\nSenior Engineer")
    assert sigblock.anchored_block(lines, name_tokens=[], emails=[]) == ["X Y", "Senior Engineer"]


def test_the_persons_own_address_or_a_phone_anchors_a_block_with_no_name_known():
    lines = sigblock.tail_lines("We agreed the scope on Tuesday.\nR. Okafor\nPlanner\nr.okafor@zz.example")
    assert sigblock.anchored_block(lines, name_tokens=[], emails=["r.okafor@zz.example"])[0] == "R. Okafor"


def test_a_block_is_at_most_fourteen_non_blank_lines():
    lines = sigblock.tail_lines("Thanks,\n" + "\n\n".join(f"line {i}" for i in range(20)))
    assert len(sigblock.anchored_block(lines, name_tokens=[], emails=[])) == 14


def test_no_anchor_no_block():
    assert sigblock.anchored_block(sigblock.tail_lines("Thanks, see you Tuesday."), name_tokens=["ann"],
                                   emails=["ann@x.example"]) == []


# ---- 3d: phones the right country ---------------------------------------------------------------------------------

def test_the_region_comes_from_a_country_named_in_the_block_then_the_domain_then_the_owner():
    named = parse_block(["Sara Aziz", "Riyadh, Saudi Arabia", "T: 011 555 0123"], name_tokens=["sara"],
                        sender_email="sara@zz.example", region="GB")
    assert phones.normalise(named["phone"][0][1]) == "+966115550123"
    by_domain = parse_block(["Sara Aziz", "T: 2255 0123"], name_tokens=["sara"], sender_email="sara@zz.com.kw")
    assert phones.normalise(by_domain["phone"][0][1]) == "+96522550123"
    owner = parse_block(["Sara Aziz", "T: 020 7946 0123"], name_tokens=["sara"], sender_email="sara@zz.example")
    assert phones.normalise(owner["phone"][0][1], "GB") == "+442079460123"


def test_fax_is_never_offered_and_direct_keeps_its_label():
    sig = parse_block(["Ann Lee", "Fax: 020 7946 0999", "DDI: 020 7946 0111", "F: 020 7946 0998"],
                      name_tokens=["ann"])
    assert sig["phone"] == [("direct", "020 7946 0111")]


# ---- 3e: addresses with no postcode ------------------------------------------------------------------------------

def test_a_po_box_with_a_city_is_an_address():
    sig = parse_block(["Omar Haddad", "P.O. Box 5551, Doha, Qatar"], name_tokens=["omar"])
    assert sig["address"] == {"street": "P.O. Box 5551", "city": "Doha", "country": "Qatar"}


def test_a_building_line_followed_by_a_city_and_country_is_an_address():
    sig = parse_block(["Nadia Farouk", "Level 12, Falcon Tower, Sheikh Zayed Road", "Dubai, United Arab Emirates"],
                      name_tokens=["nadia"])
    assert sig["address"] == {"street": "Level 12, Falcon Tower, Sheikh Zayed Road", "city": "Dubai",
                              "country": "United Arab Emirates"}


def test_an_address_takes_its_country_from_the_domain_when_the_lines_do_not_say():
    sig = parse_block(["Salim Rawas", "Office 4, Harbour House", "Muscat"], name_tokens=["salim"],
                      sender_email="salim@zz.om")
    assert sig["address"]["city"] == "Muscat" and sig["address"]["country"] == "Oman"


def test_a_street_with_no_known_city_is_not_an_address():
    assert "address" not in parse_block(["Ann Lee", "Unit 4, Mill Road", "Somewhere Nice"], name_tokens=["ann"])


# ---- 3f: title and company, harder to fool ---------------------------------------------------------------------

def test_a_title_must_sit_within_two_lines_of_the_name_line():
    far = parse_block(["Ann Lee", "Acme Widgets Ltd", "T: 020 7946 0018", "Engineering Manager"],
                      name_tokens=["ann"])
    assert "title" not in far and far["company"] == "Acme Widgets Ltd"
    assert "title" not in parse_block(["Engineering Manager", "Acme Widgets Ltd"], name_tokens=["ann"])


def test_a_title_on_the_name_line_itself_is_read():
    assert parse_block(["Ann Lee | Senior Engineer"], name_tokens=["ann"])["title"] == "Senior Engineer"


def test_a_department_line_goes_to_department_not_company():
    sig = parse_block(["Ann Lee", "Senior Engineer", "Energy & Natural Resources Business Unit", "Acme Widgets Ltd"],
                      name_tokens=["ann"])
    assert sig["department"] == "Energy & Natural Resources Business Unit" and sig["company"] == "Acme Widgets Ltd"


def test_a_sentence_is_never_a_title_or_a_company():
    sig = parse_block(["Ann Lee", "Our lead engineer will call you.", "We are the Widget Company."],
                      name_tokens=["ann"], sender_email="ann@widget.example")
    assert "title" not in sig and "company" not in sig


def test_a_company_known_on_another_contact_at_the_domain_is_accepted():
    block = ["Ann Lee", "Senior Engineer", "Zenith Widgets"]
    assert "company" not in parse_block(block, name_tokens=["ann"], sender_email="ann@zw-group.example")
    sig = parse_block(block, name_tokens=["ann"], sender_email="ann@zw-group.example",
                      domain_companies={"zenith widgets"})
    assert sig["company"] == "Zenith Widgets"


# ---- 3g: other addresses of the same person ------------------------------------------------------------------------

def test_another_address_with_the_persons_local_part_or_name_is_offered_and_a_shared_one_is_not():
    sig = parse_block(["Ann Lee", "E-Mail: ann.lee@acme-holdings.example", "info@acme-holdings.example",
                       "Ann.Lee@acme-widgets.co.uk"],
                      name_tokens=["ann", "lee"], sender_email="ann.lee@acme-widgets.co.uk",
                      own_emails=["ann.lee@acme-widgets.co.uk"])
    assert sig["emails"] == ["ann.lee@acme-holdings.example"]


def test_the_old_find_block_still_reads_a_plain_sign_off():
    assert signatures.find_block("Hi\n\nKind regards,\n\nAnn Lee\nEngineer") == ["Ann Lee", "Engineer"]
