"""Order 1k 6g, 6i, 6n, 6o, 6p, 6u - the Contacts tool dialogs that change the book.

Layer: L5

Duplicates, merge, clean-up and import, driven the way the page drives them, on a real hub. Each claim the
order makes about them is a test here: nothing is added before the person confirms (6i); 'Merge all safe
matches' never touches a name-only group and one undo restores all of it (6n, 6u); a rejected pair is not
suggested again after a rescan (6n.3); the merge preview is the card that is written (6o); a clean-up and
an import each undo in one step (6p, 6i).
"""

from __future__ import annotations

import hashlib
import json

import pytest
from PySide6.QtCore import Qt

from app.contacts.card import Capabilities, Card, Field, Photo, units
from app.ui.contacts import openers
from app.ui.contacts_tools import actions
from app.ui.contacts_tools.cleanup_dialog import CleanupDialog
from app.ui.contacts_tools.duplicates_dialog import DuplicatesDialog
from app.ui.contacts_tools.import_dialog import ImportDialog
from app.ui.contacts_tools.merge_dialog import MergeDialog
from tests.unit.test_contacts_tools_ui_common import (  # noqa: F401 - world is a fixture
    open_dialog, settle, unnamed_controls, world)


def _live_ids(world) -> set[int]:
    return set(world.live())


def _batches(world) -> list[str]:
    return [r[0] for r in world.hub.conn.execute(
        "SELECT DISTINCT batch_id FROM contact_journal WHERE batch_id IS NOT NULL AND action != 'undo'")]


# --------------------------------------------------------------------------
# The doors
# --------------------------------------------------------------------------

def test_every_door_is_registered():
    import app.ui.contacts_tools  # noqa: F401

    for name in ("import", "export", "duplicates", "merge", "cleanup", "print", "qr", "enrich"):
        assert name in openers.OPENERS, name
        assert openers.is_registered(name)


# --------------------------------------------------------------------------
# Duplicates (6n, 6u)
# --------------------------------------------------------------------------

def _seed_duplicates(world):
    safe_a = [world.add("Ann", "Lee", email="ann@x.example", company="Acme"),
              world.add("A.", "Lee", email="ANN@x.example", phone="07700 900123")]
    safe_b = [world.add("Cy", "Reed", phone="+44 7700 900555"),
              world.add("Cyrus", "Reed", phone="07700 900555")]
    name_only = [world.add("Bob", "Stone", email="bob@one.example", company="Widgets"),
                 world.add("Bob", "Stone", email="bob@two.example", company="Widgets")]
    alone = world.add("Dee", "Unique", email="dee@x.example")
    return safe_a, safe_b, name_only, alone


def test_scan_shows_groups_why_confidence_and_the_total(world, qtbot):
    safe_a, safe_b, name_only, _ = _seed_duplicates(world)
    dlg = open_dialog(qtbot, DuplicatesDialog, world.ctx)
    assert dlg.summary.text().startswith("3 groups, 6 contacts")
    assert dlg.table.rowCount() == 3
    whys = [dlg.table.item(r, 0).text() for r in range(3)]
    assert "same email" in whys and "same phone" in whys and "same name and company" in whys
    assert all(dlg.table.item(r, 1).text().endswith("%") for r in range(3))
    safe_flags = {dlg.table.item(r, 0).text(): dlg.table.item(r, 3).text() for r in range(3)}
    assert safe_flags["same name and company"] == "Look first" and safe_flags["same email"] == "Safe"


