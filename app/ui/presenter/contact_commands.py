"""The `/` menu of the Contacts box: the switches `app.contacts.slash` reads, as menu rows. Qt-free.

Layer: L5. Built from the same `Command` rows as every other box, so the popup, its hint line and its
value menu are the ones the other pages have.
"""

from __future__ import annotations

from app.contacts.slash import HAS_VALUES
from app.search.commands import Command

__all__ = ["CONTACT_COMMANDS", "contact_matching"]


def _c(name: str, summary: str, example: str, hint: str, icon: str, values: tuple[str, ...] = ()) -> Command:
    return Command(name=name, aliases=(), summary=summary, example=example, value_hint=hint, icon=icon,
                   values=values)


CONTACT_COMMANDS: tuple[Command, ...] = (
    _c("company", "Only people at this company", "/company acme", "part of a company name", "▣"),
    _c("title", "Only people with this job title", "/title director", "part of a job title", "◈"),
    _c("email", "Only people with this email address", "/email gmail", "part of an address", "@"),
    _c("phone", "Only people with this phone number", "/phone 0161", "part of a number", "☎"),
    _c("name", "Only people with this in their name", "/name smith", "part of a name", "◉"),
    _c("tag", "Only people with this tag", "/tag supplier", "a tag, exactly as named", "#"),
    _c("group", "Only people in this group", "/group family", "a group, exactly as named", "☷"),
    _c("has", "Only people who have a ...", "/has phone", "email, phone, company or title", "✓", HAS_VALUES),
    _c("quiet", "Only people not contacted for this many days", "/quiet 90", "a number of days", "◷",
       ("30", "90", "180", "365")),
    _c("favourite", "Only favourites", "/favourite", "no value", "★"),
)


def contact_matching(prefix: str) -> list[Command]:
    cleaned = prefix.strip().lower().lstrip("/")
    return [c for c in CONTACT_COMMANDS if not cleaned or c.name.startswith(cleaned)]
