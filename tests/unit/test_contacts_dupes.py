"""Order 1k 6n, 6o, D9 - finding duplicates, and the merge preview.

Layer: L7
"""

from __future__ import annotations

import pytest

from app.contacts import dupes
from app.contacts.card import Card, Field, Photo, units
from tests.unit.contacts_samples import TINY_PNG


def c(given="", family="", *, email=(), phone=(), company="", **more) -> Card:
    card = Card(scalars={k: v for k, v in dict(given=given, family=family, company=company, **more).items() if v})
    card.fields = [Field("email", e) for e in email] + [Field("phone", p) for p in phone]
    return card


def groups_by_ids(result):
    return {frozenset(g.ids): g for g in result}


def test_exact_email_and_exact_phone_are_safe():
    items = [(1, c("Ann", "Lee", email=["Ann@X.example"])),
             (2, c("A.", "Lee", email=[" ann@x.example "])),
             (3, c("Bob", "Bell", phone=["07911 123456"])),
             (4, c("Bob", "B.", phone=["+44 7911 123456"])),
             (5, c("Cy", "Cox", email=["cy@x.example"]))]
    g = groups_by_ids(dupes.scan_duplicates(items, set()))
    assert set(g) == {frozenset({1, 2}), frozenset({3, 4})}
    assert g[frozenset({1, 2})].safe and "same email" in g[frozenset({1, 2})].reasons
    assert g[frozenset({3, 4})].safe and "same phone" in g[frozenset({3, 4})].reasons


def test_a_name_alone_is_a_suggestion_never_safe():
    items = [(1, c("Ann", "Lee", email=["a@x.example"])), (2, c("ann", "LEE", email=["different@y.example"]))]
    (g,) = dupes.scan_duplicates(items, set())
    assert not g.safe and g.reasons == ("same name",)


def test_name_and_company_is_a_suggestion_with_higher_confidence_than_name_alone():
    items = [(1, c("Ann", "Lee", company="Acme")), (2, c("Ann", "Lee", company="acme ")),
             (3, c("Joe", "Doe", company="A")), (4, c("Joe", "Doe", company="B"))]
    g = groups_by_ids(dupes.scan_duplicates(items, set()))
    both, name = g[frozenset({1, 2})], g[frozenset({3, 4})]
    assert not both.safe and not name.safe
    assert both.reasons == ("same name and company",) and name.reasons == ("same name",)
    assert both.confidence > name.confidence
    assert both.confidence < 0.9                                                    # below any exact match


def test_confidence_ordering_is_deterministic_and_safe_first():
    items = [(10, c("Zed", "Zulu")), (11, c("Zed", "Zulu")),
             (20, c("Ann", "Aa", email=["e@x.example"])), (21, c("A.", "Aa", email=["e@x.example"])),
             (30, c("Cy", "Cox", phone=["07911 123456"])), (31, c("C.", "Cox", phone=["07911123456"])),
             (40, c("Ann", "Lee", company="Acme")), (41, c("Ann", "Lee", company="Acme"))]
    first = dupes.scan_duplicates(items, set())
    again = dupes.scan_duplicates(list(items), set())
    assert first == again
    conf = [g.confidence for g in first]
    assert conf == sorted(conf, reverse=True)
    assert [g.safe for g in first] == [True, True, False, False]
    assert first[0].ids == (20, 21)                                               # email ahead of phone
    assert tuple(sorted(first[1].ids)) == (30, 31)
    shuffled = dupes.scan_duplicates(list(reversed(items)), set())
    assert [frozenset(g.ids) for g in shuffled] == [frozenset(g.ids) for g in first]


def test_merge_all_safe_candidates_never_include_a_name_only_group():
    items = [(1, c("Ann", "Lee")), (2, c("Ann", "Lee")),                              # name only
             (3, c("Bob", "Bell", email=["b@x.example"])), (4, c("Bob", "Bell", email=["b@x.example"])),
             (5, c("Cy", "Cox", company="Z")), (6, c("Cy", "Cox", company="Z"))]
    result = dupes.scan_duplicates(items, set())
    safe = [g for g in result if g.safe]
    assert [g.ids for g in safe] == [(3, 4)]
    assert {i for g in safe for i in g.ids}.isdisjoint({1, 2, 5, 6})
    assert dupes.safe_groups(result) == safe


def test_a_chain_through_a_name_is_not_safe_and_keeps_its_safe_core_separate():
    items = [(1, c("Ann", "Lee", email=["a@x.example"])), (2, c("Ann", "Lee", email=["a@x.example"])),
             (3, c("Ann", "Lee", email=["other@y.example"]))]
    result = dupes.scan_duplicates(items, set())
    safe = [g for g in result if g.safe]
    assert [g.ids for g in safe] == [(1, 2)]
    wider = [g for g in result if not g.safe]
    assert len(wider) == 1 and set(wider[0].ids) == {1, 2, 3}


