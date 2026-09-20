r"""Non-negotiable 12: a library where one exists, a converter only where none does.

The rule is easy to state and easy to quietly abandon - the next format is
always faster to add as a `soffice` line than as a reader. So the rule is
enforced here rather than remembered, in the same shape the settings registry
uses for its exemptions: **an exception must be declared, and the declaration
must say something.**

The four formats that moved (`.xls`, `.rtf`, `.epub`, `.fb2`) get real fixtures
rather than mocks. The `.xls` one is a genuine BIFF2 workbook assembled byte by
byte, because `xlrd` reads a real file or it does not, and a mocked workbook
would prove only that the mock works.
"""

from __future__ import annotations

import struct
import zipfile
from pathlib import Path
from typing import Iterable

import pytest

from app.core.formats import load_rules
from app.extract.base import REGISTRY, extractor_for
from app.extract.converter import ALLOWED_BINARIES, CONVERTER_JUSTIFIED
from app.extract.ebook import text_from_fb2, text_from_xhtml

PACKAGED_CONFIG = Path(__file__).resolve().parents[2] / "config" / "extractors.toml"


#: Formats read in-process first with the converter kept behind it, on the
#: owner's instruction of 2026-09-20 ("make conversion as efficient as possible"):
#: a cold LibreOffice is 5-10 s a file, the readers are milliseconds. Not an
#: escape hatch - each entry needs an extractor that says `falls_back_to_converter`.
READ_IN_PROCESS_FIRST = frozenset({".doc", ".ppt", ".pub", ".pages", ".numbers", ".key"})


def shipped_converters() -> dict[str, dict]:
    """The `[converters]` table as shipped, read from the packaged file."""
    try:
        import tomllib
    except ModuleNotFoundError:                              # pragma: no cover
        import tomli as tomllib                              # type: ignore[no-redef]
    with PACKAGED_CONFIG.open("rb") as handle:
        return tomllib.load(handle).get("converters", {})


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------


def test_every_shipped_converter_states_why_no_library_will_do() -> None:
    """A converter cannot be added without a justification.

    This is the test that makes the rule real. Adding a `[converters.".foo"]`
    block without an entry here fails, and the failure names the format.
    """
    missing = sorted(set(shipped_converters()) - set(CONVERTER_JUSTIFIED))
    assert not missing, (
        f"{missing} route to an external converter with no entry in "
        "CONVERTER_JUSTIFIED. Non-negotiable 12: use a library if one exists, "
        "and if none does, say so there."
    )


def test_no_justification_is_left_for_a_format_that_moved_to_a_library() -> None:
    """The reverse direction - the map must not accumulate dead entries.

    Without this, `.xls` would still claim it needs LibreOffice long after
    `xls.py` shipped, and the next person would believe it.
    """
    stale = sorted(set(CONVERTER_JUSTIFIED) - set(shipped_converters()))
    assert not stale, f"{stale} are justified but no longer routed to a converter"


@pytest.mark.parametrize("extension", sorted(CONVERTER_JUSTIFIED))
def test_a_justification_says_something(extension: str) -> None:
    """Same length guard the settings exemptions use.

    "no library" passes a presence check and teaches nobody anything. Thirty
    characters is roughly one clause explaining *why* - enough that writing it
    requires having thought about it.
    """
    reason = CONVERTER_JUSTIFIED[extension]
    assert len(reason) >= 30, f"{extension}: {reason!r} does not explain anything"


@pytest.mark.parametrize("extension", sorted(CONVERTER_JUSTIFIED))
def test_a_converted_format_has_no_library_extractor(extension: str) -> None:
    """One format, one route.

    A format with both an extractor and a converter is read one way on a
    machine with LibreOffice and another way without it - which shows up as the
    same document producing different search results on two installs, and is
    close to undebuggable from the outside.
    """
    if extension in READ_IN_PROCESS_FIRST:
        # The one sanctioned exception: an in-process reader first, the
        # converter only for what it declines. The extractor must say so, or a
        # format could quietly acquire both routes and behave differently on a
        # machine without LibreOffice.
        extractor = REGISTRY.get(extension)
        assert extractor is not None and getattr(extractor, "falls_back_to_converter", False), (
            f"{extension} is listed as read in-process first, but its extractor "
            "does not declare falls_back_to_converter"
        )
        return
    assert extension not in REGISTRY, (
        f"{extension} has both a registered extractor ({REGISTRY[extension].name!r}) "
        "and a converter entry. Remove the converter."
    )