def test_merge_all_safe_never_touches_a_name_only_group_and_one_undo_restores_all(world, qtbot):
    safe_a, safe_b, name_only, alone = _seed_duplicates(world)
    before = {i: c.dumps() for i, c in world.live().items()}
    dlg = open_dialog(qtbot, DuplicatesDialog, world.ctx)

    steps = dlg.plan_safe()                                    # the list shown before anything happens
    assert sorted(tuple(sorted(s.ids)) for s in steps) == sorted([tuple(sorted(safe_a)), tuple(sorted(safe_b))])
    assert not {i for s in steps for i in s.ids} & set(name_only)
    assert _live_ids(world) == set(before)                      # planning wrote nothing

    dlg.run_merge_safe(steps)
    settle(qtbot)
    live = _live_ids(world)
    assert set(name_only) <= live and alone in live             # untouched
    assert len(live) == len(before) - 2                         # two groups folded to one each
    assert len({tuple(sorted(s.ids)) for s in steps}) == 2
    assert dlg.undo_button.isVisible() is False or dlg.undo_button.isHidden() is False
    assert dlg.last_batch and len(_batches(world)) == 1         # ONE journal batch for both merges
    for cid in name_only:
        assert world.hub.get_card(cid).dumps() == before[cid]

    dlg.undo_merge()
    settle(qtbot)
    assert _live_ids(world) == set(before)                      # one button, everything back
    assert {i: c.dumps() for i, c in world.live().items()} == before
    assert dlg.table.rowCount() == 3                            # the groups are back on screen


def test_a_name_only_group_cannot_be_merged_by_the_batch_even_if_handed_to_it(world):
    _, _, name_only, _ = _seed_duplicates(world)
    from app.contacts import dupes

    cards = dict(world.hub.all_cards())
    groups = dupes.scan_duplicates(list(cards.items()), set(), world.hub.region)
    unsafe = [g for g in groups if not g.safe]
    assert unsafe and set(unsafe[0].ids) == set(name_only)
    forced = [dupes.DuplicateGroup(unsafe[0].ids, unsafe[0].reasons, unsafe[0].confidence, True)]   # lies about itself
    assert actions.plan_safe_merges(forced, cards, world.hub.region) == []


def test_a_rejected_pair_is_not_suggested_again_after_a_rescan(world, qtbot):
    _, _, name_only, _ = _seed_duplicates(world)
    dlg = open_dialog(qtbot, DuplicatesDialog, world.ctx)
    row = next(r for r in range(dlg.table.rowCount()) if dlg.table.item(r, 0).text() == "same name and company")
    dlg.table.selectRow(row)
    dlg.not_a_duplicate()
    settle(qtbot)
    assert all(dlg.table.item(r, 0).text() != "same name and company" for r in range(dlg.table.rowCount()))
    assert dlg.tabs.tabText(1) == "Rejected (1)"
    assert world.hub.rejected_pairs() == {tuple(sorted(name_only))}

    again = open_dialog(qtbot, DuplicatesDialog, world.ctx)       # a fresh scan of the same book
    assert all(again.table.item(r, 0).text() != "same name and company" for r in range(again.table.rowCount()))
    again.scan_button.click()
    settle(qtbot)
    assert again.rejected_table.rowCount() == 1

    again.rejected_table.selectRow(0)
    again.suggest_button.click()
    settle(qtbot)
    assert world.hub.rejected_pairs() == set()
    assert any(again.table.item(r, 0).text() == "same name and company" for r in range(again.table.rowCount()))


def test_the_scan_is_kept_until_the_book_changes(world):
    _seed_duplicates(world)
    first = actions.scan_book(world.hub)
    assert not first.cached
    assert actions.scan_book(world.hub).cached                   # same book: no rescan
    world.add("New", "Person", email="new@x.example")
    assert not actions.scan_book(world.hub).cached               # the book moved
    cid = next(iter(world.live()))
    card = world.hub.get_card(cid)
    card.scalars["notes"] = "edited"
    world.hub.update(cid, card)
    assert not actions.scan_book(world.hub).cached               # an edit to any contact counts
    assert actions.scan_book(world.hub).cached


def test_review_opens_the_merge_for_the_selected_group(world, qtbot, monkeypatch):
    safe_a, _, _, _ = _seed_duplicates(world)
    dlg = open_dialog(qtbot, DuplicatesDialog, world.ctx)
    opened = {}

    class Fake:
        def __init__(self, ctx, ids, parent=None):
            opened["ids"] = ids

        def exec(self):
            return 0

    monkeypatch.setattr("app.ui.contacts_tools.merge_dialog.MergeDialog", Fake)
    row = next(r for r in range(dlg.table.rowCount()) if dlg.table.item(r, 0).text() == "same email")
    dlg.table.selectRow(row)
    dlg.review()
    assert sorted(opened["ids"]) == sorted(safe_a)


