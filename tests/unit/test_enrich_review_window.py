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


@pytest.mark.slow
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


@pytest.mark.slow
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


@pytest.mark.slow
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


# --------------------------------------------------------------------------
# A world-class window (owner, 2026-10-15): "the photo suggestions should show the photo and double
# click should open source else how do i check ? ... make the window sizable and maximize and minimise"
# --------------------------------------------------------------------------

def _png(width=40, height=20, colour=(200, 30, 30)):
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(out, "PNG")
    return out.getvalue()


def _photo(colour=(200, 30, 30)):
    from app.contacts.card import Photo

    return Photo("image/png", _png(colour=colour))


def _leaf(dlg, words, column=0):
    for i in range(dlg.tree.topLevelItemCount()):
        top = dlg.tree.topLevelItem(i)
        for j in range(top.childCount()):
            if top.child(j).text(column) == words:
                return top.child(j)
    raise AssertionError(f"no row {words!r}")


def _mixed(world):
    """A photo (the card has one), a signature value that opens a message, a correction, Wikidata, and a
    company worked out from the email domain (nothing to open)."""
    a = world.add("Ann", "Lee", email="ann@acme-widgets.co.uk")
    b = world.add("Bo", "Bell", email="bo@acme-widgts.co.uk")
    card = world.hub.get_card(a)
    card.photo = _photo((10, 120, 10))
    world.hub.update(a, card)
    photo = Proposal(a, "photo", "Photo", "", _photo(), "gravatar", "https://gravatar.com/avatar/abc", 0.6,
                     opens="https://gravatar.com/avatar/abc")
    phone = Proposal(a, "phone", "work", "", "020 7946 0018", "signature", "Re: order, 3 Mar 2026", 0.9,
                     seen=3, opens="account://1//1a0d")
    fix = Proposal(b, "email", "fixed", "bo@acme-widgts.co.uk", "bo@acme-widgets.co.uk", "correction",
                   "The domain acme-widgts.co.uk has no web site; acme-widgets.co.uk has", 0.9)
    fix.update = True
    site = Proposal(b, "url", "company", "", "https://acme-widgets.co.uk", "wikidata",
                    "Wikidata Q123, read https://www.wikidata.org/wiki/Q123", 0.8)
    company = Proposal(b, "company", "Company", "", "Acme Widgets", "company", "work email domain acme", 0.5)
    world.ctx.enrich = lambda: [photo, phone, fix, site, company]
    return a, b


def test_the_source_is_said_in_words_and_where_it_opens_is_worked_out():
    from app.ui.contacts_tools import enrich_rows as rows
    from app.ui.widgets.icons import ICON_NAMES

    assert [rows.source_words(s) for s in ("signature", "gravatar", "logo", "wikidata", "face", "vcard",
                                           "calendar", "linkedin", "document", "company", "correction")] == [
        "Email signature", "Gravatar", "Company website", "Wikidata", "Your photos", "Contact file",
        "Calendar invitation", "LinkedIn export", "Document", "Email domain", "Correction"]
    assert rows.source_words("newthing") == "Newthing" and rows.source_icon("newthing") == rows.OTHER_ICON
    assert all(glyph in ICON_NAMES for _w, glyph in rows.SOURCES.values())
    assert all(glyph in ICON_NAMES for _w, glyph in rows.KIND_FILTERS)
    p = SimpleNamespace(opens="pst://x/1", receipt="https://a.example/b", source="signature")
    assert rows.where_to_open(p) == "pst://x/1"
    p.opens = ""
    assert rows.where_to_open(p) == "https://a.example/b"
    assert rows.where_to_open(SimpleNamespace(receipt="Re: order, 3 Mar", source="signature")) == ""
    assert rows.nothing_to_open(SimpleNamespace(source="company")) == (
        "Nothing to open for this suggestion: it was worked out from the email address.")
    assert rows.notes_summary(["One.", "Two.", "Three."]) == "One. And 2 more notes in Details."
    assert rows.notes_summary(["One."]) == "One." and rows.notes_summary([]) == ""
    assert rows.shown_words(12, 140, 5) == "Showing 12 of 140 - 5 ticked"
    fix = SimpleNamespace(field_kind="email", source="correction", new="x")
    assert rows.kind_tags(fix) == {"All", "Emails", "Corrections"}
    assert rows.matches(fix, "Bo Bell", kind="Corrections", text="BO")
    assert not rows.matches(fix, "Bo Bell", source="Wikidata")