@pytest.mark.parametrize("extension", [".xls", ".rtf", ".epub", ".fb2"])
def test_the_four_moved_formats_are_read_in_process(extension: str) -> None:
    """The specific claim this work made, asserted directly."""
    assert extension not in shipped_converters(), f"{extension} still routes to a converter"
    extractor = extractor_for(Path(f"sample{extension}"))
    assert extractor is not None, f"{extension} has no extractor"
    assert extractor.reads_externally is False


def test_pandoc_is_no_longer_an_allowed_binary() -> None:
    """The allow-list holds only what is used.

    It is a security boundary: every name on it is a program this application
    may execute. Nothing routes to pandoc now, so it comes off - and putting it
    back is a commit with a diff, which is the whole design of that list.
    """
    assert "pandoc" not in ALLOWED_BINARIES


def test_every_converter_binary_is_still_allowed() -> None:
    """Removing pandoc must not have orphaned a route."""
    for extension, entry in shipped_converters().items():
        binary = entry["command"][0]
        assert binary in ALLOWED_BINARIES, f"{extension} names {binary}, which is not allowed"


def test_libreoffice_is_needed_for_fewer_formats_than_before() -> None:
    """The user-visible point of the change, pinned so it cannot silently regress.

    Five formats route to LibreOffice now. `.xls` and `.rtf` leaving is what
    matters: those are common in any long archive, and before this a machine
    without LibreOffice read none of them.
    """
    soffice = {
        extension for extension, entry in shipped_converters().items()
        if entry["command"][0] in {"soffice", "libreoffice"}
    }
    assert ".xls" not in soffice
    assert ".rtf" not in soffice
    assert soffice == {".doc", ".ppt", ".pages", ".numbers", ".key", ".wpd", ".pub"}


def test_the_packaged_config_still_loads() -> None:
    """A removed converter must not have left the file unparseable.

    The cheapest test here: this file is read at every startup, so if it does
    not parse, the application does not open.
    """
    rules = load_rules()
    assert rules.extensions, "the shipped defaults should still route something"
    assert ".rtf" not in rules.converters
    assert ".doc" in rules.converters


# ---------------------------------------------------------------------------
# .fb2 - stdlib
# ---------------------------------------------------------------------------

FB2 = """<?xml version="1.0" encoding="utf-8"?>
<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">
  <description>
    <title-info>
      <book-title>The Leeds Inspection</book-title>
      <author><first-name>Ada</first-name><last-name>Bell</last-name></author>
    </title-info>
  </description>
  <body>
    <section>
      <title><p>Chapter One</p></title>
      <p>Findings from the <emphasis>annual</emphasis> inspection.</p>
      <p>The licence number is 12400.</p>
    </section>
  </body>
  <binary id="cover.jpg" content-type="image/jpeg">{blob}</binary>
</FictionBook>
"""


def test_fb2_reads_title_and_author() -> None:
    _, meta = text_from_fb2(FB2.format(blob="").encode("utf-8"))
    assert meta["title"] == "The Leeds Inspection"
    assert meta["author"] == "Ada Bell"


def test_fb2_keeps_inline_markup_in_its_own_paragraph() -> None:
    """The lesson odf.py learned: text after a nested tag is that tag's *tail*.

    Getting this wrong yields "Findings from the / inspection. annual", which is
    not what the document says.
    """
    text, _ = text_from_fb2(FB2.format(blob="").encode("utf-8"))
    assert "Findings from the annual inspection." in text


