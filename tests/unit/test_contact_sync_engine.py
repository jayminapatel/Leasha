"""Order 1k 9a: the contact sync engine against a fake provider.

Layer: L7

The six scenarios the order names - two accounts and the hub all changing at
once, a conflicting field (D6), a delete in one account (D7) and the delete
brake, a token expired mid-sync and a crash mid-page (resume), a provider that
keeps refusing, and undo of a whole sync - plus the dry run (D8), "Kept in"
(D4), a write that loses a race (3c), a capability gap (D10) and a read-only
direction (10r).
"""

from __future__ import annotations

import pytest

from app.accounts.engine import ContactSyncEngine
from app.accounts.provider import TokenExpired
from app.accounts.store import AccountStore
from app.contacts.card import Capabilities, Card, Field, units
from app.contacts.hub import ContactFilter, ContactHub
from app.core.errors import AppErrorException, make_error
from app.storage.sqlite_store import SqliteStore

from tests.unit.accounts_fakes import FakeProvider, FakeServer, person


class World:
    def __init__(self, tmp_path):
        self.store = SqliteStore(tmp_path / "k.db").connect()
        self.hub = ContactHub(self.store)
        self.accounts = AccountStore(self.store)
        self.accounts.update_settings(dry_run_first=False)
        self.servers: dict[int, FakeServer] = {}
        self.providers: dict[int, FakeProvider] = {}
        self.engine = ContactSyncEngine(self.store, self.hub, self.accounts, self._provider)

    def _provider(self, acct):
        return self.providers[acct.id]

    def account(self, name: str, *, caps=None, direction="two-way", page_size=2) -> int:
        aid = self.accounts.add("google", f"{name.lower()}@example.com", name, direction=direction)
        self.servers[aid] = FakeServer()
        kwargs = {"caps": caps} if caps is not None else {}
        self.providers[aid] = FakeProvider(self.servers[aid], page_size=page_size, **kwargs)
        return aid

    def sync(self, ids=None, **kw):
        return self.engine.sync(ids, **kw)

    def close(self):
        self.store.close()


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


def only(outcomes, aid):
    return next(o for o in outcomes if o.account_id == aid)


def test_first_pull_brings_an_account_in_and_links_it(world):
    a = world.account("Home")
    world.servers[a].add(person("Ann", "Lee", email="ann@x.com", phone="07700 900123"))
    world.servers[a].add(person("Bob", "Ray"))
    world.servers[a].add(person("Cy", "Dee"))
    out = only(world.sync(), a)
    assert out.status == "ok" and out.plan.add_in == 3
    assert world.hub.count() == 3
    row = next(r for r in world.hub.list_rows() if r.display_name == "Ann Lee")
    assert row.accounts == [a] and row.phone == "07700 900123"
    assert world.accounts.get(a).first_sync_done


@pytest.mark.slow
def test_two_accounts_and_the_hub_all_change_at_once(world):
    """9a.1 - a phone changed in one account reaches the other within one sync (A2)."""
    g, m = world.account("Google"), world.account("Work")
    rid = world.servers[g].add(person("Ann", "Lee", phone="07700 900123"))
    world.sync()
    cid = world.hub.list_rows()[0].id
    world.hub.set_kept_in(cid, [g, m])
    world.sync()
    assert len(world.servers[m].live()) == 1
    # now: the phone changes in Google, a note in the hub, a new person in Work - all before one sync
    card = world.servers[g].recs[rid].card.copy()
    card.fields = [Field("phone", "07700 900999", "mobile")]
    world.servers[g].edit(rid, card)
    hub_card = world.hub.get_card(cid)
    hub_card.scalars["notes"] = "met at the fair"
    world.hub.update(cid, hub_card)
    world.servers[m].add(person("Zed", "Moon"))
    out = world.sync()
    assert all(o.status == "ok" for o in out)
    for aid in (g, m):
        cards = list(world.servers[aid].live().values())
        ann = next(c for c in cards if c.get("given") == "Ann")
        assert [f.value for f in ann.fields if f.kind == "phone"] == ["07700 900999"]
        assert ann.get("notes") == "met at the fair"
    assert world.hub.count() == 2                                # Ann and Zed
    # and a second sync is quiet: nothing to write anywhere
    before = [list(s.writes) for s in world.servers.values()]
    world.sync()
    assert [list(s.writes) for s in world.servers.values()] == before


