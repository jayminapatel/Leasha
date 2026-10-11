"""Review and accept the contacts changed in two places, like merges: tick, then keep a side, one undo."""

from __future__ import annotations

from tests.unit.test_contacts_tools_ui_common import open_dialog, settle, world  # noqa: F401

from app.ui.contacts_tools.conflicts_dialog import ConflictsDialog


def _clash(world, cid, unit, local, remote, winner):
    return world.hub.add_conflict(cid, None, unit, local, remote, winner)    # the hub stores them as JSON


def test_tick_all_and_keep_the_accounts_value_switches_each_and_one_undo_restores(world, qtbot):
    a = world.add("Ann", "Lee", company="Acme")
    b = world.add("Bob", "Bell", company="Zeta")
    _clash(world, a, "s:company", "Acme", "Acme Ltd", "local")      # Leasha's value is kept now
    _clash(world, b, "s:company", "Zeta", "Zeta plc", "local")
    dlg = open_dialog(qtbot, ConflictsDialog, world.ctx)
    assert len(dlg.rows) == 2 and not dlg.keep_button.isEnabled()
    dlg.tick(lambda r: True)
    assert dlg.ticked_label.text() == "2 ticked" and dlg.remote_button.isEnabled()
    dlg.remote_button.click()
    settle(qtbot)
    assert world.hub.get_card(a).get("company") == "Acme Ltd" and world.hub.get_card(b).get("company") == "Zeta plc"
    assert world.hub.open_conflicts() == [] and dlg.rows == []
    assert world.hub.counts()["conflict"] == 0
    dlg.undo_button.click()
    settle(qtbot)
    assert world.hub.get_card(a).get("company") == "Acme" and world.hub.get_card(b).get("company") == "Zeta"


def test_keep_what_is_there_only_clears_the_clash(world, qtbot):
    a = world.add("Ann", "Lee", company="Acme")
    _clash(world, a, "s:company", "Acme", "Other", "local")
    dlg = open_dialog(qtbot, ConflictsDialog, world.ctx)
    dlg.tick(lambda r: r["winner"] == "local")
    dlg.keep_button.click()
    settle(qtbot)
    assert world.hub.get_card(a).get("company") == "Acme" and world.hub.open_conflicts() == []


def test_a_phone_whose_label_was_changed_in_two_places_can_take_the_other_side(world, qtbot):
    from app.contacts.card import field_key

    a = world.add("Ann", "Lee", phone="07911 123456")
    card = world.hub.get_card(a)
    unit = "f:" + field_key(card.fields[0], world.hub.region)
    _clash(world, a, unit, ["mobile", "07911 123456", [], False], ["work", "07911 123456", [], True], "local")
    dlg = open_dialog(qtbot, ConflictsDialog, world.ctx)
    dlg.tick(lambda r: True)
    dlg.remote_button.click()
    settle(qtbot)
    phone = world.hub.get_card(a).fields[0]
    assert phone.label == "work" and phone.primary and phone.value == "07911 123456"