def test_fb2_never_indexes_the_base64_cover() -> None:
    """The one thing about FictionBook that has to be right.

    A cover image lives as base64 *inside the document*. Indexed, it would be
    the overwhelming majority of the file's text, every chunk would be random
    letters, and the embedding for the book would mean nothing.
    """
    blob = "QUJDREVG" * 5_000                 # 40,000 characters of "base64"
    text, _ = text_from_fb2(FB2.format(blob=blob).encode("utf-8"))
    assert "QUJDREVG" not in text
    assert len(text) < 1_000
    assert "licence number is 12400" in text


def test_fb2_that_is_not_xml_returns_nothing_rather_than_raising() -> None:
    text, meta = text_from_fb2(b"\x00\x01 not xml at all")
    assert text == "" and meta == {}


def test_fb2_extractor_reports_a_corrupt_file(tmp_path: Path) -> None:
    from app.core.errors import AppErrorException

    path = tmp_path / "broken.fb2"
    path.write_bytes(b"\x00\x01 not xml at all")
    with pytest.raises(AppErrorException) as caught:
        list(extractor_for(path).extract(path))              # type: ignore[union-attr]
    assert caught.value.error.code == "ERR_FILE_CORRUPT"


def test_fb2_extractor_yields_a_document(tmp_path: Path) -> None:
    path = tmp_path / "book.fb2"
    path.write_text(FB2.format(blob=""), encoding="utf-8")
    documents = list(extractor_for(path).extract(path))      # type: ignore[union-attr]
    assert len(documents) == 1
    assert "annual inspection" in documents[0].text
    assert documents[0].meta["title"] == "The Leeds Inspection"


# ---------------------------------------------------------------------------
# .epub - stdlib
# ---------------------------------------------------------------------------


def make_epub(path: Path, *, spine_backwards: bool = False, omit_container: bool = False) -> Path:
    chapters = {
        "c1.xhtml": "<html><body><h1>Chapter One</h1><p>The first &amp; earliest part.</p>"
                    "<script>var x = 1;</script></body></html>",
        "c2.xhtml": "<html><body><p>The second part&nbsp;follows.</p></body></html>",
    }
    order = ["c2", "c1"] if spine_backwards else ["c1", "c2"]
    opf = f"""<?xml version="1.0"?>
    <package xmlns="http://www.idpf.org/2007/opf" version="3.0">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:title>A Short Book</dc:title>
        <dc:creator>Ada Bell</dc:creator>
      </metadata>
      <manifest>
        <item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>
        <item id="c2" href="c2.xhtml" media-type="application/xhtml+xml"/>
      </manifest>
      <spine>{''.join(f'<itemref idref="{i}"/>' for i in order)}</spine>
    </package>"""
    container = """<?xml version="1.0"?>
    <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
      <rootfiles><rootfile full-path="OEBPS/book.opf"
        media-type="application/oebps-package+xml"/></rootfiles>
    </container>"""

    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        if not omit_container:
            archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/book.opf", opf)
        for name, markup in chapters.items():
            archive.writestr(f"OEBPS/{name}", markup)
    return path


def test_xhtml_entities_survive() -> None:
    """Why this is html.parser and not ElementTree.

    A strict XML parser rejects the whole chapter over an undeclared `&nbsp;`,
    and real books are full of them.
    """
    assert "&" in text_from_xhtml("<p>Marks &amp; Spencer</p>")
    assert "follows" in text_from_xhtml("<p>The second part&nbsp;follows.</p>")


def test_xhtml_drops_script_and_style() -> None:
    text = text_from_xhtml("<body><style>p{color:red}</style><p>Real text</p></body>")
    assert "color" not in text
    assert "Real text" in text


def test_epub_is_read_in_spine_order_not_manifest_order(tmp_path: Path) -> None:
    """The bug that survives longest, because all the text is present.

    Manifest order is arbitrary; spine order is reading order. Get it wrong and
    a snippet reads like two sentences from different chapters glued together.
    """
    path = make_epub(tmp_path / "backwards.epub", spine_backwards=True)
    text = list(extractor_for(path).extract(path))[0].text   # type: ignore[union-attr]
    assert text.index("second part") < text.index("first")


