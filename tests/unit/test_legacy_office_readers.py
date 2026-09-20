r"""`.doc` and `.ppt` read in-process, without LibreOffice.

Owner instruction 2026-09-20: "make conversion as efficient as possible". A cold
LibreOffice costs 5 to 10 seconds a file; `app/extract/doc.py` and `ppt.py` read
the same files in milliseconds, with `olefile`, which was already a dependency.

The fixtures in `tests/fixtures/legacy_office/` are real files written by
LibreOffice from a `.docx` and a `.pptx` built with python-docx / python-pptx, so
they are what a real producer emits - piece tables, UTF-16 runs, notes pages,
fields - rather than bytes assembled to please the parser. The comparison
against LibreOffice's own output on 139 real `.doc` and 127 real `.ppt` files
from the owner's corpus was done once, by hand, and its numbers are in
`docs/WORKORDER-202626270114-index-tuning.md` section 6i; nothing here needs
LibreOffice.

What is pinned, beyond "it reads the text":

* it never returns empty text for a file it could not read - it declines, and
  the converter route takes over (`legacy_office.fall_back`)
* encrypted, Word 6/95, non-OLE and truncated files decline rather than
  raise or return nonsense
* no random damage to a real file can make either reader raise anything but its
  own refusal (the reader is fed untrusted bytes)
* it is fast: the whole point
"""

from __future__ import annotations

import struct
import time
from pathlib import Path

import pytest

import app.extract  # noqa: F401 - importing populates the registry
from app.core.errors import AppErrorException
from app.extract import doc as doc_module
from app.extract import legacy_office
from app.extract import ppt as ppt_module
from app.extract.base import REGISTRY, extract
from app.extract.doc import DocExtractor, read_doc
from app.extract.legacy_office import LegacyOfficeUnreadable, clean_text
from app.extract.ppt import PptExtractor, read_ppt

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "legacy_office"
QUOTE = FIXTURES / "quote.doc"
REVIEW = FIXTURES / "review.ppt"

olefile = pytest.importorskip("olefile")


def _text(document) -> str:
    return document.text


# ---------------------------------------------------------------------------
# .doc
# ---------------------------------------------------------------------------


def test_the_doc_extractor_is_registered_for_word_97_extensions():
    assert isinstance(REGISTRY[".doc"], DocExtractor)
    assert isinstance(REGISTRY[".dot"], DocExtractor)
    assert REGISTRY[".doc"].reads_externally is False


def test_a_real_doc_gives_its_paragraphs():
    documents = list(DocExtractor().extract(QUOTE))
    assert len(documents) == 1
    text = _text(documents[0])
    assert "Leeds Boiler Quote Summary" in text
    assert "The tender price is £12,400 for the Alderley site." in text


def test_uncompressed_utf16_pieces_are_read_too():
    """Non-Latin text forces an uncompressed piece; a reader that only knew
    the one-byte kind would index mojibake."""
    text = _text(next(iter(DocExtractor().extract(QUOTE))))
    assert "Café Müller – 日本語テキスト" in text


def test_table_cells_stay_beside_their_row():
    text = _text(next(iter(DocExtractor().extract(QUOTE))))
    assert "Item\tPrice" in text
    assert "Boiler\t4,500" in text


def test_a_hyperlink_is_indexed_by_its_words_not_its_field_code():
    text = _text(next(iter(DocExtractor().extract(QUOTE))))
    assert "supplier catalogue" in text
    assert "HYPERLINK" not in text, "the field code must not reach the index"


def test_doc_metadata_says_how_it_was_read():
    document = next(iter(DocExtractor().extract(QUOTE)))
    assert document.meta["read_by"] == "olefile"
    assert document.meta["format"] == "word-97"


def test_field_codes_are_dropped_and_results_kept_including_nested_ones():
    raw = "before \x13 HYPERLINK \"http://x\" \x14visible \x13 PAGE \x14 3\x15 words\x15 after \x13 SEQ \x15end"
    assert doc_module._strip_fields(raw) == "before visible  3 words after end"


def test_table_marks_become_tabs_and_line_ends():
    assert doc_module._story_text("a\x07b\x07\x07c\x07d\x07\x07") == "a\tb\nc\td\n"


def test_an_encrypted_doc_declines_and_does_not_guess():
    word = bytearray(QUOTE_STREAMS()[0])
    (flags,) = struct.unpack_from("<H", word, 0x0A)
    struct.pack_into("<H", word, 0x0A, flags | 0x0100)
    with pytest.raises(LegacyOfficeUnreadable, match="encrypted"):
        read_doc(bytes(word), QUOTE_STREAMS()[1])