def test_a_conflicting_field_the_later_edit_wins_and_is_journalled(world):
    """9a.2 - D6."""
    g, m = world.account("Google"), world.account("Work")
    rg = world.servers[g].add(person("Ann", "Lee", company="Old"), edited_at=1000)
    world.sync()
    cid = world.hub.list_rows()[0].id
    world.hub.set_kept_in(cid, [g, m])
    world.sync()
    rm = next(iter(world.servers[m].recs))
    # both accounts change the company; Work's edit is the later one
    for aid, rid, value, when in ((g, rg, "FromGoogle", 5_000_000_000.0), (m, rm, "FromWork", 9_000_000_000.0)):
        card = world.servers[aid].recs[rid].card.copy()
        card.scalars["company"] = value
        world.servers[aid].edit(rid, card, edited_at=when)
    world.sync()
    assert world.hub.get_card(cid).get("company") == "FromWork"
    assert world.servers[g].live()[rg].get("company") == "FromWork"        # the loser account was corrected
    row = world.hub.get_row(cid)
    assert row.conflict_at is not None                                       # the "changed in two places" marker
    conflicts = world.hub.open_conflicts(cid)
    assert conflicts and conflicts[0]["winner"] in ("local", "remote")
    assert any(h.action == "conflict" for h in world.hub.history(cid))       # the loser is in the journal
    world.hub.acknowledge_conflicts(cid)
    assert world.hub.get_row(cid).conflict_at is None


def test_ask_every_time_holds_the_clash_and_writes_nothing(world):
    world.accounts.update_settings(conflict_policy="ask")
    g = world.account("Google")
    rid = world.servers[g].add(person("Ann", "Lee", company="A"), edited_at=1000)
    world.sync()
    cid = world.hub.list_rows()[0].id
    card = world.hub.get_card(cid)
    card.scalars["company"] = "Hub"
    world.hub.update(cid, card)
    rc = world.servers[g].recs[rid].card.copy()
    rc.scalars["company"] = "Remote"
    world.servers[g].edit(rid, rc)
    out = only(world.sync(), g)
    assert out.plan.held_conflicts == 1
    assert world.servers[g].live()[rid].get("company") == "Remote"          # not overwritten
    assert world.hub.get_card(cid).get("company") == "Hub"
    assert world.engine.resolve_conflict(cid, g, "remote").status == "ok"
    assert world.hub.get_card(cid).get("company") == "Remote"
    assert world.hub.get_row(cid).conflict_at is None


def test_a_delete_in_one_account_is_held_not_propagated(world):
    """9a.3 - D7.1: deleted elsewhere is listed for confirmation; nothing else is touched."""
    g, m = world.account("Google"), world.account("Work")
    rg = world.servers[g].add(person("Ann", "Lee", email="ann@x.com"))
    world.sync()
    cid = world.hub.list_rows()[0].id
    world.hub.set_kept_in(cid, [g, m])
    world.sync()
    world.servers[g].remove(rg)
    world.sync()
    assert world.hub.count() == 1                                         # still in Leasha
    assert len(world.servers[m].live()) == 1                              # and in Work
    assert [r.id for r in world.hub.list_rows(ContactFilter("deleted_elsewhere"))] == [cid]
    assert world.hub.counts()["deleted_elsewhere"] == 1
    # restore writes it back to Google as a new record
    world.hub.restore_to_account(cid, g)
    world.sync()
    assert len(world.servers[g].live()) == 1
    assert world.hub.list_rows(ContactFilter("deleted_elsewhere")) == []