def test_a_photo_row_shows_the_picture_and_the_one_it_would_replace(world, qtbot):
    from app.ui.contacts_tools.enrich_parts import THUMB

    _mixed(world)
    dlg = open_dialog(qtbot, EnrichDialog, world.ctx)
    _run(qtbot, dlg)
    row = _leaf(dlg, "Photo")
    new = row.data(2, Qt.ItemDataRole.DecorationRole)
    now = row.data(1, Qt.ItemDataRole.DecorationRole)
    side = round(THUMB * dlg.devicePixelRatioF())
    assert new is not None and not new.isNull() and new.width() == new.height() == side
    assert now is not None and not now.isNull()                          # the card's own photo, in Now
    assert row.text(2) == ""                                             # no more "(a photo)"
    assert row.font(1).strikeOut()                                       # what it would replace
    assert row.text(3) == "Gravatar"


def test_double_click_and_enter_open_where_it_came_from_through_the_one_route(world, qtbot, monkeypatch):
    from PySide6.QtTest import QTest

    from app.ui import workers

    _mixed(world)
    opened = []
    monkeypatch.setattr(workers, "open_row_async", lambda store, row, **kw: opened.append((store, row.path)))
    dlg = open_dialog(qtbot, EnrichDialog, world.ctx)
    _run(qtbot, dlg)
    dlg.tree.activated.emit(dlg.tree.indexFromItem(_leaf(dlg, "work")))  # what a double-click sends
    assert opened == [(world.store, "account://1//1a0d")]
    dlg.tree.setCurrentItem(_leaf(dlg, "company"))                       # the receipt's web address
    QTest.keyClick(dlg.tree, Qt.Key.Key_Return)
    assert opened[-1][1] == "https://www.wikidata.org/wiki/Q123"
    assert _leaf(dlg, "work").font(3).underline()                        # it looks like a link


def test_a_row_with_nothing_to_open_says_so(world, qtbot, monkeypatch):
    from app.ui import workers

    _mixed(world)
    monkeypatch.setattr(workers, "open_row_async", lambda *a, **k: pytest.fail("nothing should open"))
    dlg = open_dialog(qtbot, EnrichDialog, world.ctx)
    _run(qtbot, dlg)
    dlg.tree.activated.emit(dlg.tree.indexFromItem(_leaf(dlg, "Company")))
    assert dlg.status.text() == "Nothing to open for this suggestion: it was worked out from the email address."
    assert not _leaf(dlg, "Company").font(3).underline()


