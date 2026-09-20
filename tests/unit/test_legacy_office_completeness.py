r"""The legacy Office readers must fail closed: a lossy read is never returned as text.

Work order 202626270114 (index tuning) section 6i. Measured against LibreOffice on
real decks, the in-process `.ppt` reader missed three classes of text:

* WordArt and shape text - the `gtextUNICODE` property of an Escher `OfficeArtFOPT`
  record, which is in no text atom (one real deck lost 51% of its words to it)
* the deck-wide Header & Footer, stored in the document container's own
  `HeadersFooters` record and not in any slide
* text boxes drawn on the master slide

Those are now read. What the reader still cannot know it has missed, it must not
paper over: the file itself declares how many text runs each slide has
(`SlidePersistAtom.numberTexts`), how many characters each Word story holds
(FIB `ccp*`) and how many the piece table holds, and a mismatch raises
`LegacyOfficeUnreadable` so the converter reads the file instead. The fall-back is
counted and summarised once per run.

The `.ppt` streams here are assembled record by record ([MS-PPT] 2.3) in a few
lines, so each test states exactly the bytes it depends on and nothing else.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

import app.extract  # noqa: F401 - importing populates the registry
from app.core.errors import AppErrorException
from app.extract import doc as doc_module
from app.extract import legacy_office
from app.extract.doc import read_doc
from app.extract.legacy_office import LegacyOfficeUnreadable
from app.extract.ppt import read_ppt

olefile = pytest.importorskip("olefile")

QUOTE = Path(__file__).resolve().parents[1] / "fixtures" / "legacy_office" / "quote.doc"


# ---------------------------------------------------------------------------
# A minimal .ppt "PowerPoint Document" stream, built by hand
# ---------------------------------------------------------------------------


def rec(record_type: int, body: bytes = b"", *, instance: int = 0, version: int = 0) -> bytes:
    return struct.pack("<HHI", (instance << 4) | version, record_type, len(body)) + body


def container(record_type: int, *children: bytes, instance: int = 0) -> bytes:
    return rec(record_type, b"".join(children), instance=instance, version=0xF)


def utf16(text: str) -> bytes:
    return text.encode("utf-16-le")


def text_header(kind: int = 1) -> bytes:
    return rec(0x0F9F, struct.pack("<I", kind))


def text_chars(text: str) -> bytes:
    return rec(0x0FA0, utf16(text))


def cstring(text: str, instance: int) -> bytes:
    return rec(0x0FBA, utf16(text), instance=instance)


def opt_with_text(wordart: str) -> bytes:
    """An `OfficeArtFOPT` with one plain property and `gtextUNICODE` (0xC0)."""
    data = utf16(wordart + "\x00")
    table = struct.pack("<HI", 0x0080, 7) + struct.pack("<HI", 0x8000 | 0x00C0, len(data))
    return rec(0xF00B, table + data, instance=2, version=3)


def shape_with(*children: bytes) -> bytes:
    return container(0xF004, *children)


def slide_container(*shapes: bytes, master_id: int = 0) -> bytes:
    slide_atom = rec(0x03EF, struct.pack("<I8sIIHH", 0, b"\0" * 8, master_id, 0, 0, 0), version=2)
    drawing = container(0xF002, container(0xF003, *shapes))
    return container(0x03EE, slide_atom, container(0x040C, drawing))


def persist_atom(persist_id: int, declared_runs: int, identifier: int) -> bytes:
    return rec(0x03F3, struct.pack("<IIiII", persist_id, 0, declared_runs, identifier, 0))


def build(slide: bytes, *, declared_runs: int, listed_text: bytes = b"", doc_extra: bytes = b"",
          extra_objects: dict[int, bytes] | None = None) -> tuple[bytes, bytes]:
    """`(PowerPoint Document, Current User)` for a one-slide deck."""
    slide_list = container(0x0FF0, persist_atom(2, declared_runs, 256), listed_text, instance=0)
    document = container(0x03E8, slide_list, doc_extra)
    objects = {1: document, 2: slide, **(extra_objects or {})}
    stream = b""
    offsets: dict[int, int] = {}
    for persist_id, blob in objects.items():
        offsets[persist_id] = len(stream)
        stream += blob
    directory_at = len(stream)
    ids = sorted(offsets)
    assert ids == list(range(1, len(ids) + 1))
    directory = rec(0x1772, struct.pack("<I", ids[0] | (len(ids) << 20))
                    + b"".join(struct.pack("<I", offsets[i]) for i in ids))
    stream += directory
    edit_at = len(stream)
    stream += rec(0x0FF5, struct.pack("<IHBBIIIII", 0, 0, 0, 3, 0, directory_at, 1, len(ids) + 1, 0)
                  + b"\0" * 4)
    current_user = rec(0x0FF6, struct.pack("<III", 20, 0xE391C05F, edit_at) + b"\0" * 8)
    return stream, current_user


# ---------------------------------------------------------------------------
# .ppt: text classes the reader now reads
# ---------------------------------------------------------------------------


def test_wordart_text_in_an_escher_property_table_is_read():
    slide = slide_container(shape_with(opt_with_text("Sour water stripper TK-106")))
    stream, current_user = build(slide, declared_runs=0)
    slides, _notes, _footers = read_ppt(stream, current_user)
    assert "Sour water stripper TK-106" in slides[0]


def test_a_property_table_with_no_text_property_adds_nothing():
    plain = rec(0xF00B, struct.pack("<HI", 0x0080, 7), instance=1, version=3)
    stream, current_user = build(slide_container(shape_with(plain)), declared_runs=0)
    slides, _notes, _footers = read_ppt(stream, current_user)
    assert slides == [""]


def test_the_deck_wide_header_and_footer_is_read_once_for_the_deck():
    footer = container(0x0FD9, rec(0x0FDA, b"\0\0\0\0"), cstring("OSI PI Users Conference 2004", 2))
    stream, current_user = build(slide_container(), declared_runs=0, doc_extra=footer)
    _slides, _notes, footers = read_ppt(stream, current_user)
    assert footers == ["OSI PI Users Conference 2004"]


def test_a_text_box_on_the_master_is_read_and_master_furniture_is_not():
    other_box = container(0xF00D, text_header(4), text_chars("Jon Bach, Quardev Laboratories"))
    placeholder = container(0xF00D, text_header(0), text_chars("Click to edit Master title style"))
    body_box = container(0xF00D, text_header(1), text_chars("Second level"))
    master = container(0x03F8, rec(0x03EF, b"\0" * 24, version=2),
                       container(0x040C, container(0xF002, container(0xF003, shape_with(other_box),
                                                                     shape_with(placeholder),
                                                                     shape_with(body_box)))))
    slide = slide_container(master_id=0x80000001)
    master_list = container(0x0FF0, persist_atom(3, 0, 0x80000001), instance=1)
    stream, current_user = build(slide, declared_runs=0, doc_extra=master_list,
                                 extra_objects={3: master})
    _slides, _notes, footers = read_ppt(stream, current_user)
    assert footers == ["Jon Bach, Quardev Laboratories"]


# ---------------------------------------------------------------------------
# .ppt: the fail-closed signals
# ---------------------------------------------------------------------------


def test_a_slide_that_declares_more_text_runs_than_can_be_found_declines():
    """The slide says three runs. One is in the list and none in its shapes: two
    are somewhere this reader did not look, so it must not answer."""
    listed = text_header() + text_chars("Only the one run")
    stream, current_user = build(slide_container(), declared_runs=3, listed_text=listed)
    with pytest.raises(LegacyOfficeUnreadable, match="declares 3 text runs and 1 were found"):
        read_ppt(stream, current_user)


def test_the_same_slide_reads_when_the_declared_count_is_honest():
    listed = text_header() + text_chars("Only the one run")
    stream, current_user = build(slide_container(), declared_runs=1, listed_text=listed)
    slides, _notes, _footers = read_ppt(stream, current_user)
    assert slides == ["Only the one run"]


def test_runs_in_the_slides_own_shapes_count_toward_the_declared_total():
    listed = text_header() + text_chars("In the list")
    box = container(0xF00D, text_header(4), text_chars("In a shape"))
    stream, current_user = build(slide_container(shape_with(box)), declared_runs=2, listed_text=listed)
    slides, _notes, _footers = read_ppt(stream, current_user)
    assert "In the list" in slides[0] and "In a shape" in slides[0]


def test_a_listed_slide_whose_container_is_missing_declines():
    stream, current_user = build(slide_container(), declared_runs=0)
    directory_at = stream.rindex(struct.pack("<HH", 0x0000, 0x1772))
    damaged = bytearray(stream)
    # header(8) + first-id/count word(4) + entry for persist id 1(4), then id 2.
    struct.pack_into("<I", damaged, directory_at + 8 + 4 + 4, len(stream) + 1000)
    with pytest.raises(LegacyOfficeUnreadable, match="container is not where the directory says"):
        read_ppt(bytes(damaged), current_user)


# ---------------------------------------------------------------------------
# .doc: the FIB's own totals against the piece table
# ---------------------------------------------------------------------------


def _quote() -> tuple[bytes, dict[int, bytes]]:
    with olefile.OleFileIO(str(QUOTE)) as ole:
        word = ole.openstream("WordDocument").read()
        tables = {n: ole.openstream(f"{n}Table").read() for n in (0, 1) if ole.exists(f"{n}Table")}
    return word, tables


def _cc_offset(word: bytes, index: int) -> int:
    """Byte offset of FibRgLw97 entry `index` (3 = ccpText, 4 = ccpFtn ...)."""
    (csw,) = struct.unpack_from("<H", word, 32)
    return 34 + csw * 2 + 2 + index * 4


def test_the_untouched_real_doc_still_reads():
    word, tables = _quote()
    assert "Leeds Boiler Quote Summary" in read_doc(word, tables)["body"]


def test_a_doc_whose_fib_declares_more_characters_than_the_pieces_hold_declines():
    word, tables = _quote()
    damaged = bytearray(word)
    (body,) = struct.unpack_from("<I", damaged, _cc_offset(word, 3))
    struct.pack_into("<I", damaged, _cc_offset(word, 3), body + 500)
    with pytest.raises(LegacyOfficeUnreadable, match="declares"):
        read_doc(bytes(damaged), tables)


def test_a_doc_with_text_beyond_every_story_it_knows_declines():
    """Pieces hold more than the story counts add up to: text this reader would
    not read. That is a partial read, so it declines."""
    word, tables = _quote()
    damaged = bytearray(word)
    (body,) = struct.unpack_from("<I", damaged, _cc_offset(word, 3))
    struct.pack_into("<I", damaged, _cc_offset(word, 3), max(0, body - 40))
    with pytest.raises(LegacyOfficeUnreadable, match="declares"):
        read_doc(bytes(damaged), tables)


def test_a_doc_whose_piece_runs_past_the_stream_declines_rather_than_reading_short():
    word, tables = _quote()
    fib = doc_module._fib(word)
    pieces = doc_module._pieces(tables[fib["table1"]], fib["fcClx"], fib["lcbClx"])
    _start, _end, offset, _compressed = pieces[0]
    with pytest.raises(LegacyOfficeUnreadable, match="past the end"):
        read_doc(word[: offset + 5], tables)


# ---------------------------------------------------------------------------
# The fall-back is counted and reported, never silent
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_counter():
    legacy_office.take_fallback_summary()
    yield
    legacy_office.take_fallback_summary()


def test_nothing_falling_back_gives_no_summary_line():
    assert legacy_office.take_fallback_summary() == ""


def test_each_fall_back_is_counted_and_the_summary_says_why(tmp_path, monkeypatch):
    monkeypatch.setattr("app.core.formats.load_rules",
                        lambda: type("R", (), {"converter_for": lambda self, ext: None})())
    fake = tmp_path / "x.ppt"
    fake.write_bytes(b"x")
    for reason in ("slide 3 declares 5 text runs and 2 were found",
                   "slide 9 declares 4 text runs and 1 were found",
                   "not an OLE2 container"):
        with pytest.raises(AppErrorException):
            list(legacy_office.fall_back(fake, "extract.ppt", reason))
    line = legacy_office.take_fallback_summary()
    assert line.startswith("3 legacy Office files went to the slower reader")
    assert "slide N declares N text runs and N were found (2)" in line
    assert legacy_office.take_fallback_summary() == "", "reading the summary resets it"
