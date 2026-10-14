"""Linking a contact to a named person on the photo side.

Layer: L7

Order 1k 2c. The Photo Tagger names people (`piles`); a contact is a person with
an address. A link says they are the same person, so the contact can borrow a
photo from that person's faces and the person's photos can be listed on the
contact (6f).

**Suggested, then accepted by the person (D9).** A name that matches is only a
suggestion; nothing is linked until the person says so, and a "no" is remembered
(`declined`) so it is not suggested again. Never merged on a name alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

__all__ = ["PileLink", "suggest_pile_links", "pile_link", "accept_pile_link", "decline_pile_link",
           "link_to_pile", "unlink", "pending_suggestions", "faces_of_contact",
           "contacts_of_piles", "named_piles", "contact_choices"]


@dataclass
class PileLink:
    contact_id: int
    pile_id: int
    pile_name: str
    state: str                 # suggested | accepted | declined


def _fold(text: Optional[str]) -> str:
    return " ".join((text or "").casefold().split())


def suggest_pile_links(hub: Any) -> int:
    """Suggest a named person for every contact that has none and whose name matches. Returns how many."""
    conn = hub.conn
    named = {}
    for r in conn.execute("SELECT id, name FROM piles WHERE name IS NOT NULL"):
        named.setdefault(_fold(r["name"]), r["id"])
    if not named:
        return 0
    n = 0
    with hub.store.write() as w:
        for r in w.execute("SELECT c.id, c.display_name FROM contacts c WHERE c.deleted_at IS NULL "
                           "AND NOT EXISTS (SELECT 1 FROM contact_pile_links l WHERE l.contact_id = c.id)"):
            pid = named.get(_fold(r["display_name"]))
            if pid is not None:
                w.execute("INSERT OR IGNORE INTO contact_pile_links (contact_id, pile_id, state) "
                          "VALUES (?,?,'suggested')", (r["id"], pid))
                n += 1
    return n


def pile_link(hub: Any, contact_id: int) -> Optional[PileLink]:
    r = hub.conn.execute(
        "SELECT l.contact_id, l.pile_id, l.state, p.name FROM contact_pile_links l "
        "JOIN piles p ON p.id = l.pile_id WHERE l.contact_id = ?", (contact_id,)).fetchone()
    return PileLink(r["contact_id"], r["pile_id"], r["name"] or "", r["state"]) if r else None


def pending_suggestions(hub: Any) -> list[PileLink]:
    return [PileLink(r["contact_id"], r["pile_id"], r["name"] or "", r["state"]) for r in hub.conn.execute(
        "SELECT l.contact_id, l.pile_id, l.state, p.name FROM contact_pile_links l "
        "JOIN piles p ON p.id = l.pile_id WHERE l.state = 'suggested' ORDER BY p.name")]


def accept_pile_link(hub: Any, contact_id: int) -> None:
    with hub.store.write() as w:
        w.execute("UPDATE contact_pile_links SET state = 'accepted' WHERE contact_id = ?", (contact_id,))


def decline_pile_link(hub: Any, contact_id: int) -> None:
    with hub.store.write() as w:
        w.execute("UPDATE contact_pile_links SET state = 'declined' WHERE contact_id = ?", (contact_id,))


def link_to_pile(hub: Any, contact_id: int, pile_id: int) -> None:
    """The person chose it by hand: accepted at once."""
    with hub.store.write() as w:
        w.execute("INSERT INTO contact_pile_links (contact_id, pile_id, state) VALUES (?,?,'accepted') "
                  "ON CONFLICT(contact_id) DO UPDATE SET pile_id = excluded.pile_id, state = 'accepted'",
                  (contact_id, pile_id))


def unlink(hub: Any, contact_id: int) -> None:
    with hub.store.write() as w:
        w.execute("DELETE FROM contact_pile_links WHERE contact_id = ?", (contact_id,))


def faces_of_contact(hub: Any, contact_id: int, limit: int = 50) -> list[dict]:
    """The faces of the person an accepted link names: face id, the photo's path, its box."""
    link = pile_link(hub, contact_id)
    if link is None or link.state != "accepted":
        return []
    return [dict(r) for r in hub.conn.execute(
        "SELECT f.id AS face_id, f.file_id, fl.path, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h, f.confidence "
        "FROM faces f JOIN files fl ON fl.id = f.file_id WHERE f.pile_id = ? "
        "ORDER BY COALESCE(f.confidence, 0) DESC, f.id LIMIT ?", (link.pile_id, limit))]


# -- IA-6 (UX review 2026-10-14): the same links, read from the photo side ------------------

def contacts_of_piles(conn: Any) -> dict[int, list[tuple[int, str]]]:
    """Every person in Photos an *accepted* link names -> its contacts `(id, name)`, by name.

    A suggestion or a "no" is not a link (D9). Contacts in the bin are left out. Several
    contacts may be linked to one person; each contact is linked to at most one."""
    found: dict[int, list[tuple[int, str]]] = {}
    for r in conn.execute(
            "SELECT l.pile_id, c.id, c.display_name FROM contact_pile_links l "
            "JOIN contacts c ON c.id = l.contact_id "
            "WHERE l.state = 'accepted' AND c.deleted_at IS NULL "
            "ORDER BY c.display_name COLLATE NOCASE, c.id"):
        found.setdefault(int(r[0]), []).append((int(r[1]), str(r[2] or "")))
    return found


def named_piles(conn: Any) -> dict[str, int]:
    """Every person named in Photos: name, folded -> the person's id (the first, should
    two share a name)."""
    named: dict[str, int] = {}
    for r in conn.execute("SELECT id, name FROM piles WHERE name IS NOT NULL ORDER BY id"):
        named.setdefault(_fold(r[1]), int(r[0]))
    return named


def contact_choices(conn: Any) -> list[tuple[int, str, str]]:
    """Every contact not in the bin, for a picker: `(id, name, first email or "")`, by name."""
    return [(int(r[0]), str(r[1] or ""), str(r[2] or "")) for r in conn.execute(
        "SELECT c.id, c.display_name, (SELECT f.value FROM contact_fields f "
        "WHERE f.contact_id = c.id AND f.kind = 'email' ORDER BY f.is_main DESC, f.position, f.id "
        "LIMIT 1) FROM contacts c WHERE c.deleted_at IS NULL "
        "ORDER BY c.display_name COLLATE NOCASE, c.id")]
