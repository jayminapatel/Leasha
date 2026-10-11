"""Cards the contact tests share.

Layer: L7

Order 1k. Plain builders, no fixtures, so each test file stays readable on its
own. Labels are lower case because a vCard keeps them that way.
"""

from __future__ import annotations

import base64

from app.contacts.card import Card, Field, Photo

# A real 1x1 PNG: reportlab and Pillow both open it.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def addr(street="", city="", region="", postcode="", country="", label="home") -> Field:
    data = {k: v for k, v in (("street", street), ("city", city), ("region", region),
                              ("postcode", postcode), ("country", country)) if v}
    text = ", ".join(data.values())
    return Field("address", text, label, data=data)


def person(given="Ann", family="Lee", *, email="", phone="", company="", title="") -> Card:
    c = Card()
    if given:
        c.scalars["given"] = given
    if family:
        c.scalars["family"] = family
    if company:
        c.scalars["company"] = company
    if title:
        c.scalars["title"] = title
    if email:
        c.fields.append(Field("email", email, "work"))
    if phone:
        c.fields.append(Field("phone", phone, "mobile"))
    return c


def rich_card() -> Card:
    """Every scalar and every kind the plain layout and vCard hold."""
    c = Card(scalars={
        "prefix": "Dr", "given": "Ann", "additional": "Marie", "family": "O'Neil-Smith",
        "suffix": "PhD", "nickname": "Annie", "company": "Acme; Widgets, Ltd",
        "department": "R&D", "title": "Chief Engineer",
        "notes": "Met at the fair.\nLikes tea, not coffee; allergic to cats.",
    })
    c.fields = [
        Field("email", "ann@acme.example", "work", primary=True),
        Field("email", "ann.home@mail.example", "home"),
        Field("phone", "+44 7700 900123", "mobile", primary=True),
        Field("phone", "020 7946 0018", "work"),
        Field("phone", "+44 20 7946 0999", "work fax"),
        addr("1 High St\nFlat 2", "London", "Greater London", "N1 1AA", "United Kingdom", "home"),
        addr("5 Park Rd", "Leeds", "", "LS1 4AB", "UK", "work"),
        Field("url", "https://acme.example/ann", "work"),
        Field("date", "1980-05-17", "birthday"),
        Field("date", "2010-06-01", "anniversary"),
        Field("date", "2001-02-03", "met"),
        Field("related", "Bob Lee", "spouse"),
        Field("social", "@annlee", "twitter"),
        Field("im", "ann.skype", "skype"),
        Field("custom", "Rex", "pet"),
    ]
    c.groups = ["Family", "Work friends"]
    c.favourite = True
    c.photo = Photo("image/png", TINY_PNG)
    return c


def google_card() -> Card:
    """What Google's CSV holds: no photo, social, im, nor the primary flag."""
    c = rich_card()
    c.photo = None
    c.fields = [f for f in c.fields if f.kind not in ("social", "im")]
    for f in c.fields:
        f.primary = False
    return c


def outlook_card() -> Card:
    """What Outlook's CSV holds: unlabelled email, url and im; slot-named phones."""
    c = Card(scalars={"prefix": "Dr", "given": "Ann", "additional": "Marie", "family": "Lee",
                      "suffix": "PhD", "nickname": "Annie", "company": "Acme", "department": "R&D",
                      "title": "Engineer", "notes": "Line one\nLine two"})
    c.fields = [
        Field("email", "ann@acme.example"), Field("email", "ann2@acme.example"),
        Field("phone", "+44 7700 900123", "mobile"), Field("phone", "+44 20 7946 0018", "work"),
        Field("phone", "+44 1632 960001", "home"), Field("phone", "+44 20 7946 0999", "work fax"),
        Field("phone", "+44 1632 960002", "other"),
        addr("5 Park Rd", "Leeds", "West Yorkshire", "LS1 4AB", "UK", "work"),
        addr("1 High St", "London", "Greater London", "N1 1AA", "UK", "home"),
        Field("url", "https://acme.example"),
        Field("date", "1980-05-17", "birthday"), Field("date", "2010-06-01", "anniversary"),
        Field("related", "Bob Lee", "spouse"),
        Field("im", "ann.skype"),
    ]
    c.groups = ["Family", "Work friends"]
    return c
