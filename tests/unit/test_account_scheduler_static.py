"""Order 1k 10c, D11.2, 10u.2: nothing but the schedule and Sync now starts a sync.

Layer: L5

A static scan of `app/` for the calls that run a sync: `.run_due(`, `.sync_now(` and
`engine.sync(`. Only the modules that are *meant* to may contain one. Opening the window,
waking from sleep and the network coming back start nothing because no other module can.
"""

from __future__ import annotations

import re
from pathlib import Path
import pytest

APP = Path(__file__).resolve().parents[2] / "app"

#: Path prefixes (relative to the repo, forward slashes) that may start a sync.
ALLOWED = (
    "app/accounts/service.py",       # the facade: sync_now / run_due are defined and call each other here
    "app/cli/",                      # `accounts sync`, `contacts sync`: a person at a terminal
    "app/ui/account_scheduler.py",   # the tick and Sync now
    "app/ui/accounts",               # accounts_page.py and the accounts/ package (Sync now, Approve, Allow)
    "app/ui/contacts",               # the Contacts page's own Sync now (contacts_view, contacts/, contacts_tools/)
)

CALL = re.compile(r"\.run_due\(|\.sync_now\(|\bengine\.sync\(")


def _offenders() -> list[str]:
    root = APP.parent
    found = []
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(ALLOWED):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if CALL.search(line.split("#", 1)[0]):
                found.append(f"{rel}:{number}: {line.strip()}")
    return found


@pytest.mark.slow
def test_only_the_schedule_and_sync_now_can_start_a_sync():
    assert _offenders() == [], (
        "these modules start a sync but are not the schedule or a Sync now:\n  " + "\n  ".join(_offenders()))


def test_the_scan_would_catch_a_stray_caller(tmp_path, monkeypatch):
    sample = "def on_start(svc):\n    svc.run_due()\n"
    assert CALL.search(sample) and CALL.search("x.sync_now(1)") and CALL.search("self.engine.sync([1])")
    assert not CALL.search("sync_all_now()")


def test_the_main_window_and_the_runtime_do_not_name_a_starter_directly():
    for name in ("shell.py", "account_runtime.py", "tray.py"):
        text = (APP / "ui" / name).read_text(encoding="utf-8")
        assert not CALL.search(text), f"{name} must go through AccountScheduler"
