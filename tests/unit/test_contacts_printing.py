"""Order 1k 6q - address labels, envelopes and a printed address book.

Layer: L7

Every function also returns where it put things (millimetres, from the page's
top-left), so a known sheet is checked by arithmetic rather than by pixels.
"""

from __future__ import annotations

import re

import pytest

from app.contacts import printing as pr
from app.contacts.card import Card, Field
from tests.unit.contacts_samples import addr, person, rich_card


def _with_address(i: int, label="home") -> Card:
    c = person(f"G{i}", f"F{i:02d}")
    c.fields.append(addr(f"{i} High St", "London", "", f"N{i % 9 + 1} 1AA", "UK", label))
    return c


def _pages(path) -> int:
    data = path.read_bytes()
    assert data.startswith(b"%PDF")
    return len(re.findall(rb"/Type\s*/Page(?![s\w])", data))


def test_every_sheet_fits_its_page_and_has_a_real_size():
    assert {"L7163", "L7160", "5160"} <= set(pr.LABEL_SHEETS)
    for code, s in pr.LABEL_SHEETS.items():
        assert s.code == code and s.cols >= 1 and s.rows >= 1
        last = pr.position_rect(s, s.per_page)
        assert last[0] + last[2] <= s.page_w_mm + 0.01 and last[1] + last[3] <= s.page_h_mm + 0.01, code
        # labels never overlap their neighbours
        assert s.pitch_x_mm >= s.label_w_mm - 0.01 and s.pitch_y_mm >= s.label_h_mm - 0.01, code


def test_known_sheet_sizes():
    l7163 = pr.LABEL_SHEETS["L7163"]
    assert (l7163.per_page, l7163.cols, l7163.rows) == (14, 2, 7)
    assert (l7163.label_w_mm, l7163.label_h_mm) == (99.1, 38.1)
    assert (l7163.page_w_mm, l7163.page_h_mm) == (210.0, 297.0)
    l7160 = pr.LABEL_SHEETS["L7160"]
    assert (l7160.per_page, l7160.label_w_mm, l7160.label_h_mm) == (21, 63.5, 38.1)
    a5160 = pr.LABEL_SHEETS["5160"]
    assert (a5160.per_page, a5160.cols, a5160.rows) == (30, 3, 10)
    assert (a5160.page_w_mm, a5160.page_h_mm) == (215.9, 279.4)
    assert round(a5160.label_w_mm, 1) == 66.7 and a5160.label_h_mm == 25.4


def test_labels_for_30_contacts_on_avery_l7163(tmp_path):
    cards = [_with_address(i) for i in range(30)]
    path = tmp_path / "labels.pdf"
    lay = pr.labels_pdf(cards, "L7163", "home", path)
    assert lay.pages == 3 and _pages(path) == 3
    assert len(lay.placements) == 30 and lay.skipped == []
    per_page = [sum(1 for p in lay.placements if p.page == n) for n in (1, 2, 3)]
    assert per_page == [14, 14, 2]
    first, second, third = (lay.placements[0], lay.placements[1], lay.placements[2])
    assert (first.position, second.position, third.position) == (1, 2, 3)
    assert (first.w_mm, first.h_mm) == (99.1, 38.1)
    assert second.x_mm - first.x_mm == pytest.approx(101.6) and second.y_mm == first.y_mm
    assert third.y_mm - first.y_mm == pytest.approx(38.1) and third.x_mm == first.x_mm
    assert first.x_mm == pytest.approx(4.7, abs=0.2) and first.y_mm == pytest.approx(15.1, abs=0.2)
    assert [p.index for p in lay.placements] == list(range(30))


def test_start_position_skips_used_labels(tmp_path):
    cards = [_with_address(i) for i in range(30)]
    lay = pr.labels_pdf(cards, pr.LABEL_SHEETS["L7163"], "home", tmp_path / "l.pdf", start_position=5)
    assert lay.placements[0].position == 5 and lay.placements[0].page == 1
    assert sum(1 for p in lay.placements if p.page == 1) == 10
    assert lay.pages == 3
    with pytest.raises(ValueError):
        pr.labels_pdf(cards, "L7163", "home", tmp_path / "l2.pdf", start_position=15)
    with pytest.raises(ValueError):
        pr.labels_pdf(cards, "L7163", "home", tmp_path / "l3.pdf", start_position=0)


