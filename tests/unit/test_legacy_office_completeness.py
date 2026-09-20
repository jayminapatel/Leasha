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

import io
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

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "legacy_office"
QUOTE = FIXTURES / "quote.doc"


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


def _streams(path: Path) -> tuple[bytes, dict[int, bytes]]:
    with olefile.OleFileIO(str(path)) as ole:
        word = ole.openstream("WordDocument").read()
        tables = {n: ole.openstream(f"{n}Table").read() for n in (0, 1) if ole.exists(f"{n}Table")}
    return word, tables


def _quote() -> tuple[bytes, dict[int, bytes]]:
    return _streams(QUOTE)


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


# ---------------------------------------------------------------------------
# The drawing layer: WordArt in a .doc (owner instruction, 2026-09-20 later)
# ---------------------------------------------------------------------------
#
# `banner.doc` is synthetic. `tests/fixtures/legacy_office/src/banner.fodt` is a
# hand-written flat-ODF document holding one Fontwork shape and two ordinary
# paragraphs; LibreOffice converted it once, by hand, with
# `soffice --headless --convert-to "doc:MS Word 97"`. Nothing of the owner's is
# in it. Its point: the words "Kirkstall Fontwork Headline" are in the *table*
# stream, as a `gtextUNICODE` property, and in no text story at all - checked,
# they do not appear in `WordDocument` in any encoding.


BANNER = FIXTURES / "banner.doc"


def test_wordart_in_a_doc_is_read_not_silently_dropped():
    stories = doc_module.DocExtractor._read(BANNER)
    assert "Kirkstall Fontwork Headline" in stories["drawings"]
    assert "Alderley boiler tender" in stories["body"]


def test_the_wordart_text_is_in_the_table_stream_and_no_story():
    """The reason this needed a second reader: the body has none of it."""
    word, tables = _streams(BANNER)
    assert b"K\x00i\x00r\x00k\x00s\x00t\x00a\x00l\x00l" in tables[doc_module._fib(word)["table1"]]
    assert b"Kirkstall" not in word and b"K\x00i\x00r\x00k" not in word


def test_a_doc_with_no_drawing_reads_no_drawing_text():
    stories = doc_module.DocExtractor._read(QUOTE)
    assert "drawings" not in stories


def test_a_drawing_span_outside_the_table_stream_declines():
    with pytest.raises(LegacyOfficeUnreadable, match="outside the table stream"):
        doc_module._drawing_text(b"\x00" * 32, 16, 64)


def test_a_drawing_whose_records_do_not_add_up_declines_rather_than_reading_part():
    """Half a drawing read is words missing with nobody told."""
    drawing = struct.pack("<HHI", 0x000F, 0xF000, 32) + b"\x00" * 8
    with pytest.raises(LegacyOfficeUnreadable, match="do not add up"):
        doc_module._drawing_text(drawing, 0, len(drawing))


def test_wordart_survives_being_grouped_several_shapes_deep():
    text = "Halifax works".encode("utf-16-le")
    opt = struct.pack("<HHI", (1 << 4) | 3, 0xF00B, 6 + len(text))
    opt += struct.pack("<HI", 0x00C0 | 0x8000, len(text)) + text
    for _ in range(3):                                    # group inside group
        opt = struct.pack("<HHI", 0x000F, 0xF003, len(opt)) + opt
    drawing = struct.pack("<HHI", 0x000F, 0xF000, len(opt)) + opt
    assert doc_module._drawing_text(drawing, 0, len(drawing)) == ["Halifax works"]


# ---------------------------------------------------------------------------
# Embedded OLE objects
# ---------------------------------------------------------------------------


class _StubOle:
    """The three `olefile` calls `_embedded_streams` makes, over a dict.

    Real embedded objects cannot be written by `olefile` (it only reads), and no
    synthetic fixture with one may be built from the owner's documents, so the
    classification is pinned over the stream *names* real objects have - taken
    from a survey of 390 real `.doc` copies, not invented.
    """

    def __init__(self, objects: dict[str, dict[str, bytes]]):
        self._objects = objects

    def listdir(self, streams=True, storages=True):
        return [["ObjectPool", name, stream]
                for name, contents in self._objects.items() for stream in contents]

    def openstream(self, path):
        return io.BytesIO(self._objects[path[1]][path[2]])


