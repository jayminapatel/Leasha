r"""Photos' people and the contact book, linked from the photo side (IA-6, 2026-10-14).

Layer: L5

The words and switches are `presenter.photo_people`; this is the Qt half:

- `read_links` - which person in Photos is linked to which contacts, and every named
  person's id - for the Photos page's People list (read with the library, on its worker).
- `pick_and_link` - "Link to contact…": the contact book read on a worker, one picker,
  the link written on a worker (`people.link_to_pile`: chosen by hand, so accepted).
- `wire` - the People list's right-click lines on the Photos page.

Every read and write here is on a worker (rule 5); the picker is the one thing on the
window's thread, and it waits for the person, not for the disk.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtWidgets import QInputDialog, QMessageBox, QWidget

from app.core.logging import logger
from app.ui.presenter.photo_people import choice_labels, fold, linked_contact

__all__ = ["read_links", "contact_book", "write_link", "pick_and_link", "wire", "person_action",
           "PICK_TITLE"]

_log = logger.bind(component="ui.photo_contacts")

#: The picker's title.
PICK_TITLE = "Link to contact"


def read_links(store: Any) -> dict:
    """`{"contacts": pile id -> [(contact id, name)], "piles": folded name -> pile id}`.
    **Worker.** Empty, never an error, before the contact tables exist."""
    from app.contacts import people

    try:
        return {"contacts": people.contacts_of_piles(store.conn),
                "piles": people.named_piles(store.conn)}
    except Exception as exc:                            # noqa: BLE001 - a link is an extra
        _log.debug("could not read the photo-contact links: {}", exc)
        return {"contacts": {}, "piles": {}}


def contact_book(store: Any) -> list:
    """Every contact for the picker, `(id, name, email)`. **Worker.**"""
    from app.contacts import people

    return people.contact_choices(store.conn)


def write_link(store: Any, contact_id: int, pile_id: int) -> int:
    """Link `contact_id` to the person `pile_id` - the row the contact's own panel writes.
    **Worker.**"""
    from app.contacts import people
    from app.contacts.hub import ContactHub

    people.link_to_pile(ContactHub(store), int(contact_id), int(pile_id))
    return int(contact_id)


def _start(owner: Any, worker: Any, finished: Callable[[Any], None],
           failed: Callable[[Any], None]) -> None:
    from PySide6.QtCore import QThreadPool

    from app.ui.later import when_done
    from app.ui.workers import run

    when_done(owner, worker, finished=finished, failed=failed)
    run(QThreadPool.globalInstance(), worker)


def _failed(parent: QWidget, error: Any) -> None:
    QMessageBox.warning(parent, PICK_TITLE, f"Could not link them: {error}")


def pick_and_link(parent: QWidget, store: Any, pile_id: int, person: str,
                  done: Optional[Callable[[int], None]] = None) -> None:
    """"Link to contact…" for the person `pile_id`: read the contact book on a worker,
    ask which contact this is, write the link on a worker, then `done(contact_id)`."""
    from app.ui.workers import CallableWorker

    def landed(choices: Any) -> None:
        choices = list(choices or [])
        if not choices:
            QMessageBox.information(parent, PICK_TITLE,
                                    "There are no contacts yet. Add one on the Contacts page first.")
            return
        labels = choice_labels(choices)
        picked, ok = QInputDialog.getItem(parent, PICK_TITLE,
                                          f"Which contact is {person}?", labels, 0, True)
        if not ok or picked not in labels:
            return
        contact_id = choices[labels.index(picked)][0]
        _start(parent, CallableWorker(write_link, store, contact_id, pile_id,
                                      component="ui.photo_contacts"),
               finished=lambda cid: done(int(cid)) if done is not None else None,
               failed=lambda error: _failed(parent, error))

    _start(parent, CallableWorker(contact_book, store, component="ui.photo_contacts"),
           finished=landed, failed=lambda error: _failed(parent, error))


# -- the Photos page's People list ------------------------------------------------------------

def person_action(view: Any, entry: Any, action: str) -> None:
    """A line chosen on a person in the People list: open their contact, or link one."""
    value = getattr(entry, "value", None)
    if not isinstance(value, tuple) or len(value) != 2 or value[0] != "who":
        return
    name = str(value[1])
    links = getattr(view.sidebar, "links", None) or {}
    pile_id = (links.get("piles") or {}).get(fold(name))
    if action == "open_contact":
        contact_id = linked_contact(links.get("contacts"), pile_id)
        if contact_id is not None:
            view.contact_requested.emit(contact_id)
    elif action == "link_contact" and pile_id is not None:
        pick_and_link(view, view._store, pile_id, name, done=lambda _cid: view.refresh())


def wire(view: Any) -> None:
    """The People list's right-click lines on the Photos page `view`."""
    view.sidebar.menu_chosen.connect(lambda entry, action: person_action(view, entry, action))