# --------------------------------------------------------------------------
# Merge (6g, 6o)
# --------------------------------------------------------------------------

GOOGLE_LIKE = Capabilities(kinds=frozenset({"email", "phone", "address", "url", "date", "related", "im", "custom"}),
                           photo=False)


def _merge_world(world):
    acc = world.accounts.add("google", "me@example.com", "Home")
    a = world.add("Ann", "Lee", email="ann@x.example", company="Acme", title="Buyer", kept_in=[acc])
    card_b = Card(scalars={"given": "Ann", "family": "Lee", "company": "Acme Ltd", "notes": "met in May"},
                  fields=[Field("email", "ann@x.example", "home"), Field("phone", "07700 900123", "mobile"),
                          Field("social", "@annlee", "twitter")],
                  photo=Photo("image/png", b"\x89PNG-fake"))
    b = world.hub.create(card_b)
    world.ctx.caps_for = lambda account: GOOGLE_LIKE
    return acc, a, b


def test_merge_preview_is_what_the_hub_writes(world, qtbot):
    acc, a, b = _merge_world(world)
    dlg = open_dialog(qtbot, MergeDialog, world.ctx, [a, b])
    assert dlg.model is not None and dlg.confirm_button.isEnabled()
    assert "s:company" in dlg._combos                            # the clash is a drop-down; Buyer / notes are not
    dlg.set_choice("s:company", 1)                               # take "Acme Ltd" from the second contact
    dlg.set_keep(next(k for k, v in dlg.check_keys().items() if "@annlee" in v), False)   # drop the twitter handle

    shown = dlg.result_card()
    assert shown.get("company") == "Acme Ltd" and shown.get("title") == "Buyer" and shown.get("notes") == "met in May"
    assert not [f for f in shown.fields if f.kind == "social"]
    assert [t for t in dlg.result_list.texts() if "Acme Ltd" in t]
    before = _live_ids(world)
    assert world.hub.get_card(a).get("company") == "Acme"       # nothing written until Confirm

    dlg.confirm_button.click()
    settle(qtbot)
    survivor = dlg.model.survivor
    written = world.hub.get_card(survivor)
    assert units(written) == units(shown)                        # the preview IS the write
    assert written.photo is not None and written.photo.data == b"\x89PNG-fake"
    assert len(_live_ids(world)) == len(before) - 1
    assert dlg.split_button.isHidden() is False and dlg.merged


def test_split_again_undoes_the_merge(world, qtbot):
    acc, a, b = _merge_world(world)
    before = {i: c.dumps() for i, c in world.live().items()}
    dlg = open_dialog(qtbot, MergeDialog, world.ctx, [a, b])
    dlg.confirm_button.click()
    settle(qtbot)
    assert len(world.live()) == 1
    dlg.split_button.click()
    settle(qtbot)
    assert {i: c.dumps() for i, c in world.live().items()} == before
    assert dlg.confirm_button.isEnabled() and not dlg.merged


def test_each_account_in_the_kept_in_set_is_shown_what_it_will_be_sent(world, qtbot):
    acc, a, b = _merge_world(world)
    dlg = open_dialog(qtbot, MergeDialog, world.ctx, [a, b])
    sent = dlg.sent_texts()
    assert list(sent) == [acc]
    lines = " | ".join(sent[acc])
    assert "ann@x.example" in lines and "07700 900123" in lines
    assert "@annlee" not in lines.split("Not sent to this account")[0]      # the account cannot hold a social handle
    assert "Not sent to this account" in lines and "social" in lines.lower()
    assert "Photo" not in lines.split("Not sent to this account")[0]        # nor a photo
    top = dlg.sent_tree.topLevelItem(0)
    assert "Home" in top.text(0)


def test_without_capabilities_the_full_card_is_shown(world, qtbot):
    acc, a, b = _merge_world(world)
    del world.ctx.caps_for
    dlg = open_dialog(qtbot, MergeDialog, world.ctx, [a, b])
    lines = " | ".join(dlg.sent_texts()[acc])
    assert "@annlee" in lines and "Not sent" not in lines


