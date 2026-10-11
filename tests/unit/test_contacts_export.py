"""Order 1k 6j - exporting contacts, each format round-tripped.

Layer: L7

"Exported, imported into an empty book, compared field by field": the comparison
is `card.units`, cut to what the layout can hold (`exporters.LAYOUT_CAPS`).
"""

from __future__ import annotations

import pytest

from app.contacts import exporters as exp
from app.contacts import importers as imp
from app.contacts.card import Card, Field, units
from tests.unit.contacts_samples import google_card, outlook_card, person, rich_card


def _same(a: list[Card], b: list[Card], caps) -> None:
    assert len(a) == len(b)
    for x, y in zip(a, b):
        ux, uy = units(x, caps), units(y, caps)
        assert ux == uy, {k: (ux.get(k), uy.get(k)) for k in set(ux) | set(uy) if ux.get(k) != uy.get(k)}


def _cards():
    second = Card(scalars={"given": "Zoe", "family": "Zed", "company": "Zed & Co"},
                  fields=[Field("email", "zoe@zed.example"), Field("phone", "07700 900555", "home")])
    only_company = Card(scalars={"company": "Just A Company"}, fields=[Field("email", "info@jac.example")])
    return [rich_card(), second, only_company]


@pytest.mark.parametrize("version", ["3.0", "4.0"])
def test_vcard_round_trip_one_file(version):
    cards = _cards()
    text = exp.export_vcard(cards, version=version)
    assert text.count("BEGIN:VCARD") == 3 and f"VERSION:{version}" in text
    back = [c.card for c in imp.import_vcard(text).contacts]
    _same(cards, back, exp.LAYOUT_CAPS["vcard"])


def test_vcard_holds_everything_including_favourite_and_labelled_im():
    c = rich_card()
    for v in ("3.0", "4.0"):
        back = imp.import_vcard(exp.export_vcard([c], version=v)).contacts[0].card
        assert back.favourite and back.photo is not None and back.photo.data == c.photo.data
        assert [(f.kind, f.label, f.value) for f in back.fields if f.kind in ("im", "related")] == [
            ("related", "spouse", "Bob Lee"), ("im", "skype", "ann.skype")]
    assert exp.LAYOUT_CAPS["vcard"].favourite and exp.LAYOUT_CAPS["plain"].photo


def test_vcard_one_file_per_contact():
    cards = _cards()
    files = exp.export_vcard(cards, version="3.0", one_file=False)
    assert len(files) == 3 and len({name for name, _ in files}) == 3
    assert all(name.endswith(".vcf") and text.count("BEGIN:VCARD") == 1 for name, text in files)
    assert files[0][0].startswith("Dr Ann") or "Ann" in files[0][0]
    back = [imp.import_vcard(t).contacts[0].card for _, t in files]
    _same(cards, back, exp.LAYOUT_CAPS["vcard"])
    assert all("/" not in n and "\\" not in n and ":" not in n for n, _ in files)


def test_vcard_version_is_checked():
    with pytest.raises(ValueError):
        exp.export_vcard([rich_card()], version="2.1")


def test_plain_csv_holds_everything():
    cards = _cards()
    text = exp.export_csv(cards, layout="plain")
    p = imp.import_csv(text)
    assert p.format == "plain"
    _same(cards, [c.card for c in p.contacts], exp.LAYOUT_CAPS["plain"])
    back = p.contacts[0].card
    assert back.photo is not None and back.favourite and back.values("email")[0].primary


def test_google_csv_round_trip_and_columns():
    cards = [google_card(), person("Bo", "Bell", email="bo@x.example", phone="07700 900111")]
    text = exp.export_csv(cards, layout="google")
    first = text.splitlines()[0]
    assert "E-mail 1 - Label" in first and "E-mail 2 - Value" in first and "Labels" in first
    p = imp.import_csv(text)
    assert p.format == "google"
    _same(cards, [c.card for c in p.contacts], exp.LAYOUT_CAPS["google"])
    assert not exp.LAYOUT_CAPS["google"].photo


def test_outlook_csv_round_trip():
    cards = [outlook_card()]
    p = imp.import_csv(exp.export_csv(cards, layout="outlook"))
    assert p.format == "outlook"
    _same(cards, [c.card for c in p.contacts], exp.LAYOUT_CAPS["outlook"])