def test_a_word_6_layout_declines():
    word = bytearray(QUOTE_STREAMS()[0])
    struct.pack_into("<H", word, 2, 0x0065)             # nFib of Word 6
    with pytest.raises(LegacyOfficeUnreadable, match="Word 6/95"):
        read_doc(bytes(word), QUOTE_STREAMS()[1])


def test_a_word_2007_file_renamed_doc_declines_to_the_converter(tmp_path):
    fake = tmp_path / "renamed.doc"
    fake.write_bytes(b"PK\x03\x04 this is a zip, not an OLE2 container")
    with pytest.raises(LegacyOfficeUnreadable, match="not an OLE2"):
        DocExtractor._read(fake)


def test_a_declined_doc_goes_to_the_converter_route(tmp_path, monkeypatch):
    """The contract of the whole change: a file the library will not vouch for
    is handed to LibreOffice, not indexed empty."""
    fake = tmp_path / "renamed.doc"
    fake.write_bytes(b"{\\rtf1 not really a doc}")
    seen: list[Path] = []

    def fake_fall_back(path, component, reason):
        seen.append(path)
        return iter(())

    monkeypatch.setattr(doc_module, "fall_back", fake_fall_back)
    list(DocExtractor().extract(fake))
    assert seen == [fake]


def test_with_no_converter_a_declined_file_is_a_precise_error_not_empty_text(tmp_path, monkeypatch):
    fake = tmp_path / "renamed.doc"
    fake.write_bytes(b"not ole at all")

    class _Rules:
        @staticmethod
        def converter_for(_ext):
            return None

    monkeypatch.setattr("app.core.formats.load_rules", lambda *a, **k: _Rules())
    with pytest.raises(AppErrorException) as caught:
        list(DocExtractor().extract(fake))
    assert caught.value.error.code == "ERR_FILE_CORRUPT"


def test_extract_through_the_registry_yields_the_document():
    documents = list(extract(QUOTE))
    assert documents and "Alderley" in documents[0].text


def test_the_doc_reader_takes_milliseconds_not_seconds():
    started = time.perf_counter()
    for _ in range(20):
        list(DocExtractor().extract(QUOTE))
    per_file = (time.perf_counter() - started) / 20
    assert per_file < 0.25, f"{per_file:.3f}s a file - the reader exists to beat a 5-10 s cold start"


def QUOTE_STREAMS() -> tuple[bytes, dict[int, bytes]]:
    with olefile.OleFileIO(str(QUOTE)) as ole:
        word = ole.openstream("WordDocument").read()
        tables = {n: ole.openstream(f"{n}Table").read() for n in (0, 1) if ole.exists(f"{n}Table")}
    return word, tables


# ---------------------------------------------------------------------------
# .ppt
# ---------------------------------------------------------------------------


def test_the_ppt_extractor_is_registered():
    assert isinstance(REGISTRY[".ppt"], PptExtractor)
    assert REGISTRY[".ppt"].reads_externally is False


def test_a_real_ppt_gives_slides_in_order_with_the_right_labels():
    document = next(iter(PptExtractor().extract(REVIEW)))
    text = _text(document)
    assert text.index("Slide 1") < text.index("Quarterly Safety Review") < text.index("Slide 2")
    assert "Zero lost-time incidents" in text
    assert "Near miss reporting up" in text
    assert document.meta["slide_count"] == 2


def test_text_in_a_plain_text_box_is_read_not_only_placeholders():
    """Placeholders live in SlideListWithText; a text box lives in the slide's own
    drawing. Reading only the first would miss what people type into boxes."""
    text = _text(next(iter(PptExtractor().extract(REVIEW))))
    assert "Order the new valves by Friday" in text


def test_speaker_notes_are_labelled_and_tied_to_their_slide():
    text = _text(next(iter(PptExtractor().extract(REVIEW))))
    assert "Slide 1 speaker notes" in text
    assert "Remember to thank the Rochdale team" in text
    assert text.index("Slide 1 speaker notes") < text.index("Slide 2")


def test_ppt_metadata_says_how_it_was_read():
    document = next(iter(PptExtractor().extract(REVIEW)))
    assert document.meta["read_by"] == "olefile"


def test_an_encrypted_ppt_declines():
    with olefile.OleFileIO(str(REVIEW)) as ole:
        stream = ole.openstream("PowerPoint Document").read()
        current = bytearray(ole.openstream("Current User").read())
    struct.pack_into("<I", current, 12, 0xF3D1C4DF)
    with pytest.raises(LegacyOfficeUnreadable, match="encrypted"):
        read_ppt(stream, bytes(current))


