"""Order 1k: the contact book is copied before it can change a lot, and an account can be checked.

Layer: L7
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from app.contacts import backup
from app.accounts.service import AccountService
from app.accounts.secrets import SecretStore
from tests.unit.accounts_fakes import person
from tests.unit.accounts_ui_fakes import MemoryKeyring
from tests.unit.test_contact_sync_engine import World


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


def _service(world):
    svc = AccountService(world.store, secrets=SecretStore(backend=MemoryKeyring()),
                         provider_for=lambda acct: world.providers[acct.id])
    return svc


def test_a_backup_is_a_vcard_file_and_the_contact_tables(world, tmp_path):
    g = world.account("Google")
    world.servers[g].add(person("Ann", "Lee", email="ann@x.example"))
    world.sync()
    out = backup.write_backup(world.store, world.hub, tmp_path / "b", reason="Manual test!")
    assert out.name.endswith("-manual-test.db") and out.with_suffix(".vcf").is_file()
    assert "Ann" in out.with_suffix(".vcf").read_text(encoding="utf-8")
    con = sqlite3.connect(out)
    try:
        assert con.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM contact_links").fetchone()[0] == 1       # who is linked to which account
    finally:
        con.close()


@pytest.mark.slow
def test_only_the_newest_copies_are_kept(world, tmp_path):
    folder = tmp_path / "b"
    for n in range(5):
        backup.write_backup(world.store, world.hub, folder, reason="t", keep=3, now=1_800_000_000 + n * 10)
    assert len([p for p in backup.backups(folder) if p.suffix == ".db"]) == 3
    assert len([p for p in backup.backups(folder) if p.suffix == ".vcf"]) == 3
    assert backup.newest_age_s(folder, now=1_800_000_100) is not None


def test_a_sync_writes_a_copy_before_the_first_sync_and_not_again_the_same_day(world):
    g = world.account("Google")
    world.servers[g].add(person("Ann", "Lee"))
    svc = _service(world)
    assert backup.backups(svc.backup_folder()) == []
    svc.sync_now(g, "contacts")
    first = backup.backups(svc.backup_folder())
    assert first and any("first-sync" in p.name for p in first)
    svc.sync_now(g, "contacts")
    assert backup.backups(svc.backup_folder()) == first                      # not twice in a day


def test_switching_the_backup_off_writes_nothing(world):
    g = world.account("Google")
    world.accounts.update_settings(backup_contacts=False)
    svc = _service(world)
    svc.sync_now(g, "contacts")
    assert backup.backups(svc.backup_folder()) == []


def test_verify_says_in_sync_after_a_sync_and_names_what_differs(world):
    g = world.account("Google")
    rid = world.servers[g].add(person("Ann", "Lee", email="ann@x.example"))
    world.servers[g].add(person("Bob", "Bell"))
    world.sync()
    svc = _service(world)
    acct = world.accounts.get(g)
    assert svc.verify_contacts(acct).in_sync
    world.servers[g].add(person("Cy", "Cox"))                                # new at the account
    world.servers[g].remove(rid)                                              # gone at the account
    report = svc.verify_contacts(acct)
    assert not report.in_sync and report.missing_here == ["Cy Cox"] and report.missing_there == ["Ann Lee"]
    assert "Cy Cox" not in str([r.display_name for r in world.hub.list_rows()])   # and it wrote nothing
    assert "not yet here" in report.summary() and "no longer at the account" in report.summary()


def test_verify_counts_a_local_change_that_is_waiting_to_be_written(world):
    g = world.account("Google")
    world.servers[g].add(person("Ann", "Lee"))
    world.sync()
    cid = world.hub.list_rows()[0].id
    card = world.hub.get_card(cid)
    card.scalars["notes"] = "edited here"
    world.hub.update(cid, card)
    assert _service(world).verify_contacts(world.accounts.get(g)).waiting_to_write == 1


def test_verify_counts_a_binned_contact_still_at_the_account_as_a_delete_waiting_not_missing_here(world):
    """Verify said "at the account, not yet here" for contacts binned here whose delete had not been written."""
    g = world.account("Google")
    world.servers[g].add(person("Ann", "Lee"))
    world.sync()
    world.hub.delete(world.hub.list_rows()[0].id)
    report = _service(world).verify_contacts(world.accounts.get(g))
    assert report.missing_here == [] and report.waiting_to_write == 1
    assert "not yet here" not in report.summary()


def test_removing_the_master_account_keeps_its_contacts_and_clears_the_master(world):
    g, m = world.account("Google"), world.account("Work")
    world.servers[g].add(person("Ann", "Lee", email="ann@x.example"))
    world.accounts.update_settings(master_account_id=g)
    world.sync()
    svc = _service(world)
    svc.remove_account(g)                                           # the default: contacts are kept here
    assert world.accounts.settings().master_account_id is None
    assert world.hub.count() == 1 and world.hub.list_rows()[0].accounts == []


def test_merge_safe_shows_the_groups_first_and_only_merges_with_yes(world, capsys):
    import argparse

    from app.cli.contacts import _dispatch

    world.hub.create(person("Ann", "Lee", email="ann@x.example"))
    world.hub.create(person("A.", "Lee", email="ANN@x.example"))
    world.hub.create(person("Alaa", "Arawi", phone="020 7946 0018"))          # an office landline ...
    world.hub.create(person("Stephen", "Tagg", phone="020 7946 0018"))        # ... shared: never a duplicate
    svc = _service(world)
    say = lambda yes: _dispatch(argparse.Namespace(contacts_action="merge-safe", yes=yes, json=False), svc)
    assert say(False) == 0
    shown = capsys.readouterr().out
    assert "1 groups of contacts are certainly one person" in shown and "Nothing was changed" in shown
    assert "Alaa" not in shown and world.hub.count() == 4
    assert say(True) == 0
    assert "Merged 1 groups" in capsys.readouterr().out
    assert world.hub.count() == 3 and backup.backups(svc.backup_folder())          # and a copy was written first
