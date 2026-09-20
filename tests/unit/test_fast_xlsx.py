r"""The `.xlsx` fast path (`app/extract/ooxml_xlsx.py`) against openpyxl's own reader.

The claim it makes is strong - *the index does not change* - so the test is the
strongest available form of it: build a workbook with the things that make cell
text differ between readers (dates, booleans, floats, blank rows and columns,
shared strings with rich text, more rows than the cap), read it both ways through
the real extractor, and require identical text, anchors, labels and warnings.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

import pytest

openpyxl = pytest.importorskip("openpyxl")

import app.extract  # noqa: F401,E402
from app.extract.office import MAX_SHEET_ROWS, XlsxExtractor  # noqa: E402


def both(path: Path) -> tuple[Any, Any]:
    """`(fast document, openpyxl document)` from the same extractor."""
    extractor = XlsxExtractor()
    fast = extractor._read_fast(path)
    original = extractor._read_fast
    extractor._read_fast = lambda _path: None             # type: ignore[method-assign]
    try:
        slow = list(extractor._read(path, openpyxl))[0]
    finally:
        extractor._read_fast = original                    # type: ignore[method-assign]
    return fast, slow


def build(path: Path) -> Path:
    book = openpyxl.Workbook()
    costs = book.active
    costs.title = "Costs"
    costs.append(["Item", "Site", "Amount", "Ratio", "When", "Paid"])
    costs.append(["Burner", "Leeds", 4200, 1 / 7, datetime.datetime(2026, 9, 20, 13, 5), True])
    costs.append([])                                       # a blank row still counts as a row
    costs.append([None, None, "third column only"])       # the anchor is on column C
    costs.append(["Date only", "x", 7, 2.5, datetime.date(2025, 1, 2), False])
    costs.append(["with_x005F_escape", "  padded  "])
    notes = book.create_sheet("Notes & <more>")
    notes["B3"] = "starts at B3"
    notes["AZ3"] = "beyond the column cap"
    notes["BZ3"] = "way beyond"                            # column 78 > 64
    book.save(str(path))
    return path


def test_fast_and_openpyxl_agree_on_text_anchors_labels_and_sheets(tmp_path: Path) -> None:
    fast, slow = both(build(tmp_path / "book.xlsx"))
    assert fast is not None
    assert fast.text == slow.text
    assert fast.anchors == slow.anchors
    assert [(s.page, s.label) for s in fast.segments] == [(s.page, s.label) for s in slow.segments]
    assert fast.meta["sheets"] == slow.meta["sheets"] == ["Costs", "Notes & <more>"]
    assert "2026-09-20 13:05:00" in fast.text                   # a real datetime, not a serial number
    assert "way beyond" not in fast.text and "beyond the column cap" in fast.text


def test_a_sheet_over_the_row_cap_is_truncated_the_same_way_with_the_same_warning(tmp_path: Path) -> None:
    book = openpyxl.Workbook()
    sheet = book.active
    for row in range(1, MAX_SHEET_ROWS + 50):
        sheet.append([f"row {row}"])
    path = tmp_path / "long.xlsx"
    book.save(str(path))
    fast, slow = both(path)
    assert fast.text == slow.text
    assert [w.code for w in fast.warnings] == [w.code for w in slow.warnings] == ["ERR_FILE_TRUNCATED"]
    assert f"row {MAX_SHEET_ROWS}" in fast.text and f"row {MAX_SHEET_ROWS + 1}" not in fast.text


def test_a_workbook_with_a_chart_sheet_is_read_where_the_old_path_raised(tmp_path: Path) -> None:
    """openpyxl's read-only reader hands back a chartsheet with no `iter_rows`; the
    old extractor died on it with an AttributeError. A chart sheet has no cells."""
    book = openpyxl.Workbook()
    book.active["A1"] = "data"
    book.create_chartsheet("Chart")
    path = tmp_path / "chart.xlsx"
    book.save(str(path))
    fast = XlsxExtractor()._read_fast(path)
    assert fast is not None and "data" in fast.text


def test_a_file_that_is_not_a_workbook_is_declined_so_the_old_path_reports_it(tmp_path: Path) -> None:
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"this is not a zip")
    assert XlsxExtractor()._read_fast(path) is None
