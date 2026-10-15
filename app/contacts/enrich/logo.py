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
from app.contacts.enrich.company import company_guess, registrable_name, work_domains

__all__ = ["company_domain", "usable_domain", "logo_proposals", "CONFIDENCE", "home_page"]

#: How sure a logo is: at Hide below's default (0.5, order 1s D4), so it is shown but never starts ticked.
#: It was 0.4 - under that default - so every logo found was hidden: 150 of them on the owner's index, with
#: the Enrich window offering only what mail gave (found 2026-10-15). The domain is already the company's
#: own (6d); what stays unsure is whether the site's icon is the company's logo.
CONFIDENCE = 0.5

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
    """Is `domain` a social site - matched at its own name, never inside another (CodeRabbit, #68)?

    `_SOCIAL_HOSTS` holds whole hosts (`x.com`, `github.com`) and brand prefixes (`linkedin.`). A whole host
    matches itself or a subdomain of it; a brand matches when it is the domain's own name with only a short
    country or generic ending after it (`linkedin.com`, `ae.linkedin.com`, `linkedin.co.uk`) - so
    `linkedin.com.example.org`, somebody's own site, is not LinkedIn."""
    from app.contacts.enrich.sigparse import _SOCIAL_HOSTS  # noqa: PLC0415 - one list for the package

    d = domain.lower().strip(".")
    labels = d.split(".")
    for host in _SOCIAL_HOSTS:
        if not host.endswith("."):
            if d == host or d.endswith("." + host):
                return True
            continue
        brand = host[:-1]
        if brand in labels:
            ending = labels[labels.index(brand) + 1:]
            if 1 <= len(ending) <= 2 and all(len(x) <= 3 for x in ending):
                return True
    return False


def company_domain(card: Card) -> str:
    """The one domain to ask: the contact's web address first, then a work email's domain.

    Order 1s 6d: a web address is the company's only when its registrable name is a work email's
    (`shop.acme-widgets.com` for `ann@acme-widgets.co.uk`), or the card has no work email to say otherwise.
    A card's link to a supplier, a client or a personal site would otherwise put that site's icon on it."""
    names = {registrable_name(d) for d in work_domains(card)} - {""}
    for f in card.values("url"):
        text = f.value.strip()
        if not text:
            continue
        host = urlsplit(text if "//" in text else "//" + text).hostname or ""
        d = usable_domain(host)
        # A LinkedIn or Facebook profile is the person's page, not the company's site: its icon would be
        # LinkedIn's. Found on the owner's index, 2026-10-12 (`ae.linkedin.com` asked for a logo).
        if d and not _social(d) and (not names or registrable_name(d) in names):
            return d
    for f in card.values("email"):
        if company_guess(f.value):
            d = usable_domain(f.value.rsplit("@", 1)[-1])
            if d:
                return d
    return ""


def home_page(domain: str) -> str:
    """Where a logo can be checked (`Proposal.opens`, owner 2026-10-15): the company's home page, not the
    icon file - the page shows whose site it is, a favicon on its own shows a few pixels."""
    return f"https://{domain}/" if domain else ""


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
                return [Proposal(cid, "photo", "", "", photo, "logo", url, CONFIDENCE, opens=home_page(domain))]
        page = get(f"https://{domain}/")
        if page is not None and page.status == 200:
            for href in _icon_links(page.body[:MAX_PAGE_BYTES].decode("utf-8", "replace"), f"https://{domain}/"):
                if href in icon_urls:
                    continue
                photo = _icon(get(href))
                if photo is not None:
                    return [Proposal(cid, "photo", "", "", photo, "logo", href, CONFIDENCE, opens=home_page(domain))]
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
