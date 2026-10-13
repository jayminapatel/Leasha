"""Which country a signature is in: a named country, then the sender's domain, then the owner's region.

Order 1u section 4d. `app.contacts.enrich.sigplaces` had no test that imported it (static scan 2026-10-13). A number
typed without `+` is read in the region this returns, so a wrong answer is a wrong number offered.
"""

from __future__ import annotations

import pytest

from app.contacts.enrich import sigplaces


@pytest.mark.parametrize("text, code", [
    ("Riyadh, Kingdom of Saudi Arabia", "SA"),       # the longer name, read whole
    ("P.O. Box 123, Dubai, U.A.E.", "AE"),             # an initialism with its dots
    ("Rotterdam, The Netherlands", "NL"),
    ("Belfast, Northern Ireland", "GB"),             # not Ireland: the longer name wins
    ("London, UK", "GB"),
    ("Houston TX, U.S.A.", "US"),
])
def test_a_country_named_in_a_line_is_found_whole(text, code):
    assert sigplaces.country_in(text) == code


@pytest.mark.parametrize("text", [
    "The woman in charge",              # "oman" inside a word is not Oman
    "Indiana Jones",                    # nor "india" inside "Indiana"
    "Head of Sales",
    "",
])
def test_a_country_inside_another_word_or_none_at_all_is_not_found(text):
    assert sigplaces.country_in(text) is None


def test_a_city_is_given_back_as_the_line_spells_it():
    """Found by this test, 2026-10-13: a city of two or more words came back title-cased, never as written -
    `re.escape` escapes the space, so swapping the space afterwards left a pattern that matched nothing."""
    assert sigplaces.city_in("Office 12, Abu  Dhabi") == "Abu  Dhabi"
    assert sigplaces.city_in("PO Box 5, ABU DHABI") == "ABU DHABI"
    assert sigplaces.city_in("Dubai Marina") == "Dubai"
    assert sigplaces.city_in("Ras Al Khaimah free zone") == "Ras Al Khaimah"
    assert sigplaces.city_in("Unit 4, Mill Road") is None


@pytest.mark.parametrize("address, code", [
    ("ann@acme.co.uk", "GB"),
    ("acme.ae", "AE"),
    ("ann@acme.de.", "DE"),             # a trailing dot is still the domain
    ("ann@startup.io", None),           # a vanity ending says nothing about where anybody is
    ("ann@acme.com", None),
    ("", None),
])
def test_a_domain_country_is_its_country_code_ending_only(address, code):
    assert sigplaces.domain_country(address) == code


def test_the_region_is_a_named_country_then_the_domain_then_the_owners():
    block = ["Ann Lee", "ann@acme.co.uk", "www.acme.de/contact", "Doha, Qatar"]
    assert sigplaces.region_for(block, "ann@acme.co.uk", "GB") == "QA"
    assert sigplaces.region_for(["Ann Lee", "Senior Engineer"], "ann@acme.ae", "GB") == "AE"
    assert sigplaces.region_for(["Ann Lee"], "ann@acme.com", "IN") == "IN"


def test_an_address_or_a_link_line_never_names_the_region():
    """`ann@oman-trading.com` or `https://uk.example.com` say nothing about where the sender is."""
    lines = ["ann@acme.oman.example", "https://www.uk-shop.example/qatar"]
    assert sigplaces.region_for(lines, "ann@acme.com", "FR") == "FR"


def test_every_country_code_has_a_name_for_an_address():
    assert set(sigplaces.COUNTRIES.values()) <= set(sigplaces.COUNTRY_NAME)
    assert set(sigplaces.CCTLD.values()) <= set(sigplaces.COUNTRY_NAME)
