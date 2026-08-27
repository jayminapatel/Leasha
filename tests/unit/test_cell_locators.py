r"""Which row a spreadsheet hit came from.

Layer: L2/L4/L5. Adoptions §6a.

**A spreadsheet is the one file type where "which page" is not an answer.** A
40,000-row workbook matching your search is a place to start looking; the row
is the answer. Every other format either has no interior address a person
could act on, or has one - a chunk ordinal - that means nothing to anybody.

**The trap this file exists to hold shut.** The old extractor dropped empty
cells while flattening a row, so the first *written* cell of a row could be
column D and any locator built by counting tab-separated fields would have
said A. The text it indexes is byte-for-byte what it always was; only the
column number now travels beside it.

The other rule: **`Q3!D14` is what is stored and *Sheet 'Q3' · near D14* is
what is shown.** The store keeps a code, the words are written in the
presenter, and nothing anywhere parses a sentence back apart.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from app.extract.base import DocumentBuilder
from app.extract.cells import (
    MAX_COLUMN, cached_letter, column_letter, column_number, locator, parse,
)
from app.extract.chunker import chunk_document
from app.ui.presenter import cell_location


# ---------------------------------------------------------------------------
# The address itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("index", "letters"), [
    (1, "A"), (2, "B"), (25, "Y"), (26, "Z"), (27, "AA"), (28, "AB"),
    (52, "AZ"), (53, "BA"), (702, "ZZ"), (703, "AAA"), (16_384, "XFD"),
])
def test_the_column_letters_are_bijective_base_26(index, letters):
    r"""**Not ordinary base-26**, and this is where an off-by-one hides. There
    is no zero digit, so 26 is `Z` rather than `A0` and the borrow happens
    before the divide. A fixture with five columns would never catch it, and
    every real workbook has more than five.
    """
    assert column_letter(index) == letters
    assert column_number(letters) == index


@pytest.mark.parametrize("index", [0, -1, MAX_COLUMN + 1])
def test_a_column_outside_a_real_sheet_has_no_spelling(index):
    """`""` rather than an invented four-letter name: there is no such column,
    and a locator pointing at one would look right and be wrong."""
    assert column_letter(index) == ""


def test_the_memo_gives_the_same_answer_as_the_function():
    """The extractor calls this once per row on a corpus-sized run. A cache
    that ever disagreed with the thing it caches would be worse than the
    microseconds it saves."""
    for index in (1, 4, 26, 27, 200, 16_384, 0, -3):
        assert cached_letter(index) == column_letter(index)
        assert cached_letter(index) == column_letter(index)   # and again


def test_a_locator_round_trips():
    assert locator("Q3", 14, 4) == "Q3!D14"
    assert parse("Q3!D14") == ("Q3", "D", 14)


def test_a_sheet_with_no_name_produces_no_locator():
    """`!D14` points nowhere and reads as a bug. Nothing is the honest answer."""
    assert locator("", 14, 4) == ""
    assert locator("   ", 14, 4) == ""


def test_a_row_that_is_not_a_row_produces_no_locator():
    assert locator("Q3", 0) == ""
    assert locator("Q3", -1) == ""


def test_a_sheet_name_containing_the_separator_still_parses():
    """`!` is not reserved in a sheet name, so the split is on the **last**
    one. A sheet somebody called `Q3!draft` must not silently address a
    different sheet."""
    assert parse("Q3!draft!D14") == ("Q3!draft", "D", 14)


@pytest.mark.parametrize("rubbish", ["", None, "Q3", "!D14", "Q3!", "Q3!14",
                                     "Q3!D", "Q3!DD", "page 3"])
def test_anything_that_is_not_a_locator_is_none_rather_than_a_crash(rubbish):
    """**Never raises.** This reads a value out of the database beside a result
    row; a row written by a later version must cost the locator, not the page.
    """
    assert parse(rubbish) is None


# ---------------------------------------------------------------------------
# Anchors: landmarks inside a segment
# ---------------------------------------------------------------------------

def test_an_anchor_is_shifted_by_the_label_the_segment_prefixes():
    r"""`prefix_label=True` writes `Sheet: Q3` into the text, moving every row
    down by its length. The caller counts from its own text and cannot know
    that, so the shift is applied where the prefix is - which is the only
    place it cannot be forgotten."""
    builder = DocumentBuilder(pathlib.Path("book.xlsx"))
    builder.add("one\ntwo", page=1, label="Sheet: Q3", prefix_label=True,
                anchors=[(0, "Q3!A1"), (4, "Q3!A2")])
    document = builder.build()

    look = document.anchor_lookup()
    assert look(document.text.index("two")) == "Q3!A2"


def test_the_first_anchor_claims_the_heading_above_it():
    r"""**The bug this test was written for.** The label line sits above every
    anchor, so the *first chunk of every sheet* - which starts at offset zero -
    resolved to nothing and showed no locator at all. A heading belongs to the
    rows under it.
    """
    builder = DocumentBuilder(pathlib.Path("book.xlsx"))
    builder.add("one\ntwo", page=1, label="Sheet: Q3", prefix_label=True,
                anchors=[(0, "Q3!A1"), (4, "Q3!A2")])
    document = builder.build()
    assert document.anchor_lookup()(0) == "Q3!A1"


def test_a_second_segment_starts_where_it_should():
    """The separator between segments belongs to neither, and an offset landing
    in it resolves to what it follows - the rule `page_for_offset` already
    states, applied to anchors so the two cannot disagree."""
    builder = DocumentBuilder(pathlib.Path("book.xlsx"))
    builder.add("alpha", page=1, label="Sheet: One", prefix_label=True,
                anchors=[(0, "One!A1")])
    builder.add("beta", page=2, label="Sheet: Two", prefix_label=True,
                anchors=[(0, "Two!A1")])
    document = builder.build()

    look = document.anchor_lookup()
    assert look(document.text.index("alpha")) == "One!A1"
    assert look(document.text.index("beta")) == "Two!A1"


def test_a_document_with_no_anchors_answers_none_and_costs_nothing():
    """Which is every document that is not a spreadsheet - nearly all of them."""
    builder = DocumentBuilder(pathlib.Path("notes.txt"))
    builder.add("just some prose")
    document = builder.build()
    assert document.anchors == ()
    assert document.anchor_lookup()(0) is None


def test_a_chunk_carries_the_locator_of_where_it_starts():
    """The row a person sees at the top of the snippet - the same rule the
    page follows, for the same reason."""
    builder = DocumentBuilder(pathlib.Path("book.xlsx"))
    body = "\n".join(f"row {n} of the safety register" for n in range(1, 400))
    builder.add(body, page=1, label="Sheet: Q3", prefix_label=True,
                anchors=[(sum(len(f"row {m} of the safety register") + 1
                              for m in range(1, n)), f"Q3!A{n}")
                         for n in range(1, 400)])
    chunks = chunk_document(builder.build())

    assert len(chunks) > 1, "the fixture has to span more than one chunk"
    assert all(chunk.label for chunk in chunks)
    assert chunks[0].label == "Q3!A1"
    # Later chunks are further down the sheet, never back up it.
    rows = [int(parse(chunk.label)[2]) for chunk in chunks]
    assert rows == sorted(rows)


def test_a_chunker_stand_in_without_anchors_still_chunks():
    r"""`chunk_document` is duck-typed on purpose - the module does not import
    `base` - and several callers pass document-shaped objects. A stand-in with
    no `anchor_lookup` must cost the locator, never the chunking."""
    class _NotQuiteADocument:
        text = "some text that is definitely long enough to chunk. " * 40

        def page_lookup(self):
            return lambda _offset: 1

    chunks = chunk_document(_NotQuiteADocument())
    assert chunks and all(chunk.label is None for chunk in chunks)


# ---------------------------------------------------------------------------
# The real extractor
# ---------------------------------------------------------------------------

@pytest.fixture()
def workbook():
    """A sheet whose data starts in column B, because that is the trap."""
    openpyxl = pytest.importorskip("openpyxl")

    path = pathlib.Path(tempfile.mkdtemp()) / "register.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "Q3 Costs"
    for row in range(1, 200):
        # Column A left empty on purpose.
        sheet.cell(row=row, column=2, value=f"line {row}")
        sheet.cell(row=row, column=5, value=f"amount {row}")
    sheet.cell(row=14, column=5, value="pump station commissioning")
    book.create_sheet("Notes")["A1"] = "a note about the register"
    book.save(path)
    return path


def test_the_extractor_locates_a_row_at_its_first_written_cell(workbook):
    r"""**Column B, not column A.** The row's data starts at B, and a locator
    counting tab-separated fields would have said A - a cell reference that is
    confidently wrong, which is worse than none at all.
    """
    from app.extract.office import XlsxExtractor

    document = next(iter(XlsxExtractor().extract(workbook)))
    look = document.anchor_lookup()
    assert look(document.text.index("pump station commissioning")) == "Q3 Costs!B14"


def test_the_indexed_text_is_unchanged_by_any_of_this(workbook):
    r"""**The load-bearing guarantee.** Anchors ride alongside the text; they
    do not alter it. If they did, every existing chunk boundary, every stored
    offset and the whole corpus's comparability with the measured baseline
    would move - for a label.
    """
    from app.extract.office import XlsxExtractor

    document = next(iter(XlsxExtractor().extract(workbook)))
    lines = document.text.splitlines()
    assert lines[0] == "Sheet: Q3 Costs"
    assert lines[1] == "line 1\tamount 1"
    assert "\t\t" not in document.text, "an empty cell must not become a field"


def test_every_written_row_gets_an_anchor_and_no_blank_one_does(workbook):
    from app.extract.office import XlsxExtractor

    document = next(iter(XlsxExtractor().extract(workbook)))
    # 199 rows on Q3 Costs, one on Notes.
    assert len(document.anchors) == 200
    assert all(parse(text) for _offset, text in document.anchors)


def test_the_sheet_name_still_reaches_the_text_as_well(workbook):
    """It was there before this order and it stays: the label is written into
    the text because there is no column for it, which is what makes *searching*
    for a sheet name work. The locator is for showing, not for finding."""
    from app.extract.office import XlsxExtractor

    document = next(iter(XlsxExtractor().extract(workbook)))
    assert "Sheet: Q3 Costs" in document.text
    assert "Sheet: Notes" in document.text


def test_the_second_sheet_is_located_on_the_second_sheet(workbook):
    from app.extract.office import XlsxExtractor

    document = next(iter(XlsxExtractor().extract(workbook)))
    look = document.anchor_lookup()
    assert look(document.text.index("a note about the register")) == "Notes!A1"


def test_a_legacy_workbook_says_the_same_things():
    r"""A 1998 spreadsheet and a 2024 one should tell you the same things -
    the rule `xls.py` already states about its row and column caps, applied to
    the locator so the two do not drift.
    """
    xlwt = pytest.importorskip("xlwt")
    pytest.importorskip("xlrd")

    from app.extract.xls import XlsExtractor

    path = pathlib.Path(tempfile.mkdtemp()) / "old.xls"
    book = xlwt.Workbook()
    sheet = book.add_sheet("Q3")
    for row in range(0, 20):
        # Row 14 (index 13) gets the phrase instead; xlwt refuses to overwrite
        # a cell, which is a detail of the writer and not of what is tested.
        sheet.write(row, 1,                              # column B throughout
                    "pump station commissioning" if row == 13
                    else f"line {row + 1}")
    book.save(str(path))

    document = next(iter(XlsExtractor().extract(path)))
    look = document.anchor_lookup()
    assert look(document.text.index("pump station commissioning")) == "Q3!B14"


# ---------------------------------------------------------------------------
# What a person actually reads
# ---------------------------------------------------------------------------

def test_the_row_says_the_sheet_and_the_cell():
    assert cell_location("Q3!D14") == "Sheet 'Q3' · near D14"


def test_it_says_near_rather_than_at():
    r"""**A precision the value does not have.** The locator is the row the
    passage *starts* on and a passage is many rows long, so "at" would be a
    claim this cannot stand behind - on the one screen where somebody is
    deciding whether to open a forty-thousand-row workbook."""
    assert " near " in cell_location("Q3!D14")
    assert " at " not in cell_location("Q3!D14")


def test_a_result_that_is_not_a_spreadsheet_says_nothing_extra():
    assert cell_location("") == ""
    assert cell_location(None) == ""
    assert cell_location("page 3") == ""


def test_the_cell_replaces_the_sheet_number_on_the_row():
    r"""**"page 3" for a spreadsheet is true and useless.** `page` holds the
    sheet *index*, so the row said a thing nobody's spreadsheet calls a page
    and gave no way to find the data. The cell wins wherever there is one.
    """
    from app.search.engine import SearchResult
    from app.ui.presenter import to_row

    fields = dict(chunk_id=1, file_id=1, path="C:/work/register.xlsx", rank=1,
                  score=0.5, text="pump station commissioning", page=3)
    assert to_row(SearchResult(**fields, label="Q3!D14"), ()).location == (
        "Sheet 'Q3' · near D14")
    # And a PDF is untouched: page numbering is exactly right for one.
    assert to_row(SearchResult(**fields), ()).location == "page 3"


def test_the_row_keeps_the_machine_readable_form_too():
    """The grid preview will need it to scroll to the region, and getting it
    back by re-parsing the sentence is the mistake `cell_location` exists to
    prevent."""
    from app.search.engine import SearchResult
    from app.ui.presenter import to_row

    row = to_row(SearchResult(
        chunk_id=1, file_id=1, path="C:/work/x.xlsx", rank=1, score=0.5,
        text="x", label="Q3!D14"), ())
    assert row.label == "Q3!D14"


def test_the_locator_survives_the_database(workbook):
    r"""**End to end, because every part of this worked and the whole did
    not, twice before in this project.** Extract, chunk, store, search - and
    the sentence a person reads at the far end.
    """
    from app.search.engine import SearchEngine
    from app.extract.office import XlsxExtractor
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "cells.db").connect()
    document = next(iter(XlsxExtractor().extract(workbook)))
    file_id = store.upsert_file(
        str(workbook), parent_dir=str(workbook.parent), ext="xlsx",
        size_bytes=1, mtime_ns=1, status="INDEXED", source_kind="file")
    store.replace_chunks(file_id, [
        {"ordinal": ordinal, "text": chunk.text, "page": chunk.page,
         "char_start": chunk.char_start, "char_end": chunk.char_end,
         "label": chunk.label}
        for ordinal, chunk in enumerate(chunk_document(document))
    ])

    class _NoVectors:
        def search(self, *_a, **_k):
            return []

    class _NoModel:
        def embed(self, _t):
            raise RuntimeError("no model")

        def embed_all(self, _t):
            raise RuntimeError("no model")

    engine = SearchEngine(store, _NoVectors(), _NoModel())
    try:
        result = engine.search("commissioning", use_cache=False).results[0]
        assert parse(result.label), f"no locator came back: {result.label!r}"
        assert result.label.startswith("Q3 Costs!")
        from app.ui.presenter import to_row

        assert to_row(result, ("commissioning",)).location.startswith(
            "Sheet 'Q3 Costs' · near ")
    finally:
        engine.close()
