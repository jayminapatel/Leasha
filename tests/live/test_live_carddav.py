"""Opt-in live test: the CardDAV connector against a real, throwaway CardDAV account.

Layer: L7

Order 1k 4c, 4d, 4e. Skipped unless `LEASHA_LIVE=1` and these are set:
`LEASHA_LIVE_CARDDAV_URL` (a server URL for discovery, e.g. https://contacts.icloud.com,
or the address book's own URL), `LEASHA_LIVE_CARDDAV_USER`, `LEASHA_LIVE_CARDDAV_PASSWORD`
(an app-specific password for iCloud). Optional: `LEASHA_LIVE_CARDDAV_BOOK` (the
address book's name), `LEASHA_LIVE_CARDDAV_DIRECT=1` (the URL is the address book).
Never part of the normal suite. It creates one contact, finds it by a pull, edits
it, checks a stale etag is refused, and deletes it.
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import urlparse

import pytest

_NEED = ("LEASHA_LIVE_CARDDAV_URL", "LEASHA_LIVE_CARDDAV_USER", "LEASHA_LIVE_CARDDAV_PASSWORD")
pytestmark = pytest.mark.skipif(
    not (os.environ.get("LEASHA_LIVE") == "1" and all(os.environ.get(k) for k in _NEED)),
    reason="live test: set LEASHA_LIVE=1 and " + ", ".join(_NEED))


def test_create_pull_update_delete_against_a_carddav_server():
    from app.accounts.http import AllowedHosts, UrllibTransport
    from app.accounts.provider import VersionConflict
    from app.accounts.providers.carddav import CardDavProvider, basic_auth_header
    from app.contacts.card import Card, Field

    url = os.environ["LEASHA_LIVE_CARDDAV_URL"]
    allowed = AllowedHosts(("apple",))
    allowed.allow_server(urlparse(url).hostname or "")
    kw = {"addressbook_url": url} if os.environ.get("LEASHA_LIVE_CARDDAV_DIRECT") == "1" else {"server_url": url}
    cd = CardDavProvider(UrllibTransport(allowed), basic_auth_header(
        os.environ["LEASHA_LIVE_CARDDAV_USER"], lambda: os.environ["LEASHA_LIVE_CARDDAV_PASSWORD"]),
        addressbook_name=os.environ.get("LEASHA_LIVE_CARDDAV_BOOK", ""), **kw)
    tag = f"Leasha live {uuid.uuid4().hex[:8]}"
    rid = ""
    try:
        rid, etag = cd.create(Card(scalars={"given": tag, "family": "Test"},
                                   fields=[Field("email", "live@example.com", "work")],
                                   groups=["Leasha live test"]))
        pages = list(cd.pull(None))
        assert any(c.remote_id == rid for p in pages for c in p.changes)
        token = pages[-1].next_token

        got = cd.get(rid)
        assert "Leasha live test" in got.card.groups
        got.card.scalars["title"] = "Tester"
        new = cd.update(rid, got.version, got.card)
        with pytest.raises(VersionConflict):
            cd.update(rid, got.version, got.card)               # the old etag is stale now
        assert any(c.remote_id == rid for p in cd.pull(token) for c in p.changes)
        cd.delete(rid, new)
        rid = ""
        assert any(c.card is None for p in cd.pull(token) for c in p.changes)
    finally:
        if rid:
            current = cd.get(rid)
            if current is not None:
                cd.delete(rid, current.version)
