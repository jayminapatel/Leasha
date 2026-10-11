"""Order 1k 6j, 6q, 6r, 7 - the Contacts tool dialogs that make files, pictures and proposals.

Layer: L5

Export, print, the QR code and the enrichment shell, offscreen, on a real hub. Export is read only and each
file it writes is read back by the importer; print shows a preview before it will save; the QR dialog opens a
maps site only on a click and only the site Settings chose; enrichment keeps only what was accepted.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import Qt

from app.contacts import importers
from app.contacts.card import Card, Field, units
from app.contacts.exporters import LAYOUT_CAPS
from app.ui.contacts_tools import export_dialog, print_dialog, qr_dialog
from app.ui.contacts_tools.enrich_dialog import EnrichDialog
from app.ui.contacts_tools.export_dialog import ExportDialog, left_out, write_export
from app.ui.contacts_tools.print_dialog import PrintDialog
from app.ui.contacts_tools.qr_dialog import QrDialog
from tests.unit.contacts_samples import addr
from tests.unit.test_contacts_tools_ui_common import (  # noqa: F401 - world is a fixture
    open_dialog, settle, unnamed_controls, world)


def _book(world):
    ids = []
    for i in range(5):
        card = Card(scalars={"given": f"Given{i}", "family": f"Family{i}", "company": "Acme"},
                    fields=[Field("email", f"p{i}@x.example", "work"), Field("phone", f"+44 20 7946 00{i:02d}", "work"),
                            addr(f"{i} High St", "London", "", f"N{i} 1AA", "UK", "home")])
        ids.append(world.hub.create(card))
    return ids


def _format(dlg, kind):
    for i in range(dlg.format.count()):
        if dlg.format.itemData(i)[0] == kind:
            dlg.format.setCurrentIndex(i)
            return
    raise AssertionError(kind)


# --------------------------------------------------------------------------
# Export (6j)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("kind,suffix", [("vcard3", ".vcf"), ("vcard4", ".vcf"), ("csv-google", ".csv"),
                                         ("csv-outlook", ".csv"), ("csv-plain", ".csv"), ("xlsx", ".xlsx")])
def test_export_every_format_reads_back(world, qtbot, tmp_path, kind, suffix):
    ids = _book(world)
    before = {i: c.dumps() for i, c in world.live().items()}
    dlg = open_dialog(qtbot, ExportDialog, world.ctx)
    _format(dlg, kind)
    out = tmp_path / f"out{suffix}"
    dlg.run_export(str(out))
    settle(qtbot)
    assert dlg.last_result.count == 5 and dlg.last_result.files == [str(out)]
    back = [c.card for c in importers.import_file(out).contacts]
    assert len(back) == 5
    layout = dict(vcard3="vcard", vcard4="vcard").get(kind) or kind.split("-")[-1]
    caps = LAYOUT_CAPS[layout]
    got = sorted(units(c, caps)["s:given"] for c in back)
    assert got == [f"Given{i}" for i in range(5)]
    assert {i: c.dumps() for i, c in world.live().items()} == before          # an export only reads


def test_export_one_vcard_per_contact_into_a_folder(world, qtbot, tmp_path):
    _book(world)
    dlg = open_dialog(qtbot, ExportDialog, world.ctx)
    _format(dlg, "vcard3")
    dlg.per_contact.setChecked(True)
    folder = tmp_path / "vcards"
    dlg.run_export(str(folder))
    settle(qtbot)
    files = sorted(folder.glob("*.vcf"))
    assert len(files) == 5 and all(f.read_text(encoding="utf-8").count("BEGIN:VCARD") == 1 for f in files)
    dlg.run_export(str(folder))                                   # again: nothing is overwritten
    settle(qtbot)
    assert len(list(folder.glob("*.vcf"))) == 10


def test_export_scope_selection_and_filter(world, qtbot, tmp_path):
    ids = _book(world)
    world.ctx.selected_ids = ids[:2]
    world.ctx.visible_ids = ids[:4]
    dlg = open_dialog(qtbot, ExportDialog, world.ctx)
    assert [dlg.scope.itemText(i).split(" (")[0] for i in range(dlg.scope.count())] == [
        "Everything in my book", "The selected contacts", "The contacts in the current view"]
    assert dlg.scope.currentIndex() == 1                           # a selection is what the person most likely wants
    _format(dlg, "csv-plain")
    out = tmp_path / "sel.csv"
    dlg.run_export(str(out))
    settle(qtbot)
    assert dlg.last_result.count == 2
    dlg.scope.setCurrentIndex(2)
    dlg.run_export(str(tmp_path / "view.csv"))
    settle(qtbot)
    assert dlg.last_result.count == 4
    dlg.scope.setCurrentIndex(0)
    dlg.run_export(str(tmp_path / "all.csv"))
    settle(qtbot)
    assert dlg.last_result.count == 5


def test_export_pdf_list_and_cards(world, qtbot, tmp_path):
    _book(world)
    dlg = open_dialog(qtbot, ExportDialog, world.ctx)
    for kind in ("pdf-list", "pdf-cards"):
        _format(dlg, kind)
        out = tmp_path / f"{kind}.pdf"
        dlg.run_export(str(out))
        settle(qtbot)
        assert out.read_bytes().startswith(b"%PDF") and dlg.last_result.count == 5


def test_export_says_what_a_layout_cannot_hold_and_refuses_nothing_politely(world, qtbot, tmp_path):
    dlg = open_dialog(qtbot, ExportDialog, world.ctx)
    _format(dlg, "csv-outlook")
    assert "photos" in dlg.left_out.text() and "favourite" in dlg.left_out.text()
    _format(dlg, "vcard4")
    assert dlg.left_out.text() == "Everything is saved."
    assert left_out(LAYOUT_CAPS["google"]) and "photos" in left_out(LAYOUT_CAPS["google"])
    dlg.run_export(str(tmp_path / "none.vcf"))                     # an empty book
    settle(qtbot)
    assert "no contacts" in dlg.status.text().lower() and not (tmp_path / "none.vcf").exists()
    assert write_export(world.hub, [], "vcard3", tmp_path / "x.vcf").error


# --------------------------------------------------------------------------
# Print (6q)
# --------------------------------------------------------------------------

def test_print_previews_before_it_will_save(world, qtbot, tmp_path):
    _book(world)
    dlg = open_dialog(qtbot, PrintDialog, world.ctx)
    assert not dlg.save_button.isEnabled()                         # nothing to save until the person has seen it
    dlg.save_to(str(tmp_path / "early.pdf"))
    assert not (tmp_path / "early.pdf").exists()

    dlg.make_preview()
    settle(qtbot)
    assert dlg.preview is not None and dlg.preview.pages == 1 and dlg.save_button.isEnabled()
    assert dlg.preview.count == 5 and len(dlg.preview.placements) == 5
    pix = dlg.page_view.pixmap()
    assert pix is not None and not pix.isNull() and pix.width() > 200
    assert dlg.page_label.text() == "Page 1 of 1"
    first = dlg.preview.texts[(1, 0, "label")]
    assert first[0] == "Given0 Family0" and "0 High St" in first

    out = tmp_path / "labels.pdf"
    dlg.save_to(str(out))
    settle(qtbot)
    assert out.read_bytes().startswith(b"%PDF") and str(out) in dlg.status.text()

    dlg.start.setValue(5)                                          # an option changed: the old preview no longer counts
    assert dlg.preview is None and not dlg.save_button.isEnabled()


def test_print_labels_choose_the_sheet_the_address_and_the_start(world, qtbot):
    _book(world)
    work = Card(scalars={"given": "Wendy", "family": "Work"}, fields=[addr("1 Office Rd", "Leeds", "", "LS1 1AA", "UK", "work")])
    world.hub.create(work)
    dlg = open_dialog(qtbot, PrintDialog, world.ctx)
    dlg.sheet.setCurrentIndex(dlg.sheet.findData("L7160"))
    assert dlg.start.maximum() == 21
    dlg.address.setCurrentIndex(dlg.address.findData("work"))
    dlg.start.setValue(3)
    dlg.make_preview()
    settle(qtbot)
    assert dlg.preview.count == 6 and len(dlg.preview.placements) == 1          # only Wendy has a work address
    assert dlg.preview.placements[0].position == 3
    assert len(dlg.preview.skipped) == 5 and "left out" in dlg.status.text()


def test_print_envelopes_with_a_sender_from_the_book(world, qtbot):
    ids = _book(world)
    dlg = open_dialog(qtbot, PrintDialog, world.ctx)
    dlg.tabs.setCurrentIndex(1)
    assert dlg.sender.count() == 6                                  # "No return address" and the five people
    dlg.sender.setCurrentIndex(dlg.sender.findData(ids[0]))
    dlg.env_size.setCurrentIndex(dlg.env_size.findData("C5"))
    dlg.make_preview()
    settle(qtbot)
    kinds = [p.kind for p in dlg.preview.placements]
    assert kinds.count("recipient") == 5 and kinds.count("sender") == 5 and dlg.preview.pages == 5
    assert dlg.preview.page_size_mm == (229.0, 162.0)
    assert dlg.page_view.pixmap().width() > dlg.page_view.pixmap().height()       # landscape


def test_print_address_book_with_and_without_photos(world, qtbot):
    _book(world)
    dlg = open_dialog(qtbot, PrintDialog, world.ctx)
    dlg.tabs.setCurrentIndex(2)
    dlg.photos.setChecked(False)
    dlg.make_preview()
    settle(qtbot)
    heads = [p.text for p in dlg.preview.placements if p.kind == "heading"]
    assert heads == ["F"] and sum(1 for p in dlg.preview.placements if p.kind == "entry") == 5


def test_print_with_nothing_to_print_says_so(world, qtbot):
    dlg = open_dialog(qtbot, PrintDialog, world.ctx)
    dlg.make_preview()
    settle(qtbot)
    assert dlg.preview is None and "no contacts" in dlg.status.text().lower()
    world.add("No", "Address", email="n@x.example")
    dlg.make_preview()
    settle(qtbot)
    assert "address" in dlg.status.text().lower() and dlg.preview is None


# --------------------------------------------------------------------------
# QR (6r)
# --------------------------------------------------------------------------

def _qr_world(world, site="openstreetmap"):
    world.accounts.update_settings(maps_site=site)
    cid = world.hub.create(Card(scalars={"given": "Ann", "family": "Lee", "company": "Acme"},
                                fields=[Field("email", "ann@acme.example", "work", primary=True),
                                        Field("phone", "+44 20 7946 0018", "work"),
                                        addr("10 Downing Street", "London", "", "SW1A 2AA", "UK", "home")]))
    world.ctx.selected_ids = [cid]
    return cid


def test_qr_dialog_draws_the_code_and_copies(world, qtbot, monkeypatch):
    _qr_world(world)
    copied = []
    monkeypatch.setattr(qr_dialog, "copy_text", copied.append)
    dlg = open_dialog(qtbot, QrDialog, world.ctx)
    assert dlg.qr_pixmap is not None and dlg.qr_pixmap.width() == 300
    image = dlg.qr_pixmap.toImage()
    dark = sum(1 for x in range(0, 300, 6) for y in range(0, 300, 6) if image.pixelColor(x, y).lightness() < 100)
    assert dark > 100                                               # a real code, not a blank square
    dlg.copy_vcard.click()
    dlg.copy_email_button.click()
    dlg.copy_phone_button.click()
    assert copied[0].startswith("BEGIN:VCARD") and "ann@acme.example" in copied[0]
    assert copied[1:] == ["ann@acme.example", "+44 20 7946 0018"]


@pytest.mark.parametrize("site,prefix", [("openstreetmap", "https://www.openstreetmap.org/search?query="),
                                         ("google", "https://www.google.com/maps/search/?api=1&query="),
                                         ("bing", "https://www.bing.com/maps?q=")])
def test_maps_opens_the_chosen_site_only_on_a_click(world, qtbot, monkeypatch, site, prefix):
    _qr_world(world, site)
    opened = []
    monkeypatch.setattr(qr_dialog, "open_url", lambda url: opened.append(url) or True)
    dlg = open_dialog(qtbot, QrDialog, world.ctx)
    assert opened == []                                             # nothing opened by showing the dialog
    assert dlg.maps_button.isEnabled()
    dlg.maps_button.click()
    assert len(opened) == 1 and opened[0].startswith(prefix) and "Downing" in opened[0]


def test_qr_dialog_for_a_contact_with_no_address_or_nothing_at_all(world, qtbot):
    cid = world.hub.create(Card(scalars={"given": "Bo"}, fields=[Field("email", "bo@x.example")]))
    world.ctx.selected_ids = [cid]
    dlg = open_dialog(qtbot, QrDialog, world.ctx)
    assert not dlg.maps_button.isEnabled() and not dlg.copy_phone_button.isEnabled() and dlg.copy_email_button.isEnabled()
    empty = world.hub.create(Card())
    again = open_dialog(qtbot, QrDialog, world.ctx, None, empty)
    assert again.qr_pixmap is None and "nothing" in again.status.text().lower()


# --------------------------------------------------------------------------
# Enrich (7)
# --------------------------------------------------------------------------

def test_enrich_without_a_source_says_it_is_off(world, qtbot):
    dlg = open_dialog(qtbot, EnrichDialog, world.ctx)
    assert not dlg.find_button.isEnabled() and "not switched on" in dlg.status.text()


def test_enrich_keeps_only_what_is_accepted_and_undoes_in_one_step(world, qtbot):
    from app.contacts.enrich.base import Proposal

    a = world.add("Ann", "Lee", email="ann@acme-widgets.co.uk")
    b = world.add("Bo", "Bell", email="bo@other.example")
    proposals = [Proposal(a, "company", "Company", "", "Acme Widgets", "company", "from the email domain"),
                 Proposal(a, "title", "Job title", "", "Buyer", "signature", "Re: order, 3 May"),
                 Proposal(b, "company", "Company", "", "Other", "company", "from the email domain")]
    world.ctx.enrich = lambda: proposals
    dlg = open_dialog(qtbot, EnrichDialog, world.ctx)
    dlg.find_button.click()
    settle(qtbot)
    assert dlg.tree.topLevelItemCount() == 2 and dlg.tree.topLevelItem(0).childCount() == 2
    assert world.hub.get_card(a).get("company") == ""               # proposing changes nothing
    dlg.tree.topLevelItem(0).child(0).setCheckState(0, Qt.CheckState.Checked)       # accept Ann's company only
    dlg.accept_button.click()
    settle(qtbot)
    assert world.hub.get_card(a).get("company") == "Acme Widgets"
    assert world.hub.get_card(a).get("title") == "" and world.hub.get_card(b).get("company") == ""
    assert dlg.tree.topLevelItem(0).childCount() == 1 and dlg.undo_button.isHidden() is False
    dlg.undo_button.click()
    settle(qtbot)
    assert world.hub.get_card(a).get("company") == ""


def test_enrich_carries_on_when_its_window_closes_and_waits_to_be_accepted(world, qtbot):
    """The look-up belongs to the page: close the window, it finishes and says so; open it, accept later."""
    import threading

    from app.contacts.enrich.base import Proposal

    a = world.add("Ann", "Lee", email="ann@acme-widgets.co.uk")
    gate, said = threading.Event(), []
    proposals = [Proposal(a, "company", "Company", "", "Acme Widgets", "company", "from the email domain")]

    def slow():
        gate.wait(10)
        return proposals

    world.ctx.enrich = slow
    world.ctx.notify = said.append
    dlg = open_dialog(qtbot, EnrichDialog, world.ctx)
    dlg.find_button.click()
    assert dlg.run.running and "close this window" in dlg.status.text()
    dlg.close()
    dlg.deleteLater()                                           # the window is gone; the run is not
    gate.set()
    qtbot.waitUntil(lambda: bool(said), timeout=5000)
    assert "1 suggestion" in said[0] and "Open Enrich contacts" in said[0]
    assert world.hub.get_card(a).get("company") == ""           # nothing kept without a say-so

    again = open_dialog(qtbot, EnrichDialog, world.ctx)         # later: the suggestions are waiting
    assert again.tree.topLevelItemCount() == 1
    again.accept_all_button.click()
    settle(qtbot)
    assert world.hub.get_card(a).get("company") == "Acme Widgets"
    third = open_dialog(qtbot, EnrichDialog, world.ctx)         # and what was accepted is not offered twice
    assert third.tree.topLevelItemCount() == 0


def test_enrich_accept_all_and_a_result_with_its_own_apply(world, qtbot):
    class P:
        def __init__(self, cid, label, old, new, source):
            self.contact_id, self.field_label, self.old, self.new, self.source = cid, label, old, new, source

    a = world.add("Ann", "Lee")
    seen = []

    class Result:
        proposals = [P(a, "Company", "", "Acme", "company"), P(a, "Title", "", "Buyer", "signature")]

        def apply(self, selected):
            seen.append([p.new for p in selected])

    world.ctx.enrich = lambda: Result()
    dlg = open_dialog(qtbot, EnrichDialog, world.ctx)
    dlg.find_button.click()
    settle(qtbot)
    dlg.accept_all_button.click()
    settle(qtbot)
    assert seen == [["Acme", "Buyer"]] and dlg.tree.topLevelItemCount() == 0


def test_every_control_in_the_file_dialogs_has_a_name_and_a_tooltip(world, qtbot):
    _qr_world(world)
    world.ctx.enrich = lambda: []
    _book(world)
    for cls in (ExportDialog, PrintDialog, QrDialog, EnrichDialog):
        dlg = open_dialog(qtbot, cls, world.ctx)
        assert unnamed_controls(dlg) == [], cls.__name__
        assert dlg.windowTitle() and dlg.accessibleName()
