"""7f: the photo and public profile a person published on Gravatar.

Layer: L7

Order 1k section 7f. **Internet; off by default** (`AccountSettings.enrich_gravatar`).

**What is sent.** Only the SHA-256 hash of the lower-cased, trimmed email address,
inside the URL. The address itself is never in a URL, header or body - `_url`
asserts it. Gravatar can still tell that someone asked about that hash, which is
what the switch's text says.

Two requests per address, both to `www.gravatar.com` (the host is on
`ENRICHMENT_HOSTS`):

* `GET /avatar/<hash>?d=404&s=256` - 200 is a picture, 404 means the person has
  none (the `d=404` is what stops Gravatar answering with a generic stand-in);
* `GET /<hash>.json` - the public profile; its website links are offered as URLs.
  Gravatar answers this with a redirect for some profiles; a redirect is not
  followed (the allow-list is a check on the first hop only), so those give no
  profile and the photo alone is offered.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Optional

from app.accounts.provider import Transport
from app.contacts.card import Card
from app.contacts.enrich.base import Proposal, emails_of, normalise_image

__all__ = ["email_hash", "avatar_url", "profile_url", "gravatar_proposals", "MAX_ADDRESSES"]

HOST = "www.gravatar.com"
MAX_ADDRESSES = 3
_USER_AGENT = "Leasha (desktop app; looks up a published Gravatar by hash)"


def email_hash(email: str) -> str:
    """SHA-256 of the trimmed, lower-cased address - Gravatar's own recipe."""
    return hashlib.sha256((email or "").strip().lower().encode("utf-8")).hexdigest()


def _url(path: str) -> str:
    url = f"https://{HOST}{path}"
    assert "@" not in url, "an email address must never be put in a Gravatar URL"
    return url


def avatar_url(email: str, size: int = 256) -> str:
    return _url(f"/avatar/{email_hash(email)}?d=404&s={int(size)}")


def profile_url(email: str) -> str:
    return _url(f"/{email_hash(email)}.json")


def gravatar_proposals(transport: Transport, cid: int, card: Card) -> list[Proposal]:
    """Photo and links for the contact's first few addresses. Raises only what the transport raises."""
    out: list[Proposal] = []
    have_urls = {_norm_url(f.value) for f in card.values("url")}
    for email in emails_of(card)[:MAX_ADDRESSES]:
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            continue
        receipt = _url(f"/{email_hash(email)}")
        headers = {"User-Agent": _USER_AGENT, "Accept": "*/*"}
        if card.photo is None and not any(p.field_kind == "photo" for p in out):
            r = transport.request("GET", avatar_url(email), headers=headers, timeout=15.0)
            if r.status == 200:
                photo = normalise_image(r.body)
                if photo is not None:
                    out.append(Proposal(cid, "photo", "", "", photo, "gravatar", receipt, 0.6, opens=receipt))
        r = transport.request("GET", profile_url(email), headers={**headers, "Accept": "application/json"},
                              timeout=15.0)
        if r.status == 200:
            out.extend(_profile_links(r.body, cid, receipt, have_urls))
    return out


def _norm_url(u: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", (u or "").strip().lower()).rstrip("/")


def _profile_links(body: bytes, cid: int, receipt: str, have: set[str]) -> list[Proposal]:
    try:
        data: Any = json.loads(body.decode("utf-8", "replace"))
        entry = (data.get("entry") or [{}])[0]
        urls = entry.get("urls") or []
    except (ValueError, AttributeError, IndexError, TypeError):
        return []
    out: list[Proposal] = []
    for u in urls[:5]:
        if not isinstance(u, dict):
            continue
        value = str(u.get("value") or "").strip()
        if not value.lower().startswith("https://") or len(value) > 300:
            continue
        key = _norm_url(value)
        if key in have:
            continue
        have.add(key)
        label = str(u.get("title") or "").strip()[:40] or "profile"
        out.append(Proposal(cid, "url", label, "", value, "gravatar", receipt, 0.5, opens=receipt))
    return out
