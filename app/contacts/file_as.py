r"""How a contact is filed in an address book that has a "File as" field.

Layer: L1 - plain text from a contact's names; no I/O.

2026-10-25, the owner: "the file as in contact does not come up in outlook, by default it
should be first space last name, not sure others have this function but needs to be there and
make it configurable". Outlook (Microsoft Graph's `fileAs`) and Google Contacts (`fileAses`)
both have the field; Leasha wrote neither, so a contact written from here was filed under
nothing. CardDAV has no standard field for it, so a CardDAV address book is not given one.

The formats are Outlook's own five, named after what they produce, and "First Last" is the
default. A contact without the names a format asks for is filed by what it does have - the
company, then its first email address - so no contact is ever filed under nothing.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

__all__ = ["FORMATS", "DEFAULT", "file_as"]

#: `(key, what it produces, as the setting lists it)`. Keys are what the settings keep.
FORMATS: tuple[tuple[str, str], ...] = (
    ("first_last", "First Last"),
    ("last_first", "Last, First"),
    ("company", "Company"),
    ("last_first_company", "Last, First (Company)"),
    ("company_last_first", "Company (Last, First)"),
)

#: The owner's choice of default.
DEFAULT = "first_last"


def file_as(scalars: Mapping[str, Any], fmt: str = DEFAULT,
            emails: Sequence[str] = ()) -> str:
    """The "File as" text for a contact's `scalars` (`given`, `family`, `company`...).

    `fmt` is a `FORMATS` key; an unknown one is read as `DEFAULT`. Never raises; "" only
    for a contact with no name, no company and no email."""
    def part(key: str) -> str:
        return " ".join(str(scalars.get(key) or "").split())

    first, last, company = part("given"), part("family"), part("company")
    first_last = " ".join(p for p in (first, last) if p)
    last_first = ", ".join(p for p in (last, first) if p)
    keys = {key for key, _label in FORMATS}
    fmt = fmt if fmt in keys else DEFAULT
    if fmt == "first_last":
        text = first_last
    elif fmt == "last_first":
        text = last_first
    elif fmt == "company":
        text = company
    elif fmt == "last_first_company":
        text = f"{last_first} ({company})" if last_first and company else last_first
    else:                                           # company_last_first
        text = f"{company} ({last_first})" if last_first and company else company
    if text:
        return text
    for fallback in (first_last, company):
        if fallback:
            return fallback
    for email in emails:
        if str(email or "").strip():
            return str(email).strip()
    return ""
