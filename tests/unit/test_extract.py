"""Layer 2: extraction.

Structured around the acceptance checklist in BUILD_SPEC_V2.md. The rule the
whole layer exists to serve is the third non-negotiable: **one bad file never
halts a 100GB run.** So every failure here is asserted to be an `AppError` with
`SKIP_CONTINUE` and a usable suggestion - not merely "an exception was raised".

Fixtures are generated, not committed; see `tests/fixtures/generate.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.errors import ActionType, AppErrorException
from app.extract import chunk_document, extract, extractor_for, supported_extensions
from app.extract.base import DocumentBuilder, normalise_whitespace
from app.extract.email_files import html_to_text
from app.extract.plaintext import decode_bytes, looks_binary
from tests.fixtures.generate import DOCX_TABLE, PDF_PAGE_TEXT


def extract_one(path: Path):
    documents = list(extract(path))
    assert len(documents) == 1, f"expected one document from {path.name}, got {len(documents)}"
    return documents[0]


def skip_code(path: Path) -> AppErrorException:
    with pytest.raises(AppErrorException) as caught:
        list(extract(path))
    return caught.value


# --- the contract every failure must honour ---------------------------------

CORRUPT_FIXTURES = [
    ("corrupt/lies.pdf", "ERR_FILE_CORRUPT"),
    ("corrupt/empty.docx", "ERR_FILE_CORRUPT"),
    ("corrupt/notoffice.xlsx", "ERR_FILE_CORRUPT"),
    ("corrupt/unsupported.xyz", "ERR_UNSUPPORTED_TYPE"),
    ("pdf/truncated.pdf", "ERR_FILE_CORRUPT"),
    ("pdf/encrypted.pdf", "ERR_FILE_CORRUPT"),
    ("pdf/scanned.pdf", "ERR_NO_TEXT_LAYER"),
    ("plaintext/binary.log", "ERR_NO_TEXT_LAYER"),
    ("email/empty_body.eml", "ERR_NO_TEXT_LAYER"),
    ("email/broken.msg", "ERR_FILE_CORRUPT"),
]


@pytest.mark.parametrize("name,code", CORRUPT_FIXTURES, ids=[n for n, _ in CORRUPT_FIXTURES])
def test_bad_files_skip_with_the_right_code(fixture_root: Path, name: str, code: str) -> None:
    error = skip_code(fixture_root / name).error
    assert error.code == code


@pytest.mark.parametrize("name,_code", CORRUPT_FIXTURES, ids=[n for n, _ in CORRUPT_FIXTURES])
def test_every_skip_is_continuable_and_actionable(fixture_root: Path, name: str, _code: str) -> None:
    """A skip must not stop the run, and must tell the user what to do."""
    error = skip_code(fixture_root / name).error
    assert error.action_type is ActionType.SKIP_CONTINUE
    assert error.suggestion.strip(), f"{name} skipped with no suggestion"
    assert not error.is_fatal


def test_a_bad_file_does_not_stop_the_batch(fixture_root: Path) -> None:
    """The whole point of the layer: mark it, count it, carry on."""
    paths = [
        fixture_root / "corrupt/lies.pdf",
        fixture_root / "plaintext/utf8.txt",
        fixture_root / "pdf/scanned.pdf",
        fixture_root / "pdf/healthy.pdf",
    ]
    indexed, skipped = [], []
    for path in paths:
        try:
            indexed.extend(extract(path))
        except AppErrorException as exc:
            skipped.append(exc.error.code)

    assert len(indexed) == 2
    assert skipped == ["ERR_FILE_CORRUPT", "ERR_NO_TEXT_LAYER"]


# --- healthy fixtures yield plausible content -------------------------------

HEALTHY = [
    "pdf/healthy.pdf",
    "office/healthy.docx",
    "office/healthy.xlsx",
    "office/healthy.pptx",
    "email/thread_root.eml",
    "plaintext/utf8.txt",
    "plaintext/table.csv",
]


@pytest.mark.parametrize("name", HEALTHY)
def test_healthy_fixtures_yield_text_and_chunks(fixture_root: Path, name: str) -> None:
    document = extract_one(fixture_root / name)
    assert document.text.strip()
    chunks = chunk_document(document)
    assert chunks, f"{name} produced text but no chunks"
    for chunk in chunks:
        assert document.text[chunk.char_start:chunk.char_end] == chunk.text


@pytest.mark.parametrize("name", HEALTHY)
def test_segments_locate_themselves_exactly(fixture_root: Path, name: str) -> None:
    """The invariant every offset downstream depends on."""
    document = extract_one(fixture_root / name)
    for segment in document.segments:
        assert document.text[segment.char_start:segment.char_end] == segment.text


# --- PDF --------------------------------------------------------------------

def test_pdf_keeps_page_numbers(fixture_root: Path) -> None:
    document = extract_one(fixture_root / "pdf/healthy.pdf")
    assert [segment.page for segment in document.segments] == [1, 2, 3]
    for number, expected in PDF_PAGE_TEXT.items():
        segment = next(s for s in document.segments if s.page == number)
        assert expected in segment.text


def test_pdf_chunks_carry_the_page_they_start_on(fixture_root: Path) -> None:
    from app.extract.chunker import chunk_text

    document = extract_one(fixture_root / "pdf/healthy.pdf")
    # min_chunk_chars=0 disables runt-folding, which would otherwise merge this
    # fixture's one-sentence third page into the chunk that starts on page two.
    chunks = chunk_text(
        document.text,
        target_tokens=16,
        overlap_tokens=0,
        min_chunk_chars=0,
        page_lookup=document.page_lookup(),
    )
    assert {chunk.page for chunk in chunks} == {1, 2, 3}


def test_scanned_pdf_is_not_indexed_as_empty(fixture_root: Path) -> None:
    """The silent failure this layer exists to prevent: a scan that indexes
    'successfully' with no text, and is then unfindable forever."""
    error = skip_code(fixture_root / "pdf/scanned.pdf").error
    assert error.code == "ERR_NO_TEXT_LAYER"
    assert "scan" in error.details.lower()


def test_password_protected_pdf_says_so(fixture_root: Path) -> None:
    error = skip_code(fixture_root / "pdf/encrypted.pdf").error
    assert "password" in error.details.lower()


# --- Office -----------------------------------------------------------------

def test_docx_keeps_tables_in_document_order(fixture_root: Path) -> None:
    """Reading paragraphs then tables would put every table at the end, moving a
    contract's obligations after its signature block."""
    document = extract_one(fixture_root / "office/healthy.docx")
    text = document.text
    heading = text.index("Commissioning summary")
    table = text.index(DOCX_TABLE[1][0])
    closing = text.index("Signed off by the site engineer")
    assert heading < table < closing


