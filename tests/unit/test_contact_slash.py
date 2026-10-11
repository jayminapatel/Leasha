"""The Contacts box takes the same `/` switches as the other boxes: typed, or picked from its menu."""

from __future__ import annotations

from app.contacts.slash import split
from app.ui.presenter.contact_commands import CONTACT_COMMANDS, contact_matching


def test_a_slash_switch_becomes_a_rule_and_the_rest_stays_text() -> None:
    free, rules = split("/company acme jones")
    assert free == "jones"
    assert rules == [{"field": "company", "op": "contains", "value": "acme"}]


def test_the_form_the_menu_inserts_reads_the_same() -> None:
    assert split('company:"acme ltd" has:phone favourite:')[1] == [
        {"field": "company", "op": "contains", "value": "acme ltd"},
        {"field": "phone", "op": "not_empty", "value": ""},
        {"field": "favourite", "op": "is", "value": "yes"}]


def test_what_is_not_a_switch_is_left_alone() -> None:
    assert split("/var/log 12/03 http://x") == ("/var/log 12/03 http://x", [])
    assert split("/company") == ("", [])               # no value yet: no rule, no text


def test_every_offered_switch_is_honoured() -> None:
    for command in CONTACT_COMMANDS:
        value = "" if command.name == "favourite" else (command.values or ("x",))[0]
        free, rules = split(f"/{command.name} {value}")
        assert rules, f"/{command.name} is offered but reads as nothing"
        assert free == ""


def test_the_menu_filters_by_prefix() -> None:
    assert [c.name for c in contact_matching("/co")] == ["company"]
    assert len(contact_matching("")) == len(CONTACT_COMMANDS)