def test_the_right_click_menu_opens_first_in_bold_and_never_suggest_comes_last(world, qtbot):
    from PySide6.QtWidgets import QApplication

    from app.ui.contacts_tools.enrich_parts import PictureWindow
    from app.ui.widgets import menus

    _mixed(world)
    dlg = open_dialog(qtbot, EnrichDialog, world.ctx)
    _run(qtbot, dlg)
    menu = dlg.row_menu(_leaf(dlg, "Photo"))
    words = ["-" if a.isSeparator() else a.text() for a in menu.actions()]
    assert words[0] == "Open where it came from" and menu.defaultAction().text() == words[0]
    assert menu.defaultAction().font().bold()
    assert "Show the picture" in words and words[-1] == "Never suggest this"
    assert "Copy the suggestion" not in words                             # a picture has no words to copy
    assert all(not a.icon().isNull() for a in menu.actions() if not a.isSeparator())
    menus.lines(menu)["Show the picture"].trigger()
    shown = dlg._pictures[-1]
    assert isinstance(shown, PictureWindow) and not shown.picture.pixmap().isNull()
    shown.close()
    lines = menus.lines(dlg.row_menu(_leaf(dlg, "work")))
    assert "Copy the suggestion" in lines and "Show the picture" not in lines
    lines["Copy the suggestion"].trigger()
    assert QApplication.clipboard().text() == "020 7946 0018"
    assert not menus.lines(dlg.row_menu(_leaf(dlg, "Company")))["Open where it came from"].isEnabled()
    contact = menus.lines(dlg.row_menu(dlg.tree.topLevelItem(0)))
    assert set(contact) == {"Tick all for this contact", "Untick all for this contact"}
    menus.lines(dlg.row_menu(_leaf(dlg, "company")))["Never suggest this"].trigger()
    settle(qtbot)
    assert declines.count(world.store) == 1
    with pytest.raises(AssertionError):
        _leaf(dlg, "company")


def test_every_contacts_tool_window_minimises_maximises_and_remembers_itself(world, qtbot):
    from PySide6.QtWidgets import QHeaderView

    from app.ui import state_writes
    from app.ui.contacts_tools._common import COLUMNS_KEY, GEOMETRY_KEY

    flags = open_dialog(qtbot, EnrichDialog, world.ctx).windowFlags()
    for wanted in (Qt.WindowType.WindowMinimizeButtonHint, Qt.WindowType.WindowMaximizeButtonHint,
                   Qt.WindowType.WindowCloseButtonHint, Qt.WindowType.Window):
        assert flags & wanted, wanted
    world.store.set_state(COLUMNS_KEY.format("contacts-enrich"), "300,111,0,222,133,144")
    dlg = open_dialog(qtbot, EnrichDialog, world.ctx)
    header = dlg.tree.header()
    assert [header.sectionSize(i) for i in (0, 1, 3, 4, 5)] == [300, 111, 222, 133, 144]
    assert header.sectionResizeMode(2) == QHeaderView.ResizeMode.Stretch   # Suggested takes the room
    dlg.show()
    qtbot.waitExposed(dlg)
    header.resizeSection(0, 260)
    dlg.close()
    state_writes.pool().waitForDone(3000)
    assert world.store.get_state(COLUMNS_KEY.format("contacts-enrich")).startswith("260,")
    assert world.store.get_state(GEOMETRY_KEY.format("contacts-enrich"))


def test_the_filter_narrows_the_list_and_accept_selected_takes_only_what_is_shown(world, qtbot):
    a, b = _mixed(world)
    dlg = open_dialog(qtbot, EnrichDialog, world.ctx)
    _run(qtbot, dlg)
    assert dlg.filters.count.text() == "Showing all 5 - 1 ticked"        # the phone; the rest are less sure
    _leaf(dlg, "company").setCheckState(0, Qt.CheckState.Checked)        # Bo's website, ticked by hand
    assert dlg.filters.count.text() == "Showing all 5 - 2 ticked"
    dlg.filters.kind.setCurrentText("Corrections")
    assert [r.text(3) for r in dlg._rows()] == ["Correction"]
    assert dlg.filters.count.text().startswith("Showing 1 of 5")
    dlg.filters.kind.setCurrentText("All")
    dlg.filters.source.setCurrentText("Wikidata")
    assert [r.text(2) for r in dlg._rows()] == ["https://acme-widgets.co.uk"]
    dlg.filters.source.setCurrentText("Any source")
    dlg.filters.name.setText("ann")
    assert {r.text(0) for r in dlg._rows()} == {"Photo", "work"}
    dlg.filters.name.setText("nobody")
    assert not dlg.tree.isVisibleTo(dlg) and dlg.empty.text() == "No suggestion matches the filter."
    dlg.filters.name.setText("ann")                                      # Ann's rows: only the phone ticked
    assert dlg.ticked() == [1]                                           # Bo's website is ticked, but hidden
    dlg.accept_button.click()
    settle(qtbot)
    assert world.hub.get_card(a).values("phone") and not world.hub.get_card(b).values("url")


