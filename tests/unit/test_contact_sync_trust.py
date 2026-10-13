"""Order 1t: a contact sync that can be trusted - it settles, it holds only deletes, it survives the app going away,
it names what was refused, it checks itself, and it does not spread a server's rewrite to every account.

Layer: L7

Each test is one sentence of the order, against the fake account server (`accounts_fakes`). The Apple scenario
(`test_an_icloud_round_trip_settles_*`) is the owner's own failure of 2026-10-12: run 485 recorded 4,072
conflicts on a 1,445-contact book, because iCloud returned every number un-preferred and every card carrying
its own `X-FC-CLUSTER-ID`.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest

from app.accounts.engine import ContactSyncEngine, estimate_requests
from app.accounts.provider import RemoteContact
from app.accounts.store import AccountStore
from app.contacts import vcard
from app.contacts.card import ALL_CAPS, Card, Field
from app.contacts.hub import ContactHub
from app.storage.sqlite_store import SqliteStore

from tests.unit.accounts_fakes import BatchFakeProvider, FakeProvider, FakeServer, person


class World:
    def __init__(self, tmp_path, *, parallel: bool = True):
        self.store = SqliteStore(tmp_path / "k.db").connect()
        self.hub = ContactHub(self.store)
        self.accounts = AccountStore(self.store)
        self.accounts.update_settings(dry_run_first=False)
        self.servers: dict[int, FakeServer] = {}
        self.providers: dict[int, FakeProvider] = {}
        self.engine = ContactSyncEngine(self.store, self.hub, self.accounts, lambda a: self.providers[a.id],
                                        parallel=parallel)

    def account(self, name: str, provider_cls=FakeProvider, **kw) -> int:
        aid = self.accounts.add("google", f"{name.lower()}@example.com", name)
        self.servers[aid] = FakeServer()
        self.providers[aid] = provider_cls(self.servers[aid], **kw)
        return aid

    def sync(self, ids=None, **kw):
        return {o.account_id: o for o in self.engine.sync(ids, **kw)}


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.store.close()


# ------------------------------------------------------------------------------------------- settles (4a, 4b)
class ICloudLike(FakeProvider):
    """Stores what it is sent as a vCard the way iCloud does: the preferred flag dropped and its own
    `X-FC-CLUSTER-ID` added; reads it back by parsing that vCard."""

    def __init__(self, server, **kw):
        super().__init__(server, caps=replace(ALL_CAPS, primary=False, photo=False), **kw)

    @staticmethod
    def _as_stored(card: Card) -> Card:
        text = vcard.dumps(card, version="3.0", uid="u").replace(";TYPE=CELL,PREF", ";TYPE=CELL").replace(
            ",PREF", "").replace(";TYPE=PREF", "")
        text = text.replace("END:VCARD", "X-FC-CLUSTER-ID:dde6b742-13d6-11f1-82a4-0e54131859a9\r\nEND:VCARD")
        return vcard.parse_one(text)

    def create(self, card):
        return super().create(self._as_stored(card))

    def update(self, remote_id, version, card):
        return super().update(remote_id, version, self._as_stored(card))


def test_an_icloud_round_trip_settles_and_a_second_sync_writes_nothing(world):
    home = world.account("Home")
    apple = world.account("Apple", ICloudLike)
    c = person("Ann", "Lee", email="ann@x.com", phone="07700 900123")
    c.fields[1].primary = True
    world.servers[home].add(c)
    world.servers[home].add(person("Bob", "Ray", phone="07700 900456"))
    world.hub.mirror_to([home, apple])
    world.accounts.update_settings(mirror_contacts=True)
    world.sync()
    world.sync()
    writes_before = len(world.servers[apple].writes) + len(world.servers[home].writes)
    third = world.sync()
    assert len(world.servers[apple].writes) + len(world.servers[home].writes) == writes_before
    assert third[apple].plan.conflicts == 0 and third[apple].plan.update_out == 0
    assert third[home].plan.update_out == 0
    for _cid, card in world.hub.all_cards():
        assert not any(f.kind == "custom" for f in card.fields), "a server's bookkeeping became a contact value"


def test_a_server_bookkeeping_property_is_not_a_custom_value_but_a_persons_is():
    card = vcard.parse_one("BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Ann Lee\r\nN:Lee;Ann;;;\r\n"
                           "X-FC-CLUSTER-ID:abc\r\nX-ABUID:1\r\nX-APPLE-SUBLOCALITY:x\r\n"
                           "X-PHONETIC-FIRST-NAME:An\r\nX-SHOE-SIZE:9\r\nEND:VCARD\r\n")
    labels = {f.label for f in card.fields if f.kind == "custom"}
    assert labels == {"phonetic first name", "shoe size"}


def test_the_preferred_flag_is_not_compared_where_the_account_does_not_keep_it():
    from app.contacts.card import merge, units
    caps = replace(ALL_CAPS, primary=False)
    local = person("Ann", phone="07700 900123")
    local.fields[0].primary = True
    remote = person("Ann", phone="07700 900123")
    assert units(local, caps) == units(remote, caps)
    res = merge(remote, local, remote, caps)
    assert not res.conflicts and res.card.fields[0].primary, "Leasha's own preferred flag stays"


def test_the_book_is_cleared_of_bookkeeping_values_once_and_the_undo_is_one_batch(world):
    aid = world.account("Home")
    card = person("Ann", "Lee", email="ann@x.com")
    card.fields.append(Field("custom", "dde6b742", "fc cluster id"))
    card.fields.append(Field("custom", "9", "shoe size"))
    cid = world.hub.create(card)
    world.sync()
    kept = world.hub.get_card(cid)
    assert [f.label for f in kept.fields if f.kind == "custom"] == ["shoe size"]
    batches = {r[0] for r in world.store.conn.execute(
        "SELECT batch_id FROM contact_journal WHERE actor = 'repair'")}
    assert len(batches) == 1 and next(iter(batches)).startswith("repair-vendor-fields-")
    card2 = world.hub.get_card(cid)
    card2.fields.append(Field("custom", "x", "fc cluster id"))
    world.hub.update(cid, card2)
    world.sync()
    assert any(f.label == "fc cluster id" for f in world.hub.get_card(cid).fields), "it runs once, not every sync"
    assert aid


# ------------------------------------------------------------------------------------------------ brake (D2)
def test_the_brake_holds_only_the_deletes_and_everything_else_is_written(world):
    aid = world.account("Home")
    for i in range(10):
        world.servers[aid].add(person(f"P{i}", email=f"p{i}@x.com"))
    world.sync()
    world.accounts.update_settings(brake_count=2, brake_percent=100.0)
    rows = world.hub.list_rows()
    for r in rows[:4]:
        world.hub.delete(r.id)
    for r in rows[4:]:
        card = world.hub.get_card(r.id)
        card.scalars["company"] = "Acme"
        world.hub.update(r.id, card)
    out = world.sync()[aid]
    assert out.status == "held" and out.error.code == "ERR_SYNC_DELETE_BRAKE"
    assert out.plan.update_out == 6 and out.plan.held_deletes == 4 and out.plan.delete_out == 0
    assert sum(1 for c in world.servers[aid].live().values() if c.get("company") == "Acme") == 6
    assert len(world.servers[aid].live()) == 10
    allowed = world.sync(allow_brake=True)[aid]
    assert allowed.status == "ok" and allowed.plan.delete_out == 4 and len(world.servers[aid].live()) == 6


# ------------------------------------------------------------------------------------------- bin race (1t)
def test_a_contact_binned_while_its_write_is_in_flight_stays_on_its_way_out(world):
    aid = world.account("Home")
    rid = world.servers[aid].add(person("Ann", email="ann@x.com"))
    world.sync()
    cid = world.hub.list_rows()[0].id
    card = world.hub.get_card(cid)
    card.scalars["company"] = "Acme"
    world.hub.update(cid, card)
    world.providers[aid].before_update = lambda _rid: world.hub.delete(cid)   # binned mid-write
    world.sync()
    link = world.hub.link(cid, aid)
    assert link is None or link.state == "pending_remove"
    world.sync()
    assert rid not in world.servers[aid].live(), "the binned contact left the account"


def test_a_binned_contact_still_marked_synced_is_removed_at_the_next_push(world):
    aid = world.account("Home")
    rid = world.servers[aid].add(person("Ann", email="ann@x.com"))
    world.sync()
    cid = world.hub.list_rows()[0].id
    world.hub.delete(cid)
    with world.store.write() as conn:                       # what the old race left behind
        conn.execute("UPDATE contact_links SET state = 'synced' WHERE contact_id = ?", (cid,))
    world.sync()
    assert rid not in world.servers[aid].live()


# ---------------------------------------------------------------------------------------- progress (2a, 2b, 7f)
def test_a_run_writes_its_counts_as_it_goes(world):
    aid = world.account("Home")
    for i in range(6):
        world.servers[aid].add(person(f"P{i}", email=f"p{i}@x.com"))
    world.sync()
    for r in world.hub.list_rows():
        card = world.hub.get_card(r.id)
        card.scalars["company"] = "Acme"
        world.hub.update(r.id, card)
    seen = []
    original = world.providers[aid].update

    def spy(rid, version, card):
        run = world.accounts.runs(account_id=aid, limit=1)[0]
        seen.append((run.status, (run.plan or {}).get("update_out", 0), (run.plan or {}).get("beat")))
        return original(rid, version, card)

    world.providers[aid].update = spy
    world.engine._save_ctx.__func__                         # noqa: B018 - the method exists
    import app.accounts.engine as eng
    saved = eng.ContactSyncEngine._save_ctx

    def always(self, ctx, *, conn=None, force=False):
        return saved(self, ctx, conn=conn, force=True)

    eng.ContactSyncEngine._save_ctx = always
    try:
        world.sync()
    finally:
        eng.ContactSyncEngine._save_ctx = saved
    assert seen[-1][0] == "running" and seen[-1][1] >= 4 and seen[-1][2]


def test_a_crash_mid_push_leaves_the_counts_and_the_next_sync_writes_only_the_rest(world):
    aid = world.account("Home")
    for i in range(6):
        world.servers[aid].add(person(f"P{i}", email=f"p{i}@x.com"))
    world.sync()
    for r in world.hub.list_rows():
        card = world.hub.get_card(r.id)
        card.scalars["company"] = "Acme"
        world.hub.update(r.id, card)
    original = world.providers[aid].update
    calls = {"n": 0}

    def dies(rid, version, card):
        calls["n"] += 1
        if calls["n"] == 4:
            raise RuntimeError("the app went away")
        return original(rid, version, card)

    world.providers[aid].update = dies
    out = world.sync()[aid]
    assert out.status == "failed"
    run = world.accounts.run(out.run_id)
    assert run.changed >= 3 or (run.plan or {}).get("update_out", 0) >= 3
    world.providers[aid].update = original
    before = len(world.servers[aid].writes)
    world.sync()
    assert len(world.servers[aid].writes) - before == 3


def test_a_running_run_whose_heartbeat_stopped_long_ago_is_closed_with_its_counts(world):
    acct = world.accounts.get(world.account("Home"))
    rid = world.accounts.start_run(acct, "contacts", "sync-now")
    world.accounts.save_progress(rid, {"update_out": 7}, changed=7)
    with world.store.write() as conn:
        plan = json.loads(conn.execute("SELECT plan FROM sync_runs WHERE id = ?", (rid,)).fetchone()[0])
        plan["beat"] = int(time.time()) - 3 * 3600
        conn.execute("UPDATE sync_runs SET plan = ? WHERE id = ?", (json.dumps(plan), rid))
    assert world.accounts.close_stale_runs() == 1
    run = world.accounts.run(rid)
    assert run.status == "interrupted" and run.changed == 7


def test_a_live_run_with_a_fresh_heartbeat_is_left_alone(world):
    acct = world.accounts.get(world.account("Home"))
    rid = world.accounts.start_run(acct, "contacts", "sync-now")
    world.accounts.save_progress(rid, {"update_out": 1}, changed=1)
    assert world.accounts.close_stale_runs() == 0 and world.accounts.run(rid).status == "running"


# ------------------------------------------------------------------------------------------ refused (2c)
def test_a_refused_contact_is_listed_with_its_reason_and_leaves_the_list_once_written(world):
    aid = world.account("Home", BatchFakeProvider)
    for i in range(4):
        world.servers[aid].add(person(f"P{i}", email=f"p{i}@x.com"))
    world.sync()
    rows = world.hub.list_rows()
    for r in rows:
        card = world.hub.get_card(r.id)
        card.scalars["company"] = "Acme"
        world.hub.update(r.id, card)
    bad = world.hub.link(rows[0].id, aid).remote_id
    world.providers[aid].refuse = {bad}
    out = world.sync()[aid]
    assert out.status == "partial" and out.plan.update_out == 3
    failures = world.accounts.failures(aid)
    assert [f["contact_id"] for f in failures] == [rows[0].id] and "refused" in failures[0]["reason"]
    world.providers[aid].refuse = set()
    world.sync()
    assert world.accounts.failures(aid) == []


# --------------------------------------------------------------------------------------------- batches (5b)
def test_writes_go_a_batch_at_a_time_and_a_race_inside_a_batch_is_retried_on_its_own(world):
    aid = world.account("Home", BatchFakeProvider)
    for i in range(7):
        world.servers[aid].add(person(f"P{i}", email=f"p{i}@x.com"))
    world.sync()
    rows = world.hub.list_rows()
    for r in rows:
        card = world.hub.get_card(r.id)
        card.scalars["company"] = "Acme"
        world.hub.update(r.id, card)
    racer = world.hub.link(rows[2].id, aid).remote_id
    world.providers[aid].race = {racer}
    world.providers[aid].batch_calls.clear()
    out = world.sync()[aid]
    assert out.status == "ok" and out.plan.update_out == 7
    assert [c for c in world.providers[aid].batch_calls if c[0] == "update"] == [("update", 3), ("update", 3),
                                                                                  ("update", 1)]
    assert all(c.get("company") == "Acme" for c in world.servers[aid].live().values())


def test_new_contacts_are_created_in_batches_and_linked(world):
    aid = world.account("Home", BatchFakeProvider)
    for i in range(5):
        world.hub.create(person(f"N{i}", email=f"n{i}@x.com"), kept_in=[aid])
    out = world.sync()[aid]
    assert out.plan.create_out == 5 and len(world.servers[aid].live()) == 5
    assert ("create", 3) in world.providers[aid].batch_calls
    assert all(world.hub.link(r.id, aid).state == "synced" for r in world.hub.list_rows())


def test_the_estimate_counts_requests_one_at_a_time_and_batched():
    class P:
        batch_size = 200
        batch_cost = {"create": 1, "update": 1, "delete": 2}

        def update_many(self, items):                     # pragma: no cover - only its presence matters
            return []
    est = estimate_requests(P(), 58, 1099, 45)
    assert est["one_by_one"] == 2 * (58 + 1099 + 45) and est["batched"] == 1 + 6 + 2


# ------------------------------------------------------------------------------------------- check (3c, D4)
def test_after_a_sync_the_account_is_checked_and_found_in_step(world):
    aid = world.account("Home")
    for i in range(3):
        world.servers[aid].add(person(f"P{i}", email=f"p{i}@x.com"))
    out = world.sync()[aid]
    assert out.check["in_step"] and out.check["count"] == 3
    assert world.accounts.check(aid)["in_step"]


def test_the_check_names_what_differs(world):
    aid = world.account("Home")
    ann = world.servers[aid].add(person("Ann", "Lee", email="ann@x.com"))
    world.servers[aid].add(person("Bob", "Ray", email="bob@x.com"))
    world.sync()
    world.servers[aid].recs.pop(ann)                         # gone without the change log saying so
    world.servers[aid].add(person("Cy", "Dee"))              # arrived after the read
    found = world.engine.check(world.accounts.get(aid), world.providers[aid])
    assert not found["in_step"]
    assert found["missing_there"] == ["Ann Lee"] and found["not_here"] == ["Cy Dee"]


def test_the_check_can_be_turned_off(world):
    aid = world.account("Home")
    world.accounts.update_settings(check_after_sync=False)
    world.servers[aid].add(person("Ann", email="ann@x.com"))
    assert world.sync()[aid].check is None


# ------------------------------------------------------------------------------------------- churn guard (4c)
def test_an_account_returning_most_contacts_changed_the_same_way_is_held_and_nothing_spreads(world):
    home, other = world.account("Home"), world.account("Other")
    for i in range(60):
        world.servers[home].add(person(f"P{i}", email=f"p{i}@x.com"))
    world.accounts.update_settings(mirror_contacts=True)
    world.sync()
    world.sync()
    writes = len(world.servers[other].writes)
    for rid, rec in list(world.servers[home].recs.items()):
        card = rec.card.copy()
        card.fields.append(Field("custom", "zzz", "server tag"))
        world.servers[home].edit(rid, card)
    out = world.sync()
    assert out[home].status == "held" and out[home].error.code == "ERR_SYNC_CHANGES_HELD"
    assert "server tag" in out[home].error.message
    assert len(world.servers[other].writes) == writes, "nothing was written to any account"
    allowed = world.sync(allow_brake=True)
    assert allowed[other].status == "ok"


# ------------------------------------------------------------------------------------------- one count (3a)
def test_every_account_has_one_set_of_numbers(world):
    aid = world.account("Home")
    world.servers[aid].add(person("Ann", email="ann@x.com"))
    world.servers[aid].add(person("Bob", email="bob@x.com"))
    world.sync()
    world.hub.create(person("Cy", email="cy@x.com"), kept_in=[aid])
    world.hub.create(person("Di", email="di@x.com"))
    world.hub.delete(world.hub.list_rows()[0].id)
    counts = world.hub.account_counts()[aid]
    assert counts == {"kept": 2, "linked": 1, "waiting_create": 1, "waiting_delete": 1, "deleted_there": 0,
                      "not_kept": 1}


def test_no_screen_counts_links_with_its_own_sql():
    """The two queries that disagreed (Settings > Accounts and the Contacts list) are gone; every screen reads
    `ContactHub.account_counts`."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "app" / "ui"
    offenders = [str(p) for p in root.rglob("*.py")
                 if "COUNT(" in (text := p.read_text(encoding="utf-8")) and "contact_links" in text]
    assert offenders == []