def test_an_embedded_spreadsheet_is_counted_not_declined():
    """Lead decision, 2026-09-20: LibreOffice's `.doc` text export does not hold
    embedded-object text either (measured: two real documents with an embedded
    workbook scored recall 1.000 against LibreOffice without it being read), so
    paying the converter 5-10 s for the same words is waste. The words' absence
    is named and counted instead - the `ERR_PST_PARTIAL` shape."""
    ole = _StubOle({"_1": {"\x01CompObj": b"", "\x01Ole": b"", "Workbook": b"BIFF"}})
    found, unread = doc_module._embedded_streams(ole)
    assert found == [] and unread == ["excel"]


def test_an_embedded_visio_drawing_and_a_packaged_file_are_counted_too():
    for stream, kind in (("VisioDocument", "visio"), ("\x01Ole10Native", "package")):
        ole = _StubOle({"_1": {"\x03ObjInfo": b"", stream: b"x"}})
        assert doc_module._embedded_streams(ole)[1] == [kind]


def test_a_document_with_an_unreadable_embedded_object_is_still_indexed_whole(tmp_path):
    """The host's own words stay searchable, at in-process speed."""
    stories = doc_module.DocExtractor._read(QUOTE)
    assert "Leeds Boiler Quote Summary" in stories["body"]


def test_the_run_says_how_many_files_held_text_it_could_not_read(tmp_path):
    legacy_office.take_unread_embedded_summary()
    legacy_office.note_unread_embedded(tmp_path / "tender.doc", ["excel", "excel"])
    legacy_office.note_unread_embedded(tmp_path / "scope.doc", ["visio"])
    line = legacy_office.take_unread_embedded_summary()
    assert line.startswith("2 files hold text inside embedded objects that was not read")
    assert "excel (1)" in line and "visio (1)" in line
    assert legacy_office.take_unread_embedded_summary() == "", "reading it resets it"


def test_a_file_with_an_unreadable_embedded_object_says_so_in_its_metadata(monkeypatch):
    """Nothing fails silently: the document itself carries what was left unread."""
    monkeypatch.setattr(doc_module, "_embedded_streams", lambda ole: ([], ["excel"]))
    document = next(iter(doc_module.DocExtractor().extract(QUOTE)))
    assert document.meta["embedded_unread"] == "excel"
    assert "excel" not in document.text, "the note is metadata, not text to search"


def test_an_unreadable_embedded_object_never_reaches_the_converter(monkeypatch):
    """The whole point of the change: no LibreOffice for words it does not have."""
    monkeypatch.setattr(doc_module, "_embedded_streams", lambda ole: ([], ["visio"]))
    monkeypatch.setattr(doc_module, "fall_back",
                        lambda *a, **k: pytest.fail("the converter was called"))
    assert list(doc_module.DocExtractor().extract(QUOTE))


def test_an_embedded_word_document_is_read_in_process():
    word, tables = _quote()
    ole = _StubOle({"_1": {"\x01CompObj": b"", "WordDocument": word,
                           **{f"{n}Table": data for n, data in tables.items()}}})
    text, unread = doc_module._embedded_text(doc_module._embedded_streams(ole)[0])
    assert "Leeds Boiler Quote Summary" in text and unread == []


def test_an_embedded_word_document_the_reader_cannot_parse_is_counted_not_raised():
    ole = _StubOle({"_1": {"WordDocument": b"not a word stream at all"}})
    text, unread = doc_module._embedded_text(doc_module._embedded_streams(ole)[0])
    assert text == "" and unread == ["word"]


def test_an_equation_an_activex_control_and_an_empty_pool_are_not_text_and_are_not_counted():
    ole = _StubOle({
        "_1": {"\x01CompObj": b"", "\x03ObjInfo": b"", "Equation Native": b"\x00"},
        "_2": {"\x03OCXNAME": b"", "\x03OCXDATA": b""},
        "_3": {"\x01Ole": b"", "\x01CompObj": b"", "ObjectPool": b""},
    })
    assert doc_module._embedded_streams(ole) == ([], [])


def test_an_embedded_document_with_embedded_objects_of_its_own_is_counted():
    """This reader goes one level down. It says so rather than assuming."""
    class _Nested(_StubOle):
        def listdir(self, streams=True, storages=True):
            return [["ObjectPool", "_1", "WordDocument"],
                    ["ObjectPool", "_1", "ObjectPool", "_2", "Workbook"]]

    assert doc_module._embedded_streams(_Nested({}))[1] == ["unknown"]


def test_an_unrecognised_embedded_object_is_counted_rather_than_ignored():
    ole = _StubOle({"_1": {"\x01Ole": b"", "CONTENTS": b"something with words in it"}})
    assert doc_module._embedded_streams(ole)[1] == ["unknown"]