def test_only_exact_e164_is_safe_a_loose_digit_match_is_not():
    items = [(1, c("Ann", "Aa", phone=["12345678901234567"])), (2, c("A.", "Aa", phone=["1234 5678 9012 34567"]))]
    (g,) = dupes.scan_duplicates(items, set())
    assert not g.safe and "digits" in g.reasons[0]
    short = [(1, c("Ann", "Aa", phone=["123"])), (2, c("A.", "Aa", phone=["123"]))]
    assert dupes.scan_duplicates(short, set()) == []


def test_a_rejected_pair_is_not_suggested_again_after_a_rescan():
    items = [(1, c("Ann", "Lee", email=["a@x.example"])), (2, c("Ann", "Lee", email=["a@x.example"])),
             (3, c("Bob", "Bell", phone=["07911 123456"])), (4, c("B.", "B.", phone=["07911 123456"]))]
    first = dupes.scan_duplicates(items, set())
    assert len(first) == 2
    rejected = {(2, 1)}                                                              # either order
    again = dupes.scan_duplicates(items, rejected)
    assert [g.ids for g in again] == [(3, 4)]
    rescan = dupes.scan_duplicates(items + [(5, c("Cy", "C"))], rejected)
    assert [g.ids for g in rescan] == [(3, 4)]
    assert dupes.scan_duplicates(items, {(1, 2), (3, 4)}) == []


def test_group_where_a_rejected_pair_is_linked_through_a_third_is_not_safe():
    items = [(1, c("Ann", "Aa", email=["x@x.example"])), (2, c("A.", "Aa", email=["x@x.example"])),
             (3, c("A", "Aa", email=["x@x.example"]))]
    assert dupes.scan_duplicates(items, set())[0].safe
    (g,) = dupes.scan_duplicates(items, {(1, 2)})
    assert not g.safe and set(g.ids) == {1, 2, 3}


def test_pairs_helper_expands_a_rejected_group():
    assert dupes.pairs_of([3, 1, 2]) == {(1, 2), (1, 3), (2, 3)}


def test_blank_cards_do_not_match_each_other():
    items = [(1, Card()), (2, Card()), (3, c("", "", email=[""])), (4, c("", "", email=[""]))]
    assert dupes.scan_duplicates(items, set()) == []


# ---- merge preview -----------------------------------------------------------

def test_merge_unions_repeatable_fields_and_first_non_empty_scalar_wins():
    a = c("Ann", "Lee", email=["a@x.example"], company="Acme", title="")
    a.fields.append(Field("phone", "07911 123456", "mobile"))
    a.groups = ["Family"]
    b = c("Annie", "Lee", email=["A@X.example", "b@x.example"], company="Other", title="Buyer")
    b.fields.append(Field("phone", "+44 7911 123456", "work"))
    b.groups = ["family", "Work"]
    b.favourite = True
    b.photo = Photo("image/png", TINY_PNG)
    pv = dupes.merge_cards([a, b])
    m = pv.card
    assert (m.get("given"), m.get("company"), m.get("title")) == ("Ann", "Acme", "Buyer")
    assert [f.value for f in m.values("email")] == ["a@x.example", "b@x.example"]
    assert [f.value for f in m.values("phone")] == ["07911 123456"]                  # same number: one value, first label
    assert m.values("phone")[0].label == "mobile"
    assert m.groups == ["Family", "Work"] and m.favourite and m.photo is not None
    assert pv.field_sources["s:given"] == [0] and pv.field_sources["s:title"] == [1]
    assert pv.field_sources["f:email:a@x.example"] == [0, 1] and pv.field_sources["f:email:b@x.example"] == [1]
    assert pv.field_sources["photo"] == [1]
    assert set(pv.field_sources) == set(units(m))                                    # every unit says where it came from


def test_merge_choices_pick_a_scalar_from_a_chosen_card_and_conflicts_are_listed():
    a = c("Ann", "Lee", company="Acme")
    b = c("Annie", "Lee", company="Other")
    pv = dupes.merge_cards([a, b])
    assert pv.card.get("company") == "Acme"
    assert pv.conflicts["s:company"] == [(0, "Acme"), (1, "Other")] and "s:family" not in pv.conflicts
    assert pv.per_field_choice["s:company"] == 0 and pv.per_field_choice["s:given"] == 0
    pv2 = dupes.merge_cards([a, b], choices={"s:company": 1, "s:given": 1})
    assert (pv2.card.get("company"), pv2.card.get("given")) == ("Other", "Annie")
    assert pv2.per_field_choice["s:company"] == 1 and pv2.field_sources["s:company"] == [1]
    with pytest.raises(ValueError):
        dupes.merge_cards([a, b], choices={"s:company": 5})