def test_docx_extracts_table_cells(fixture_root: Path) -> None:
    document = extract_one(fixture_root / "office/healthy.docx")
    for row in DOCX_TABLE:
        for cell in row:
            assert cell in document.text


def test_xlsx_labels_each_sheet_and_numbers_it(fixture_root: Path) -> None:
    document = extract_one(fixture_root / "office/healthy.xlsx")
    assert "Sheet: Costs" in document.text
    assert "Sheet: Notes" in document.text
    assert [segment.page for segment in document.segments] == [1, 2]
    assert document.meta["sheets"] == ["Costs", "Notes"]


def test_xlsx_indexes_values_not_formulas(fixture_root: Path) -> None:
    document = extract_one(fixture_root / "office/healthy.xlsx")
    assert "=SUM" not in document.text, "a formula string is not what anyone searches for"
    assert "4150" in document.text


def test_pptx_extracts_slides_and_speaker_notes(fixture_root: Path) -> None:
    document = extract_one(fixture_root / "office/healthy.pptx")
    assert "Quarterly review" in document.text
    assert "on schedule for the March handover" in document.text
    assert "speaker notes" in document.text.lower()
    assert {segment.page for segment in document.segments} == {1, 2}


# --- plaintext and encodings ------------------------------------------------

def test_utf8_bom_is_stripped(fixture_root: Path) -> None:
    """Otherwise the first word is '\\ufeffSite', which no query matches."""
    document = extract_one(fixture_root / "plaintext/utf8_bom.txt")
    assert not document.text.startswith("﻿")
    assert document.text.startswith("Site survey notes")


def test_bom_and_plain_utf8_produce_identical_text(fixture_root: Path) -> None:
    plain = extract_one(fixture_root / "plaintext/utf8.txt")
    with_bom = extract_one(fixture_root / "plaintext/utf8_bom.txt")
    assert plain.text == with_bom.text


