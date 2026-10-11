"""Quick actions on a contact: what the card's menu and right-click call.

Layer: L7

Order 1k 6r. Pure helpers that return text or a link. Nothing here copies to the
clipboard, opens a browser or sends anything: the UI does that when the person
clicks, and `maps_url` only *builds* the link for the site they chose in Settings.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import quote_plus

from app.contacts.card import Card, Field
from app.contacts.exporters import vcard_text

__all__ = ["MAP_SITES", "copy_email", "copy_phone", "address_query", "maps_url", "card_as_vcard"]

#: The map sites on offer; `{q}` is the URL-encoded address. A fixed list: each
#: needs its own link shape, and a site that is not here is a code change.
MAP_SITES = {
    "google": "https://www.google.com/maps/search/?api=1&query={q}",
    "openstreetmap": "https://www.openstreetmap.org/search?query={q}",
    "apple": "https://maps.apple.com/?q={q}",
    "bing": "https://www.bing.com/maps?q={q}",
}


def _pick(card: Card, kind: str, label: Optional[str]) -> str:
    values = [f for f in card.values(kind) if f.value.strip()]
    if label:
        want = label.strip().lower()
        for f in values:
            if f.label.strip().lower() == want:
                return f.value.strip()
        return ""
    for f in values:
        if f.primary:
            return f.value.strip()
    return values[0].value.strip() if values else ""


def copy_email(card: Card, label: Optional[str] = None) -> str:
    """The email address to copy: the one with `label`, else the main one, else the
    first. `""` when there is none."""
    return _pick(card, "email", label)


def copy_phone(card: Card, label: Optional[str] = None) -> str:
    """The phone number to copy, as typed (the person sees what they copied)."""
    return _pick(card, "phone", label)


def address_query(address_field: Field) -> str:
    """An address on one line: street lines, town, region, postcode, country."""
    d = address_field.data or {}
    if d:
        parts = [ln.strip() for ln in d.get("street", "").splitlines() if ln.strip()]
        parts += [d[k].strip() for k in ("city", "region", "postcode", "country") if d.get(k, "").strip()]
        return ", ".join(parts)
    return ", ".join(ln.strip() for ln in address_field.value.splitlines() if ln.strip())


def maps_url(address_field: Field, site: str = "google") -> str:
    """The link that shows this address on `site`. Built only; nothing is opened or sent."""
    if site not in MAP_SITES:
        raise ValueError(f"The map site {site!r} is not known. Choose one of: {', '.join(MAP_SITES)}.")
    query = address_query(address_field)
    if not query:
        raise ValueError("This address is empty, so there is nothing to look up on a map.")
    return MAP_SITES[site].format(q=quote_plus(query))


def card_as_vcard(card: Card, version: str = "3.0") -> str:
    """The contact as a vCard, for "Copy as vCard"."""
    return vcard_text(card, version)
