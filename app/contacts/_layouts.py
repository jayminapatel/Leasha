"""The column layouts of the contact CSV files, shared by the importers and exporters.

Layer: L7

Order 1k 6i, 6j. One table, so what `exporters` writes is exactly what
`importers` reads back. The Google and Outlook layouts are those programs' own
(Google Contacts "Google CSV", Outlook "Comma Separated Values"); the plain
layout is Leasha's: every field, as many of each kind as the book needs.
"""

from __future__ import annotations

import re

from app.contacts.card import Capabilities, KINDS, SCALARS

__all__ = [
    "MULTI", "STANDARD_LABELS", "label_in", "label_out", "PLAIN_SCALARS", "PLAIN_KINDS", "PLAIN_HEADER_RE",
    "OUTLOOK_PHONE_IN", "OUTLOOK_PHONE_OUT", "OUTLOOK_ADDR", "LAYOUT_CAPS", "split_multi", "NO_DATE_RE",
]

#: Google joins several values in one cell with this.
MULTI = " ::: "

#: Labels the programs offer as standard choices; kept in lower case inside Leasha
#: and capitalised for the programs. Any other label is the person's own and is
#: written and read exactly as typed.
STANDARD_LABELS = frozenset({
    "home", "work", "mobile", "main", "other", "pager", "fax", "work fax", "home fax", "car", "isdn", "video",
    "school", "assistant", "callback", "radio", "telex", "tty", "birthday", "anniversary", "homepage", "blog",
    "profile", "ftp", "spouse", "child", "mother", "father", "parent", "brother", "sister", "friend", "relative",
    "manager", "partner", "domestic partner", "referred by", "work mobile", "home mobile", "google voice",
})


def label_in(text: str) -> str:
    """A label as Leasha keeps it: a leading '* ' (Google's mark for a built-in
    label) removed, the standard ones in lower case, the person's own as typed."""
    t = (text or "").strip()
    if t.startswith("* "):
        t = t[2:].strip()
    return t.lower() if t.lower() in STANDARD_LABELS else t


def label_out(label: str) -> str:
    t = (label or "").strip()
    return " ".join(w.capitalize() for w in t.split(" ")) if t.lower() == t and t in STANDARD_LABELS else t


def split_multi(text: str) -> list[str]:
    """The parts of a Google multi-value cell, empty parts kept so a label and its
    value stay lined up; no parts at all for an empty cell."""
    t = (text or "").strip()
    return [p.strip() for p in t.split(MULTI.strip())] if t else []


#: Plain layout.
PLAIN_SCALARS = [("Prefix", "prefix"), ("First Name", "given"), ("Middle Name", "additional"),
                 ("Last Name", "family"), ("Suffix", "suffix"), ("Nickname", "nickname"),
                 ("Company", "company"), ("Department", "department"), ("Job Title", "title"), ("Notes", "notes")]
assert {n for _, n in PLAIN_SCALARS} == set(SCALARS)

PLAIN_KINDS = [("email", "Email"), ("phone", "Phone"), ("address", "Address"), ("url", "Website"),
               ("date", "Date"), ("related", "Related"), ("social", "Social"), ("im", "IM"), ("custom", "Custom")]
assert {k for k, _ in PLAIN_KINDS} == set(KINDS)

PLAIN_HEADER_RE = re.compile(
    r"^(Email|Phone|Address|Website|Date|Related|Social|IM|Custom) (\d+) "
    r"(Label|Value|Primary|Street|City|Region|Postcode|Country)$")

#: Outlook. Slot -> label on the way in; label -> slots to try, in order, on the way out.
OUTLOOK_PHONE_IN = {
    "home phone": "home", "home phone 2": "home", "business phone": "work", "business phone 2": "work",
    "mobile phone": "mobile", "car phone": "car", "other phone": "other", "primary phone": "main",
    "pager": "pager", "business fax": "work fax", "home fax": "home fax", "other fax": "fax",
    "company main phone": "main", "callback": "callback", "radio phone": "radio", "telex": "telex",
    "tty/tdd phone": "tty", "assistant's phone": "assistant", "isdn": "isdn",
}
OUTLOOK_PHONE_OUT = {
    "mobile": ["Mobile Phone", "Other Phone"],
    "home": ["Home Phone", "Home Phone 2", "Other Phone"],
    "work": ["Business Phone", "Business Phone 2", "Other Phone"],
    "work fax": ["Business Fax", "Other Fax"],
    "home fax": ["Home Fax", "Other Fax"],
    "fax": ["Other Fax"],
    "car": ["Car Phone", "Other Phone"],
    "pager": ["Pager", "Other Phone"],
    "main": ["Primary Phone", "Company Main Phone", "Other Phone"],
}
#: Outlook address block prefix -> label.
OUTLOOK_ADDR = {"Business": "work", "Home": "home", "Other": "other"}

#: Outlook writes an empty date like this.
NO_DATE_RE = re.compile(r"^0+[/.-]0+[/.-]0+$|^(0{1,4}[/.-]){2}0{1,4}$|^4501-01-01$")

_ALL = frozenset(SCALARS)

#: What each layout can hold, as `card.Capabilities`, so a test (or the Export
#: dialog's "what will not be saved" line) compares `card.units(c, LAYOUT_CAPS[x])`.
#: Not expressible there, and documented in `exporters`: Google and Outlook keep no
#: "main" flag; Outlook keeps no labels on email, web pages or the IM address, at
#: most the slots it has (two email addresses past the first, one web page past
#: the second...), and vCard keeps labels in lower case.
LAYOUT_CAPS = {
    "vcard": Capabilities(),
    "plain": Capabilities(),
    "xlsx": Capabilities(photo=False),
    "google": Capabilities(kinds=frozenset({"email", "phone", "address", "url", "date", "related", "custom"}),
                           photo=False),
    "outlook": Capabilities(kinds=frozenset({"email", "phone", "address", "url", "date", "related", "im"}),
                            favourite=False, photo=False, date_labels=frozenset({"birthday", "anniversary"})),
}