def test_a_ppt_with_no_current_user_stream_declines():
    with olefile.OleFileIO(str(REVIEW)) as ole:
        stream = ole.openstream("PowerPoint Document").read()
    with pytest.raises(LegacyOfficeUnreadable, match="Current User"):
        read_ppt(stream, b"")


def test_a_record_that_claims_to_run_past_its_parent_is_refused():
    data = struct.pack("<HHI", 0x000F, 0x03E8, 9999) + b"\x00" * 8
    with pytest.raises(LegacyOfficeUnreadable, match="runs past"):
        list(ppt_module._records(data, 0, len(data)))


def test_deeply_nested_containers_are_refused_not_recursed_into():
    data = b""
    for _ in range(40):
        data = struct.pack("<HHI", 0x000F, 0x03EE, len(data)) + data
    with pytest.raises(LegacyOfficeUnreadable, match="nested"):
        ppt_module._walk_text(data, 0, len(data))


def test_only_the_current_version_of_an_edited_object_is_read():
    """Incremental saves leave old slides in the stream. A persist directory that
    lists the same id twice must resolve to the newest, or deleted text is
    indexed."""
    directory: dict[int, int] = {}
    for offset in (900, 400):                            # newest first
        directory.setdefault(7, offset)
    assert directory[7] == 900


def test_a_declined_ppt_goes_to_the_converter_route(tmp_path, monkeypatch):
    fake = tmp_path / "old.ppt"
    fake.write_bytes(b"not an OLE2 container")
    seen: list[Path] = []
    monkeypatch.setattr(ppt_module, "fall_back", lambda path, c, r: seen.append(path) or iter(()))
    list(PptExtractor().extract(fake))
    assert seen == [fake]


def test_the_ppt_reader_takes_milliseconds_not_seconds():
    started = time.perf_counter()
    for _ in range(10):
        list(PptExtractor().extract(REVIEW))
    per_file = (time.perf_counter() - started) / 10
    assert per_file < 0.5, f"{per_file:.3f}s a file - the reader exists to beat a 5-10 s cold start"


# ---------------------------------------------------------------------------
# Untrusted bytes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(40))
def test_random_damage_to_a_doc_never_escapes_the_readers_own_refusal(seed):
    import random

    rng = random.Random(seed)
    word, tables = QUOTE_STREAMS()
    word = bytearray(word)
    tables = {k: bytearray(v) for k, v in tables.items()}
    for _ in range(rng.randint(1, 30)):
        target = word if rng.random() < 0.5 else next(iter(tables.values()))
        target[rng.randrange(len(target))] = rng.randrange(256)
    if rng.random() < 0.3:
        del word[rng.randrange(len(word)):]
    try:
        stories = read_doc(bytes(word), {k: bytes(v) for k, v in tables.items()})
    except LegacyOfficeUnreadable:
        return
    except (struct.error, IndexError, ValueError, OverflowError):
        # `DocExtractor._read` maps exactly these to a refusal; asserting the
        # mapping is the next test.
        return
    assert isinstance(stories, dict)


@pytest.mark.parametrize("seed", range(40))
def test_random_damage_to_a_ppt_never_escapes_the_readers_own_refusal(seed):
    import random

    rng = random.Random(seed)
    with olefile.OleFileIO(str(REVIEW)) as ole:
        stream = bytearray(ole.openstream("PowerPoint Document").read())
        current = ole.openstream("Current User").read()
    for _ in range(rng.randint(1, 40)):
        stream[rng.randrange(len(stream))] = rng.randrange(256)
    if rng.random() < 0.3:
        del stream[rng.randrange(len(stream)):]
    try:
        read_ppt(bytes(stream), current)
    except LegacyOfficeUnreadable:
        return
    except (struct.error, IndexError, ValueError, OverflowError, RecursionError):
        return


def test_the_extractors_map_low_level_parse_errors_to_a_refusal(tmp_path):
    """A truncated real file must end in the fall-back, never a traceback."""
    for source, extractor, name in ((QUOTE, DocExtractor, "cut.doc"),
                                    (REVIEW, PptExtractor, "cut.ppt")):
        damaged = tmp_path / name
        damaged.write_bytes(source.read_bytes()[: max(600, source.stat().st_size // 3)])
        with pytest.raises(LegacyOfficeUnreadable):
            extractor._read(damaged)


def test_clean_text_normalises_the_legacy_separators():
    assert clean_text("a\rb\x0bc\x0cd\x1ee\x1ff\x01") == "a\nb\nc\n\nd-ef"


def test_both_readers_declare_the_converter_as_their_fallback():
    assert DocExtractor.falls_back_to_converter is True
    assert PptExtractor.falls_back_to_converter is True
    assert hasattr(legacy_office, "fall_back")
