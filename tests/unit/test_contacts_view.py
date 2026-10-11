"""Order 1k section 6: the Contacts page.

Layer: L5

The rules that need no window (columns, sorting, the sidebar's entries, the words), then the page
itself offscreen against a real hub: the rail pill, accessible names on every control, the
remembered view, the editor's round trip, "Kept in" (adding at once, removing only after a
confirmation), undo through the journal, the bulk bar, the sidebar's filters, the openers, the
keyboard. The 10,000-contact measurement is in `test_contacts_view_scale.py`.
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt                                            # noqa: E402
from PySide6.QtWidgets import (QAbstractButton, QComboBox, QLineEdit, QPlainTextEdit,   # noqa: E402
                             QToolButton, QTreeWidget)

from app.contacts.card import Card, Field                                # noqa: E402
from app.contacts.hub import ContactRow                                  # noqa: E402
from app.ui.contacts import logic, openers                               # noqa: E402
from app.ui.contacts.model import ContactModel                           # noqa: E402

from tests.unit.test_contacts_view_world import card, settle, world      # noqa: E402,F401


# --------------------------------------------------------------------------- pure rules
def _row(i, name, company="", accounts=(), last=None, tags=(), groups=()):
    return ContactRow(i, f"u{i}", name, company=company, accounts=list(accounts), last_contacted_at=last,
                      tags=list(tags), groups=list(groups))


def test_the_list_has_every_column_the_order_names_and_where_it_lives():
    heads = [h for _k, h in logic.COLUMNS]
    assert heads == ["Name", "Company", "Title", "Email", "Phone", "Accounts", "Groups", "Tags",
                     "Last contacted", "Where it lives"]


def test_sorting_puts_blanks_last_in_both_directions():
    rows = [_row(1, "Cy", ""), _row(2, "Ann", "Zeta"), _row(3, "Bob", "Acme")]
    up = [r.id for r in logic.sort_rows(rows, "company", False, {})]
    down = [r.id for r in logic.sort_rows(rows, "company", True, {})]
    assert up == [3, 2, 1] and down == [2, 3, 1]


def test_parse_sort_accepts_what_it_saved_and_forgives_the_rest():
    assert logic.parse_sort("company:desc") == (1, True)
    assert logic.parse_sort("nonsense") == (0, False)
    assert logic.parse_sort("") == (0, False)


def test_where_it_lives_names_the_single_account_case():
    names = {1: "Home", 2: "Work"}
    assert logic.where_it_lives(_row(1, "A", accounts=[1]), names) == ("Only in Home", "single")
    assert logic.where_it_lives(_row(1, "A", accounts=[1, 2]), names) == ("In 2 accounts", "multi")
    assert logic.where_it_lives(_row(1, "A"), names) == ("Only in Leasha", "none")


def test_last_contacted_is_said_in_words():
    now = 1_700_000_000
    assert logic.last_contacted_text(None, now) == "Never"
    assert logic.last_contacted_text(int(now * 1000), now) == "Today"
    assert logic.last_contacted_text(int((now - 3 * 86400) * 1000), now) == "3 days ago"
    assert logic.last_contacted_text(int((now - 800 * 86400) * 1000), now) == "2 years ago"


def test_initials():
    assert logic.initials("Ann Lee") == "AL"
    assert logic.initials("Cher") == "C"
    assert logic.initials("") == "?"


def test_the_sidebar_lists_everything_6c_and_6t_name(world):
    from app.accounts.store import AccountStore

    smart = world.hub.save_smart_group("No phone", [{"field": "phone", "op": "empty", "value": ""}])
    world.hub.create_tag("VIP", "#E8710A")
    entries = logic.build_entries(
        {"all": 5, "favourites": 1, "conflict": 0, "deleted_elsewhere": 0, "single_account": 3, "bin": 2},
        [(1, "Family", 2)], world.hub.tags(), world.accounts.list(), world.hub.smart_groups())
    keys = [e.key for e in entries]
    for wanted in ("all", "favourites", "group:1", "conflict", "duplicates", "deleted_elsewhere",
                   "single_account", "bin", f"smart:{smart}"):
        assert wanted in keys
    assert any(e.kind == "tag" and e.colour == "#E8710A" for e in entries)
    accounts = [e for e in entries if e.kind == "account"]
    assert [e.colour for e in accounts] == ["#4C8BF5", "#E8710A", "#188038"]
    assert [e.icon for e in accounts] == ["contact", "contact", "server"]      # never a provider logo
    assert next(e for e in entries if e.key == "single_account").count == 3
    assert all(e.icon for e in entries)
    assert isinstance(world.accounts, AccountStore)


def test_every_icon_the_page_uses_is_shipped():
    from app.ui.widgets.icons import ICON_NAMES, icon_file

    used = {g for _n, g, _w, _t in __import__("app.ui.contacts.toolbar", fromlist=["TOOLS"]).TOOLS}
    used |= {info[1] for info in logic.KIND_INFO.values()} | {"user-plus", "list", "layout-grid", "history",
                                                              "trash-2", "star", "grip-vertical", "cake",
                                                              "calendar", "sticky-note", "tag", "users"}
    for name in used:
        assert name in ICON_NAMES and icon_file(name) is not None, name


def test_merge_order_keeps_the_card_as_it_was():
    original = [Field("phone", "1"), Field("email", "a@x"), Field("phone", "2")]
    per_kind = {"email": [Field("email", "a@x")], "phone": [Field("phone", "1"), Field("phone", "2")],
                "url": [Field("url", "http://x")]}
    out = logic.merge_order(original, per_kind)
    assert [f.value for f in out] == ["1", "a@x", "2", "http://x"]


# ----------------------------------------------------------------------------- openers
def test_an_unregistered_tool_says_so_and_a_registered_one_runs(qapp, monkeypatch):
    seen = []
    monkeypatch.setattr(openers, "_not_available", lambda name, parent: seen.append(name))
    monkeypatch.setattr(openers, "_loaded", True)
    saved = dict(openers.OPENERS)
    try:
        openers.OPENERS.pop("qr", None)
        assert openers.open("qr", None, openers.Ctx(hub=None, accounts=None)) is None
        assert seen == ["qr"]
        openers.register("qr", lambda parent, ctx: ("ran", ctx.selected_ids))
        assert openers.open("qr", None, openers.Ctx(hub=None, accounts=None, selected_ids=[3])) == ("ran", [3])
    finally:
        openers.OPENERS.clear()
        openers.OPENERS.update(saved)


def test_every_toolbar_button_and_the_sidebar_door_calls_the_opener(world, monkeypatch):
    calls = []
    for name in openers.NAMES:
        monkeypatch.setitem(openers.OPENERS, name, lambda parent, ctx, n=name: calls.append((n, ctx)))
    monkeypatch.setattr(openers, "_loaded", True)
    world.add(card("Ann", "Lee"))
    page = world.page()
    page.select_ids([page.model.ids()[0]])
    for name in ("import", "export", "print", "duplicates", "sync"):
        page.toolbar.tools[name].click()
    page.sidebar.select_key("duplicates")
    names = [n for n, _c in calls]
    assert names == ["import", "export", "print", "duplicates", "sync", "duplicates"]
    ctx = calls[1][1]
    assert ctx.hub is world.hub and ctx.accounts is world.accounts
    assert ctx.selected_ids == page.selected_ids() and ctx.visible_ids == page.model.ids()
    ctx.refresh()                                                  # the callback re-reads the page
    settle(page)
    assert page.sidebar.current_key() == "all"                    # the door did not move the list


# ------------------------------------------------------------------------------- model
def test_the_model_sorts_without_losing_what_is_selected(qapp):
    from PySide6.QtCore import QPersistentModelIndex

    m = ContactModel()
    m.set_rows([_row(1, "Cy"), _row(2, "Ann"), _row(3, "Bob")])
    keep = QPersistentModelIndex(m.index(0, 0))                    # Cy
    m.sort(0, Qt.SortOrder.AscendingOrder)
    assert m.ids() == [2, 3, 1]
    assert m.row_at(keep.row()).id == 1
    m.sort(0, Qt.SortOrder.DescendingOrder)
    assert m.ids() == [1, 3, 2]


def test_the_model_draws_chips_from_data(qapp, world):
    world.hub.create_tag("VIP", "#E8710A")
    m = ContactModel()
    m.set_accounts(world.accounts.list())
    m.set_tag_colours(world.hub.tags())
    r = _row(1, "Ann", accounts=[world.a_home, world.a_fast], tags=["VIP"], groups=["Family"])
    assert m.chips(r, "accounts") == [("Home Google", "#4C8BF5", "contact"), ("Fastmail", "#188038", "server")]
    assert m.chips(r, "tags") == [("VIP", "#E8710A", "tag")]
    assert m.chips(r, "groups") == [("Family", "", "users")]


# --------------------------------------------------------------------------------- page
def test_the_page_lists_filters_and_searches(world):
    ann = world.add(card("Ann", "Lee", email="ann@acme.example", company="Acme"))
    world.add(card("Bob", "Bell", email="bob@zeta.example", company="Zeta"), kept=[world.a_work])
    world.hub.set_favourite([ann], True)
    page = world.page()
    assert page.model.rowCount() == 2
    page.sidebar.select_key("favourites")
    settle(page)
    assert page.model.rowCount() == 1 and page.model.row_at(0).display_name == "Ann Lee"
    page.sidebar.select_key(f"account:{world.a_work}")
    settle(page)
    assert page.model.rowCount() == 1 and page.model.row_at(0).display_name == "Bob Bell"
    page.sidebar.select_key("all")
    settle(page)
    page.toolbar.filter.setText("zeta")
    page._timer.stop()
    page.reload(full=False)
    settle(page)
    assert [page.model.row_at(i).display_name for i in range(page.model.rowCount())] == ["Bob Bell"]


def test_single_account_is_a_filter_a_count_and_a_column(world):
    only = world.add(card("Solo", "One"), kept=[world.a_home])
    world.add(card("Both", "Two"), kept=[world.a_home, world.a_work])
    page = world.page()
    entry = page.sidebar.entry_for("single_account")
    assert entry.count == 1
    page.sidebar.select_key("single_account")
    settle(page)
    assert page.model.ids() == [only]
    index = page.model.index(0, 9)
    assert page.model.data(index) == "Only in Home Google"
    assert not page.model.data(index, Qt.ItemDataRole.DecorationRole).isNull()


def test_the_rows_are_the_hubs_and_show_their_chips(world):
    world.hub.create_tag("VIP", "#E8710A")
    cid = world.add(card("Ann", "Lee", groups=["Family"]), kept=[world.a_home, world.a_work])
    world.hub.tag([cid], "VIP")
    page = world.page()
    row = page.model.row_at(0)
    assert row.tags == ["VIP"] and row.groups == ["Family"] and sorted(row.accounts) == [world.a_home, world.a_work]
    assert page.model.data(page.model.index(0, 5)) == "Home Google, Work"


def test_every_control_has_an_accessible_name_and_every_icon_only_one_a_tooltip(world):
    cid = world.add(card("Ann", "Lee", email="a@x.example", phone="07700900123"), kept=[world.a_home])
    other = world.add(card("Bob", "Bell"))
    page = world.page()
    page.select_ids([cid])
    settle(page)
    page.toolbar.history_button.setChecked(True)
    settle(page)
    page.toolbar.history_button.setChecked(False)
    page.bulk.show()
    missing = []
    for w in page.findChildren(QAbstractButton):
        if not (w.accessibleName() or w.text()):
            missing.append(("button", w.toolTip()))
        if isinstance(w, QToolButton) and not w.text() and not w.toolTip():
            missing.append(("icon-only without tooltip", w.accessibleName()))
    for w in page.findChildren(QLineEdit):
        if w.parent() is not None and w.parent().inherits("QComboBox"):
            continue
        if not (w.accessibleName() or w.placeholderText()):
            missing.append(("line edit", w.toolTip()))
    for w in page.findChildren(QPlainTextEdit):
        if not w.accessibleName():
            missing.append(("text edit", ""))
    for w in page.findChildren(QComboBox):
        if not w.accessibleName():
            missing.append(("combo", w.toolTip()))
    for w in (page.table, page.cards, page.sidebar.tree, page.history.tree, page.editor, page.bulk):
        if not w.accessibleName():
            missing.append(("view", type(w).__name__))
    assert not missing, missing
    assert other


def test_the_view_toggle_is_remembered(world):
    from app.ui.state_writes import pool

    world.add(card("Ann", "Lee"))
    page = world.page()
    assert page.view_mode() == "list"
    page.toolbar.cards_button.click()
    assert page.view_mode() == "cards" and page.toolbar.cards_button.isChecked()
    pool().waitForDone(5000)
    assert world.store.get_state("ui:contacts_view") == "cards"
    again = world.page()
    assert again.view_mode() == "cards" and again.toolbar.cards_button.isChecked()
    page.toolbar.list_button.click()
    pool().waitForDone(5000)
    assert world.store.get_state("ui:contacts_view") == "list"


def test_the_sort_is_remembered_too(world):
    from app.ui.state_writes import pool

    world.add(card("Ann", "Lee", company="Zeta"))
    world.add(card("Bob", "Bell", company="Acme"))
    page = world.page()
    page.table.sortByColumn(1, Qt.SortOrder.DescendingOrder)
    pool().waitForDone(5000)
    assert world.store.get_state("ui:contacts_sort") == "company:desc"
    assert [page.model.row_at(i).company for i in range(2)] == ["Zeta", "Acme"]


# ------------------------------------------------------------------------------ editor
def _editor_for(world, page, cid):
    page.select_ids([cid])
    settle(page)
    assert page.editor.cid == cid and page.editor.isEnabled()
    return page.editor


def test_editing_round_trips_through_the_hub(world):
    cid = world.add(card("Ann", "Lee", email="ann@acme.example", phone="07700900123", company="Acme"),
                    kept=[world.a_home])
    page = world.page()
    ed = _editor_for(world, page, cid)
    assert not ed.save_button.isEnabled()                         # nothing changed, nothing to save
    ed.edits["given"].setText("Annabel")
    ed.edits["department"].setText("R&D")
    ed.edits["title"].setText("Chief Engineer")
    ed.notes.setPlainText("Met at the fair.")
    ed.lists["email"].add_row(Field("email", "home@mail.example", "home"))
    ed.lists["phone"].rows[0].label.setCurrentText("work")
    ed.lists["address"].add_row(None)
    addr = ed.lists["address"].rows[0]
    addr.parts["street"].setText("1 High St")
    addr.parts["city"].setText("London")
    addr.parts["postcode"].setText("N1 1AA")
    ed.lists["url"].add_row(Field("url", "https://acme.example", "work"))
    ed.lists["date"].add_row(Field("date", "1980-04-12", "birthday"))
    ed.lists["related"].add_row(Field("related", "Joe Lee", "spouse"))
    ed.lists["social"].add_row(Field("social", "@annlee", "x"))
    ed.groups.add("Family")
    ed.tags.add("VIP")
    ed.favourite.setChecked(True)
    assert ed.save_button.isEnabled()
    expected = ed.collect()
    ed.save_button.click()
    settle(page)
    stored = world.hub.get_card(cid)
    assert stored.to_json() == expected.to_json()
    assert stored.get("given") == "Annabel" and stored.get("notes") == "Met at the fair."
    assert [f.value for f in stored.values("email")] == ["ann@acme.example", "home@mail.example"]
    assert stored.values("address")[0].data == {"street": "1 High St", "city": "London", "postcode": "N1 1AA"}
    assert stored.values("date")[0].label == "birthday" and stored.favourite
    assert stored.groups == ["Family"] and world.hub.tags_of(cid) == ["VIP"]
    assert world.hub.tags()[0][2] != ""                              # a colour was given to the new tag
    assert not page.editor.save_button.isEnabled()


def test_a_save_that_changes_nothing_writes_no_journal_entry(world):
    cid = world.add(card("Ann", "Lee", email="a@x.example", phone="07700900123"))
    before = len(world.hub.history(cid))
    page = world.page()
    ed = _editor_for(world, page, cid)
    ed.save_if_dirty()
    ed._save()                                                    # even a forced Save writes nothing new
    settle(page)
    assert len(world.hub.history(cid)) == before


def test_rows_reorder_and_the_main_one_is_exclusive(world):
    cid = world.add(Card(scalars={"given": "Ann"}, fields=[Field("email", "a@x.example", "work"),
                                                           Field("email", "b@x.example", "home")]))
    page = world.page()
    ed = _editor_for(world, page, cid)
    fl = ed.lists["email"]
    fl.move_row(fl.rows[1], -1)
    assert [r.value.text() for r in fl.rows] == ["b@x.example", "a@x.example"]
    fl.rows[0].main.setChecked(True)
    fl.rows[1].main.setChecked(True)
    assert [r.main.isChecked() for r in fl.rows] == [False, True]
    ed.save_button.click()
    settle(page)
    got = world.hub.get_card(cid)
    assert [f.value for f in got.values("email")] == ["b@x.example", "a@x.example"]
    assert [f.primary for f in got.values("email")] == [False, True]


def test_a_field_row_says_where_it_came_from(world):
    cid = world.hub.create(card("Ann", "Lee", email="a@x.example"), kept_in=[world.a_home],
                           source=f"account:{world.a_home}", source_ref="people/1")
    page = world.page()
    ed = _editor_for(world, page, cid)
    tip = ed.lists["email"].rows[0].value.toolTip()
    assert "synced from Home Google" in tip
    assert "synced from Home Google" in ed.edits["given"].toolTip()


def test_a_new_contact_goes_to_the_default_account(world):
    world.accounts.update_settings(default_account_id=world.a_work)
    page = world.page()
    page.new_contact()
    ed = page.editor
    assert ed.isEnabled() and ed.cid is None and ed.kept_in() == [world.a_work]
    ed.edits["given"].setText("New")
    ed.edits["family"].setText("Person")
    ed.lists["email"].add_row(Field("email", "new@x.example", "work"))
    ed.save_button.click()
    settle(page)
    (row,) = [r for r in (page.model.row_at(i) for i in range(page.model.rowCount())) if r.display_name == "New Person"]
    assert world.hub.kept_in(row.id) == [world.a_work]
    assert page.editor.cid == row.id


def test_saving_an_empty_new_contact_asks_for_something_first(world):
    page = world.page()
    said = []
    page.notice.connect(said.append)
    page.new_contact()
    page.editor.edits["given"].setText(" ")
    page.editor._save()
    assert said and "name" in said[0]
    assert world.hub.count() == 0


def test_a_photo_is_chosen_and_removed(world):
    from tests.unit.contacts_samples import TINY_PNG
    from app.contacts.card import Photo

    cid = world.add(card("Ann", "Lee"))
    page = world.page()
    ed = _editor_for(world, page, cid)
    ed.set_photo(Photo("image/png", TINY_PNG))
    assert ed.save_button.isEnabled()
    ed.save_button.click()
    settle(page)
    assert world.hub.get_card(cid).photo.mime == "image/png"
    ed = _editor_for(world, page, cid)
    ed.remove_photo.click()
    ed.save_button.click()
    settle(page)
    assert world.hub.get_card(cid).photo is None


def test_from_my_photos_is_a_stub_that_uses_the_hosts_hook_when_there_is_one(world):
    from tests.unit.contacts_samples import TINY_PNG

    cid = world.add(card("Ann", "Lee"))
    asked = []

    class Host:
        def pick_photo_from_person(self, contact_id):
            asked.append(contact_id)
            return ("image/png", TINY_PNG)

    page = world.page(host=Host())
    ed = _editor_for(world, page, cid)
    assert ed.person_photo.isEnabled()
    ed.person_photo.click()
    assert asked == [cid] and ed.collect().photo.mime == "image/png"
    bare = world.page()
    ed2 = _editor_for(world, bare, cid)
    assert not ed2.person_photo.isEnabled() and "Not available" in ed2.person_photo.toolTip()


# --------------------------------------------------------------------------- kept in (D4)
def test_adding_an_account_is_immediate_and_needs_no_question(world):
    cid = world.add(card("Ann", "Lee"), kept=[world.a_home])
    page = world.page()
    ed = _editor_for(world, page, cid)
    ed.confirm_removal = lambda name: pytest.fail("adding must not ask")
    ed.kept_buttons[world.a_work].click()
    settle(page)
    assert sorted(world.hub.kept_in(cid)) == sorted([world.a_home, world.a_work])
    assert world.hub.history(cid)[0].action == "kept_in"


def test_removing_an_account_asks_and_only_then_acts(world):
    cid = world.add(card("Ann", "Lee"), kept=[world.a_home, world.a_work])
    for acct in (world.a_home, world.a_work):                     # both accounts hold it already
        world.hub.set_link(cid, acct, remote_id=f"r{acct}", state="synced")
    page = world.page()
    ed = _editor_for(world, page, cid)
    asked = []
    ed.confirm_removal = lambda name: (asked.append(name), False)[1]
    ed.kept_buttons[world.a_work].click()
    settle(page)
    assert asked == ["Work"]
    assert sorted(world.hub.kept_in(cid)) == sorted([world.a_home, world.a_work])
    assert ed.kept_buttons[world.a_work].isChecked()                 # the toggle went back
    ed.confirm_removal = lambda name: (asked.append(name), True)[1]
    ed.kept_buttons[world.a_work].click()
    settle(page)
    assert world.hub.kept_in(cid) == [world.a_home]
    assert world.hub.link(cid, world.a_work).state == "pending_remove"
    assert world.hub.history(cid)[0].detail["removed"] == [world.a_work]


# ---------------------------------------------------------------- the banner (D6) and history
def test_the_changed_in_two_places_banner_and_acknowledge(world):
    cid = world.add(card("Ann", "Lee"))
    world.hub.add_conflict(cid, world.a_home, "f:email:a@x", "old", "new", "remote")
    page = world.page()
    assert page.sidebar.entry_for("conflict").count == 1
    ed = _editor_for(world, page, cid)
    assert not ed.banner.isHidden()
    ed.acknowledge.click()
    settle(page)
    assert world.hub.open_conflicts(cid) == []
    assert page.editor.banner.isHidden()
    assert page.sidebar.entry_for("conflict").count == 0


def test_history_lists_the_journal_and_undo_puts_a_change_back(world):
    cid = world.add(card("Ann", "Lee", company="Acme"))
    world.hub.update(cid, card("Ann", "Lee", company="Zeta"))
    pushed = []

    class Service:
        def push_accounts(self, ids):
            pushed.append(list(ids))

    page = world.page(service=Service())
    page.select_ids([cid])
    settle(page)
    page.toolbar.history_button.setChecked(True)
    settle(page)
    assert page.right.currentWidget() is page.history
    items = [page.history.tree.topLevelItem(i) for i in range(page.history.tree.topLevelItemCount())]
    assert len(items) == 2 and "Edited" in items[0].text(1) and "Created" in items[1].text(1)
    jid = items[0].data(0, Qt.ItemDataRole.UserRole)
    assert page.history.tree.itemWidget(items[0], 2) is not None          # an Undo button on the change
    page.history.undo_requested.emit(jid)
    settle(page)
    assert world.hub.get_card(cid).get("company") == "Acme"
    assert all(isinstance(i, int) for p in pushed for i in p)


def test_after_an_undo_the_accounts_that_follow_are_pushed(world):
    cid = world.add(card("Ann", "Lee"), kept=[world.a_home])
    world.hub.set_kept_in(cid, [world.a_home, world.a_work])
    pushed = []

    class Service:
        def push_accounts(self, ids):
            pushed.append(sorted(ids))

    page = world.page(service=Service())
    page.select_ids([cid])
    settle(page)
    page.undo_last()
    settle(page)
    assert world.hub.kept_in(cid) == [world.a_home]
    assert pushed and all(isinstance(i, int) for i in pushed[0])


def test_ctrl_z_undoes_the_last_change_even_a_whole_bulk_action(world):
    ids = [world.add(card(f"P{i}", "Q")) for i in range(3)]
    page = world.page()
    page.select_ids(ids)
    settle(page)
    page._bulk("group_add", "Friends")
    settle(page)
    assert all(world.hub.get_card(i).groups == ["Friends"] for i in ids)
    page.undo_last()
    settle(page)
    assert all(world.hub.get_card(i).groups == [] for i in ids)       # one step, all three
    said = []
    page.notice.connect(said.append)
    page.undo_last()
    settle(page)
    assert said                                                      # it says what happened either way


def test_the_ctrl_z_shortcut_is_bound_on_the_page(world):
    page = world.page()
    keys = sorted(sc.key().toString() for sc in page.shortcuts)
    assert keys == ["/", "Ctrl+N", "Ctrl+S", "Ctrl+Z", "Del", "Del"]


# ------------------------------------------------------------------------------- bulk bar
def test_the_bulk_bar_groups_tags_accounts_company_favourite_and_delete(world):
    ids = [world.add(card(f"P{i}", "Q"), kept=[world.a_home]) for i in range(3)]
    page = world.page()
    page.select_ids(ids)
    settle(page)
    assert not page.bulk.isHidden() and page.bulk.count.text() == "3 picked"
    page._bulk("group_add", "Friends")
    settle(page)
    assert all(world.hub.get_card(i).groups == ["Friends"] for i in ids)
    page._bulk("group_remove", "Friends")
    settle(page)
    assert all(world.hub.get_card(i).groups == [] for i in ids)
    page._bulk("tag_add", "VIP")
    settle(page)
    assert all(world.hub.tags_of(i) == ["VIP"] for i in ids)
    page._bulk("tag_remove", "VIP")
    settle(page)
    assert all(world.hub.tags_of(i) == [] for i in ids)
    page._bulk("account_add", world.a_work)                       # 6t: keep in another account
    settle(page)
    assert all(sorted(world.hub.kept_in(i)) == sorted([world.a_home, world.a_work]) for i in ids)
    asked = []
    page.confirm = lambda title, detail, yes: (asked.append(title), False)[1]
    page._bulk("account_remove", world.a_work)
    settle(page)
    assert asked and all(len(world.hub.kept_in(i)) == 2 for i in ids)          # declined: nothing happened
    page.confirm = lambda title, detail, yes: True
    page._bulk("account_remove", world.a_work)
    settle(page)
    assert all(world.hub.kept_in(i) == [world.a_home] for i in ids)
    page._bulk("favourite", None)
    settle(page)
    assert all(world.hub.get_card(i).favourite for i in ids)
    page._bulk("favourite", None)
    settle(page)
    assert not any(world.hub.get_card(i).favourite for i in ids)
    page._ask_text = lambda title, label: "Globex"
    page._bulk("company", None)
    settle(page)
    assert all(world.hub.get_card(i).get("company") == "Globex" for i in ids)
    page._bulk("delete", None)
    settle(page)
    assert world.hub.count() == 0 and world.hub.counts()["bin"] == 3
    page.sidebar.select_key("bin")
    settle(page)
    assert page.model.rowCount() == 3 and not page.bulk.restore.isHidden()
    page.select_ids(ids)
    settle(page)
    page._bulk("restore", None)
    settle(page)
    assert world.hub.count() == 3


def test_the_bulk_menus_offer_what_exists_and_what_the_selection_has(world):
    a = world.add(card("A", "A", groups=["Family"]), kept=[world.a_home])
    b = world.add(card("B", "B"), kept=[world.a_home, world.a_fast])
    world.hub.tag([a], "VIP")
    page = world.page()
    page.select_ids([a, b])
    settle(page)
    page.bulk.add_group.menu().aboutToShow.emit()
    assert [x.text() for x in page.bulk.add_group.menu().actions() if x.text()] == ["New group…", "Family"]
    page.bulk.remove_tag.menu().aboutToShow.emit()
    assert [x.text() for x in page.bulk.remove_tag.menu().actions()] == ["VIP"]
    page.bulk.remove_account.menu().aboutToShow.emit()
    assert [x.text() for x in page.bulk.remove_account.menu().actions()] == ["Home Google", "Fastmail"]
    page.bulk.add_account.menu().aboutToShow.emit()
    assert len(page.bulk.add_account.menu().actions()) == 3


def test_delete_key_moves_the_selection_to_the_bin_and_the_bin_empties(world):
    a, b = world.add(card("A", "A")), world.add(card("B", "B"))
    page = world.page()
    page.select_ids([a])
    settle(page)
    page.delete_selected()
    settle(page)
    assert world.hub.counts()["bin"] == 1 and page.model.ids() == [b]
    page.sidebar.select_key("bin")
    settle(page)
    page.confirm = lambda *a: True
    page._bulk("empty_bin", None)
    settle(page)
    assert world.hub.counts()["bin"] == 0


# ----------------------------------------------------------------------------- sidebar
def test_smart_groups_are_created_edited_and_deleted_from_rules(world):
    from app.ui.contacts.smart import SmartDialog

    world.add(card("Ann", "Lee", company="Acme", phone="07700900123"))
    world.add(card("Bob", "Bell", company="Zeta"))
    page = world.page()
    dlg = SmartDialog(page, accounts=world.accounts.list())
    dlg.name_edit.setText("No phone")
    dlg.rows[0].field.setCurrentIndex(dlg.rows[0].field.findData("phone"))
    dlg.rows[0].op.setCurrentIndex(dlg.rows[0].op.findData("empty"))
    assert dlg.rules() == [{"field": "phone", "op": "empty", "value": ""}]
    sid = world.hub.save_smart_group(dlg.name(), dlg.rules())
    page.refresh()
    settle(page)
    page.sidebar.select_key(f"smart:{sid}")
    settle(page)
    assert [page.model.row_at(i).display_name for i in range(page.model.rowCount())] == ["Bob Bell"]
    again = SmartDialog(page, name="No phone", rules=world.hub.smart_groups()[0].rules, accounts=world.accounts.list())
    assert again.rules() == dlg.rules()
    page.confirm = lambda *a: True
    page._smart_delete(sid)
    settle(page)
    assert world.hub.smart_groups() == [] and page.sidebar.current_key() == "all"


def test_a_tag_shows_in_its_colour_and_filters(world):
    world.hub.create_tag("VIP", "#E8710A")
    a = world.add(card("Ann", "Lee"))
    world.add(card("Bob", "Bell"))
    world.hub.tag([a], "VIP")
    page = world.page()
    entry = next(e for e in page.sidebar._entries if e.kind == "tag")
    assert entry.colour == "#E8710A" and entry.count == 1
    page.sidebar.select_key(entry.key)
    settle(page)
    assert page.model.ids() == [a]
    page._tag_colour(entry.arg, "#188038")
    settle(page)
    assert world.hub.tags()[0][2] == "#188038"


def test_deleted_elsewhere_lists_what_an_account_removed(world):
    cid = world.add(card("Ann", "Lee"), kept=[world.a_home])
    world.hub.flag_deleted_remote(cid, world.a_home)
    page = world.page()
    assert page.sidebar.entry_for("deleted_elsewhere").count == 1
    page.sidebar.select_key("deleted_elsewhere")
    settle(page)
    assert page.model.ids() == [cid]


# ---------------------------------------------------------------------------- keyboard
def test_slash_focuses_the_filter_and_enter_opens_the_editor(world):
    cid = world.add(card("Ann", "Lee"))
    page = world.page()
    slash = next(sc for sc in page.shortcuts if sc.key().toString() == "/")
    slash.activated.emit()
    assert page.toolbar.filter.hasFocus() or page.toolbar.filter.window().focusWidget() is page.toolbar.filter
    page.select_ids([cid])
    settle(page)
    page.table.activated.emit(page.model.index(0, 0))
    assert page.right.currentWidget() is page.editor


def test_ctrl_n_starts_a_blank_card(world):
    page = world.page()
    ctrl_n = next(sc for sc in page.shortcuts if sc.key().toString() == "Ctrl+N")
    ctrl_n.activated.emit()
    assert page.right.currentWidget() is page.editor and page.editor.cid is None and page.editor.isEnabled()


def test_unsaved_changes_are_not_lost_by_picking_someone_else(world):
    a, b = world.add(card("Ann", "Lee")), world.add(card("Bob", "Bell"))
    page = world.page()
    ed = _editor_for(world, page, a)
    ed.edits["company"].setText("Unsaved Ltd")
    asked = []
    page._ask_discard = lambda: (asked.append(1), False)[1]
    page.select_ids([b])
    settle(page)
    assert asked and page.selected_ids() == [a] and ed.edits["company"].text() == "Unsaved Ltd"


# ---------------------------------------------------------------------------- the window
def test_the_contacts_pill_is_after_files_and_the_page_is_built_on_first_show(gui_mainwindow):
    app, window, store, _engine = gui_mainwindow
    rail = window.rail
    titles = [rail.tabText(i) for i in range(rail.count())]
    assert titles[:3] == ["Search", "Files", "Contacts"]
    index = titles.index("Contacts")
    assert rail._icons[index] == "contact"
    assert "Contacts" in [b.text() for b in rail._buttons.values()]
    assert window._tab_index[window.contacts_view] == index
    assert not window.contacts_view.is_built()
    window.rail.setCurrentIndex(index)
    settle()
    assert window.contacts_view.is_built()
    page = window.contacts_view.page
    settle(page)
    assert page.toolbar.new_button.accessibleName() == "New contact"
    window.rail.setCurrentIndex(0)


def test_the_menus_reach_contacts(gui_mainwindow):
    app, window, store, _engine = gui_mainwindow
    labels = {a.text(): a for a in window._menu_actions}
    assert "Contacts" in labels and "New contact" in labels
    labels["Contacts"].trigger()
    assert window.rail.currentIndex() == window._tab_index[window.contacts_view]
    window.rail.setCurrentIndex(0)


# ------------------------------------------------------------------- quick actions, theme
def test_the_right_click_menu_copies_and_offers_the_tools(world, monkeypatch):
    from PySide6.QtGui import QGuiApplication

    from app.ui.contacts import quick

    cid = world.add(card("Ann", "Lee", email="ann@x.example", phone="07700900123"))
    page = world.page()
    page.select_ids([cid])
    settle(page)
    shown = {}

    def fake_open(menu, pos):
        shown["menu"] = menu
    monkeypatch.setattr(quick, "open_menu", fake_open)
    quick.show_menu(page, page.mapToGlobal(page.rect().center()))
    actions = {a.text(): a for a in shown["menu"].actions() if a.text()}
    assert {"Copy email address", "Copy phone number", "Copy as vCard", "Show QR code or map…",
            "Move to the bin"} <= set(actions)
    assert all(not a.icon().isNull() for a in actions.values())
    actions["Copy email address"].trigger()
    assert QGuiApplication.clipboard().text() == "ann@x.example"
    actions["Copy as vCard"].trigger()
    settle(page)
    assert "BEGIN:VCARD" in QGuiApplication.clipboard().text() and "Ann" in QGuiApplication.clipboard().text()


def test_a_theme_change_redraws_the_page_without_losing_anything(world):
    from app.ui.theme import palette_for

    cid = world.add(card("Ann", "Lee", email="a@x.example"))
    page = world.page()
    page.select_ids([cid])
    settle(page)
    page.retint(palette_for("dark"))
    page.retint(palette_for("light"))
    assert page.selected_ids() == [cid] and page.editor.cid == cid


def test_typing_a_slash_or_pressing_ctrl_z_inside_the_editor_belongs_to_the_editor(world):
    from PySide6.QtTest import QTest

    cid = world.add(card("Ann", "Lee"))
    page = world.page()
    ed = _editor_for(world, page, cid)
    page.window().activateWindow()
    row = ed.lists["url"].add_row(None, focus=True)
    row.value.setFocus()
    QTest.keyClicks(row.value, "http://x")
    assert row.value.text() == "http://x"                                  # '/' was typed, not a shortcut
    before = len(world.hub.history(cid))
    QTest.keyClick(row.value, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    settle(page)
    assert len(world.hub.history(cid)) == before                           # text undo, not a journal undo


# --------------------------------------------------------------- rule 5, kept by a test
def test_the_page_never_calls_the_hub_or_the_accounts_off_a_worker():
    """Every call on `hub` or `accounts` is inside a nested function or lambda (the worker body),
    or in `loader.py`, whose functions exist to be run by `run_task`."""
    import ast
    from pathlib import Path

    ui = Path(__file__).resolve().parents[2] / "app" / "ui"
    files = [ui / "contacts_view.py", *sorted((ui / "contacts").glob("*.py"))]
    bad = []
    for path in files:
        if path.name in ("loader.py", "openers.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def walk(node, depth):
            for child in ast.iter_child_nodes(node):
                nested = depth + (1 if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) else 0)
                if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                    owner = child.func.value
                    name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
                    if name in ("hub", "accounts") and depth < 2:
                        bad.append(f"{path.name}:{child.lineno} {name}.{child.func.attr}()")
                walk(child, nested)
        walk(tree, 0)
    assert not bad, "the window's thread must not query - hand these to a worker:\n" + "\n".join(bad)
