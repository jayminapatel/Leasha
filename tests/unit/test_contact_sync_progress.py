"""Order 1k: a sync reports what it is doing, can be stopped, and one bad contact never ends it.

Layer: L7

2026-10-11, the owner: after pressing Sync the contacts were not identical here and at the accounts.
One Microsoft record answered HTTP 500 and the whole account's write stopped there.
"""

from __future__ import annotations

import pytest

from app.accounts.engine import _GIVE_UP_AFTER
from app.contacts.card import Card
from app.core.errors import AppErrorException, make_error
from tests.unit.accounts_fakes import person
from tests.unit.test_contact_sync_engine import World, only


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


class _Refuses:
    """An account that refuses to update the contacts whose given name is in `bad`."""

    def __init__(self, inner, bad, code="ERR_ACCOUNT_REJECTED"):
        self._inner, self.bad, self.code = inner, set(bad), code

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def update(self, remote_id, version, card):
        if card.get("given") in self.bad:
            raise AppErrorException(make_error(self.code, "test", account="Work", reason="HTTP 500"))
        return self._inner.update(remote_id, version, card)


def _edit_all(world, aid, n, prefix="P"):
    """n contacts known to both sides, then each given a new note here, so each is waiting to be written."""
    for i in range(n):
        world.servers[aid].add(person(f"{prefix}{i}", "Q", email=f"{prefix.lower()}{i}@x.example"))
    world.sync()
    for r in world.hub.list_rows():
        card = world.hub.get_card(r.id)
        card.scalars["notes"] = "edited here"
        world.hub.update(r.id, card)


def test_one_contact_the_account_refuses_does_not_stop_the_others(world):
    g = world.account("Work")
    _edit_all(world, g, 6)
    world.providers[g] = _Refuses(world.providers[g], {"P2"})
    out = only(world.sync(), g)
    assert out.status == "partial" and out.plan.failed_out == 1 and out.plan.update_out == 5
    assert out.plan.failures == [["P2 Q", "An error occurred in test."]] or out.plan.failures[0][0] == "P2 Q"
    notes = sorted(c.get("notes") for c in world.servers[g].live().values())
    assert notes.count("edited here") == 5                              # the five others were written
    run = world.accounts.runs(account_id=g)[0]
    assert run.status == "partial" and "1 contact(s) could not be written" in run.error_text
    assert world.accounts.get(g).last_error_code is None                # the account itself is fine
    world.providers[g] = world.providers[g]._inner                     # the account recovers
    out = only(world.sync(), g)
    assert out.status == "ok" and out.plan.update_out == 1              # the refused one is tried again, alone


def test_a_failure_of_the_whole_account_ends_the_run_and_is_not_listed_per_contact(world):
    g = world.account("Work")
    _edit_all(world, g, 4)
    world.providers[g] = _Refuses(world.providers[g], {"P0", "P1", "P2", "P3"}, code="ERR_ACCOUNT_SIGNIN_EXPIRED")
    out = only(world.sync(), g)
    assert out.status == "failed" and out.error.code == "ERR_ACCOUNT_SIGNIN_EXPIRED" and out.plan.failed_out == 0


def test_a_run_of_refusals_means_the_account_is_not_working_and_ends_the_run(world):
    g = world.account("Work")
    n = _GIVE_UP_AFTER + 5
    _edit_all(world, g, n)
    world.providers[g] = _Refuses(world.providers[g], {f"P{i}" for i in range(n)})
    out = only(world.sync(), g)
    assert out.status == "failed" and out.plan.failed_out == _GIVE_UP_AFTER


def test_progress_is_reported_while_a_sync_goes_on(world):
    g = world.account("Work")
    _edit_all(world, g, 5)
    seen = []
    world.engine.sync(progress=seen.append)
    phases = [e["phase"] for e in seen]
    assert phases[0] == "reading" and phases[-1] == "done" and "writing" in phases
    writing = [e for e in seen if e["phase"] == "writing" and e["total"]]
    assert writing[-1]["done"] == writing[-1]["total"] == 5
    assert all(e["account"] == "Work" for e in seen) and seen[-1]["status"] == "ok"
    assert any(e["current"] for e in writing)                           # it says which contact


def test_stopping_a_sync_keeps_what_was_written_and_the_next_one_carries_on(world):
    g = world.account("Work")
    _edit_all(world, g, 8)
    calls = []

    def stop_after_three():
        calls.append(1)
        return len(world.servers[g].writes) >= 3

    out = only(world.engine.sync(cancel=stop_after_three), g)
    assert out.status == "stopped" and 3 <= out.plan.update_out < 8
    run = world.accounts.runs(account_id=g)[0]
    assert run.status == "interrupted" and "You stopped this sync" in run.error_text
    done_first = out.plan.update_out
    out = only(world.sync(), g)
    assert out.status == "ok" and done_first + out.plan.update_out == 8