def test_epub_reads_forwards_when_the_spine_says_so(tmp_path: Path) -> None:
    path = make_epub(tmp_path / "book.epub")
    document = list(extractor_for(path).extract(path))[0]    # type: ignore[union-attr]
    assert document.text.index("first") < document.text.index("second part")
    assert document.meta["title"] == "A Short Book"
    assert document.meta["author"] == "Ada Bell"


def test_epub_without_a_container_says_which_part_is_missing(tmp_path: Path) -> None:
    from app.core.errors import AppErrorException

    path = make_epub(tmp_path / "nocontainer.epub", omit_container=True)
    with pytest.raises(AppErrorException) as caught:
        list(extractor_for(path).extract(path))              # type: ignore[union-attr]
    assert caught.value.error.code == "ERR_FILE_CORRUPT"
    assert "META-INF/container.xml" in str(caught.value.error.details)


def test_epub_that_is_not_a_zip_is_corrupt_not_a_crash(tmp_path: Path) -> None:
    from app.core.errors import AppErrorException

    path = tmp_path / "fake.epub"
    path.write_bytes(b"this is not a zip file")
    with pytest.raises(AppErrorException) as caught:
        list(extractor_for(path).extract(path))              # type: ignore[union-attr]
    assert caught.value.error.code == "ERR_FILE_CORRUPT"


# ---------------------------------------------------------------------------
# .rtf - striprtf
# ---------------------------------------------------------------------------

RTF = (
    r"{\rtf1\ansi\deff0{\fonttbl{\f0 Times New Roman;}}"
    r"\f0\fs24 Findings from the annual inspection.\par "
    r"The licence number is 12400.\par}"
)


def test_rtf_yields_its_text(tmp_path: Path) -> None:
    path = tmp_path / "note.rtf"
    path.write_text(RTF, encoding="latin-1")
    document = list(extractor_for(path).extract(path))[0]    # type: ignore[union-attr]
    assert "Findings from the annual inspection." in document.text
    assert "12400" in document.text


def test_rtf_control_words_do_not_leak_into_the_text(tmp_path: Path) -> None:
    """The failure that looks like success: indexing `\\fonttbl` and `Times`."""
    path = tmp_path / "note.rtf"
    path.write_text(RTF, encoding="latin-1")
    text = list(extractor_for(path).extract(path))[0].text   # type: ignore[union-attr]
    assert "fonttbl" not in text
    assert "rtf1" not in text


def test_a_file_that_is_not_rtf_is_refused_rather_than_half_read(tmp_path: Path) -> None:
    """Without the magic-number check, a mislabelled binary's stray ASCII is
    indexed as though it were the document's text."""
    from app.core.errors import AppErrorException

    path = tmp_path / "actually-a-jpeg.rtf"
    path.write_bytes(b"\xff\xd8\xff\xe0 JFIF nonsense")
    with pytest.raises(AppErrorException) as caught:
        list(extractor_for(path).extract(path))              # type: ignore[union-attr]
    assert caught.value.error.code == "ERR_FILE_CORRUPT"


# ---------------------------------------------------------------------------
# .xls - xlrd, against a real workbook
# ---------------------------------------------------------------------------


def biff2_workbook(rows: list[list[object]]) -> bytes:
    """A genuine BIFF2 workbook, assembled by hand.

    `xlrd` cannot be given a mock: it reads the byte stream or it fails. BIFF2
    is the one Excel format simple enough to write in a test - BOF, a record per
    cell, EOF - and `xlrd` reads it exactly as it reads a 1997 file.
    """
    def record(opcode: int, payload: bytes) -> bytes:
        return struct.pack("<HH", opcode, len(payload)) + payload

    out = record(0x0009, struct.pack("<HH", 2, 0x0010))      # BOF, worksheet
    attributes = b"\x00\x00\x00"
    for row, cells in enumerate(rows):
        for column, value in enumerate(cells):
            head = struct.pack("<HH", row, column) + attributes
            if isinstance(value, str):
                encoded = value.encode("latin-1")
                out += record(0x0004, head + bytes([len(encoded)]) + encoded)
            else:
                out += record(0x0003, head + struct.pack("<d", float(value)))
    return out + record(0x000A, b"")