# ------------------------------------------------------------------------------------------ parallel (5g)
def test_two_accounts_are_written_at_the_same_time(world):
    a, b = world.account("A"), world.account("B")
    world.accounts.update_settings(mirror_contacts=True)
    world.servers[a].add(person("Ann", email="ann@x.com"))
    world.servers[b].add(person("Bob", email="bob@x.com"))
    threads = set()
    import threading
    for aid in (a, b):
        original = world.providers[aid].create

        def spy(card, _o=original):
            threads.add(threading.current_thread().name)
            return _o(card)
        world.providers[aid].create = spy
    world.sync()
    assert len(world.servers[a].live()) == 2 and len(world.servers[b].live()) == 2
    assert all(t.startswith("contacts-push") for t in threads) and len(threads) == 2


def test_remote_contact_without_version_is_harmless_to_the_check(world):
    aid = world.account("Home")
    world.providers[aid].list_ids = lambda: {"r9": ""}
    found = world.engine.check(world.accounts.get(aid), world.providers[aid])
    assert found["not_here_n"] == 1 and RemoteContact


@pytest.mark.slow
def test_the_owners_upgrade_does_not_trip_the_guard_and_cleans_google_once(tmp_path):
    """The book as it stood on 2026-10-12: every card, and every account's base, carries "fc cluster id". The first
    sync with this build must not read that as a mass change, must take it off Google (which shows it), and must not
    rewrite the CardDAV server (which adds it itself)."""
    w = World(tmp_path)
    try:
        g = w.account("Google")
        apple = w.accounts.add("apple", "me@icloud.com", "Apple")
        w.servers[apple] = FakeServer()
        w.providers[apple] = FakeProvider(w.servers[apple])
        w.store.set_state(ContactSyncEngine._REPAIR_KEY, "1")          # the old build: no repair yet
        for i in range(60):
            card = person(f"P{i}", email=f"p{i}@x.com")
            card.fields.append(Field("custom", f"id-{i}", "fc cluster id"))
            w.servers[g].add(card)
            w.servers[apple].add(card.copy())
        w.sync()
        with w.store.write() as conn:                                    # bases as the old build wrote them
            for r in conn.execute("SELECT id, snapshot FROM contact_links").fetchall():
                snap = json.loads(r["snapshot"])
                snap["fields"].append({"kind": "custom", "label": "fc cluster id", "value": "old"})
                conn.execute("UPDATE contact_links SET snapshot = ? WHERE id = ?", (json.dumps(snap), r["id"]))
        w.store.delete_state(ContactSyncEngine._REPAIR_KEY)              # this build's first sync
        before_apple = len(w.servers[apple].writes)
        out = w.sync()
        assert out[g].status == "ok" and out[apple].status == "ok", (out[g].error, out[apple].error)
        assert not any(f.label == "fc cluster id" for c in w.servers[g].live().values() for f in c.fields)
        assert len(w.servers[apple].writes) == before_apple
        writes = len(w.servers[g].writes)
        w.sync()
        assert len(w.servers[g].writes) == writes, "settled: the next sync writes nothing"
    finally:
        w.store.close()