def test_cp1252_decodes_without_a_warning(fixture_root: Path) -> None:
    document = extract_one(fixture_root / "plaintext/cp1252.txt")
    assert "£4,150" in document.text
    assert "–" in document.text
    assert not document.warnings, "cp1252 is a clean decode, not a degraded one"


def test_undecodable_bytes_warn_but_still_index(fixture_root: Path) -> None:
    document = extract_one(fixture_root / "plaintext/undecodable.txt")
    assert "Header" in document.text
    codes = [warning.code for warning in document.warnings]
    assert codes == ["ERR_ENCODING"]
    assert document.warnings[0].action_type is ActionType.AUTO_FIX


def test_decode_ladder() -> None:
    assert decode_bytes(b"plain") == ("plain", False)
    assert decode_bytes(b"\xef\xbb\xbfplain") == ("plain", False)
    text, degraded = decode_bytes("£50 – ok".encode("cp1252"))
    assert (text, degraded) == ("£50 – ok", False)
    text, degraded = decode_bytes(b"\x81\x90")
    assert degraded is True


def test_binary_sniffing() -> None:
    assert looks_binary(b"\x00\x00\x00\x00" * 100)
    assert not looks_binary(b"ordinary text with no nulls")
    assert not looks_binary(b"")
    assert not looks_binary(b"\xff\xfeU\x00T\x00F\x001\x006\x00"), "UTF-16 is text"


# --- email ------------------------------------------------------------------

def test_eml_extracts_headers_and_body(fixture_root: Path) -> None:
    document = extract_one(fixture_root / "email/thread_root.eml")
    assert document.meta["subject"] == "Site survey findings"
    assert document.meta["sender"] == "priya@example.com"
    assert json.loads(document.meta["recipients"]) == ["sam@example.com", "dev@example.com"]
    assert document.meta["sent_at"] is not None
    assert "two valves needing replacement" in document.text


def test_email_headers_are_searchable_text(fixture_root: Path) -> None:
    """'the email from Priya about the survey' only works if both are indexed."""
    document = extract_one(fixture_root / "email/thread_root.eml")
    assert "priya@example.com" in document.text
    assert "Site survey findings" in document.text


def test_a_reply_shares_its_thread_key(fixture_root: Path) -> None:
    """Conversation grouping: a decision is rarely in a single message."""
    root = extract_one(fixture_root / "email/thread_root.eml")
    reply = extract_one(fixture_root / "email/thread_reply.eml")
    assert root.meta["conversation"] == reply.meta["conversation"] == "<thread-root@example.com>"


def test_html_only_email_is_stripped_to_text(fixture_root: Path) -> None:
    document = extract_one(fixture_root / "email/html_only.eml")
    assert "Throughput rose by 4%" in document.text
    assert "No incidents were & recorded" in document.text
    assert "<p>" not in document.text
    assert "alert(" not in document.text, "script bodies are not content"
    assert "color:red" not in document.text


def test_html_to_text_basics() -> None:
    assert html_to_text("<p>one</p><p>two</p>").split() == ["one", "two"]
    assert html_to_text("a<br>b") == "a\nb"
    assert html_to_text("<style>p{}</style>text") == "text"
    assert html_to_text("&amp;&lt;&gt;") == "&<>"


def test_eml_source_kind_is_recorded(fixture_root: Path) -> None:
    assert extract_one(fixture_root / "email/thread_root.eml").source_kind == "eml"


