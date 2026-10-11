"""Order 1k 6r - the QR code of a card, and the quick actions.

Layer: L7

The QR test is a real decode: `decode_matrix` below reads the module grid the
way a phone does (format bits, mask, zig-zag, de-interleave, byte segments) and
does not look at what the encoder was given. It borrows only two constant tables
from segno - the error-correction block sizes and the alignment-pattern
positions - which are data, not logic.
"""

from __future__ import annotations

import pytest
from segno import consts

from app.contacts import qr, quick_actions as qa, vcard
from app.contacts.card import Card, Field, Photo, units
from tests.unit.contacts_samples import TINY_PNG, addr, person, rich_card


# --------------------------------------------------------------------------
# A small QR reader (byte mode only; that is all a vCard needs)
# --------------------------------------------------------------------------

def _bch_format(data5: int) -> int:
    v = data5 << 10
    for i in range(14, 9, -1):
        if v & (1 << i):
            v ^= 0x537 << (i - 10)
    return ((data5 << 10) | v) ^ 0x5412


_MASKS = [
    lambda i, j: (i + j) % 2 == 0, lambda i, j: i % 2 == 0, lambda i, j: j % 3 == 0,
    lambda i, j: (i + j) % 3 == 0, lambda i, j: (i // 2 + j // 3) % 2 == 0,
    lambda i, j: (i * j) % 2 + (i * j) % 3 == 0, lambda i, j: ((i * j) % 2 + (i * j) % 3) % 2 == 0,
    lambda i, j: ((i + j) % 2 + (i * j) % 3) % 2 == 0,
]


def _function_mask(n: int, version: int) -> list[list[bool]]:
    f = [[False] * n for _ in range(n)]
    for r in range(9):                              # finders + separators + format strips
        for c in range(9):
            f[r][c] = True
    for r in range(8):
        for c in range(n - 8, n):
            f[r][c] = True
    for r in range(n - 8, n):
        for c in range(8):
            f[r][c] = True
    for c in range(n - 8, n):
        f[8][c] = True
    for r in range(n - 8, n):
        f[r][8] = True
    for k in range(n):                              # timing
        f[6][k] = f[k][6] = True
    if version >= 2:                                # alignment
        pos = consts.ALIGNMENT_POS[version - 2]
        last = pos[-1]
        for r in pos:
            for c in pos:
                if (r, c) in ((6, 6), (6, last), (last, 6)):      # these would sit on a finder
                    continue
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        f[r + dr][c + dc] = True
    if version >= 7:                                # version info blocks
        for a in range(6):
            for b in range(3):
                f[a][n - 11 + b] = True
                f[n - 11 + b][a] = True
    return f


def decode_matrix(matrix) -> bytes:
    rows = [[bool(v) for v in row] for row in matrix]
    n = len(rows)
    version = (n - 17) // 4
    # format information, second copy (row 8: right strip, then columns 7, 5..0), best match of 32
    bits = 0
    for i in range(15):
        c = n - 1 - i if i < 8 else (7 if i == 8 else 14 - i)
        bits |= int(rows[8][c]) << i
    best = min(range(32), key=lambda d: bin(_bch_format(d) ^ bits).count("1"))
    assert bin(_bch_format(best) ^ bits).count("1") <= 3, "format information unreadable"
    ec_level, mask = best >> 3, best & 7
    func = _function_mask(n, version)
    stream = []
    up = True
    col = n - 1
    while col > 0:
        if col == 6:
            col -= 1
        for k in range(n):
            r = n - 1 - k if up else k
            for c in (col, col - 1):
                if not func[r][c]:
                    stream.append(rows[r][c] ^ _MASKS[mask](r, c))
        up = not up
        col -= 2
    codewords = [int("".join("1" if b else "0" for b in stream[i:i + 8]), 2)
                 for i in range(0, len(stream) // 8 * 8, 8)]
    infos = consts.ECC[version][ec_level]
    blocks: list[list[int]] = []
    for e in infos:
        blocks.extend([] for _ in range(e.num_blocks))
    sizes = [e.num_data for e in infos for _ in range(e.num_blocks)]
    pos = 0
    for i in range(max(sizes)):                       # data codewords are interleaved
        for b, size in enumerate(sizes):
            if i < size:
                blocks[b].append(codewords[pos])
                pos += 1
    data = [cw for b in blocks for cw in b]
    bitstr = "".join(f"{cw:08b}" for cw in data)
    out = bytearray()
    i = 0
    count_bits = 8 if version < 10 else 16
    while i + 4 <= len(bitstr):
        mode = int(bitstr[i:i + 4], 2)
        i += 4
        if mode == 0:
            break
        if mode == 7:                                 # ECI: skip its designator
            i += 8
            continue
        assert mode == 4, f"unexpected mode {mode}"
        length = int(bitstr[i:i + count_bits], 2)
        i += count_bits
        for _ in range(length):
            out.append(int(bitstr[i:i + 8], 2))
            i += 8
    return bytes(out)


# --------------------------------------------------------------------------

def test_decoder_reads_a_known_code():
    import segno
    for text in (b"hello", b"x" * 300, b"BEGIN:VCARD\r\n" + b"y" * 900):
        m = segno.make(text, mode="byte", error="m", micro=False).matrix
        assert decode_matrix(m) == text


def test_qr_decodes_to_the_cards_vcard():
    c = person("Ann", "Lee", email="ann@acme.example", phone="+44 7700 900123", company="Acme", title="Buyer")
    c.fields.append(Field("url", "https://acme.example", "work"))
    c.fields.append(addr("1 High St", "London", "", "N1 1AA", "UK", "home"))
    text = decode_matrix(qr.vcard_qr_matrix(c)).decode("utf-8")
    assert text == qr.vcard_qr_text(c)
    back = vcard.parse(text)
    assert len(back) == 1
    assert units(back[0]) == units(c)                              # everything fit, so everything came back
    assert text.startswith("BEGIN:VCARD\r\nVERSION:3.0") and text.rstrip().endswith("END:VCARD")


def test_qr_is_compact_drops_the_big_things_and_decodes_non_ascii():
    c = rich_card()
    c.scalars["given"] = "Zoë"
    c.scalars["notes"] = "n" * 3000
    text = qr.vcard_qr_text(c)
    assert len(text.encode("utf-8")) <= qr.MAX_BYTES
    assert "PHOTO" not in text and "NOTE" not in text and "\r\n " not in text      # no photo, no folding
    back = vcard.parse(decode_matrix(qr.vcard_qr_matrix(c)).decode("utf-8"))[0]
    assert back.get("given") == "Zoë" and back.get("family") == c.get("family")
    assert back.values("email")[0].value == "ann@acme.example"
    assert back.values("phone")[0].value == "+44 7700 900123"
    assert len(qr.vcard_qr_matrix(c)) <= 117                                       # at most version 25: scannable


def test_qr_keeps_the_most_useful_fields_first_when_space_runs_out():
    c = person("Ann", "Lee", email="ann@acme.example", phone="+441632960001")
    for i in range(80):
        c.fields.append(Field("custom", "v" * 40, f"label{i}"))
        c.fields.append(Field("email", f"extra{i}@acme.example", "other"))
    text = qr.vcard_qr_text(c)
    assert len(text.encode()) <= qr.MAX_BYTES
    back = vcard.parse(text)[0]
    assert back.values("email")[0].value == "ann@acme.example" and back.values("phone")
    assert not back.values("custom")                 # lowest priority went first


def test_qr_svg_is_self_contained():
    c = person("Ann", "Lee", email="ann@acme.example")
    svg = qr.vcard_qr_svg(c)
    assert svg.lstrip().startswith("<svg") and "</svg>" in svg
    for bad in ("href", "<image", "<script", "url(", "@import"):
        assert bad not in svg
    assert "<?xml" not in svg
    m = qr.vcard_qr_matrix(c)
    assert len(m) == len(m[0]) and all(isinstance(v, bool) for v in m[0])


def test_qr_of_an_empty_card_is_refused_politely():
    with pytest.raises(ValueError, match="nothing"):
        qr.vcard_qr_text(Card())


# --------------------------------------------------------------------------
# quick actions
# --------------------------------------------------------------------------

def test_copy_email_and_phone():
    c = Card(fields=[Field("email", "a@x.example", "home"), Field("email", "b@x.example", "work", primary=True),
                     Field("phone", "07700 900123", "mobile"), Field("phone", "+44 20 7946 0018", "work", primary=True)])
    assert qa.copy_email(c) == "b@x.example" and qa.copy_phone(c) == "+44 20 7946 0018"
    assert qa.copy_email(c, label="home") == "a@x.example"
    assert qa.copy_email(Card()) == "" and qa.copy_phone(Card()) == ""
    c2 = Card(fields=[Field("phone", "07700 900123"), Field("phone", "020 7946 0018", "work")])
    assert qa.copy_phone(c2) == "07700 900123"                                     # as typed, first when none is main


def test_maps_url_per_site_and_nothing_is_fetched():
    a = addr("1 High St & Co", "London", "", "N1 1AA", "UK")
    urls = {s: qa.maps_url(a, s) for s in qa.MAP_SITES}
    assert set(urls) == {"google", "openstreetmap", "apple", "bing"}
    assert urls["google"].startswith("https://www.google.com/maps/search/?api=1&query=")
    assert urls["openstreetmap"].startswith("https://www.openstreetmap.org/search?query=")
    assert urls["apple"].startswith("https://maps.apple.com/?q=")
    assert urls["bing"].startswith("https://www.bing.com/maps?q=")
    for u in urls.values():
        assert "1+High+St+%26+Co" in u and "N1+1AA" in u and " " not in u and "&Co" not in u
    with pytest.raises(ValueError, match="map site"):
        qa.maps_url(a, "mapquest")
    plain = Field("address", "10 Downing Street, London")
    assert "10+Downing+Street" in qa.maps_url(plain, "bing")
    with pytest.raises(ValueError):
        qa.maps_url(Field("address", ""), "google")


def test_card_as_vcard_matches_the_exporter():
    c = rich_card()
    text = qa.card_as_vcard(c)
    assert text.startswith("BEGIN:VCARD") and text.count("BEGIN:VCARD") == 1
    from app.contacts import importers
    assert units(importers.import_vcard(text).contacts[0].card) == units(c)
    assert "VERSION:4.0" in qa.card_as_vcard(c, version="4.0")
