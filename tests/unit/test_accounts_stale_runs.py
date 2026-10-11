"""Order 1k: a sync the last session never finished does not say "Running" for ever.

Layer: L2

Seen on the owner's index (2026-10-11): two mail runs left as running after the app closed. The
newest run of an account and kind is left alone, because it may really be in progress.
"""

from __future__ import annotations

import pytest

pytest.importorskip("keyring")

from app.accounts.store import AccountStore                    # noqa: E402
from app.storage.sqlite_store import SqliteStore               # noqa: E402


@pytest.fixture()
def accounts(tmp_path):
    store = SqliteStore(tmp_path / "runs.db").connect()
    yield AccountStore(store)
    store.close()


def test_an_older_run_still_marked_running_is_marked_stopped(accounts):
    acct = accounts.get(accounts.add("google", "g@x.com", "G"))
    old = accounts.start_run(acct, "mail", "sync-now")
    newer = accounts.start_run(acct, "mail", "sync-now")
    accounts.finish_run(newer, "ok")
    assert accounts.close_stale_runs() == 1
    assert accounts.run(old).status == "interrupted" and accounts.run(old).finished_at
    assert accounts.run(newer).status == "ok"


def test_the_newest_run_and_other_kinds_are_left_alone(accounts):
    acct = accounts.get(accounts.add("google", "g@x.com", "G"))
    other = accounts.get(accounts.add("microsoft", "m@x.com", "M"))
    mail = accounts.start_run(acct, "mail", "sync-now")
    contacts = accounts.start_run(acct, "contacts", "sync-now")      # another kind: not a replacement
    theirs = accounts.start_run(other, "mail", "sync-now")           # another account
    assert accounts.close_stale_runs() == 0
    assert {accounts.run(i).status for i in (mail, contacts, theirs)} == {"running"}


def test_a_late_finish_still_overwrites_the_stopped_label(accounts):
    acct = accounts.get(accounts.add("google", "g@x.com", "G"))
    old = accounts.start_run(acct, "mail", "sync-now")
    accounts.start_run(acct, "mail", "sync-now")
    accounts.close_stale_runs()
    accounts.finish_run(old, "ok", added=3)
    assert accounts.run(old).status == "ok" and accounts.run(old).added == 3


def test_building_the_service_closes_stale_runs(tmp_path):
    from app.accounts.secrets import SecretStore
    from app.accounts.service import AccountService
    from tests.unit.accounts_ui_fakes import MemoryKeyring

    store = SqliteStore(tmp_path / "svc.db").connect()
    try:
        accts = AccountStore(store)
        acct = accts.get(accts.add("google", "g@x.com", "G"))
        old = accts.start_run(acct, "mail", "sync-now")
        accts.start_run(acct, "mail", "sync-now")
        AccountService(store, secrets=SecretStore(backend=MemoryKeyring()))
        assert accts.run(old).status == "interrupted"
    finally:
        store.close()
