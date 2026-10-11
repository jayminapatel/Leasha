"""Order 1k 6o, 6p - clean-up previews and finders.

Layer: L7

Each tool returns plain `Change` objects and writes nothing: the hub applies the
accepted ones as one journal batch.
"""

from __future__ import annotations

import pytest

from app.contacts import cleanup as cl
from app.contacts.card import Card, Field


def card(given="", family="", **kw) -> Card:
    return Card(scalars={k: v for k, v in dict(given=given, family=family, **kw).items() if v})


def with_phones(*numbers: str) -> Card:
    c = card("A", "B")
    c.fields = [Field("phone", n, "mobile") for n in numbers]
    return c


# ---- phone numbers -----------------------------------------------------------

def test_phones_to_international_and_national():
    items = [(1, with_phones("07700 900123", "020-7946-0018")),
             (2, with_phones("+44 7700 900124")),                    # already international
             (3, with_phones("+1 202-555-0143", "garbage"))]
    ch = cl.normalise_phones(items, "international", "GB")
    assert [(c.id, c.field, c.before, c.after) for c in ch] == [
        (1, "phone", "07700 900123", "+44 7700 900123"),
        (1, "phone", "020-7946-0018", "+44 20 7946 0018")]          # +44 and +1 numbers already in shape: no change
    nat = cl.normalise_phones(items, "national", "GB")
    assert [(c.id, c.before, c.after) for c in nat] == [
        (1, "020-7946-0018", "020 7946 0018"), (2, "+44 7700 900124", "07700 900124")]
    # a US number is never cut to a "national" form that a UK reader could not dial
    assert all(c.id != 3 for c in nat)


def test_phone_changes_are_idempotent_and_do_not_touch_the_cards():
    c1 = with_phones("07700 900123")
    items = [(1, c1)]
    ch = cl.normalise_phones(items, "international", "GB")
    assert c1.values("phone")[0].value == "07700 900123"
    applied = with_phones(*[c.after for c in ch])
    assert cl.normalise_phones([(1, applied)], "international", "GB") == []


def test_phone_style_is_checked():
    with pytest.raises(ValueError):
        cl.normalise_phones([], "dotted", "GB")


# ---- name case ---------------------------------------------------------------

def _names(changes):
    return {(c.id, c.field): (c.before, c.after) for c in changes}


def test_name_case_fixes_only_all_lower_and_all_upper():
    items = [(1, card("ann", "LEE")), (2, card("Ann", "Lee")), (3, card("jean-luc", "o'brien")),
             (4, card("ANN MARIE", "smith-jones"))]
    got = _names(cl.fix_name_case(items))
    assert got == {(1, "s:given"): ("ann", "Ann"), (1, "s:family"): ("LEE", "Lee"),
                   (3, "s:given"): ("jean-luc", "Jean-Luc"), (3, "s:family"): ("o'brien", "O'Brien"),
                   (4, "s:given"): ("ANN MARIE", "Ann Marie"), (4, "s:family"): ("smith-jones", "Smith-Jones")}


def test_name_case_leaves_particles_and_mixed_case_alone():
    items = [(1, card("jan", "van der berg")), (2, card("Ronald", "McDonald")), (3, card("Danny", "DeVito")),
             (4, card("Sean", "O'Neil")), (5, card("ibn", "BIN AL HASSAN")), (6, card("ludwig", "von beethoven")),
             (7, card("anna", "da silva")), (8, card("Marie", "de la Cruz"))]
    got = _names(cl.fix_name_case(items))
    assert got[(1, "s:given")] == ("jan", "Jan") and got[(1, "s:family")] == ("van der berg", "van der Berg")
    assert not any(k[0] in (2, 3, 4, 8) for k in got)                         # already mixed case: untouched
    assert got[(5, "s:family")] == ("BIN AL HASSAN", "bin al Hassan")
    assert got[(6, "s:family")][1] == "von Beethoven" and got[(7, "s:family")][1] == "da Silva"


def test_name_case_force_recases_everything():
    items = [(1, card("jan", "van der berg")), (2, card("Ronald", "McDonald")), (3, card("A", "de la CRUZ"))]
    got = _names(cl.fix_name_case(items, force=True))
    assert got[(1, "s:family")][1] == "Van Der Berg"
    assert got[(2, "s:family")][1] == "Mcdonald"                              # force means force: the person chose it
    assert got[(3, "s:family")][1] == "De La Cruz"
    assert (3, "s:given") not in got                                          # "A" is already right


def test_name_case_leaves_other_fields_alone():
    c = card("ann", "lee", company="acme ltd", title="buyer", nickname="annie")
    assert {k[1] for k in _names(cl.fix_name_case([(1, c)]))} == {"s:given", "s:family"}


