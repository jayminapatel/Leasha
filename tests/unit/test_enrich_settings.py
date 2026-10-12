"""Order 1s 8a: every enrichment rule is a setting, with a control and a command-line flag.

Layer: L5

`test_every_plain_setting_has_a_control` covers the `.env` registry; enrichment's
settings live in `AccountSettings` (the index), so this is its equivalent: every
`enrich_*` field has a control named after it in Settings > Accounts > Enrichment
and a flag on `accounts config` (rule 8).
"""

from __future__ import annotations

from dataclasses import fields

import pytest

from app.accounts.store import AccountSettings
from app.cli import accounts as cli_accounts

ENRICH_FIELDS = [f.name for f in fields(AccountSettings) if f.name.startswith("enrich_")]

#: Fields whose control is not a plain switch or choice: the LinkedIn file is picked with a button (5c).
NOT_A_PLAIN_CONTROL = {"enrich_linkedin_file"}


def test_the_defaults_are_the_orders():
    s = AccountSettings()
    assert s.enrich_max_months == 6                       # D1, the owner's
    assert (s.enrich_tick_from, s.enrich_hide_below) == (0.85, 0.5)     # D4
    assert s.enrich_repeat_days == 90                     # 6b
    assert s.enrich_vcards and s.enrich_calendar and s.enrich_outlook_contacts and s.enrich_linkedin   # D7
    assert not s.enrich_documents and not s.enrich_read_originals                                      # 5e, 4b


@pytest.mark.parametrize("name", ENRICH_FIELDS)
def test_every_enrichment_field_has_a_command_line_flag(name):
    dests = {d for d, *_ in cli_accounts.ENRICH_FLAGS} | {d for d, *_ in cli_accounts.ENRICH_VALUES}
    assert name in dests, f"{name} has no flag on `accounts config`"


def test_every_enrichment_field_has_a_control_named_after_it(qtbot):
    from PySide6.QtWidgets import QFormLayout, QWidget

    from app.ui.accounts import enrich_settings

    host = QWidget()
    qtbot.addWidget(host)
    form = QFormLayout(host)
    saved: list[dict] = []
    ctl = enrich_settings.build(form, lambda **v: saved.append(v))
    names = set(ctl.checks) | set(ctl.choices)
    missing = [n for n in ENRICH_FIELDS if n not in names and n not in NOT_A_PLAIN_CONTROL]
    assert not missing, f"no control for {missing}"
    for name, widget in {**ctl.checks, **ctl.choices}.items():
        assert widget.objectName() == name and widget.toolTip()


def test_a_choice_saves_its_value_and_loading_saves_nothing(qtbot):
    from PySide6.QtWidgets import QFormLayout, QWidget

    from app.ui.accounts import enrich_settings

    host = QWidget()
    qtbot.addWidget(host)
    saved: list[dict] = []
    ctl = enrich_settings.build(QFormLayout(host), lambda **v: saved.append(v))
    s = AccountSettings()
    s.enrich_max_months = 0
    enrich_settings.load(ctl, s)
    assert saved == [] and ctl.choices["enrich_max_months"].currentData() == 0
    combo = ctl.choices["enrich_max_months"]
    combo.setCurrentIndex(combo.findData(12))
    assert saved[-1] == {"enrich_max_months": 12}


def test_accounts_config_sets_the_age_window(tmp_path, capsys):
    from app.cli import main

    env = tmp_path / "scratch.env"
    env.write_text(f"DATA_PATH={tmp_path}\n", encoding="utf-8")
    assert main(["accounts", "config", "--enrich-max-months", "12", "--enrich-vcards", "no",
                 "--env", str(env)]) == 0
    out = capsys.readouterr().out
    assert '"enrich_max_months": 12' in out and '"enrich_vcards": false' in out