def test_labels_choose_the_address_and_report_who_has_none(tmp_path):
    a = _with_address(1, "home")
    b = _with_address(2, "work")
    c = person("No", "Address")
    d = _with_address(3, "Home")                                        # label case does not matter
    lay = pr.labels_pdf([a, b, c, d], "L7160", "home", tmp_path / "l.pdf")
    assert [p.index for p in lay.placements] == [0, 3] and lay.skipped == [1, 2]
    lay = pr.labels_pdf([a, b, c, d], "L7160", "", tmp_path / "l2.pdf")      # any address: the first one
    assert [p.index for p in lay.placements] == [0, 1, 3] and lay.skipped == [2]
    with pytest.raises(ValueError, match="address"):
        pr.labels_pdf([c], "L7160", "", tmp_path / "l3.pdf")
    with pytest.raises(ValueError, match="sheet"):
        pr.labels_pdf([a], "NOPE", "", tmp_path / "l4.pdf")


def test_label_text_is_the_name_then_the_address():
    c = _with_address(7)
    lines = pr.label_lines(c, "home", height_mm=38.1)
    assert lines[0] == "G7 F07" and "7 High St" in lines and any("N8 1AA" in x for x in lines)
    assert lines[-1] == "UK"
    multi = rich_card()
    assert "1 High St" in pr.label_lines(multi, "home", height_mm=38.1)           # street with a newline splits


@pytest.mark.parametrize("size,dims", [("DL", (220.0, 110.0)), ("C5", (229.0, 162.0)),
                                       ("C4", (324.0, 229.0)), ("No10", (241.3, 104.8))])
def test_envelope_sizes(tmp_path, size, dims):
    sender = _with_address(0, "work")
    recips = [_with_address(i) for i in range(1, 4)]
    path = tmp_path / f"{size}.pdf"
    lay = pr.envelope_pdf(sender, recips, size, path)
    assert lay.pages == 3 == _pages(path)
    assert lay.page_size_mm == dims
    senders = [p for p in lay.placements if p.kind == "sender"]
    tos = [p for p in lay.placements if p.kind == "recipient"]
    assert len(senders) == 3 and len(tos) == 3
    for p in lay.placements:
        assert 0 <= p.x_mm and p.x_mm + p.w_mm <= dims[0] and 0 <= p.y_mm and p.y_mm + p.h_mm <= dims[1]
    assert tos[0].x_mm > senders[0].x_mm and tos[0].y_mm > senders[0].y_mm


def test_envelope_without_sender_and_with_unaddressed_recipient(tmp_path):
    lay = pr.envelope_pdf(None, [_with_address(1), person("No", "Address")], "DL", tmp_path / "e.pdf")
    assert lay.pages == 1 and lay.skipped == [1] and not any(p.kind == "sender" for p in lay.placements)
    with pytest.raises(ValueError):
        pr.envelope_pdf(None, [person("No", "Address")], "DL", tmp_path / "e2.pdf")
    with pytest.raises(ValueError):
        pr.envelope_pdf(None, [_with_address(1)], "A0", tmp_path / "e3.pdf")


def test_address_book_is_a_to_z_with_headings(tmp_path):
    names = [("Zed", "Zulu"), ("Ann", "Adams"), ("Bob", "baker"), ("Cy", "Émile"), ("Di", "123"),
             ("Ed", "Adler")]
    cards = [person(g, f, email=f"{g}@x.example", phone="+441632960001") for g, f in names]
    cards.append(Card(scalars={"company": "Acme Ltd"}))
    cards.extend(person(f"G{i}", f"Smith{i}", email=f"s{i}@x.example") for i in range(150))
    path = tmp_path / "book.pdf"
    lay = pr.address_book_pdf(cards, path, with_photos=False)
    heads = [p.text for p in lay.placements if p.kind == "heading"]
    assert heads == ["A", "B", "E", "S", "Z", "#"]                      # accents fold; digits go last
    entries = [p for p in lay.placements if p.kind == "entry"]
    assert sorted(p.index for p in entries) == list(range(len(cards)))
    order = [cards[p.index].get("family") or cards[p.index].get("company") for p in entries]
    assert order[:3] == ["Acme Ltd", "Adams", "Adler"]
    assert lay.pages > 1 and _pages(path) == lay.pages
    # a heading is never the last thing in a column
    for h in (p for p in lay.placements if p.kind == "heading"):
        assert any(e.page == h.page and abs(e.x_mm - h.x_mm) < 1 and e.y_mm > h.y_mm for e in entries)
    # nothing leaves the page
    assert all(p.x_mm + p.w_mm <= 210.01 and p.y_mm + p.h_mm <= 297.01 for p in lay.placements)


def test_address_book_with_photos(tmp_path):
    cards = [rich_card(), person("Bo", "Bell", email="bo@x.example")]
    lay = pr.address_book_pdf(cards, tmp_path / "p.pdf", with_photos=True)
    with_photo = [p for p in lay.placements if p.kind == "entry"]
    assert len(with_photo) == 2 and lay.pages == 1
    plain = pr.address_book_pdf(cards, tmp_path / "q.pdf", with_photos=False)
    assert (tmp_path / "p.pdf").stat().st_size > (tmp_path / "q.pdf").stat().st_size
