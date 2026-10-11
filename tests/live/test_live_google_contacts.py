"""Opt-in live test: the Google People connector against a real, throwaway Google account.

Layer: L7

Order 1k 4d. Skipped unless `LEASHA_LIVE=1` and `LEASHA_LIVE_GOOGLE_TOKEN` (a
current OAuth access token with the `contacts` scope for a THROWAWAY account) are
set. Never part of the normal suite; the owner runs it by hand. It creates one
contact, reads it back through a full pull, changes it, and deletes it, and
removes it again in a `finally` if anything fails.
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not (os.environ.get("LEASHA_LIVE") == "1" and os.environ.get("LEASHA_LIVE_GOOGLE_TOKEN")),
    reason="live test: set LEASHA_LIVE=1 and LEASHA_LIVE_GOOGLE_TOKEN (a throwaway account's token)")


def test_create_pull_update_delete_against_google():
    from app.accounts.http import AllowedHosts, UrllibTransport
    from app.accounts.provider import VersionConflict
    from app.accounts.providers.google_people import GooglePeopleProvider
    from app.contacts.card import Card, Field

    token = os.environ["LEASHA_LIVE_GOOGLE_TOKEN"]
    gp = GooglePeopleProvider(UrllibTransport(AllowedHosts(("google",))), lambda: token)
    tag = f"Leasha live {uuid.uuid4().hex[:8]}"
    rid = ""
    try:
        rid, version = gp.create(Card(scalars={"given": tag, "family": "Test", "company": "Leasha"},
                                      fields=[Field("email", "live@example.com", "work")],
                                      groups=["Leasha live test"]))
        pages = list(gp.pull(None))
        assert any(c.remote_id == rid for p in pages for c in p.changes)
        sync_token = pages[-1].next_token
        assert sync_token

        got = gp.get(rid)
        got.card.scalars["title"] = "Tester"
        version = gp.update(rid, got.version, got.card)
        with pytest.raises(VersionConflict):
            gp.update(rid, got.version, got.card)               # the old etag is stale now
        delta = [c for p in gp.pull(sync_token) for c in p.changes]
        assert any(c.remote_id == rid and c.card and c.card.get("title") == "Tester" for c in delta)
        gp.delete(rid, version)
        gone_id, rid = rid, ""
        assert any(c.remote_id == gone_id and c.card is None for p in gp.pull(sync_token) for c in p.changes)
    finally:
        if rid:
            current = gp.get(rid)
            if current is not None:
                gp.delete(rid, current.version)