def test_a_merge_with_no_account_says_nothing_is_sent(world, qtbot):
    a = world.add("Ann", "Lee", email="ann@x.example")
    b = world.add("Ann", "Lee", phone="07700 900123")
    dlg = open_dialog(qtbot, MergeDialog, world.ctx, [a, b])
    assert dlg.sent_tree.topLevelItemCount() == 0 and "only" in dlg.sent_note.text().lower()


def test_merge_door_wants_two_contacts(world, qtbot, monkeypatch):
    import app.ui.contacts_tools as tools

    told = []
    monkeypatch.setattr(tools, "_tell", lambda parent, title, text: told.append(title))
    world.ctx.selected_ids = [1]
    assert openers.open("merge", None, world.ctx) is None
    assert told and "two or more" in told[0].lower()


# --------------------------------------------------------------------------
# Clean-up (6p)
# --------------------------------------------------------------------------

def _tab_rows(tab):
    return [(tab.table.item(r, 1).text(), tab.table.item(r, 3).text(), tab.table.item(r, 4).text())
            for r in range(tab.table.rowCount())]


def test_phone_cleanup_previews_then_applies_the_ticked_ones_as_one_batch(world, qtbot):
    a = world.add("Ann", "Lee", phone="020 7946 0018")
    b = world.add("Bo", "Bell", phone="(020) 7946 0019")
    c = world.add("Cy", "Cole", phone="+44 20 7946 0020")        # already international
    dlg = open_dialog(qtbot, CleanupDialog, world.ctx)
    rows = _tab_rows(dlg.phone)
    assert len(rows) == 2 and all(after.startswith("+44 20 7946") for _n, _b, after in rows)
    assert world.hub.get_card(a).values("phone")[0].value == "020 7946 0018"      # the preview wrote nothing

    dlg.phone.table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)             # accept one, not the other
    changed_name = dlg.phone.table.item(0, 1).text()
    dlg.apply_button.click()
    settle(qtbot)
    after = {world.hub.get_card(i).get("given"): world.hub.get_card(i).values("phone")[0].value for i in (a, b, c)}
    assert sum(1 for v in after.values() if v.startswith("+44 20 7946 00") and v != "+44 20 7946 0020") == 1
    assert dlg.last_batch and len(_batches(world)) == 1
    assert changed_name in {"Ann Lee", "Bo Bell"}

    dlg.undo_button.click()
    settle(qtbot)
    assert world.hub.get_card(a).values("phone")[0].value == "020 7946 0018"
    assert world.hub.get_card(b).values("phone")[0].value == "(020) 7946 0019"


def test_national_format_is_the_persons_choice(world, qtbot):
    world.add("Ann", "Lee", phone="+44 20 7946 0018")
    dlg = open_dialog(qtbot, CleanupDialog, world.ctx)
    assert dlg.phone.table.rowCount() == 0
    dlg.style_combo.setCurrentIndex(1)
    settle(qtbot)
    assert dlg.phone.table.rowCount() == 1 and dlg.phone.table.item(0, 4).text().startswith("020 7946")


def test_name_capitals_leave_particles_alone_unless_chosen(world, qtbot):
    world.add("ann", "van der berg")
    world.add("JOHN", "SMITH")
    world.add("Mary", "McDonald")
    dlg = open_dialog(qtbot, CleanupDialog, world.ctx)
    dlg.tabs.setCurrentWidget(dlg.names)
    settle(qtbot)
    changes = {(dlg.names.table.item(r, 3).text(), dlg.names.table.item(r, 4).text()) for r in range(dlg.names.table.rowCount())}
    assert ("ann", "Ann") in changes and ("van der berg", "van der Berg") in changes
    assert ("JOHN", "John") in changes and ("SMITH", "Smith") in changes
    assert not any(b == "McDonald" for b, _a in changes)
    dlg.apply_button.click()
    settle(qtbot)
    assert {c.get("family") for c in world.live().values()} == {"van der Berg", "Smith", "McDonald"}


