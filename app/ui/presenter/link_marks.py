"""The mark on each "Kept in" account of a contact: synced, waiting to be written, deleted there, refused. Qt-free.

Layer: L5 presenter. Drawn by `app.ui.contacts.reader.ContactReader` and `app.ui.contacts.editor.ContactEditor`.

Review 2026-10-14, CS-9: the reader listed "Kept in" as plain names and the editor's chips said only on or off, so
nobody could see that a contact was still waiting to be written to an account, had been deleted there, or had been
refused by it. The link state comes from `ContactHub.kept_in_states` and the refusals from `AccountStore.failures`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

__all__ = ["LinkMark", "link_mark", "link_marks"]


@dataclass(frozen=True)
class LinkMark:
    account_id: int
    name: str
    key: str           # synced | pending_create | deleted_remote | refused
    glyph: str
    tone: str          # a theme token
    words: str         # a few words beside the account's name
    tip: str           # the sentence on hover


_MARKS = {
    "synced": ("circle-check", "text_dim", "synced", "Synced: {name} holds this contact as Leasha does"),
    "pending_create": ("clock", "warning", "waiting to be written",
                       "Waiting to be written to {name} - the next sync (or Sync now) puts it there"),
    "deleted_remote": ("trash-2", "warning", "deleted there",
                       "Deleted at {name}. It is kept in Leasha; take {name} out of Kept in to stop keeping it there"),
    "refused": ("circle-alert", "danger", "refused",
                "{name} refused this contact: {why}. It stays waiting, and the next sync tries it again"),
}


def link_mark(account_id: int, name: str, state: str, refused: str = "") -> LinkMark:
    """One account's mark. A refusal outranks the link state: the account would not take it."""
    key = "refused" if refused else (state if state in _MARKS else "synced")
    glyph, tone, words, tip = _MARKS[key]
    return LinkMark(int(account_id), name, key, glyph, tone, words,
                    tip.format(name=name or "the account", why=" ".join(str(refused).split()).rstrip(".")))


def link_marks(states: Mapping[int, str], names: Mapping[int, str],
               refused: Mapping[int, str] | None = None) -> list[LinkMark]:
    """The marks of every account a contact is kept in, in the order of `states`, leaving out unnamed accounts."""
    refused = refused or {}
    return [link_mark(aid, names[aid], st, refused.get(aid, "")) for aid, st in states.items() if names.get(aid)]
