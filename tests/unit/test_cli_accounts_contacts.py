"""Order 1k 1g, 3h, 10: `app.cli accounts ...` and `app.cli contacts ...`.

Layer: L7
"""

from __future__ import annotations

import io
import json

import keyring
import pytest
from keyring.backend import KeyringBackend

from app import cli
from app.accounts.service import AccountService
from tests.unit.accounts_fakes import FakeProvider, FakeServer, person
from tests.unit.test_cli_wiring import env_file


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.data = {}

    def set_password(self, service, username, password):
        self.data[(service, username)] = password

    def get_password(self, service, username):
        return self.data.get((service, username))

    def delete_password(self, service, username):
        self.data.pop((service, username), None)


@pytest.fixture
def world(tmp_path, monkeypatch):
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    servers: dict[str, FakeServer] = {}

    def provider(self, account):
        server = servers.setdefault(account.address, FakeServer())
        return FakeProvider(server)
    monkeypatch.setattr(AccountService, "contact_provider", provider)
    # the engine captured the bound method at construction, so patch the class before it is built
    env = env_file(tmp_path)

    def run(*argv, stdin=None):
        if stdin is not None:
            monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        return cli.main(["--env", env, *argv])
    run.servers = servers
    run.backend = backend
    return run


def test_add_list_status_and_remove(world, capsys):
    assert world("accounts", "add", "carddav", "--address", "me@fast.com", "--name", "Fast",
                 "--server", "carddav.fast.com", "--password-stdin", stdin="s3cret-pw\n") == 0
    assert world("accounts", "list", "--json") == 0
    rows = json.loads(capsys.readouterr().out.split("Added")[1].split("\n", 1)[1])
    assert rows[0]["name"] == "Fast" and rows[0]["contacts"] is True
    out = world("accounts", "status")
    printed = capsys.readouterr().out
    assert "Fast" in printed and "s3cret-pw" not in printed
    assert ("Leasha", "account:1:app_password") in world.backend.data
    assert world("accounts", "remove", "Fast") == 0
    assert ("Leasha", "account:1:app_password") not in world.backend.data


def test_schedule_pause_quiet_and_config(world, capsys):
    assert world("accounts", "schedule", "--contacts", "every:30", "--mail", "daily:03:15",
                 "--quiet", "22:00-07:00", "--pause", "hour") == 0
    text = capsys.readouterr().out
    assert "contacts: interval 30" in text and "mail: daily 03:15" in text and "22:00-07:00" in text
    assert "Paused" in text
    assert world("accounts", "schedule", "--pause", "off") == 0
    assert world("accounts", "config", "--google-client-id", "abc.apps.googleusercontent.com",
                 "--conflict-policy", "ask", "--brake-count", "10", "--dry-run-first", "no") == 0
    shown = capsys.readouterr().out
    assert '"conflict_policy": "ask"' in shown and '"brake_count": 10' in shown
    assert world("accounts", "due") == 0


def test_contacts_sync_dry_run_then_real_then_log_and_undo(world, capsys):
    world("accounts", "add", "carddav", "--address", "me@fast.com", "--name", "Fast", "--server",
          "carddav.fast.com", "--password-stdin", stdin="pw\n")
    server = world.servers["me@fast.com"]
    server.add(person("Ann", "Lee", phone="07700 900123"))
    server.add(person("Bob", "Ray"))
    assert world("accounts", "config", "--dry-run-first", "yes") == 0   # 2026-10-11: off by default now; the option stays
    capsys.readouterr()
    assert world("contacts", "sync", "--dry-run") == 0
    assert "needs-approval" in capsys.readouterr().out               # a first sync is a dry run either way
    assert world("contacts", "list") == 0
    assert "Ann" not in capsys.readouterr().out                      # nothing written by a dry run
    assert world("contacts", "sync") == 0                            # still not approved: still nothing written
    assert "needs-approval" in capsys.readouterr().out
    assert world("contacts", "sync", "--approve-first") == 0
    capsys.readouterr()
    assert world("contacts", "sync", "--dry-run") == 0               # an explicit dry run later is just a plan
    assert "dry-run" in capsys.readouterr().out
    assert world("contacts", "list", "--text", "ann") == 0
    assert "Ann Lee" in capsys.readouterr().out
    assert world("contacts", "log") == 0
    log = capsys.readouterr().out
    assert "ok" in log and "needs-approval" in log
    run_id = [ln for ln in log.splitlines() if " ok " in ln][0].split()[0].lstrip("#")
    assert world("contacts", "undo", run_id) == 0
    assert world("contacts", "list") == 0
    assert "Ann" not in capsys.readouterr().out                      # the sync was put back


def test_contacts_sync_measure_and_log_timing(world, capsys):
    """Order 1t 1a, 1b: what the waiting writes would cost, and where a real run's time went."""
    world("accounts", "add", "carddav", "--address", "me@fast.com", "--name", "Fast", "--server",
          "carddav.fast.com", "--password-stdin", stdin="pw\n")
    world.servers.setdefault("me@fast.com", FakeServer()).add(person("Ann", email="ann@x.com"))
    assert world("accounts", "config", "--check-after-sync", "yes", "--churn-guard", "40") == 0
    capsys.readouterr()
    assert world("contacts", "sync", "--measure") == 0
    out = capsys.readouterr().out
    assert "requests one at a time" in out or "dry-run" in out
    assert world("contacts", "sync") == 0
    assert world("contacts", "log", "--timing") == 0
    assert " s" in capsys.readouterr().out