def test_outlook_csv_drops_what_it_cannot_hold_without_failing():
    c = rich_card()
    text = exp.export_csv([c], layout="outlook")
    back = imp.import_csv(text).contacts[0].card
    assert not back.photo and not back.favourite
    assert back.get("family") == "O'Neil-Smith" and back.values("email")


def test_csv_text_is_safe_for_excel_to_open(tmp_path):
    c = person("Eve", "Evil", email="eve@x.example")
    c.scalars["notes"] = "=HYPERLINK(\"http://bad\")"
    path = tmp_path / "out.csv"
    exp.write_csv([c], path, layout="plain")
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                          # Excel needs the BOM for UTF-8
    assert imp.import_file(path).contacts[0].card.get("notes") == c.get("notes")


def test_xlsx_round_trip_and_nothing_executes(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    evil = person("Eve", "Evil", email="eve@x.example")
    evil.scalars["notes"] = "=1+1"
    cards = _cards()[:2] + [evil]
    for c in cards:
        c.photo = None                                              # a cell cannot hold a photo: documented
    path = tmp_path / "book.xlsx"
    exp.export_xlsx(cards, path)
    wb = openpyxl.load_workbook(path)
    cells = [c for row in wb.active.iter_rows() for c in row if c.value == "=1+1"]
    assert cells and all(c.data_type == "s" for c in cells)         # stored as text, never as a formula
    back = [c.card for c in imp.import_file(path).contacts]
    _same(cards, back, exp.LAYOUT_CAPS["xlsx"])
    assert not exp.LAYOUT_CAPS["xlsx"].photo


def test_unknown_layout_is_refused():
    with pytest.raises(ValueError):
        exp.export_csv([rich_card()], layout="yahoo")


def test_empty_list_still_exports_something_valid():
    assert exp.export_vcard([]) == ""
    assert exp.export_csv([], layout="plain").splitlines()[0].startswith("Prefix")


def _pdf_pages(path) -> int:
    data = path.read_bytes()
    assert data.startswith(b"%PDF")
    return data.count(b"/Type /Page\n") + data.count(b"/Type /Page ") + data.count(b"/Type/Page\n")


def test_pdf_list_paginates_and_describes_its_layout(tmp_path):
    cards = [person(f"Given{i}", f"Family{i:03d}", email=f"p{i}@x.example", phone="+441632960001")
             for i in range(120)]
    path = tmp_path / "list.pdf"
    lay = exp.export_pdf_list(cards, path, "My contacts")
    assert path.stat().st_size > 1000 and lay.pages >= 3 and _pdf_pages(path) == lay.pages
    assert [p.index for p in lay.placements] == list(range(120))
    assert all(p.x_mm >= 0 and p.y_mm >= 0 and p.x_mm + p.w_mm <= 210 and p.y_mm + p.h_mm <= 297
               for p in lay.placements)
    assert lay.page_size_mm == (210.0, 297.0)


def test_pdf_cards_grid_is_ten_business_cards_a_page(tmp_path):
    cards = [person(f"G{i}", f"F{i}", email=f"p{i}@x.example", phone="020 7946 0018",
                    company="Acme", title="Buyer") for i in range(23)]
    cards[0] = rich_card()                                           # has a photo
    cards[1].photo = None
    path = tmp_path / "cards.pdf"
    lay = exp.export_pdf_cards(cards, path)
    assert lay.pages == 3 and len(lay.placements) == 23
    assert {(round(p.w_mm, 1), round(p.h_mm, 1)) for p in lay.placements} == {(85.0, 55.0)}
    assert sum(1 for p in lay.placements if p.page == 1) == 10
    assert _pdf_pages(path) == 3


def test_pdf_survives_non_latin_names_and_bad_photos(tmp_path):
    from app.contacts.card import Photo
    c = person("王", "小明", email="w@x.example")
    c.photo = Photo("image/jpeg", b"not an image")
    path = tmp_path / "x.pdf"
    lay = exp.export_pdf_cards([c], path)
    assert path.stat().st_size > 500 and lay.pages == 1
    assert lay.warnings                                              # the bad photo is reported, not raised
    exp.export_pdf_list([c], tmp_path / "y.pdf", "T")


def test_pdf_list_of_nothing_is_an_error_with_a_plain_message(tmp_path):
    with pytest.raises(ValueError, match="no contacts"):
        exp.export_pdf_list([], tmp_path / "e.pdf", "T")