def test_company_from_a_work_email_skips_free_mail_and_filled_companies(world, qtbot):
    a = world.add("Ann", "Lee", email="ann@acme-widgets.co.uk")
    world.add("Bo", "Bell", email="bo@gmail.com")
    world.add("Cy", "Cole", email="cy@other.example", company="Already")
    dlg = open_dialog(qtbot, CleanupDialog, world.ctx)
    dlg.tabs.setCurrentWidget(dlg.company)
    settle(qtbot)
    assert dlg.company.table.rowCount() == 1 and dlg.company.table.item(0, 4).text() == "Acme Widgets"
    dlg.apply_button.click()
    settle(qtbot)
    assert world.hub.get_card(a).get("company") == "Acme Widgets"
    dlg.undo_button.click()
    settle(qtbot)
    assert world.hub.get_card(a).get("company") == ""


def test_finders_list_and_the_bin_is_one_undo(world, qtbot):
    ghost = world.hub.create(Card(fields=[Field("note-ish", "x")]))
    named = world.add("Only", "Name")
    keeper = world.add("Has", "Email", email="has@x.example")
    dup = world.hub.create(Card(scalars={"given": "Dup"}, fields=[Field("email", "d@x.example"), Field("email", "D@X.example")]))
    dlg = open_dialog(qtbot, CleanupDialog, world.ctx)
    dlg.tabs.setCurrentWidget(dlg.finders)
    settle(qtbot)
    ids = lambda: {dlg.finders.computed.changes[i].id for i in range(len(dlg.finders.computed.changes))}   # noqa: E731
    dlg.finder_combo.setCurrentIndex(2)                        # nothing but a name
    settle(qtbot)
    assert named in ids() and keeper not in ids()
    dlg.finder_combo.setCurrentIndex(3)                        # the same value twice
    settle(qtbot)
    assert ids() == {dup}
    dlg.copies_button.click()
    settle(qtbot)
    assert len(world.hub.get_card(dup).values("email")) == 1
    dlg.undo_button.click()
    settle(qtbot)
    assert len(world.hub.get_card(dup).values("email")) == 2

    dlg.finder_combo.setCurrentIndex(1)                        # no email or phone
    settle(qtbot)
    assert {named, ghost} <= ids()
    dlg.bin_button.click()
    settle(qtbot)
    assert keeper in world.live() and named not in world.live()
    dlg.undo_button.click()
    settle(qtbot)
    assert named in world.live()                                # one undo brings the whole bin batch back


# --------------------------------------------------------------------------
# Import (6i)
# --------------------------------------------------------------------------

VCF = (
    "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Lee;Ann;;;\r\nFN:Ann Lee\r\nORG:Acme\r\nTITLE:Buyer\r\nEMAIL:ann@x.example\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:3.0\r\nN:New;Nina;;;\r\nFN:Nina New\r\nEMAIL:nina@x.example\r\nTEL:+44 20 7946 0018\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Nine;Nick;;;\r\nFN:Nick Nine\r\nEMAIL:nina@x.example\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Pat;Peter;;;\r\nFN:Peter Pat\r\nEMAIL:peter@x.example\r\nEND:VCARD\r\n")


def _vcf(tmp_path):
    path = tmp_path / "people.vcf"
    path.write_bytes(VCF.encode("utf-8"))
    return path


def test_preview_lists_every_card_and_the_duplicates_and_adds_nothing(world, qtbot, tmp_path):
    existing = world.add("Ann", "Lee", email="ANN@x.example", phone="07700 900123")
    path = _vcf(tmp_path)
    before = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    book = {i: c.dumps() for i, c in world.live().items()}

    dlg = open_dialog(qtbot, ImportDialog, world.ctx)
    dlg.load_files([path])
    settle(qtbot)
    assert dlg.stack.currentIndex() == 2 and len(dlg.rows) == 4
    assert dlg.table.rowCount() == 4
    ann = dlg.rows[0]
    assert ann.matches == [existing] and ann.choice == "merge" and "same email" in " ".join(ann.reasons)
    assert dlg.rows[2].same_as == 1 and dlg.rows[2].choice == "merge"       # Nick shares Nina's email, inside the file
    assert dlg.rows[1].choice == "new" and dlg.rows[3].choice == "new"
    assert "Merge into existing: Ann Lee" in [dlg.table.cellWidget(0, 3).itemText(i) for i in range(dlg.table.cellWidget(0, 3).count())]
    assert {i: c.dumps() for i, c in world.live().items()} == book        # nothing added before the person confirms
    assert (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) == before
    assert "Nothing is added until" in dlg.summary.text()