def test_xls_reads_a_real_workbook(tmp_path: Path) -> None:
    path = tmp_path / "old.xls"
    path.write_bytes(biff2_workbook([["Licence", "Leeds"], ["Total", 12400]]))
    document = list(extractor_for(path).extract(path))[0]    # type: ignore[union-attr]
    assert "Licence" in document.text and "Leeds" in document.text


def test_xls_keeps_cells_on_one_line_so_a_row_stays_a_fact(tmp_path: Path) -> None:
    """odf.py's argument, applied here: "12400" next to "Total" is the fact.

    Separate them across lines and the row stops meaning anything.
    """
    path = tmp_path / "old.xls"
    path.write_bytes(biff2_workbook([["Licence", "Leeds"], ["Total", 12400]]))
    text = list(extractor_for(path).extract(path))[0].text   # type: ignore[union-attr]
    assert "Licence\tLeeds" in text or "Licence Leeds" in text


def test_a_whole_number_is_not_indexed_with_a_decimal_point(tmp_path: Path) -> None:
    """Excel stores every number as a float, so an invoice number arrives as
    12400.0 and a search for `12400` misses it."""
    path = tmp_path / "old.xls"
    path.write_bytes(biff2_workbook([["Total", 12400]]))
    text = list(extractor_for(path).extract(path))[0].text   # type: ignore[union-attr]
    assert "12400" in text
    assert "12400.0" not in text


def test_a_fraction_keeps_its_decimals(tmp_path: Path) -> None:
    """The integer shortcut must not round real numbers away."""
    path = tmp_path / "rate.xls"
    path.write_bytes(biff2_workbook([["Rate", 3.5]]))
    text = list(extractor_for(path).extract(path))[0].text   # type: ignore[union-attr]
    assert "3.5" in text


def test_an_xlsx_renamed_to_xls_is_told_to_rename_it_back(tmp_path: Path) -> None:
    """The common real-world case, and the reason the message matters.

    xlrd 2.0 refuses OOXML by design. The refusal must arrive as an instruction,
    not as an XLRDError traceback.
    """
    from app.core.errors import AppErrorException

    path = tmp_path / "modern.xls"
    with zipfile.ZipFile(path, "w") as archive:              # an OOXML-shaped zip
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")

    with pytest.raises(AppErrorException) as caught:
        list(extractor_for(path).extract(path))              # type: ignore[union-attr]
    error = caught.value.error
    assert error.code == "ERR_FILE_CORRUPT"
    assert ".xlsx" in (error.suggestion or "")


def test_xls_does_not_claim_xlsx() -> None:
    """The clean split that makes xlrd the right dependency.

    `openpyxl` owns OOXML and `xlrd` owns the OLE2 binary. If these ever
    overlapped, which parser read a file would depend on import order.
    """
    assert extractor_for(Path("a.xls")).name == "xls"        # type: ignore[union-attr]
    assert extractor_for(Path("a.xlsx")).name == "xlsx"      # type: ignore[union-attr]


def test_a_file_that_is_not_a_workbook_at_all_is_corrupt(tmp_path: Path) -> None:
    from app.core.errors import AppErrorException

    path = tmp_path / "junk.xls"
    path.write_bytes(b"nothing like a workbook")
    with pytest.raises(AppErrorException) as caught:
        list(extractor_for(path).extract(path))              # type: ignore[union-attr]
    assert caught.value.error.code == "ERR_FILE_CORRUPT"


def _biff8_unicode_str(name: str) -> bytes:
    """A BIFF8 string: 1-byte length, 1-byte options (0 = compressed/latin-1),
    then the bytes themselves. Format from `xlrd.biffh.unpack_unicode`."""
    encoded = name.encode("latin-1")
    return struct.pack("<BB", len(encoded), 0) + encoded