def test_confirming_a_delete_elsewhere_bins_it_and_removes_it_from_the_others(world):
    g, m = world.account("Google"), world.account("Work")
    rg = world.servers[g].add(person("Ann", "Lee", email="ann@x.com"))
    world.sync()
    cid = world.hub.list_rows()[0].id
    world.hub.set_kept_in(cid, [g, m])
    world.sync()
    world.servers[g].remove(rg)
    world.sync()
    world.hub.confirm_deleted_elsewhere(cid)
    world.sync()
    assert world.hub.count() == 0 and world.hub.count(include_bin=True) == 1
    assert world.servers[m].live() == {}


def test_a_contact_deleted_in_leasha_leaves_its_accounts_and_restores_from_the_bin(world):
    g = world.account("Google")
    world.servers[g].add(person("Ann", "Lee"))
    world.sync()
    cid = world.hub.list_rows()[0].id
    world.hub.delete(cid)
    world.sync()
    assert world.servers[g].live() == {}
    assert world.hub.counts()["bin"] == 1
    world.hub.restore(cid)
    world.sync()
    assert len(world.servers[g].live()) == 1                              # written back (D7.2)


def test_a_delete_refused_only_because_the_version_moved_is_retried_with_the_new_one(world):
    """Four contacts folded by a merge stayed in Gmail: the delete compared a stale etag, was taken for a race,
    and "gave up ... next sync tries again" - for ever (the owner, 2026-10-12)."""
    from app.accounts.provider import VersionConflict

    g = world.account("Google")
    rid = world.servers[g].add(person("Ann", "Lee"))
    world.sync()
    cid = world.hub.list_rows()[0].id
    world.servers[g].recs[rid].version += 1               # the account re-saved the record: same content, new etag
    provider = world.providers[g]
    seen: list[str] = []
    real_delete = provider.delete

    def delete(remote_id, version):
        seen.append(version)
        if version != str(world.servers[g].recs[remote_id].version):
            raise VersionConflict(remote_id)
        real_delete(remote_id, version)

    provider.delete = delete
    world.hub.delete(cid)
    world.sync()
    assert world.servers[g].live() == {} and len(seen) == 2 and seen[0] != seen[1]


def test_a_delete_of_a_record_whose_content_changed_meanwhile_is_still_held_back(world):
    from app.accounts.provider import VersionConflict

    g = world.account("Google")
    rid = world.servers[g].add(person("Ann", "Lee"))
    world.sync()
    cid = world.hub.list_rows()[0].id
    changed = person("Ann", "Lee")
    changed.scalars["company"] = "Edited At The Account"
    world.servers[g].edit(rid, changed)
    provider = world.providers[g]

    def delete(remote_id, version):
        raise VersionConflict(remote_id)

    provider.delete = delete
    world.hub.delete(cid)
    world.sync()
    assert len(world.servers[g].live()) == 1               # rule 3c: nothing that changed meanwhile is deleted blind


def _unbased(world, cid, aid):
    """The link as a merge-in makes it (`_same_person`): no snapshot, hub_rev -1."""
    link = world.hub.link(cid, aid)
    world.hub.set_link(cid, aid, remote_id=link.remote_id, version_tag=link.version_tag, snapshot=None,
                       hub_rev=-1, state="synced")


def _stale_etag_delete(world, aid):
    from app.accounts.provider import VersionConflict

    provider = world.providers[aid]
    real_delete = provider.delete

    def delete(remote_id, version):
        if version != str(world.servers[aid].recs[remote_id].version):
            raise VersionConflict(remote_id)
        real_delete(remote_id, version)

    provider.delete = delete


def test_a_delete_of_a_merged_in_link_with_no_base_goes_through_when_the_account_holds_what_leasha_holds(world):
    """Six binned contacts stayed in Gmail for ever: joined by a merge-in they had no snapshot, Google's etag mask
    moved, and with no base the delete could never prove nothing had changed (the owner, 2026-10-13)."""
    g = world.account("Google")
    rid = world.servers[g].add(person("Ann", "Lee"))
    world.sync()
    cid = world.hub.list_rows()[0].id
    _unbased(world, cid, g)
    world.servers[g].recs[rid].version += 1               # same content, new etag
    _stale_etag_delete(world, g)
    world.hub.delete(cid)
    out = only(world.sync(), g)
    assert world.servers[g].live() == {} and out.status == "ok" and out.plan.delete_out == 1