def test_the_import_is_one_batch_and_one_undo(world, qtbot, tmp_path):
    existing = world.add("Ann", "Lee", email="ann@x.example", phone="07700 900123")
    book = {i: c.dumps() for i, c in world.live().items()}
    dlg = open_dialog(qtbot, ImportDialog, world.ctx)
    dlg.load_files([_vcf(tmp_path)])
    settle(qtbot)
    dlg.set_choice(3, "skip")                                  # leave Peter out
    dlg.add_button.click()
    settle(qtbot)

    live = world.live()
    assert dlg.result.added == 1 and dlg.result.merged == 2 and dlg.result.skipped == 1
    assert len(live) == 2                                      # Ann (merged), Nina (+ Nick folded in); Peter skipped
    ann = world.hub.get_card(existing)
    assert ann.get("title") == "Buyer" and ann.get("company") == "Acme"
    assert {f.value for f in ann.values("phone")} == {"07700 900123"}      # the existing phone is kept
    nina = next(c for i, c in live.items() if i != existing)
    assert {f.value for f in nina.values("email")} == {"nina@x.example"} and nina.values("phone")
    assert len(_batches(world)) == 1                           # ONE journal batch
    sources = world.hub.sources(existing)
    assert any(src == "import" and ref == "people.vcf" for refs in sources.values() for src, ref in refs)

    dlg.undo_button.click()
    settle(qtbot)
    assert {i: c.dumps() for i, c in world.live().items()} == book        # one step puts the book back
    assert dlg.result is None


def test_import_into_the_default_account_is_offered_and_chosen(world, qtbot, tmp_path):
    acc = world.accounts.add("google", "me@example.com", "Home")
    world.accounts.update_settings(default_account_id=acc)
    dlg = open_dialog(qtbot, ImportDialog, world.ctx)
    assert dlg.keep_combo.currentData() == [acc]
    dlg.load_files([_vcf(tmp_path)])
    settle(qtbot)
    dlg.add_button.click()
    settle(qtbot)
    new = [i for i in world.live() if world.hub.kept_in(i)]
    assert new and all(world.hub.kept_in(i) == [acc] for i in new)


def test_unknown_csv_asks_for_a_mapping_then_previews(world, qtbot, tmp_path):
    path = tmp_path / "odd.csv"
    path.write_text("Full name;Mail;Mobile no;Employer;Weird\nAnn Lee;ann@x.example;07700 900123;Acme;zzz\n", encoding="utf-8")
    dlg = open_dialog(qtbot, ImportDialog, world.ctx)
    dlg.load_files([path])
    settle(qtbot)
    assert dlg.stack.currentIndex() == 1                       # the mapping step
    mapping = dlg.current_mapping()
    assert mapping["Full name"] == "name" and mapping["Mail"] == "email" and mapping["Employer"] == "company"
    dlg._map_combos["Weird"].setCurrentIndex(dlg._map_combos["Weird"].findData("custom:Weird"))
    dlg.map_button.click()
    settle(qtbot)
    assert dlg.stack.currentIndex() == 2 and len(dlg.rows) == 1
    card = dlg.rows[0].card
    assert card.get("given") == "Ann" and card.get("company") == "Acme" and card.values("custom")[0].label == "Weird"
    assert world.live() == {}


def test_indexed_vcf_files_are_offered_as_a_one_time_import(world, qtbot, tmp_path):
    path = _vcf(tmp_path)
    world.hub.conn.execute("SELECT 1")
    with world.store.write() as conn:
        conn.execute("INSERT INTO files (path, parent_dir, ext, size_bytes, mtime_ns, status, source_kind) "
                     "VALUES (?,?,?,?,?,?,?)", (str(path), str(tmp_path), ".vcf", 10, 1, "INDEXED", "file"))
        conn.execute("INSERT INTO files (path, parent_dir, ext, size_bytes, mtime_ns, status, source_kind) "
                     "VALUES (?,?,?,?,?,?,?)", (str(tmp_path / "gone.vcf"), str(tmp_path), ".vcf", 10, 1, "INDEXED", "file"))
    dlg = open_dialog(qtbot, ImportDialog, world.ctx)
    assert dlg.indexed_list.count() == 1 and dlg.indexed_list.item(0).text() == str(path)     # the missing file is not offered
    dlg.indexed_list.item(0).setCheckState(Qt.CheckState.Checked)
    assert dlg.chosen() == [path]