def test_merge_keeps_all_notes_and_the_preview_is_what_gets_written():
    a = c("Ann", "Lee", notes="one")
    b = c("Ann", "Lee", notes="two")
    b2 = c("Ann", "Lee", notes="one")
    assert dupes.merge_cards([a, b, b2]).card.get("notes") == "one\n\ntwo"
    assert dupes.merge_cards([a, b], choices={"s:notes": 1}).card.get("notes") == "two"
    pv = dupes.merge_cards([a, b])
    assert pv.card is not a and a.get("notes") == "one"                              # inputs are never changed
    assert dupes.merge_cards([a]).card.dumps() == a.dumps()


def test_merge_adopts_a_label_the_first_copy_lacks():
    a = Card(fields=[Field("email", "x@x.example")])
    b = Card(fields=[Field("email", "x@x.example", "work", primary=True)])
    f = dupes.merge_cards([a, b]).card.values("email")[0]
    assert f.label == "work" and f.primary
    with pytest.raises(ValueError):
        dupes.merge_cards([])


# ---- shared lines are not people (the owner, 2026-10-11) ---------------------------

OFFICE = "020 7946 0018"          # a London landline
MOBILE = "07911 123456"           # a mobile


def test_people_who_share_an_office_landline_are_not_duplicates():
    items = [(1, c("Alaa", "Arawi", phone=[OFFICE, "07911 111111"])),
             (2, c("Stephen", "Tagg", phone=[OFFICE, "07911 222222"])),
             (3, c("Pradeep", "Vishvanathan", phone=[OFFICE]))]
    assert dupes.scan_duplicates(items, set()) == []


def test_the_same_person_twice_on_a_landline_is_a_suggestion_never_safe():
    items = [(1, c("Ann", "Lee", phone=[OFFICE])), (2, c("Ann", "Lee", phone=[OFFICE]))]
    (g,) = dupes.scan_duplicates(items, set())
    assert not g.safe and "same landline and name" in g.reasons


def test_different_names_on_one_mobile_or_one_email_are_not_duplicates_either():
    items = [(1, c("Ann", "Lee", phone=[MOBILE], email=["family@x.example"])),
             (2, c("Joe", "Lee", phone=[MOBILE], email=["family@x.example"]))]
    assert dupes.scan_duplicates(items, set()) == []


def test_the_same_mobile_with_the_same_person_in_two_accounts_is_still_safe():
    items = [(1, c("Ann", "Lee", phone=[MOBILE])), (2, c("A.", "Lee", phone=["+44 7911 123456"]))]
    (g,) = dupes.scan_duplicates(items, set())
    assert g.safe and g.reasons == ("same phone",)


def test_a_number_marked_shared_never_makes_a_match():
    items = [(1, c("Ann", "Lee", phone=[MOBILE])), (2, c("Ann", "Lee", phone=[MOBILE]))]
    assert dupes.scan_duplicates(items, set(), shared={"phone:+447911123456"})[0].reasons == ("same name",)
    mail = [(1, c("Ann", "Lee", email=["a@x.example"])), (2, c("Ann", "Lee", email=["A@X.example"]))]
    assert dupes.scan_duplicates(mail, set(), shared={"email:a@x.example"})[0].reasons == ("same name",)


def test_shared_keys_names_what_two_cards_both_hold():
    a = c("Ann", "Lee", phone=[OFFICE, MOBILE], email=["A@x.example"])
    b = c("Joe", "Lee", phone=["+44 20 7946 0018"], email=["j@x.example"])
    assert dupes.shared_keys(a, b) == {"phone:+442079460018"}


def test_names_compatible_in_the_cases_that_matter():
    n = dupes.names_compatible
    assert n(c("Ann", "Lee"), c("A.", "Lee")) and n(c("Manoj", "Patel"), c("Manoj Bhanubhai", "Patel"))
    assert n(c("Johar", "Bin Yusof"), c("Johar", "Yusof")) and n(Card(), c("Ann", "Lee"))
    assert not n(c("Alaa", "Arawi"), c("Stephen", "Tagg")) and not n(c("Ann", "Lee"), c("Joe", "Lee"))
    assert not n(c(company="Progressive Impact"), c("Johar", "Bin Yusof"))
    assert n(c(company="Acme Ltd"), c(company="Acme"))