def test_a_delete_with_no_base_is_held_and_named_when_the_account_holds_something_leasha_does_not(world):
    g = world.account("Google")
    rid = world.servers[g].add(person("Ann", "Lee"))
    world.sync()
    cid = world.hub.list_rows()[0].id
    _unbased(world, cid, g)
    changed = person("Ann", "Lee")
    changed.scalars["company"] = "Edited At The Account"
    world.servers[g].edit(rid, changed)
    _stale_etag_delete(world, g)
    world.hub.delete(cid)
    out = only(world.sync(), g)
    assert len(world.servers[g].live()) == 1               # rule 3c still holds
    assert out.status == "partial" and out.plan.failed_out == 1   # and the run says so, not "ok"


def test_the_delete_brake_stops_a_mass_delete_until_allowed(world):
    """9a.3 - D7.3."""
    g = world.account("Google")
    for i in range(30):
        world.servers[g].add(person(f"P{i}", "X", email=f"p{i}@x.com"))
    world.sync()
    ids = [r.id for r in world.hub.list_rows()]
    for cid in ids[:25]:
        world.hub.delete(cid)
    out = only(world.sync(), g)
    assert out.status == "held" and out.error.code == "ERR_SYNC_DELETE_BRAKE"
    assert len(world.servers[g].live()) == 30                             # nothing was deleted
    assert world.accounts.runs(account_id=g)[0].status == "held"
    out = only(world.sync(allow_brake=True), g)
    assert out.status == "ok" and len(world.servers[g].live()) == 5


def test_a_single_delete_in_a_tiny_book_is_not_a_mass_delete(world):
    g = world.account("Google")
    world.servers[g].add(person("A", "A"))
    world.servers[g].add(person("B", "B"))
    world.sync()
    world.hub.delete(world.hub.list_rows()[0].id)
    assert only(world.sync(), g).status == "ok"


def test_an_expired_token_does_a_full_pull_and_flags_what_vanished(world):
    """9a.4."""
    g = world.account("Google")
    r1 = world.servers[g].add(person("Ann", "Lee"))
    world.servers[g].add(person("Bob", "Ray"))
    world.sync()
    world.servers[g].recs.pop(r1)                                         # gone without a tombstone
    world.servers[g].seq += 1
    world.servers[g].expire_before = world.servers[g].seq + 1
    out = only(world.sync(), g)
    assert out.status == "ok" and out.plan.flagged_deleted == 1
    assert world.hub.counts()["deleted_elsewhere"] == 1


def test_a_crash_mid_pull_resumes_from_the_last_saved_page(world):
    """9a.4 - rule 4: the cost of a crash is the page in flight."""
    g = world.account("Google", page_size=2)
    for i in range(7):
        world.servers[g].add(person(f"P{i}", "X"))
    world.providers[g].crash_after_pages = 2
    out = only(world.sync(), g)
    assert out.status == "failed"
    assert world.hub.count() == 4                                         # two pages of two were kept
    token = world.accounts.state(g, "contacts").token
    assert token and "|" in token                                         # a resume point, not "done"
    world.providers[g].crash_after_pages = None
    out = only(world.sync(), g)
    assert out.status == "ok" and world.hub.count() == 7                  # no duplicates, nothing missed


def test_a_failing_account_never_stops_the_others(world):
    """9a.5 - a provider that keeps refusing (a 429 storm that outlasts the retries)."""
    bad, good = world.account("Bad"), world.account("Good")
    world.servers[good].add(person("Ann", "Lee"))
    world.providers[bad].fail_with = AppErrorException(
        make_error("ERR_PROVIDER_LIMIT", "test", account="Bad", reason="429 for 60 s"))
    out = world.sync()
    assert only(out, bad).status == "failed" and only(out, bad).error.code == "ERR_PROVIDER_LIMIT"
    assert only(out, good).status == "ok" and world.hub.count() == 1
    assert world.accounts.get(bad).last_error_code == "ERR_PROVIDER_LIMIT"
    assert world.accounts.state(bad).failures == 1
    world.providers[bad].fail_with = None
    assert only(world.sync(), bad).status == "ok"
    assert world.accounts.state(bad).failures == 0