# ------------------------------------------------------------------------- review of #70 (CodeRabbit)
@pytest.mark.slow
def test_a_churn_hold_stands_on_the_next_sync_until_allow(world):
    home, other = world.account("Home"), world.account("Other")
    for i in range(60):
        world.servers[home].add(person(f"P{i}", email=f"p{i}@x.com"))
    world.accounts.update_settings(mirror_contacts=True)
    world.sync()
    world.sync()
    for rid, rec in list(world.servers[home].recs.items()):
        card = rec.card.copy()
        card.fields.append(Field("custom", "zzz", "server tag"))
        world.servers[home].edit(rid, card)
    world.sync()
    writes = len(world.servers[other].writes)
    again = world.sync()                                     # nothing new to pull: the hold must still stand
    assert again[other].status == "held" and again[other].error.code == "ERR_SYNC_CHANGES_HELD"
    assert len(world.servers[other].writes) == writes
    world.sync(allow_brake=True)
    assert len(world.servers[other].writes) > writes
    assert world.sync()[other].status == "ok", "Allow clears the hold"


def test_a_dry_run_writes_nothing_even_to_heal_a_link(world):
    aid = world.account("Home")
    world.servers[aid].add(person("Ann", email="ann@x.com"))
    world.sync()
    cid = world.hub.list_rows()[0].id
    world.hub.delete(cid)
    with world.store.write() as conn:
        conn.execute("UPDATE contact_links SET state = 'synced' WHERE contact_id = ?", (cid,))
    out = world.sync(dry_run=True)[aid]
    assert out.plan.delete_out == 1
    assert world.hub.link(cid, aid).state == "synced", "the dry run changed nothing"


