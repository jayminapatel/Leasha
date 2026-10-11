"""Opt-in live test: the Microsoft Graph connector against a real, throwaway Microsoft account.

Layer: L7

Order 1k 4d. Skipped unless `LEASHA_LIVE=1` and `LEASHA_LIVE_GRAPH_TOKEN` (a
current access token with `Contacts.ReadWrite` for a THROWAWAY personal or work
account) are set. Never part of the normal suite. It creates a contact, finds it
through a delta pull, changes it, checks a stale change key is refused, and
deletes it (again in a `finally` on failure). Also the place to see, once, that
`If-Match: W/"<changeKey>"` is accepted: set `LEASHA_LIVE_GRAPH_IF_MATCH=1`.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("LEASHA_LIVE") == "1" and os.environ.get("LEASHA_LIVE_GRAPH_TOKEN")),
    reason="live test: set LEASHA_LIVE=1 and LEASHA_LIVE_GRAPH_TOKEN (a throwaway account's token)")


def test_create_delta_update_delete_against_graph():
    from app.accounts.http import AllowedHosts, UrllibTransport
    from app.accounts.provider import VersionConflict
    from app.accounts.providers.graph_contacts import GraphContactsProvider
    from app.contacts.card import Card, Field

    token = os.environ["LEASHA_LIVE_GRAPH_TOKEN"]
    gc = GraphContactsProvider(UrllibTransport(AllowedHosts(("microsoft",))), lambda: token,
                               send_if_match=os.environ.get("LEASHA_LIVE_GRAPH_IF_MATCH") == "1")
    tag = f"Leasha live {uuid.uuid4().hex[:8]}"
    rid = ""
    try:
        rid, version = gc.create(Card(scalars={"given": tag, "family": "Test", "company": "Leasha"},
                                      fields=[Field("email", "live@example.com"), Field("phone", "+441632960000", "mobile")],
                                      groups=["Leasha live test"]))
        pages = list(gc.pull(None))
        assert any(c.remote_id == rid for p in pages for c in p.changes)
        delta_token = pages[-1].next_token

        got = gc.get(rid)
        assert "Leasha live test" in got.card.groups
        got.card.scalars["title"] = "Tester"
        new = gc.update(rid, got.version, got.card)
        with pytest.raises(VersionConflict):
            gc.update(rid, got.version, got.card)               # the old change key is stale now
        delta = [c for p in gc.pull(delta_token) for c in p.changes]
        assert any(c.remote_id == rid for c in delta)
        gc.delete(rid, new)
        gone_id, rid = rid, ""
        assert any(c.remote_id == gone_id and c.card is None for p in gc.pull(delta_token) for c in p.changes)
    finally:
        if rid:
            current = gc.get(rid)
            if current is not None:
                gc.delete(rid, current.version)
