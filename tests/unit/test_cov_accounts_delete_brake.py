"""The delete brake at its edges: exactly at the limit, one past it, the master's deletes, and Allow.

Order 1u §4d. `test_contact_sync_engine.py`, `test_contact_sync_trust.py` and `test_contact_master.py` show the
brake holding a mass delete well past the limit. These pin the edges of `app.accounts.engine._too_many` (a
count, or a share of the book, both strictly "more than"), the push brake at the limit and one past it with
creates still written, and the master's own brake (`_apply_master_deletes`): flagged, not deleted, what was
pulled kept, the run recorded as held, and Allow carrying the deletes out.
"""

from __future__ import annotations

import pytest

from app.accounts.engine import _too_many
from app.accounts.store import AccountSettings

from tests.unit.accounts_fakes import person
from tests.unit.test_contact_sync_engine import World, only


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


# ---------------------------------------------------------------- the rule itself

@pytest.mark.parametrize("n, book, count, percent, held", [
    (20, 10_000, 20, 5.0, False),        # exactly the count: not more than it
    (21, 10_000, 20, 5.0, True),         # one past the count
    (5, 100, 20, 5.0, False),            # exactly 5% of the book
    (6, 100, 20, 5.0, True),             # one past 5%
    (1, 1, 20, 5.0, False),              # a single delete is never a mass deletion, even of the whole book
    (2, 2, 20, 5.0, True),               # two is, in a tiny book, by share
    (0, 100, 0, 0.0, False),             # nothing to delete holds nothing
])
def test_the_brake_is_more_than_the_count_or_more_than_the_share(n, book, count, percent, held):
    settings = AccountSettings(brake_count=count, brake_percent=percent)

    assert _too_many(n, book, settings) is held


# ---------------------------------------------------------------- the push brake

def _home_with(world, people):
    aid = world.account("Home")
    for i in range(people):
        world.servers[aid].add(person(f"P{i}", "X", email=f"p{i}@x.example"))
    world.sync()
    world.accounts.update_settings(brake_count=3, brake_percent=100.0)
    return aid


def test_deletes_exactly_at_the_limit_go_through(world):
    aid = _home_with(world, 10)
    for r in world.hub.list_rows()[:3]:
        world.hub.delete(r.id)

    out = only(world.sync(), aid)

    assert out.status == "ok" and out.error is None
    assert out.plan.held_deletes == 0 and out.plan.delete_out == 3
    assert len(world.servers[aid].live()) == 7


def test_one_past_the_limit_the_deletes_wait_and_a_new_contact_is_still_written(world):
    aid = _home_with(world, 10)
    for r in world.hub.list_rows()[:4]:
        world.hub.delete(r.id)
    world.hub.create(person("New", "Person", email="new@x.example"), kept_in=[aid])

    out = only(world.sync(), aid)

    assert out.status == "held" and out.error.code == "ERR_SYNC_DELETE_BRAKE"
    assert out.plan.held_deletes == 4 and out.plan.delete_out == 0
    assert out.plan.create_out == 1
    live = world.servers[aid].live()
    assert len(live) == 11 and any(c.get("given") == "New" for c in live.values())

    allowed = only(world.sync(allow_brake=True), aid)

    assert allowed.status == "ok" and allowed.plan.delete_out == 4
    assert len(world.servers[aid].live()) == 7


def test_held_deletes_stay_held_on_the_next_sync_without_allow(world):
    aid = _home_with(world, 10)
    for r in world.hub.list_rows()[:4]:
        world.hub.delete(r.id)
    world.sync()

    again = only(world.sync(), aid)

    assert again.status == "held" and again.plan.held_deletes == 4
    assert len(world.servers[aid].live()) == 10


# ---------------------------------------------------------------- the master's brake

def _master_with(world, people):
    g = world.account("Google")
    rids = [world.servers[g].add(person(f"M{i}", "Q", email=f"m{i}@x.example")) for i in range(people)]
    world.accounts.update_settings(master_account_id=g, brake_count=3, brake_percent=100.0)
    world.sync()
    assert world.hub.count() == people
    return g, rids


def test_the_master_deleting_exactly_the_limit_is_carried_out(world):
    g, rids = _master_with(world, 10)
    for rid in rids[:3]:
        world.servers[g].remove(rid)

    out = only(world.sync(), g)

    assert out.status == "ok" and out.plan.deleted_in == 3
    assert world.hub.count() == 7
    assert world.hub.count(include_bin=True) == 10, "to the Bin, so it can be put back"


def test_the_master_deleting_past_the_limit_only_flags_and_keeps_what_it_pulled(world):
    g, rids = _master_with(world, 10)
    for rid in rids[:4]:
        world.servers[g].remove(rid)
    world.servers[g].add(person("Fresh", "Face", email="fresh@x.example"))

    out = only(world.sync(), g)

    assert out.status == "held" and out.error.code == "ERR_SYNC_DELETE_BRAKE"
    assert out.plan.deleted_in == 0 and out.plan.flagged_deleted == 4
    names = {r.display_name for r in world.hub.list_rows()}
    assert len(names) == 11 and "Fresh Face" in names, "nothing deleted, and the add still came in"
    run = world.accounts.runs(account_id=g)[0]
    assert run.status == "held" and run.held == 4 and run.error_code == "ERR_SYNC_DELETE_BRAKE"

    still = only(world.sync(), g)
    assert still.status == "held" and world.hub.count() == 11, "a flag counts again until Allow"

    allowed = only(world.sync(allow_brake=True), g)

    assert allowed.status == "ok" and allowed.plan.deleted_in == 4
    assert world.hub.count() == 7
    assert world.hub.count(include_bin=True) == 11