def test_undo_of_a_whole_sync_puts_every_account_back(world):
    """9a.6 - A5."""
    g, m = world.account("Google"), world.account("Work")
    rid = world.servers[g].add(person("Ann", "Lee", phone="07700 900123"))
    world.sync()
    cid = world.hub.list_rows()[0].id
    world.hub.set_kept_in(cid, [g, m])
    world.sync()
    card = world.servers[g].recs[rid].card.copy()
    card.fields = [Field("phone", "07700 900999", "mobile")]
    world.servers[g].edit(rid, card)
    out = only(world.sync(), g)
    assert world.hub.get_card(cid).fields[0].value == "07700 900999"
    run_id = out.run_id
    res = world.hub.undo_sync(run_id)
    assert res.undone >= 1
    world.engine.push(res.accounts)
    for aid in (g, m):
        ann = list(world.servers[aid].live().values())[0]
        assert [f.value for f in ann.fields if f.kind == "phone"] == ["07700 900123"]
    assert world.hub.get_card(cid).fields[0].value == "07700 900123"


def test_undo_of_a_delete_restores_the_contact_in_its_accounts(world):
    g = world.account("Google")
    world.servers[g].add(person("Ann", "Lee"))
    world.sync()
    cid = world.hub.list_rows()[0].id
    world.hub.delete(cid)
    world.sync()
    assert world.servers[g].live() == {}
    entry = world.hub.history(cid)[0]
    res = world.hub.undo_entry(entry.id)
    world.engine.push(res.accounts)
    assert len(world.servers[g].live()) == 1 and world.hub.count() == 1


def test_a_contact_made_in_leasha_goes_to_the_accounts_it_is_kept_in(world):
    """A3."""
    g, m = world.account("Google"), world.account("Work")
    cid = world.hub.create(person("Nina", "Bell", email="nina@x.com"), kept_in=[g])
    world.sync()
    assert len(world.servers[g].live()) == 1 and world.servers[m].live() == {}
    world.hub.set_kept_in(cid, [g, m])
    world.sync()
    assert len(world.servers[m].live()) == 1


def test_removing_an_account_from_kept_in_needs_confirmation(world):
    g, m = world.account("Google"), world.account("Work")
    cid = world.hub.create(person("Nina", "Bell"), kept_in=[g, m])
    world.sync()
    res = world.hub.set_kept_in(cid, [g])
    assert res["unconfirmed"] == [m] and len(world.servers[m].live()) == 1
    world.hub.set_kept_in(cid, [g], confirmed_removals=True)
    world.sync()
    assert world.servers[m].live() == {} and len(world.servers[g].live()) == 1


def test_first_sync_is_a_dry_run_that_writes_nothing(world):
    """D8."""
    world.accounts.update_settings(dry_run_first=True)
    g = world.account("Google")
    world.servers[g].add(person("Ann", "Lee", email="ann@x.com"))
    world.hub.create(person("Ann", "Lee", email="ann@x.com"))             # the same person, already here
    # 2026-10-11: the same email and a matching name is the same person - joined, not a second copy
    world.hub.create(person("Only", "Local"), kept_in=[g])
    out = only(world.sync(), g)
    assert out.status == "needs-approval"
    assert out.plan.add_in == 0 and out.plan.merge_in == 1 and out.plan.create_out == 1
    assert world.hub.count() == 2                                         # hub untouched
    assert len(world.servers[g].live()) == 1 and world.servers[g].writes == []   # account untouched
    assert world.accounts.state(g).token is None                          # not even the token
    assert not world.accounts.get(g).first_sync_done
    run = world.accounts.runs(account_id=g)[0]
    assert run.dry_run and run.plan["merge_in"] == 1
    out = only(world.sync(approve_first=True), g)
    assert out.status == "ok" and world.hub.count() == 2
    assert world.accounts.get(g).first_sync_done
    assert len(world.servers[g].live()) == 2