def test_the_notes_are_folded_into_details(world, qtbot):
    a = world.add("Ann", "Lee", email="ann@acme-widgets.co.uk")
    world.accounts.update_settings(enrich_max_months=6)
    world.ctx.enrich = lambda: SimpleNamespace(proposals=[_p(a, "company", "Company", "Acme", 0.9)],
                                               notes=["Gravatar could not be reached.", "Wikidata was asked."])
    dlg = open_dialog(qtbot, EnrichDialog, world.ctx)
    _run(qtbot, dlg)
    details = dlg.details
    assert details.isVisibleTo(dlg) and details.summary.isVisibleTo(dlg) and not dlg.notes_line.isVisibleTo(dlg)
    assert details.summary.text().startswith("Reading mail since ")
    assert details.summary.text().endswith("And 2 more notes in Details.")
    assert dlg.notes_line.parentWidget() is details
    details.toggle.click()
    assert dlg.notes_line.isVisibleTo(dlg) and not details.summary.isVisibleTo(dlg)
    assert "Gravatar could not be reached. Wikidata was asked." in dlg.notes_line.text()


def test_the_keys_select_all_and_turn_down_as_the_house_does(world, qtbot):
    from PySide6.QtTest import QTest

    from app.ui.widgets import list_keys

    _mixed(world)
    dlg = open_dialog(qtbot, EnrichDialog, world.ctx)
    _run(qtbot, dlg)
    assert dlg.tree.property("listKeys") is True and isinstance(dlg.tree._list_keys, list_keys.ListKeys)
    dlg.tree.setFocus()
    QTest.keyClick(dlg.tree, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert len(dlg.tree.selectedItems()) == 7                            # two contacts and five rows
    dlg.tree.clearSelection()
    row = _leaf(dlg, "Company")
    dlg.tree.setCurrentItem(row)
    row.setSelected(True)
    QTest.keyClick(dlg.tree, Qt.Key.Key_Delete)
    settle(qtbot)
    assert declines.count(world.store) == 1


def test_the_window_at_a_glance(world, qtbot):
    """The self-check: a photo, a signature value that opens, a correction and Wikidata, as a person sees them."""
    from tests.unit.test_contacts_tools_ui_common import unnamed_controls

    _mixed(world)
    dlg = open_dialog(qtbot, EnrichDialog, world.ctx)
    _run(qtbot, dlg)
    assert sorted(top for top, _r in _rows(dlg)) == ["Ann Lee", "Bo Bell"]
    fix = _leaf(dlg, "Correction", column=3)
    assert (fix.text(1), fix.text(2)) == ("bo@acme-widgts.co.uk", "bo@acme-widgets.co.uk")
    assert fix.font(1).strikeOut() and fix.toolTip(2) == "Replaces bo@acme-widgts.co.uk on the card"
    assert fix.checkState(0) == Qt.CheckState.Unchecked                  # an update is never ticked for you
    assert [_leaf(dlg, w).text(3) for w in ("work", "company", "Company")] == [
        "Email signature", "Wikidata", "Email domain"]
    assert not _leaf(dlg, "work").icon(3).isNull()
    assert _leaf(dlg, "company").toolTip(3).startswith("Wikidata Q123")  # the receipt is still the tooltip
    assert dlg.filters.source.count() == 6                               # Any source and the five found
    assert dlg.status.text().startswith("5 suggestions for 2 contacts")
    assert dlg.empty.isHidden() and dlg.tree.isVisibleTo(dlg)
    assert not unnamed_controls(dlg), unnamed_controls(dlg)
