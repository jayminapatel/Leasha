r"""In-process readers that replaced per-file LibreOffice, and the fast `.docx` path.

Owner's instruction 2026-09-20: "speed is important and this should be for all
types of files applicable". Each reader here is built from a file *constructed
byte by byte in the test*, because a mock would prove only that the mock works;
the recall of each against the converter's own output on real files is measured
by `tools/reader_recall.py` and recorded in `docs/EXTRACTION_SPEED.md`.

The rule every reader shares: **it never returns empty text for a file it could
not read.** It says so, and the converter fallback (or the ordinary corrupt-file
error) takes over. That is the second half of each section below.
"""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path
from typing import Iterator

import pytest

import app.extract  # noqa: F401 - registers every extractor
from app.core.errors import AppErrorException
from app.extract.base import REGISTRY, extract, extractor_for


def read(path: Path) -> str:
    return "\n".join(document.text for document in extract(path))


class _Fallback:
    """Stands in for `legacy_office.fall_back` and records that it was asked."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, path: Path, component: str, reason: str) -> Iterator[object]:
        self.calls.append((component, reason))
        from app.core.errors import raise_error

        raise_error("ERR_FILE_CORRUPT", component, path=str(path), details=reason)
        yield  # pragma: no cover - makes this a generator, like the real one


# ---------------------------------------------------------------------------
# Registration: one rule, stated once
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("extension,name", [
    (".pub", "publisher"), (".pages", "iwork"), (".numbers", "iwork"), (".key", "iwork"),
    (".mobi", "mobi"), (".azw3", "mobi"),
])
def test_the_format_is_read_in_process_and_declares_its_fallback(extension: str, name: str) -> None:
    extractor = REGISTRY[extension]
    assert extractor.name == name
    assert extractor.reads_externally is False
    assert getattr(extractor, "falls_back_to_converter", False) is True


@pytest.mark.parametrize("extension", [".dotx", ".dotm", ".sxw", ".sxc", ".sxi", ".fodt", ".fods"])
def test_formats_that_were_skipped_as_unsupported_are_now_claimed(extension: str) -> None:
    assert extension in REGISTRY


# ---------------------------------------------------------------------------
# iWork: snappy + protobuf, built by hand
# ---------------------------------------------------------------------------

def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


def _number_field(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def snappy_literal(data: bytes) -> bytes:
    """A valid raw Snappy block that is one literal - no compression, still legal."""
    length = len(data) - 1
    if length < 60:
        head = bytes([length << 2])
    else:
        head = bytes([61 << 2]) + struct.pack("<H", length)
    return _varint(len(data)) + head + data


def iwa(records: list[tuple[int, bytes]]) -> bytes:
    """One `.iwa` member: chunk header + snappy block of ArchiveInfo+payload records."""
    body = b""
    for kind, payload in records:
        info = _number_field(1, 1) + _field(2, _number_field(1, kind) + _number_field(3, len(payload)))
        body += _varint(len(info)) + info + payload
    block = snappy_literal(body)
    return b"\x00" + len(block).to_bytes(3, "little") + block


def storage(text: str) -> tuple[int, bytes]:
    return 2001, _field(3, text.encode("utf-8"))


def data_list(entries: list[tuple[int, str]], kind: int = 1) -> tuple[int, bytes]:
    body = _number_field(1, kind)
    for key, text in entries:
        body += _field(3, _number_field(1, key) + _number_field(2, 1) + _field(3, text.encode("utf-8")))
    return 6005, body


def write_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def test_snappy_copies_including_the_overlapping_kind() -> None:
    from app.extract.iwork import snappy_decompress

    # literal "abc", then a copy of length 9 from 3 back (overlaps itself): "abcabcabcabc"
    block = _varint(12) + bytes([2 << 2]) + b"abc" + bytes([((9 - 4) << 2) | 1, 3])
    assert snappy_decompress(block) == b"abcabcabcabc"


def test_snappy_refuses_a_block_whose_length_is_a_lie() -> None:
    from app.extract.iwork import snappy_decompress

    with pytest.raises(ValueError):
        snappy_decompress(_varint(50) + bytes([2 << 2]) + b"abc")


def test_pages_body_text_is_read_and_line_separators_become_newlines(tmp_path: Path) -> None:
    path = write_zip(tmp_path / "letter.pages", {
        "Index/Document.iwa": iwa([storage("Dear Ada,\u2028the licence number is 12400\u2029\ufffc")]),
        "Index/DocumentStylesheet.iwa": iwa([storage("Style noise that must not be indexed")]),
    })
    text = read(path)
    assert "the licence number is 12400" in text
    assert "Dear Ada," in text.splitlines()
    assert "Style noise" not in text
    assert "\ufffc" not in text


def test_keynote_reads_slides_and_skips_the_master_placeholders(tmp_path: Path) -> None:
    path = write_zip(tmp_path / "deck.key", {
        "Index/Slide.iwa": iwa([storage("Quarterly boiler review")]),
        "Index/Slide-8272-2.iwa": iwa([storage("Actions for Leeds")]),
        "Index/MasterSlide.iwa": iwa([storage("Double-click to edit")]),
    })
    text = read(path)
    assert "Quarterly boiler review" in text and "Actions for Leeds" in text
    assert "Double-click" not in text


def test_numbers_keeps_every_tables_strings_even_when_their_keys_collide(tmp_path: Path) -> None:
    """The bug the real-file check caught: every table's list numbers its strings
    from 1, and a dictionary keyed on the key alone silently kept the first table's
    strings and dropped the rest (74% recall against LibreOffice, not an error)."""
    path = write_zip(tmp_path / "plan.numbers", {
        "Index/Document.iwa": iwa([(2, _field(1, b"Budget sheet"))]),
        "Index/Tables/DataList.iwa": iwa([data_list([(1, "Owner"), (2, "Status")])]),
        "Index/Tables/DataList-2525-2.iwa": iwa([data_list([(1, "Laundry list"), (2, "Adam")])]),
        "Index/Tables/Tile.iwa": iwa([storage("layout, not words")]),
    })
    text = read(path)
    for wanted in ("Budget sheet", "Owner", "Status", "Laundry list", "Adam"):
        assert wanted in text, wanted
    assert "layout, not words" not in text


def test_numbers_ignores_data_lists_that_are_not_strings(tmp_path: Path) -> None:
    path = write_zip(tmp_path / "f.numbers", {
        "Index/Document.iwa": iwa([storage("Header")]),
        "Index/Tables/DataList.iwa": iwa([data_list([(1, "format blob")], kind=2)]),
    })
    assert "format blob" not in read(path)


@pytest.mark.parametrize("make", [
    lambda p: p.write_bytes(b"not a zip at all"),                                  # damaged
    lambda p: write_zip(p, {"index.xml": b"<sf:document/>"}),                      # iWork '09
    lambda p: write_zip(p, {"Index/Document.iwa": b"\x00\x05\x00\x00garbage"}),    # bad block
])
def test_an_iwork_file_it_cannot_read_goes_to_the_fallback_and_is_never_empty(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make) -> None:
    import app.extract.iwork as iwork

    fallback = _Fallback()
    monkeypatch.setattr(iwork, "fall_back", fallback)
    path = tmp_path / "x.pages"
    make(path)
    with pytest.raises(AppErrorException):
        list(extractor_for(path).extract(path))          # type: ignore[union-attr]
    assert fallback.calls, "the reader gave up without handing the file to the fallback"


def test_a_picture_only_deck_is_no_text_not_a_failure(tmp_path: Path) -> None:
    path = write_zip(tmp_path / "pictures.key", {"Index/Slide.iwa": iwa([(5, b"\x08\x01")])})
    with pytest.raises(AppErrorException) as caught:
        list(extract(path))
    assert caught.value.error.code == "ERR_NO_TEXT_LAYER"


# ---------------------------------------------------------------------------
# Publisher: the Quill text chunk
# ---------------------------------------------------------------------------

def quill(text: str, *, header: bytes = b"CHNKINK ") -> bytes:
    directory = header + b"\x04\x00\x07\x00" + b"TEXT\x00\x00\x01\x00\x00\x00" + b"STSH\x00\x00" * 4
    pad = b"\x00" * (512 - len(directory))
    styles = "Arial".encode("utf-16le") + b"\x01\x00\x02\x00" + "Times New Roman".encode("utf-16le")
    return directory + pad + text.encode("utf-16le") + b"\x00\x00\xff\xfe\x01\x00" + styles


def test_publisher_story_text_is_the_longest_utf16_run_not_the_style_names() -> None:
    from app.extract.publisher import story_text

    text = story_text(quill("Annual review\rThe licence number is 12400\r"))
    assert "The licence number is 12400" in text
    assert "Annual review" in text.splitlines()
    assert "Arial" not in text and "Times New Roman" not in text


def test_publisher_refuses_a_stream_without_the_quill_header() -> None:
    from app.extract.legacy_office import LegacyOfficeUnreadable
    from app.extract.publisher import story_text

    with pytest.raises(LegacyOfficeUnreadable):
        story_text(quill("some words here", header=b"NOTQUILL"))


def test_publisher_hands_a_file_that_is_not_ole_to_the_fallback(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.extract.publisher as publisher

    fallback = _Fallback()
    monkeypatch.setattr(publisher, "fall_back", fallback)
    path = tmp_path / "brochure.pub"
    path.write_bytes(b"this is not a compound file")
    with pytest.raises(AppErrorException):
        list(extractor_for(path).extract(path))          # type: ignore[union-attr]
    assert fallback.calls


# ---------------------------------------------------------------------------
# Mobipocket
# ---------------------------------------------------------------------------

def palmdoc_literal(data: bytes) -> bytes:
    """A valid PalmDOC stream using only literal runs (`1..8` then that many bytes)."""
    out = bytearray()
    for i in range(0, len(data), 8):
        chunk = data[i:i + 8]
        out.append(len(chunk))
        out += chunk
    return bytes(out)


def make_mobi(html: str, *, compression: int = 2, encryption: int = 0, title: str = "A Short Book",
              flags: int = 0, trailing: bytes = b"") -> bytes:
    body = html.encode("cp1252")
    text_records = []
    for i in range(0, len(body), 4096):
        chunk = body[i:i + 4096]
        packed = palmdoc_literal(chunk) if compression == 2 else chunk
        text_records.append(packed + trailing)
    name = title.encode("cp1252")
    mobi_header = bytearray(b"MOBI" + struct.pack(">I", 0xE8) + b"\x00" * (0xE8 - 8))
    struct.pack_into(">I", mobi_header, 8, 2)                      # type
    struct.pack_into(">I", mobi_header, 12, 1252)                  # code page (at 28 in record 0)
    record0 = bytearray(struct.pack(">HHIHHH", compression, 0, len(body), len(text_records), 4096, encryption)
                        + b"\x00\x00")
    record0 += mobi_header
    record0 += b"\x00" * (0xF4 - len(record0))
    struct.pack_into(">H", record0, 0xF2, flags)
    struct.pack_into(">II", record0, 84, len(record0), len(name))
    record0 += name
    records = [bytes(record0)] + text_records
    header = bytearray(78)
    header[60:68] = b"BOOKMOBI"
    struct.pack_into(">H", header, 76, len(records))
    offset = 78 + 8 * len(records) + 2
    table, blob = b"", b""
    for record in records:
        table += struct.pack(">I", offset + len(blob)) + b"\x00\x00\x00\x00"
        blob += record
    return bytes(header) + table + b"\x00\x00" + blob


def test_mobi_text_and_title_are_read(tmp_path: Path) -> None:
    path = tmp_path / "book.mobi"
    path.write_bytes(make_mobi("<html><body><p>Chapter one</p><p>The licence number is 12400.</p></body></html>"))
    documents = list(extract(path))
    assert "The licence number is 12400." in documents[0].text
    assert documents[0].meta["title"] == "A Short Book"


def test_mobi_back_references_and_the_space_shortcut_decode() -> None:
    from app.extract.mobi import palmdoc_decompress

    # "hello" literal, 0xC1 = space + "A", then a copy: distance 7, length 3 -> "hel"
    stream = bytes([5]) + b"hello" + bytes([0xC1 | 0x00])       # space + (0xC1^0x80 = 0x41 'A')
    assert palmdoc_decompress(stream) == b"hello A"
    pair = (0x8000 | (7 << 3) | 0)                                 # distance 7, length 3
    assert palmdoc_decompress(stream + struct.pack(">H", pair)) == b"hello Ahel"


def test_mobi_trailing_entries_are_trimmed_not_read_as_text(tmp_path: Path) -> None:
    # flag bit 0: one multibyte-overlap byte at the end of each record (value & 3 + 1 == 1 byte).
    path = tmp_path / "trailing.mobi"
    path.write_bytes(make_mobi("<p>Real words here</p>", flags=1, trailing=b"\x00"))
    assert "Real words here" in read(path)


@pytest.mark.parametrize("kwargs", [{"compression": 17480}, {"encryption": 2}])
def test_mobi_that_it_cannot_decode_is_an_error_not_empty_text(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kwargs) -> None:
    import app.extract.mobi as mobi

    fallback = _Fallback()
    monkeypatch.setattr(mobi, "fall_back", fallback)
    path = tmp_path / "locked.mobi"
    path.write_bytes(make_mobi("<p>secret</p>", **kwargs))
    with pytest.raises(AppErrorException):
        list(extractor_for(path).extract(path))          # type: ignore[union-attr]
    assert fallback.calls


# ---------------------------------------------------------------------------
# OpenOffice 1.x and flat ODF
# ---------------------------------------------------------------------------

CONTENT = ('<?xml version="1.0"?><office:document-content '
           'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
           'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
           '<office:body><office:text><text:p>Boiler certificate 12400</text:p></office:text></office:body>'
           '</office:document-content>')


def test_openoffice_1x_writer_file_is_read_by_the_odf_reader(tmp_path: Path) -> None:
    path = write_zip(tmp_path / "old.sxw", {"content.xml": CONTENT.encode()})
    assert "Boiler certificate 12400" in read(path)


def test_a_flat_odf_file_reads_its_body_and_not_its_styles(tmp_path: Path) -> None:
    flat = ('<?xml version="1.0"?><office:document '
            'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
            'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">'
            '<office:styles><text:p>style-only text</text:p></office:styles>'
            '<office:body><office:text><text:p>Flat body words</text:p></office:text></office:body>'
            '</office:document>')
    path = tmp_path / "flat.fodt"
    path.write_text(flat, encoding="utf-8")
    text = read(path)
    assert "Flat body words" in text and "style-only" not in text


# ---------------------------------------------------------------------------
# .docx fast path
# ---------------------------------------------------------------------------

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_docx(path: Path, body: str, *, content_type: str = "document.main") -> Path:
    document = (f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
    types = ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="xml" ContentType="application/xml"/>'
             f'<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
             f'wordprocessingml.{content_type}+xml"/></Types>')
    rels = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>')
    return write_zip(path, {"[Content_Types].xml": types.encode(), "_rels/.rels": rels.encode(),
                            "word/document.xml": document.encode()})


def para(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def test_docx_blocks_keep_document_order_with_the_table_between_paragraphs(tmp_path: Path) -> None:
    body = (para("Before the table")
            + '<w:tbl><w:tr><w:tc>' + para("Licence") + '</w:tc><w:tc>' + para("12400") + '</w:tc></w:tr></w:tbl>'
            + para("After the table"))
    text = read(make_docx(tmp_path / "order.docx", body))
    assert text.index("Before the table") < text.index("Licence\t12400") < text.index("After the table")


def test_docx_tabs_breaks_and_deleted_text(tmp_path: Path) -> None:
    body = ('<w:p><w:r><w:t>a</w:t><w:tab/><w:t>b</w:t><w:br/><w:t>c</w:t></w:r>'
            '<w:del><w:r><w:delText>gone</w:delText></w:r></w:del>'
            '<w:ins><w:r><w:t> inserted</w:t></w:r></w:ins></w:p>')
    text = read(make_docx(tmp_path / "runs.docx", body))
    assert "a\tb\nc inserted" in text
    assert "gone" not in text


def test_docx_content_control_and_text_box_are_read(tmp_path: Path) -> None:
    box = ('<w:p><w:r><w:t>anchor</w:t><w:pict><w:txbxContent>' + para("Callout text") + '</w:txbxContent></w:pict></w:r></w:p>')
    body = f'<w:sdt><w:sdtContent>{para("Inside a control")}</w:sdtContent></w:sdt>' + box
    text = read(make_docx(tmp_path / "cc.docx", body))
    assert "Inside a control" in text and "Callout text" in text and text.count("Callout text") == 1


def test_a_macro_enabled_document_and_a_template_are_readable(tmp_path: Path) -> None:
    """python-docx rejects both on their content type; the file is the same."""
    for name, kind in (("m.docm", "document.macroEnabled.main"), ("t.dotx", "template.main")):
        text = read(make_docx(tmp_path / name, para("Body text of " + name), content_type=kind))
        assert "Body text of " + name in text


def test_the_fast_path_agrees_with_python_docx_on_a_generated_document(tmp_path: Path) -> None:
    docx = pytest.importorskip("docx")
    from app.extract.ooxml_fast import docx_blocks

    path = tmp_path / "real.docx"
    document = docx.Document()
    document.add_heading("Site report", 1)
    for i in range(5):
        document.add_paragraph(f"Finding {i}: the boiler at Leeds passed.")
    table = document.add_table(rows=3, cols=3)
    for r in range(3):
        for c in range(3):
            table.cell(r, c).text = f"r{r}c{c}"
    document.save(str(path))

    fast = "\n".join(docx_blocks(path))
    slow = "\n".join(p.text for p in docx.Document(str(path)).paragraphs)
    for line in slow.splitlines():
        if line.strip():
            assert line in fast
    assert "r2c2" in fast


def test_a_docx_that_is_not_a_zip_still_reports_corrupt_through_the_old_path(tmp_path: Path) -> None:
    pytest.importorskip("docx")
    path = tmp_path / "broken.docx"
    path.write_bytes(b"this was never a zip")
    with pytest.raises(AppErrorException) as caught:
        list(extract(path))
    assert caught.value.error.code == "ERR_FILE_CORRUPT"


def damage_member(path: Path, member: str) -> Path:
    """Overwrite the middle of one deflated member, leaving the zip directory intact."""
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(member)
    assert info.compress_type == zipfile.ZIP_DEFLATED
    data = bytearray(path.read_bytes())
    name_length, extra_length = struct.unpack("<HH", data[info.header_offset + 26:info.header_offset + 30])
    middle = info.header_offset + 30 + name_length + extra_length + info.compress_size // 2
    data[middle:middle + 16] = b"\xff" * 16
    path.write_bytes(bytes(data))
    return path


def _office_file(extension: str, path: Path) -> str:
    """Build a small file of the kind and return the member holding its text."""
    text = "Licence 12400 is current " * 200          # long enough to deflate into many bytes
    if extension == ".docx":
        docx = pytest.importorskip("docx")
        document = docx.Document()
        document.add_paragraph(text)
        document.save(str(path))
        return "word/document.xml"
    if extension == ".xlsx":
        openpyxl = pytest.importorskip("openpyxl")
        book = openpyxl.Workbook()
        for row in range(200):
            book.active.append([f"{text[:40]} {row}", row])
        book.save(str(path))
        return "xl/worksheets/sheet1.xml"
    pptx = pytest.importorskip("pptx")
    deck = pptx.Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = "Boiler review"
    slide.placeholders[1].text = text
    deck.save(str(path))
    return "ppt/slides/slide1.xml"


@pytest.mark.parametrize("extension", [".docx", ".xlsx", ".pptx"])
def test_a_damaged_compressed_part_is_the_files_fault_not_leashas(tmp_path: Path, extension: str) -> None:
    # 2026-10-11, the owner's overnight run: a deck whose slide stream was damaged
    # raised `zlib.error` ("invalid distance too far back"), which no fast reader
    # caught, and the run logged it as ERR_UNEXPECTED - "a fault in Leasha, not
    # in the file". Python's own `ZipFile.testzip` fails on the same deck.
    path = tmp_path / f"damaged{extension}"
    damage_member(path, _office_file(extension, path))
    with pytest.raises(AppErrorException) as caught:
        list(extract(path))
    assert caught.value.error.code == "ERR_FILE_CORRUPT"
