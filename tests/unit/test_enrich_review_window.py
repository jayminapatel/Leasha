"""Order 1s section 7: a review window that makes the right thing easy.

Layer: L5

7a grouped by contact, surest first, with "how sure" in words; 7b D4's pre-ticking (and an update
of a value already there is never pre-ticked), Accept all asking once with the count when it takes
rows that did not start ticked; 7c "Never suggest these", undone from the contact's page and
cleared from Settings; 7d the run's notes in one line under the list; 7e "Enrich this contact" and
Look again on a contact's page, for that contact only. Offscreen Qt on a real hub and store.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from PySide6.QtCore import Qt

from app.contacts.enrich import declines
from app.contacts.enrich.base import Proposal
from app.ui.contacts_tools.enrich_dialog import EnrichDialog, sure_words
from tests.unit.test_contacts_tools_ui_common import open_dialog, settle, world  # noqa: F401 - world is a fixture

DAY = 86400
NOW = int(time.time())


def _p(cid, kind, label, new, conf, *, source="signature", old="", when=None, seen=0, update=False):
    p = Proposal(cid, kind, label, old, new, source, f"Re: order, from {source}", conf, when=when, seen=seen)
    if update:
        p.update = True                         # set by the signature reader (3k); read with getattr
    return p


def _run(qtbot, dlg):
    dlg.find_button.click()
    qtbot.waitUntil(lambda: dlg.run.state in ("ready", "empty"), timeout=5000)
    settle(qtbot)


def _rows(dlg):
    out = []
    for i in range(dlg.tree.topLevelItemCount()):
        top = dlg.tree.topLevelItem(i)
        out.append((top.text(0), [(top.child(j).text(0), top.child(j).checkState(0) == Qt.CheckState.Checked,
                                   top.child(j).text(5)) for j in range(top.childCount())]))
    return out


def test_sure_words_say_how_many_messages_or_how_long_ago():
    assert sure_words(SimpleNamespace(seen=4, when=NOW - 3 * DAY, confidence=0.9), now=NOW) == "Seen in 4 messages"
    assert sure_words(SimpleNamespace(seen=1, when=NOW - 150 * DAY, confidence=0.6), now=NOW) == "Once, 5 months ago"
    assert sure_words(SimpleNamespace(seen=0, when=NOW - 400 * DAY, confidence=0.6), now=NOW) == "Once, 1 year ago"
    assert sure_words(SimpleNamespace(seen=0, when=None, confidence=0.9), now=NOW) == "Sure (90%)"
    assert sure_words(SimpleNamespace(seen=0, when=None, confidence=0.5), now=NOW) == "Not sure (50%)"


def test_grouped_by_contact_surest_first_pre_ticked_by_the_settings_and_the_unsure_hidden(world, qtbot):
    a = world.add("Ann", "Lee", email="ann@acme-widgets.co.uk")
    b = world.add("Bo", "Bell", email="bo@acme-widgets.co.uk", title="Engineer")
    proposals = [_p(a, "title", "Job title", "Buyer", 0.6, when=NOW - 150 * DAY, seen=1),
                 _p(a, "company", "Company", "Acme", 0.9, when=NOW - 10 * DAY, seen=4),
                 _p(a, "phone", "work", "020 7946 0018", 0.3),
                 _p(b, "title", "Job title", "Senior Engineer", 0.97, old="Engineer", update=True, seen=5)]
    world.ctx.enrich = lambda: proposals
    dlg = open_dialog(qtbot, EnrichDialog, world.ctx)
    _run(qtbot, dlg)
    assert _rows(dlg) == [
        ("Bo Bell", [("Job title", False, "Seen in 5 messages")]),          # an update: never pre-ticked (3k)
        ("Ann Lee", [("Company", True, "Seen in 4 messages"), ("Job title", False, "Once, 5 months ago")]),
    ]
    assert dlg.tree.headerItem().text(5) == "Confidence"
    assert dlg.tree.topLevelItem(1).checkState(0) == Qt.CheckState.PartiallyChecked   # the contact's own box
    assert "1 less sure suggestion is hidden" in dlg.notes_line.text()


def test_accept_all_asks_once_with_the_count_when_it_takes_rows_that_did_not_start_ticked(world, qtbot, monkeypatch):
    from app.ui.accounts import parts

    a = world.add("Ann", "Lee", email="ann@acme-widgets.co.uk")
    asked = []
    answer = {"yes": False}
    monkeypatch.setattr(parts, "confirm", lambda parent, title, text, ok, **kw: asked.append(text) or answer["yes"])
    world.ctx.enrich = lambda: [_p(a, "company", "Company", "Acme", 0.9), _p(a, "title", "Job title", "Buyer", 0.6),
                                _p(a, "phone", "work", "020 7946 0018", 0.2)]
    dlg = open_dialog(qtbot, EnrichDialog, world.ctx)
    _run(qtbot, dlg)
    dlg.accept_all_button.click()
    settle(qtbot)
    assert len(asked) == 1 and "2 suggestions" in asked[0] and "1 of them" in asked[0]
    assert world.hub.get_card(a).get("company") == ""                       # Cancel: nothing kept
    answer["yes"] = True
    dlg.accept_all_button.click()
    settle(qtbot)
    card = world.hub.get_card(a)
    assert card.get("company") == "Acme" and card.get("title") == "Buyer"
    assert not card.values("phone")                                         # a hidden row is not in "all"


def test_accept_all_does_not_ask_when_every_row_started_ticked(world, qtbot, monkeypatch):
    from app.ui.accounts import parts

    a = world.add("Ann", "Lee", email="ann@acme-widgets.co.uk")
    monkeypatch.setattr(parts, "confirm", lambda *a_, **k: pytest.fail("asked although every row was ticked"))
    world.ctx.enrich = lambda: [_p(a, "company", "Company", "Acme", 0.9)]
    dlg = open_dialog(qtbot, EnrichDialog, world.ctx)
    _run(qtbot, dlg)
    dlg.accept_all_button.click()
    settle(qtbot)
    assert world.hub.get_card(a).get("company") == "Acme"


def test_never_suggest_these_remembers_what_the_person_unticked(world, qtbot):
    a = world.add("Ann", "Lee", email="ann@acme-widgets.co.uk")
    company, title = _p(a, "company", "Company", "Acme", 0.9), _p(a, "title", "Job title", "Buyer", 0.9)
    world.ctx.enrich = lambda: [company, title]
    dlg = open_dialog(qtbot, EnrichDialog, world.ctx)
    _run(qtbot, dlg)
    assert not dlg.never_button.isEnabled()                                 # nothing unticked yet
    dlg.tree.topLevelItem(0).child(0).setCheckState(0, Qt.CheckState.Unchecked)
    assert dlg.never_button.isEnabled()
    dlg.never_button.click()
    settle(qtbot)
    assert declines.count(world.store) == 1
    assert declines.drop(world.store, [company, title], region=world.hub.region) == [title]
    assert [c[0] for c in _rows(dlg)[0][1]] == ["Job title"]               # the turned-down row is gone
    assert world.hub.get_card(a).get("company") == ""                       # and nothing was written
    again = open_dialog(qtbot, EnrichDialog, world.ctx)                     # opened again: still gone
    assert [c[0] for c in _rows(again)[0][1]] == ["Job title"]


def test_the_runs_notes_are_one_line_under_the_list(world, qtbot):
    a = world.add("Ann", "Lee", email="ann@acme-widgets.co.uk")
    world.accounts.update_settings(enrich_max_months=6)
    world.ctx.enrich = lambda: SimpleNamespace(proposals=[_p(a, "company", "Company", "Acme", 0.9)],
                                               notes=["Gravatar could not be reached."])
    dlg = open_dialog(qtbot, EnrichDialog, world.ctx)
    _run(qtbot, dlg)
    line = dlg.notes_line.text()
    assert "\n" not in line and line.startswith("Reading mail since ") and "Gravatar could not be reached." in line
    assert dlg.notes_line.isVisible() or not dlg.isVisible()
    world.accounts.update_settings(enrich_max_months=0)                     # Any age: no window to state
    _run(qtbot, dlg)
    assert "Reading mail since" not in dlg.notes_line.text()


# --------------------------------------------------------------------------
# The contact's page (7c's Undo, 7e)
# --------------------------------------------------------------------------

@pytest.fixture
def page_world(tmp_path, qapp):
    from tests.unit.test_contacts_view_world import World

    w = World(tmp_path)
    yield w
    w.close()


def test_enrich_this_contact_runs_that_contact_only_and_look_again_asks_anyway(page_world, monkeypatch):
    from app.contacts.enrich import run as enrich_run
    from app.ui.contacts import openers
    from tests.unit.test_contacts_view_world import card, settle as page_settle

    ann = page_world.add(card("Ann", "Lee", email="ann@acme-widgets.co.uk"))
    page_world.add(card("Bob", "Bell", email="bob@acme-widgets.co.uk"))
    asked, opened = [], []
    monkeypatch.setattr(enrich_run, "proposals_for",
                        lambda hub, accounts, ids, **kw: asked.append((list(ids), kw.get("force"))) or [])
    monkeypatch.setattr(openers, "open", lambda name, parent, ctx: opened.append((name, list(ctx.selected_ids))))
    page = page_world.page()
    page.select_ids([ann])
    page_settle(page)
    panel = page.editor.enrich_panel
    assert panel.enrich_button.text() == "Enrich this contact" and panel.again_button.text() == "Look again"
    panel.enrich_button.click()
    page_settle(page)
    assert asked == [([ann], False)] and opened == [("enrich", [ann])]
    panel.again_button.click()
    page_settle(page)
    assert asked[-1] == ([ann], True)


def test_suggestions_turned_down_are_listed_on_the_contact_and_undo_takes_one_back(page_world, monkeypatch):
    from tests.unit.test_contacts_view_world import card, settle as page_settle

    ann = page_world.add(card("Ann", "Lee", email="ann@acme-widgets.co.uk"))
    declines.decline(page_world.store, [_p(ann, "title", "Job title", "Buyer", 0.9),
                                        _p(ann, "email", "other", "ann.lee@yasref.example", 0.9)],
                     region=page_world.hub.region)
    page = page_world.page()
    page.select_ids([ann])
    page_settle(page)
    panel = page.editor.enrich_panel
    texts = panel.declined_texts()
    assert any("Job title" in t and "Buyer" in t for t in texts)
    assert not any("yasref" in t for t in texts) and any("an email address" in t for t in texts)
    panel.undo_buttons[0].click()
    page_settle(page)
    assert declines.count(page_world.store) == 1 and len(panel.declined_texts()) == 1


def test_settings_say_how_many_were_turned_down_and_clear_asks_first(page_world, qtbot, monkeypatch):
    from app.ui.accounts import enrich_settings, parts
    from app.ui.accounts.backend import Backend
    from tests.unit.test_contacts_view_world import card

    ann = page_world.add(card("Ann", "Lee"))
    declines.decline(page_world.store, [_p(ann, "title", "Job title", "Buyer", 0.9),
                                        _p(ann, "company", "Company", "Acme", 0.9)], region=page_world.hub.region)
    backend = Backend(lambda: SimpleNamespace(store=page_world.store))
    row = enrich_settings.DeclinesRow(backend)
    qtbot.addWidget(row)
    row.refresh()
    qtbot.waitUntil(lambda: "2" in row.label.text(), timeout=5000)
    assert row.label.text() == "Suggestions you turned down: 2" and row.clear_button.text() == "Clear"
    monkeypatch.setattr(parts, "confirm", lambda *a, **k: False)
    row.clear_button.click()
    qtbot.wait(50)
    assert declines.count(page_world.store) == 2                            # Cancel: kept
    monkeypatch.setattr(parts, "confirm", lambda *a, **k: True)
    row.clear_button.click()
    qtbot.waitUntil(lambda: row.label.text() == "Suggestions you turned down: 0", timeout=5000)
    assert declines.count(page_world.store) == 0 and not row.clear_button.isEnabled()


def test_a_row_that_started_unticked_is_turned_down_by_selecting_it(world, qtbot):
    """Review of #75: an unsure row has no tick to take off, so selecting it is enough."""
    a = world.add("Ann", "Lee", email="ann@acme-widgets.co.uk")
    unsure = _p(a, "title", "Job title", "Buyer", 0.6)                     # below "tick from"
    world.ctx.enrich = lambda: [unsure]
    dlg = open_dialog(qtbot, EnrichDialog, world.ctx)
    _run(qtbot, dlg)
    row = dlg.tree.topLevelItem(0).child(0)
    assert row.checkState(0) == Qt.CheckState.Unchecked
    dlg.tree.clearSelection()
    assert not dlg.never_button.isEnabled()
    row.setSelected(True)
    assert dlg.never_button.isEnabled()
    dlg.never_button.click()
    settle(qtbot)
    assert declines.count(world.store) == 1