def test_an_unreadable_file_is_reported_and_the_rest_still_import(world, qtbot, tmp_path):
    good = _vcf(tmp_path)
    missing = tmp_path / "nope.vcf"
    dlg = open_dialog(qtbot, ImportDialog, world.ctx)
    dlg.load_files([missing, good])
    settle(qtbot)
    assert len(dlg.rows) == 4 and "nope.vcf" in dlg.status.text()


# --------------------------------------------------------------------------
# Accessibility (6k, 6m)
# --------------------------------------------------------------------------

def test_every_control_in_these_dialogs_has_a_name_and_a_tooltip(world, qtbot, tmp_path):
    _seed_duplicates(world)
    acc, a, b = _merge_world(world)
    dialogs = [
        open_dialog(qtbot, DuplicatesDialog, world.ctx),
        open_dialog(qtbot, MergeDialog, world.ctx, [a, b]),
        open_dialog(qtbot, CleanupDialog, world.ctx),
    ]
    imp = open_dialog(qtbot, ImportDialog, world.ctx)
    imp.load_files([_vcf(tmp_path)])
    settle(qtbot)
    dialogs.append(imp)
    for dlg in dialogs:
        assert unnamed_controls(dlg) == [], type(dlg).__name__
        assert dlg.windowTitle() and dlg.accessibleName()


# --------------------------------------------------------------------------
# Workers (rule 5) and the doors, end to end
# --------------------------------------------------------------------------

def test_work_runs_off_the_ui_thread_and_answers_on_it(world, qtbot):
    import threading

    from PySide6.QtWidgets import QDialog

    from app.ui.contacts_tools._common import ToolDialog

    seen = {}
    dlg = ToolDialog(world.ctx, None, title="T", glyph="merge", subtitle="s")
    qtbot.addWidget(dlg)
    assert isinstance(dlg, QDialog)
    dlg.job(lambda: seen.setdefault("worker", threading.get_ident()),
            lambda _r: seen.setdefault("answer", threading.get_ident()))
    settle(qtbot)
    assert seen["worker"] != threading.get_ident() and seen["answer"] == threading.get_ident()


def test_a_failing_worker_shows_a_plain_message_not_a_traceback(world, qtbot):
    from app.ui.contacts_tools._common import ToolDialog

    dlg = ToolDialog(world.ctx, None, title="T", glyph="merge", subtitle="s")
    qtbot.addWidget(dlg)

    def boom():
        raise RuntimeError("disk exploded")

    dlg.job(boom)
    settle(qtbot)
    assert dlg.status.text() and "Traceback" not in dlg.status.text()


@pytest.mark.parametrize("door", ["import", "export", "duplicates", "cleanup", "print", "qr", "enrich"])
def test_the_page_opens_each_tool_through_its_door(world, qtbot, monkeypatch, door):
    from PySide6.QtWidgets import QDialog

    shown = []
    monkeypatch.setattr(QDialog, "exec", lambda self: shown.append(type(self).__name__) or 0)
    world.ctx.selected_ids = [world.add("Ann", "Lee", email="ann@x.example")]
    openers.open(door, None, world.ctx)
    settle(qtbot)
    assert len(shown) == 1 and shown[0].endswith("Dialog")
    assert world.refreshed >= 1                                  # the page is told to re-read afterwards


def test_the_merge_door_opens_the_merge_dialog_for_the_selection(world, qtbot, monkeypatch):
    from PySide6.QtWidgets import QDialog

    shown = []
    monkeypatch.setattr(QDialog, "exec", lambda self: shown.append(self) or 0)
    world.ctx.selected_ids = [world.add("Ann", "Lee", email="a@x.example"), world.add("Ann", "Lee", email="b@x.example")]
    openers.open("merge", None, world.ctx)
    settle(qtbot)
    assert isinstance(shown[0], MergeDialog) and shown[0].ids == world.ctx.selected_ids
