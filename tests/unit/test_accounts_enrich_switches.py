"""Order 1k section 7 and rule 11: the enrichment switches in Settings > Accounts, and the CLI form.

Layer: L5

Every switch saves at once and reloads (the page is rebuilt on the same store and read back); the
local ones are on by default, the internet ones off, and each internet switch says what it sends.
`accounts config --enrich-*` (rule 8) sets the same fields.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("keyring")

from tests.unit.accounts_ui_fakes import make_service, qt, settle  # noqa: E402
from tests.unit.test_accounts_page import world  # noqa: E402,F401
from tests.unit.test_cli_accounts_contacts import world as cli_world  # noqa: E402,F401

OWN = ("enrich_signatures", "enrich_company", "enrich_faces", "enrich_history", "enrich_suggest")
NET = ("enrich_gravatar", "enrich_logo", "enrich_wikidata")


#: Order 1s D7 (2026-10-12): more of the owner's own sources - on by default, except documents (5e)
#: and reading the original messages again (4b), which is slower.
MORE_ON = ("enrich_vcards", "enrich_calendar", "enrich_linkedin", "enrich_corrections")
MORE_OFF = ("enrich_documents", "enrich_read_originals")


@pytest.mark.slow
def test_defaults_are_local_on_and_internet_off(world):
    svc, page = world
    g = page().globals
    assert set(g.enrich) == set(OWN + MORE_ON + MORE_OFF + NET)
    assert all(g.enrich[k].isChecked() for k in OWN + MORE_ON)
    assert not any(g.enrich[k].isChecked() for k in NET + MORE_OFF)


@pytest.mark.slow
@pytest.mark.parametrize("key", OWN + NET)
def test_every_switch_saves_and_reloads(world, key):
    svc, page = world
    p = page()
    box = p.globals.enrich[key]
    box.setChecked(not box.isChecked())
    want = box.isChecked()
    settle(p)
    assert getattr(svc.settings(), key) is want
    assert page().globals.enrich[key].isChecked() is want


@pytest.mark.slow
def test_each_internet_switch_says_what_it_sends(world):
    svc, page = world
    g = page().globals
    assert "SHA-256 hash of the email address is sent to Gravatar" in g.enrich["enrich_gravatar"].toolTip()
    assert "only the company's own domain is contacted" in g.enrich["enrich_logo"].toolTip().lower()
    assert "companies only, never people" in g.enrich["enrich_wikidata"].toolTip()
    from PySide6.QtWidgets import QLabel
    assert any(l.text() == "Nothing is kept until you accept it." for l in g.findChildren(QLabel))
    for box in g.enrich.values():
        assert box.accessibleName() and box.toolTip()


def test_the_command_line_sets_each_switch(cli_world, capsys):
    import json
    run = cli_world
    run("accounts", "config")
    shown = json.loads(capsys.readouterr().out)
    assert all(shown[k] is True for k in OWN) and all(shown[k] is False for k in NET)
    flags = []
    for k in OWN + NET:
        flags += ["--" + k.replace("_", "-"), "no" if k in OWN else "yes"]
    assert run("accounts", "config", *flags) == 0
    shown = json.loads(capsys.readouterr().out)
    assert all(shown[k] is False for k in OWN) and all(shown[k] is True for k in NET)
    assert run("accounts", "config", "--enrich-gravatar", "no") == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["enrich_gravatar"] is False and shown["enrich_logo"] is True