def test_a_field_the_account_cannot_hold_is_kept_in_leasha(world):
    """D10 - a round trip through a poorer account never loses it."""
    poor = Capabilities(scalars=frozenset({"given", "family"}), kinds=frozenset({"email"}),
                        groups=False, favourite=False, photo=False)
    g = world.account("Poor", caps=poor)
    rid = world.servers[g].add(person("Ann", "Lee", email="ann@x.com"))
    world.sync()
    cid = world.hub.list_rows()[0].id
    card = world.hub.get_card(cid)
    card.scalars["company"] = "Acme"
    card.fields.append(Field("phone", "07700 900123", "mobile"))
    world.hub.update(cid, card)
    world.sync()
    remote = world.servers[g].live()[rid]
    assert remote.get("company") == "" and not remote.values("phone")      # never sent
    rc = remote.copy()
    rc.fields.append(Field("email", "ann2@x.com", "home"))
    world.servers[g].edit(rid, rc)
    world.sync()
    kept = world.hub.get_card(cid)
    assert kept.get("company") == "Acme" and kept.values("phone")          # not removed by the round trip
    assert {f.value for f in kept.values("email")} == {"ann@x.com", "ann2@x.com"}


def test_a_write_that_loses_a_race_is_re_read_re_merged_and_retried(world):
    """3c."""
    g = world.account("Google")
    rid = world.servers[g].add(person("Ann", "Lee", company="A"))
    world.sync()
    cid = world.hub.list_rows()[0].id
    card = world.hub.get_card(cid)
    card.scalars["title"] = "CEO"
    world.hub.update(cid, card)

    def someone_else(remote_id):
        rc = world.servers[g].recs[remote_id].card.copy()
        rc.scalars["department"] = "Sales"
        world.servers[g].edit(remote_id, rc)
    world.providers[g].before_update = someone_else
    out = only(world.sync(), g)
    assert out.status == "ok"
    final = world.servers[g].live()[rid]
    assert final.get("title") == "CEO" and final.get("department") == "Sales"   # nothing overwritten blind
    assert world.hub.get_card(cid).get("department") == "Sales"


def test_into_leasha_only_never_writes_to_the_account(world):
    """10r / A10."""
    w = world.account("Work", direction="in-only")
    rid = world.servers[w].add(person("Ann", "Lee", company="Corp"))
    world.sync()
    cid = world.hub.list_rows()[0].id
    card = world.hub.get_card(cid)
    card.scalars["company"] = "Mine"
    world.hub.update(cid, card)
    world.sync()
    assert world.servers[w].writes == []
    assert world.servers[w].live()[rid].get("company") == "Corp"
    assert world.hub.get_card(cid).get("company") == "Mine"               # the edit stays in Leasha


@pytest.mark.slow
def test_a_sync_against_a_two_thousand_contact_account_is_fast_enough_to_measure(world):
    """3f - measured, not assumed. The number is printed with -s and recorded in the order."""
    import time as _t
    g = world.account("Big", page_size=200)
    for i in range(2000):
        world.servers[g].add(person(f"P{i}", f"S{i}", email=f"p{i}@x.com", phone=f"0770090{i:04d}"))
    t0 = _t.perf_counter()
    out = only(world.sync(), g)
    elapsed = _t.perf_counter() - t0
    print(f"first sync of 2,000 contacts (fake provider, no network): {elapsed:.2f} s")
    assert out.status == "ok" and world.hub.count() == 2000
    assert elapsed < 60


def test_the_secondary_account_does_not_get_a_contact_it_is_not_kept_in(world):
    g, m = world.account("Google"), world.account("Work")
    world.servers[g].add(person("Ann", "Lee"))
    world.sync()
    assert world.servers[m].live() == {}                                  # D4: nothing copies everywhere
