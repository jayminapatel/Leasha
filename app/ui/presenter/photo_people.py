r"""Photos and Contacts, linked both ways - the words and the switches, no Qt, no I/O.

Layer: L5 (presenter)

IA-6 (UX review 2026-10-14). A contact could be linked to a person named in Photos only
from the contact's side (`contacts/index_panel.py`), and nothing led back. Now:

* **From Photos** - a person in the People list, or a group on the naming page - offers
  "Open contact" when it is linked to one, and "Link to contact…" to pick one from the
  contact book. The link is the same `contact_pile_links` row the contact's panel writes
  (`app.contacts.people.link_to_pile`): chosen by hand, so accepted at once.
* **From a contact** - "See all photos" opens Photos narrowed to the linked person
  (`who:`), and "See mail from them" opens Mail narrowed to the contact's address
  (`from:`). The switches are the ones every box already reads.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

__all__ = ["OPEN_CONTACT", "LINK_CONTACT", "person_menu", "linked_contact", "choice_labels",
           "photos_box", "mail_box", "fold"]

#: The two menu lines, and the actions `SideList.menu_chosen` names them by.
OPEN_CONTACT = ("Open contact", "contact", "open_contact")
LINK_CONTACT = ("Link to contact…", "link", "link_contact")


def fold(name: Any) -> str:
    """A person's name as the links are matched by: case and spacing folded."""
    return " ".join(str(name or "").casefold().split())


def linked_contact(links: Optional[dict], pile_id: Optional[int]) -> Optional[int]:
    """The contact to open for a person in Photos - the first linked, by name - or None.
    `links` is `people.contacts_of_piles`: pile id -> `[(contact id, name), ...]`."""
    if not links or pile_id is None:
        return None
    found = links.get(int(pile_id)) or ()
    return int(found[0][0]) if found else None


def person_menu(linked: bool) -> tuple:
    """A person's right-click lines in the People list: Open contact when there is one to
    open, then Link to contact… (to link, or to link another)."""
    return (OPEN_CONTACT, LINK_CONTACT) if linked else (LINK_CONTACT,)


def choice_labels(choices: Sequence[tuple[int, str, str]]) -> list[str]:
    """One line per contact for the picker, in the order given: the name, and the email
    after it when two contacts share a name (or a number, when they share that too).
    An unnamed contact is its email, or "(no name)"."""
    names = [str(name).strip() or str(email).strip() or "(no name)" for _cid, name, email in choices]
    counts: dict[str, int] = {}
    for name in names:
        counts[name.casefold()] = counts.get(name.casefold(), 0) + 1
    labels, seen = [], set()
    for (_cid, _name, email), name in zip(choices, names, strict=True):
        label = name
        if counts[name.casefold()] > 1 and email and email != name:
            label = f"{name} <{email}>"
        number = 2
        while label.casefold() in seen:
            label = f"{name} ({number})"
            number += 1
        seen.add(label.casefold())
        labels.append(label)
    return labels


def photos_box(person: str) -> str:
    """The Photos box for this person's photos: `who:Name`, quoted when it has a space."""
    from app.ui.presenter.photos import toggle_in_box

    return toggle_in_box("", "who", str(person).strip()) if str(person).strip() else ""


def mail_box(address: str) -> str:
    """The Mail box for the mail from this address: `from:address`."""
    address = str(address or "").strip()
    return f"from:{address}" if address else ""
