"""7g: a company's own icon, fetched from the company's own domain.

Layer: L7

Order 1k section 7g. **Internet; off by default** (`AccountSettings.enrich_logo`).

**Only the domain is contacted, and only one domain per contact.** It comes from
the contact's web address if it has one, otherwise from its first work email
(free mail has no company, so it gives none). Nothing about the person is sent -
just a request for `/favicon.ico`, or for the home page to read its
`<link rel="icon">`. The domain is registered with `AllowedHosts.allow_server`
for the length of the call and forgotten afterwards (`finally`).

A redirect is followed only to the same site: the domain itself, or its `www.`
twin (for `acme.com` -> `www.acme.com`), each registered exactly the same way. An
icon address on any other host is ignored.

Refused outright: an IP address, a single-word host, `localhost`, and the
private-network suffixes `.local`, `.lan`, `.internal`, `.home`, `.corp` - a
contact's web address must not be able to aim Leasha at the owner's own network.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Optional
from urllib.parse import urljoin, urlsplit

from app.accounts.http import AllowedHosts
from app.accounts.provider import HttpResponse, Transport
from app.contacts.card import Card
from app.contacts.enrich.base import MAX_IMAGE_BYTES, Proposal, normalise_image
from app.contacts.enrich.company import company_guess

__all__ = ["company_domain", "usable_domain", "logo_proposals"]

_PRIVATE_SUFFIXES = (".local", ".lan", ".internal", ".home", ".corp", ".localdomain", ".intranet", ".test",
                     ".invalid", ".example", ".localhost", ".arpa")
_HOST = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
_LINK = re.compile(r"<link\b[^>]*>", re.I)
_REL = re.compile(r"""\brel\s*=\s*["']?([^"'>]*)["']?""", re.I)
_HREF = re.compile(r"""\bhref\s*=\s*["']([^"']+)["']""", re.I)
_USER_AGENT = "Leasha (desktop app; fetches a company's own icon)"
MAX_PAGE_BYTES = 512 * 1024


def usable_domain(host: str) -> str:
    """`host` lower-cased if it is an ordinary public-looking domain name, else `""`."""
    h = (host or "").strip().lower().rstrip(".")
    if not h or not _HOST.match(h) or h.endswith(_PRIVATE_SUFFIXES):
        return ""
    try:
        ipaddress.ip_address(h)
        return ""                                        # an IP address
    except ValueError:
        pass
    return h


def _social(domain: str) -> bool:
    from app.contacts.enrich.signatures import _SOCIAL_HOSTS  # noqa: PLC0415 - one list for the package
    d = "." + domain
    return any(("." + s.rstrip(".")) in d for s in _SOCIAL_HOSTS)


def company_domain(card: Card) -> str:
    """The one domain to ask: the contact's web address first, then a work email's domain."""
    for f in card.values("url"):
        text = f.value.strip()
        if not text:
            continue
        host = urlsplit(text if "//" in text else "//" + text).hostname or ""
        d = usable_domain(host)
        # A LinkedIn or Facebook profile is the person's page, not the company's site: its icon would be
        # LinkedIn's. Found on the owner's index, 2026-10-12 (`ae.linkedin.com` asked for a logo).
        if d and not _social(d):
            return d
    for f in card.values("email"):
        if company_guess(f.value):
            d = usable_domain(f.value.rsplit("@", 1)[-1])
            if d:
                return d
    return ""


def _family(domain: str) -> set[str]:
    bare = domain[4:] if domain.startswith("www.") else domain
    return {bare, "www." + bare}


def logo_proposals(transport: Transport, allowed: AllowedHosts, cid: int, card: Card) -> list[Proposal]:
    """The site's own icon as a photo - only for a card with no photo and a company domain.

    `transport` must check `allowed` (the default one does, and so does
    `GuardedTransport`); registering a host here is what lets it through.
    """
    if card.photo is not None:
        return []
    domain = company_domain(card)
    if not domain:
        return []
    family = _family(domain)
    registered: list[str] = []

    def get(url: str) -> Optional[HttpResponse]:
        for _hop in range(3):
            host = urlsplit(url).hostname or ""
            if host not in family or not usable_domain(host):
                return None
            if host not in registered:
                registered.append(allowed.allow_server(host))
            r = transport.request("GET", url, headers={"User-Agent": _USER_AGENT, "Accept": "*/*"}, timeout=15.0)
            if 300 <= r.status < 400 and r.header("location"):
                url = urljoin(url, r.header("location"))
                continue
            return r
        return None

    try:
        icon_urls = [f"https://{domain}/favicon.ico"]
        for url in icon_urls:
            photo = _icon(get(url))
            if photo is not None:
                return [Proposal(cid, "photo", "", "", photo, "logo", url, 0.4)]
        page = get(f"https://{domain}/")
        if page is not None and page.status == 200:
            for href in _icon_links(page.body[:MAX_PAGE_BYTES].decode("utf-8", "replace"), f"https://{domain}/"):
                if href in icon_urls:
                    continue
                photo = _icon(get(href))
                if photo is not None:
                    return [Proposal(cid, "photo", "", "", photo, "logo", href, 0.4)]
        return []
    finally:
        for h in registered:
            allowed.forget_server(h)


def _icon(r: Optional[HttpResponse]):
    if r is None or r.status != 200 or not r.body or len(r.body) > MAX_IMAGE_BYTES:
        return None
    return normalise_image(r.body)


def _icon_links(html: str, base: str) -> list[str]:
    found: list[tuple[int, str]] = []
    for tag in _LINK.findall(html):
        rel = _REL.search(tag)
        href = _HREF.search(tag)
        if not rel or not href:
            continue
        words = rel.group(1).lower().split()
        if "icon" not in words and "apple-touch-icon" not in words:
            continue
        rank = 0 if "apple-touch-icon" in words else 1
        found.append((rank, urljoin(base, href.group(1).strip())))
    return [u for _, u in sorted(found, key=lambda x: x[0]) if u.lower().startswith("https://")][:3]