def test_msg_without_extract_msg_gives_installation_advice(
    fixture_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The library is optional, so its absence must be a message, not a crash."""
    import builtins

    real_import = builtins.__import__

    def refuse(name: str, *args, **kwargs):
        if name == "extract_msg":
            raise ImportError("No module named 'extract_msg'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)

    error = skip_code(fixture_root / "email/broken.msg").error
    assert error.code == "ERR_UNSUPPORTED_TYPE"
    assert "pip install extract-msg" in error.suggestion


# --- registry ---------------------------------------------------------------

def test_common_types_are_registered() -> None:
    for ext in (".pdf", ".docx", ".xlsx", ".pptx", ".eml", ".msg", ".txt", ".md", ".csv"):
        assert ext in supported_extensions(), f"{ext} has no extractor"


def test_legacy_office_has_no_direct_reader_and_says_what_is_needed() -> None:
    """.doc/.ppt have no parser of their own - they go through a converter.

    The message used to be `ERR_UNSUPPORTED_TYPE`, "this application does not
    read .doc", which was never true and which nobody could act on. With
    converters shipping enabled it is `ERR_CONVERTER_MISSING`, naming the
    binary and the install command, and falling back to indexing by name.

    **`.xls` used to be in this list and no longer is.** `xlrd` reads it
    in-process - see `app/extract/xls.py` and non-negotiable 12. `.doc` and
    `.ppt` stay because the OLE2 Word and PowerPoint streams genuinely have no
    Python reader; the container is openable, the document inside is not.
    """
    for ext in (".doc", ".ppt"):
        assert extractor_for(Path(f"legacy{ext}")) is None

    error = skip_code(Path("legacy.doc")).error

    # **The code depends on the machine**, so the property is what is asserted.
    # Without LibreOffice: ERR_CONVERTER_MISSING. With it, but unable to read
    # this fixture: ERR_CONVERTER_FAILED. Both are about the converter, and
    # both name it - which is the thing that makes them actionable and that
    # ERR_UNSUPPORTED_TYPE never was.
    # ERR_CONVERTER_MISSING names the binary to install; ERR_CONVERTER_FAILED
    # names the file and says indexing continues. Which one appears depends on
    # whether LibreOffice is on this machine, and both are actionable - which
    # ERR_UNSUPPORTED_TYPE, "this application does not read .doc", never was.
    assert "CONVERTER" in error.code, f"got {error.code}"
    assert ".doc" in error.render() or "soffice" in error.render()


def test_extension_matching_is_case_insensitive() -> None:
    assert extractor_for(Path("REPORT.PDF")) is not None
    assert extractor_for(Path("Book.XlSx")) is not None


def test_a_missing_file_is_a_clean_skip(tmp_path: Path) -> None:
    error = skip_code(tmp_path / "gone.txt").error
    assert error.code == "ERR_FILE_CORRUPT"


def test_registering_a_claimed_extension_is_refused() -> None:
    """Silently overwriting would mean .pdf is parsed by whichever module
    imported last - not something to discover in production."""
    from app.extract.base import register

    class Impostor:
        name = "impostor"
        extensions = frozenset({".pdf"})

        def supports(self, path: Path) -> bool:
            return True

        def extract(self, path: Path):
            return []

    with pytest.raises(ValueError, match="already handled"):
        register(Impostor())


# --- builder and helpers ----------------------------------------------------

def test_builder_maintains_the_offset_invariant() -> None:
    builder = DocumentBuilder(Path("x.txt"))
    builder.add("first", page=1)
    builder.add("second", page=2)
    document = builder.build()
    for segment in document.segments:
        assert document.text[segment.char_start:segment.char_end] == segment.text
    assert document.page_for_offset(0) == 1
    assert document.page_for_offset(document.text.index("second")) == 2


def test_builder_drops_empty_segments() -> None:
    builder = DocumentBuilder(Path("x.txt"))
    builder.add("real")
    builder.add("   \n  ")
    assert len(builder.build().segments) == 1


def test_builder_can_prefix_a_label_into_the_text() -> None:
    builder = DocumentBuilder(Path("x.xlsx"))
    builder.add("data", label="Sheet: Costs", prefix_label=True)
    assert builder.build().text.startswith("Sheet: Costs\ndata")


def test_page_lookup_matches_the_linear_scan() -> None:
    builder = DocumentBuilder(Path("x.pdf"))
    for number in range(1, 12):
        builder.add(f"page {number} body text", page=number)
    document = builder.build()
    lookup = document.page_lookup()
    for offset in range(0, len(document.text), 7):
        assert lookup(offset) == document.page_for_offset(offset)


def test_normalise_whitespace_collapses_ragged_input() -> None:
    assert normalise_whitespace("a  \n\n\n\n\nb") == "a\n\nb"
    assert normalise_whitespace("\r\nline\r\n") == "line"
    assert normalise_whitespace("   ") == ""


# --- PPTX: where the text actually hides ------------------------------------
#
# A deck-heavy corpus is the hard case. `slide.shapes` yields only top-level
# shapes, and a group is one opaque shape with no text frame - so reading
# `has_text_frame` alone silently loses every word inside every group, and
# grouping is how slides get built. Tables and charts are the same problem
# wearing different hats.

def test_pptx_reads_text_inside_grouped_shapes(fixture_root: Path) -> None:
    """The bug this guards: Ctrl+G on two shapes made their text invisible."""
    from tests.fixtures.generate import PPTX_GROUPED_TEXT

    document = extract_one(fixture_root / "office/rich.pptx")
    assert PPTX_GROUPED_TEXT in document.text
    assert "Second member of the group" in document.text


def test_pptx_reads_table_cells(fixture_root: Path) -> None:
    from tests.fixtures.generate import PPTX_TABLE

    document = extract_one(fixture_root / "office/rich.pptx")
    for row in PPTX_TABLE:
        for cell in row:
            assert cell in document.text, f"table cell {cell!r} was lost"


def test_pptx_reads_chart_labels(fixture_root: Path) -> None:
    """Category and series names are words people search for; the numbers are not."""
    from tests.fixtures.generate import (
        PPTX_CHART_CATEGORIES,
        PPTX_CHART_SERIES,
        PPTX_CHART_TITLE,
    )

    document = extract_one(fixture_root / "office/rich.pptx")
    assert PPTX_CHART_TITLE in document.text
    assert PPTX_CHART_SERIES in document.text
    for category in PPTX_CHART_CATEGORIES:
        assert category in document.text


def test_naive_shape_reading_would_have_missed_all_of_it(fixture_root: Path) -> None:
    """Proves the fix is load-bearing rather than decorative.

    Reconstructs the old logic - top-level shapes with a text frame - and
    asserts it finds none of the grouped, tabular or charted text. If this ever
    starts passing, python-pptx changed and the recursion may be redundant.
    """
    from pptx import Presentation

    from tests.fixtures.generate import PPTX_CHART_TITLE, PPTX_GROUPED_TEXT, PPTX_TABLE

    deck = Presentation(str(fixture_root / "office/rich.pptx"))
    naive = "\n".join(
        shape.text_frame.text
        for slide in deck.slides
        for shape in slide.shapes
        if getattr(shape, "has_text_frame", False)
    )
    assert PPTX_GROUPED_TEXT not in naive
    assert PPTX_TABLE[1][0] not in naive
    assert PPTX_CHART_TITLE not in naive


def test_picture_heavy_deck_is_flagged_but_still_indexed() -> None:
    """The PPTX analogue of a scanned PDF.

    A deck is rarely *entirely* pictures - there is always a title - so it
    indexes successfully while most of what it says stays unfindable. Without a
    warning the file looks fine and the absence only surfaces later, as a search
    that should have matched and didn't.

    Tested against the rule directly rather than by building a 50MB fixture:
    the threshold is the logic, and a real deck of that size would make the
    suite slow and the failure output unreadable.
    """
    from app.extract.office import _warn_if_mostly_pictures

    builder = DocumentBuilder(Path("infographic.pptx"))
    builder.add("Title only")
    _warn_if_mostly_pictures(
        builder, Path("infographic.pptx"),
        characters=300, slides=12, size_bytes=50 * 1_048_576,
    )
    document = builder.build()

    assert "Title only" in document.text, "still indexed, not skipped"
    assert [w.code for w in document.warnings] == ["ERR_NO_TEXT_LAYER"]
    assert "images rather than text" in document.warnings[0].suggestion
    assert "chars/MB" in document.warnings[0].details


def test_text_carrying_deck_is_not_flagged() -> None:
    from app.extract.office import _warn_if_mostly_pictures

    builder = DocumentBuilder(Path("report.pptx"))
    _warn_if_mostly_pictures(
        builder, Path("report.pptx"),
        characters=80_000, slides=40, size_bytes=20 * 1_048_576,
    )
    assert not builder.build().warnings


def test_small_deck_is_never_flagged() -> None:
    """Under a megabyte the ratio is noise - a 3-slide title deck is not a bug."""
    from app.extract.office import _warn_if_mostly_pictures

    builder = DocumentBuilder(Path("tiny.pptx"))
    _warn_if_mostly_pictures(
        builder, Path("tiny.pptx"), characters=10, slides=1, size_bytes=200_000
    )
    assert not builder.build().warnings


def test_normal_deck_is_not_flagged(fixture_root: Path) -> None:
    """The warning must not cry wolf on an ordinary small deck."""
    document = extract_one(fixture_root / "office/healthy.pptx")
    assert not document.warnings