def biff8_workbook(values: Iterable[float]) -> bytes:
    """A genuine BIFF8 (Excel 97-2003) workbook, one numeric column.

    `biff2_workbook` above cannot hold 60,000 rows: BIFF2/3/4/5 cap a sheet at
    `utter_max_rows = 16384` inside `xlrd` (`xlrd/sheet.py`), which matches the
    real 16,384-row ceiling of the format those bytes describe. Only BIFF8 -
    what Excel 97 onward actually writes - raises that to 65,536, which is
    exactly why the work order's own example is *"a 1998 workbook"*: 1998 is
    Excel 97/2000, not Excel 5.

    BIFF8 also needs a workbook-globals stream ahead of the worksheet one -
    unlike BIFF2, where the file *is* the worksheet - so this writes `BOF`,
    one `BOUNDSHEET` record naming the sheet and pointing at the worksheet
    BOF's absolute offset, then `EOF`, followed by the worksheet stream
    itself: `BOF`, one `NUMBER` record (opcode 0x0203) per row, `EOF`. No OLE2
    compound-file wrapper is needed - `xlrd` falls back to treating the file
    as a bare BIFF stream whenever it does not start with the CFBF signature,
    which is also how BIFF2-4 workbooks were actually written by Excel.
    """
    def record(opcode: int, payload: bytes) -> bytes:
        return struct.pack("<HH", opcode, len(payload)) + payload

    worksheet = [record(0x0809, struct.pack("<HH", 0x0600, 0x0010))]  # BOF, worksheet
    for row, value in enumerate(values):
        worksheet.append(record(0x0203, struct.pack("<HHHd", row, 0, 0, float(value))))
    worksheet.append(record(0x000A, b""))                             # EOF
    worksheet_bytes = b"".join(worksheet)

    name = _biff8_unicode_str("Sheet1")
    bof = record(0x0809, struct.pack("<HH", 0x0600, 0x0005))          # BOF, globals
    eof = record(0x000A, b"")

    def boundsheet(offset: int) -> bytes:
        return record(0x0085, struct.pack("<iBB", offset, 0, 0) + name)

    globals_len = len(bof) + len(boundsheet(0)) + len(eof)
    globals_bytes = bof + boundsheet(globals_len) + eof

    return globals_bytes + worksheet_bytes


def test_a_60000_row_workbook_is_capped_not_exhausted(tmp_path: Path) -> None:
    """docs/WORKORDER-libraries-before-converters.md item 6.

    A 1998 workbook is not obliged to be small - a fifteen-year archive turns
    up spreadsheets with tens of thousands of rows, and `xls.py` is supposed to
    treat one the same way `office.py` treats a modern `.xlsx`: read up to
    `MAX_SHEET_ROWS`, warn about the rest, and never hold the whole sheet's
    text in the resulting document regardless of how large the file was.

    60,000 rows is a real BIFF8 workbook (see `biff8_workbook`), not a mock -
    `xlrd` gets bytes to genuinely parse, and the cap has to hold against the
    row count `xlrd` itself reports, not against a small stand-in for it.
    """
    from app.extract.office import MAX_SHEET_ROWS

    total_rows = 60_000
    path = tmp_path / "big.xls"
    path.write_bytes(biff8_workbook(range(1, total_rows + 1)))

    document = list(extractor_for(path).extract(path))[0]    # type: ignore[union-attr]

    # The cap held: the text in the document reflects MAX_SHEET_ROWS values,
    # not 60,000 - the property that keeps one huge workbook from becoming an
    # unbounded amount of text in memory. +1 line for the "Sheet: Sheet1"
    # label `prefix_label=True` writes ahead of the sheet's own rows.
    kept_lines = document.text.count("\n") + 1
    assert kept_lines <= MAX_SHEET_ROWS + 1
    assert str(MAX_SHEET_ROWS) in document.text
    assert str(MAX_SHEET_ROWS + 1) not in document.text
    assert str(total_rows) not in document.text

    # And it said so, the same way office.py does for an oversized .xlsx sheet.
    codes = [warning.code for warning in document.warnings]
    assert "ERR_FILE_TRUNCATED" in codes