# ---- company from the email domain -------------------------------------------

@pytest.mark.parametrize("email,expected", [
    ("jane@acme-widgets.com", "Acme Widgets"), ("jane@mail.acme.com", "Acme"),
    ("jane@acme.co.uk", "Acme"), ("jane@sub.dept.bigcorp.com.au", "Bigcorp"),
    ("JANE@Example.ORG", "Example"), ("jane@acme.io", "Acme"),
    ("jane@gmail.com", None), ("jane@googlemail.com", None), ("jane@outlook.com", None),
    ("jane@hotmail.co.uk", None), ("jane@live.com", None), ("jane@msn.com", None),
    ("jane@yahoo.com", None), ("jane@yahoo.co.uk", None), ("jane@ymail.com", None),
    ("jane@icloud.com", None), ("jane@me.com", None), ("jane@mac.com", None), ("jane@aol.com", None),
    ("jane@proton.me", None), ("jane@protonmail.com", None), ("jane@gmx.de", None), ("jane@mail.com", None),
    ("jane@zoho.com", None), ("jane@btinternet.com", None), ("jane@sky.com", None),
    ("jane@virginmedia.com", None), ("jane@talktalk.net", None),
    ("not an email", None), ("", None), ("jane@", None), ("@acme.com", "Acme"),
])
def test_company_guess_from_email(email, expected):
    assert cl.company_guess_from_email(email) == expected


def test_free_mail_list_is_fixed_and_contains_the_named_providers():
    named = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "yahoo.com",
             "ymail.com", "icloud.com", "me.com", "mac.com", "aol.com", "protonmail.com", "gmx.com", "mail.com",
             "zoho.com", "btinternet.com", "sky.com", "virginmedia.com", "talktalk.net"}
    assert named <= set(cl.FREE_MAIL_DOMAINS) and isinstance(cl.FREE_MAIL_DOMAINS, frozenset)


def test_company_from_domain_fills_only_blanks_and_prefers_work_email():
    a = card("A", "A")
    a.fields = [Field("email", "a@gmail.com", "home"), Field("email", "a@acme.com", "work")]
    b = card("B", "B", company="Already")
    b.fields = [Field("email", "b@acme.com")]
    c = card("C", "C")
    c.fields = [Field("email", "c@gmail.com")]
    d = card("D", "D")
    d.fields = [Field("email", "d@first-co.com", "home"), Field("email", "d@second-co.com", "work")]
    ch = cl.company_from_domain([(1, a), (2, b), (3, c), (4, d)])
    assert [(x.id, x.field, x.before, x.after) for x in ch] == [
        (1, "s:company", "", "Acme"), (4, "s:company", "", "Second Co")]


# ---- finders -----------------------------------------------------------------

def test_finders():
    nothing = Card()
    no_name = Card(fields=[Field("email", "x@y.example")])
    only_name = card("Just", "AName")
    only_company = Card(scalars={"company": "Acme"})
    full = card("Ann", "Lee")
    full.fields = [Field("email", "a@x.example")]
    blank_fields = card("Ann", "Lee")
    blank_fields.fields = [Field("email", " ")]
    named_group = card("Gr", "Oup")
    named_group.groups = ["Family"]
    items = [(1, nothing), (2, no_name), (3, only_name), (4, only_company), (5, full), (6, blank_fields),
             (7, named_group)]
    assert [f.id for f in cl.no_name(items)] == [1, 2]
    assert [f.id for f in cl.no_email_or_phone(items)] == [1, 3, 4, 6, 7]
    assert [f.id for f in cl.only_a_name(items)] == [3, 6, 7]
    assert all(f.reason for f in cl.no_name(items))


def test_exact_field_copies_within_one_contact():
    a = card("A", "A")
    a.fields = [Field("email", "x@x.example", ""), Field("email", " X@X.example", "work"),
                Field("phone", "07700 900123"), Field("phone", "+44 7700 900123"), Field("phone", "020 7946 0018")]
    b = card("B", "B")
    b.fields = [Field("email", "x@x.example")]
    found = cl.exact_field_copies([(1, a), (2, b)])
    assert [(f.id, f.kind) for f in found] == [(1, "email"), (1, "phone")]
    assert found[0].count == 2 and found[0].values == ["x@x.example", " X@X.example"]
    fixed = cl.remove_field_copies(a)
    assert [f.value for f in fixed.values("email")] == ["x@x.example"]
    assert fixed.values("email")[0].label == "work"                           # the copy's label is not lost
    assert len(fixed.values("phone")) == 2 and len(a.fields) == 5             # input untouched
    assert cl.exact_field_copies([(1, fixed)]) == []