def test_giving_up_inside_a_batch_keeps_what_the_account_already_took(world):
    aid = world.account("Home", BatchFakeProvider)
    world.providers[aid].batch_size = 40
    for i in range(20):
        world.hub.create(person(f"N{i:02d}", email=f"n{i}@x.com"), kept_in=[aid])
    world.providers[aid].refuse = {f"new:N{i:02d}" for i in range(2, 20)}
    out = world.sync()[aid]
    assert out.status == "failed"
    written = [r for r in world.hub.list_rows() if world.hub.link(r.id, aid).state == "synced"]
    assert len(written) == 2 and len(world.servers[aid].live()) == 2
    # 15 in a row, then the one that ends the run (not listed, as before), then the last two - still listed
    assert len(world.accounts.failures(aid)) == 17, "refusals after the give-up are listed too"
    world.providers[aid].refuse = set()
    world.sync()
    assert len(world.servers[aid].live()) == 20, "no contact was created twice"


def test_a_persons_own_labels_are_never_taken_for_server_bookkeeping():
    from app.contacts.card import is_server_internal
    for mine in ("about", "about me", "abbreviation", "sync notes", "image url", "wrist size", "showing"):
        assert not is_server_internal(mine), mine
    for theirs in ("fc cluster id", "abadr", "abshowas", "apple ol mapping info", "evolution file as",
                   "X-FC-CLUSTER-ID", "X-ABUID"):
        assert is_server_internal(theirs), theirs
