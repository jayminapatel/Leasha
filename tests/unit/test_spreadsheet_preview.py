"""Workspace §4b: a spreadsheet, previewed as a real grid.

Layer: L5

**The biggest preview upgrade in the order.** `.xlsx` used to preview as the
same tab-separated wall of text the index holds; this reads it with
`openpyxl` - already a dependency - into a `SheetGrid` per worksheet, which
`SpreadsheetView` draws as sheet tabs over a `QTableView`.

**`.xls` goes through the existing LibreOffice converter route**, converted
to `.xlsx` and read the same way, so there is one grid-building code path
rather than a second reader for `xlrd`'s cell types. Absent a converter, the
preview falls back to the flat text table that already worked before this
feature existed - a spreadsheet that cannot become a grid still shows
*something*.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ui.preview_loader import (
    KIND_SPREADSHEET,
    KIND_TEXT,
    SHEET_PREVIEW_MAX_COLUMNS,
    SHEET_PREVIEW_MAX_ROWS,
    SheetGrid,
    _sheet_grid,
    _spreadsheet_notice,
    kind_for,
    load_preview,
)


@pytest.fixture()
def xlsx_file(tmp_path: Path) -> Path:
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    book.active.title = "Costs"
    book.active["A1"] = "Pump housing"
    book.active["B1"] = 4200
    second = book.create_sheet("Schedule")
    second["A1"] = "Autumn shutdown"
    path = tmp_path / "budget.xlsx"
    book.save(path)
    return path


# --- choosing the kind -------------------------------------------------------

@pytest.mark.parametrize("name", [
    "budget.xlsx", "budget.XLSX", "budget.xlsm", "template.xltx",
    "legacy.xls", "legacy.xlt",
])
def test_spreadsheet_extensions_are_routed_to_the_grid(name):
    assert kind_for(Path(name)) == KIND_SPREADSHEET


# --- reading a real workbook --------------------------------------------------

def test_a_workbook_previews_as_a_grid_with_its_sheet_names(xlsx_file: Path):
    preview = load_preview(str(xlsx_file))

    assert preview.kind == KIND_SPREADSHEET
    sheets = preview.meta["sheets"]
    names = [sheet.name for sheet in sheets]
    assert names == ["Costs", "Schedule"]


def test_cell_values_come_back_as_strings_in_their_row(xlsx_file: Path):
    preview = load_preview(str(xlsx_file))
    costs = preview.meta["sheets"][0]

    assert costs.rows[0] == ("Pump housing", "4200")


def test_an_empty_workbook_still_previews(tmp_path: Path):
    openpyxl = pytest.importorskip("openpyxl")
    book = openpyxl.Workbook()
    path = tmp_path / "blank.xlsx"
    book.save(path)

    preview = load_preview(str(path))

    assert preview.kind == KIND_SPREADSHEET
    assert preview.meta["sheets"]


# --- the cap and its notice --------------------------------------------------

def test_the_grid_builder_caps_rows_and_says_so():
    rows = [(f"row{i}",) for i in range(SHEET_PREVIEW_MAX_ROWS + 5)]

    grid = _sheet_grid("Big", rows)

    assert len(grid.rows) == SHEET_PREVIEW_MAX_ROWS
    assert grid.truncated_rows
    assert not grid.truncated_columns


def test_the_grid_builder_caps_columns_and_says_so():
    row = tuple(f"c{i}" for i in range(SHEET_PREVIEW_MAX_COLUMNS + 3))

    grid = _sheet_grid("Wide", [row])

    assert len(grid.rows[0]) == SHEET_PREVIEW_MAX_COLUMNS
    assert grid.truncated_columns
    assert not grid.truncated_rows


def test_a_grid_under_the_cap_is_not_marked_truncated():
    grid = _sheet_grid("Small", [("a", "b")])

    assert not grid.truncated
    assert not grid.truncated_rows
    assert not grid.truncated_columns


def test_the_notice_names_only_the_sheets_that_were_cut():
    small = SheetGrid(name="Small", rows=(("a",),))
    big = SheetGrid(name="Big", rows=(("a",),), truncated_rows=True)

    notice = _spreadsheet_notice([small, big])

    assert "Big" in notice
    assert "Small" not in notice
    assert "Large sheet" in notice
    assert str(SHEET_PREVIEW_MAX_ROWS) in notice


def test_no_notice_when_nothing_was_cut():
    small = SheetGrid(name="Small", rows=(("a",),))

    assert _spreadsheet_notice([small]) == ""


# --- .xls: the converter route, and its fallback -----------------------------

def test_xls_without_a_converter_falls_back_to_the_text_table(tmp_path, monkeypatch):
    r"""No LibreOffice on this machine - a spreadsheet that cannot become a
    grid must still preview as *something*, not as a hole where a preview
    used to be."""
    import app.ui.preview_loader as module

    monkeypatch.setattr(module, "_read_xls_via_converter", lambda _p: None)
    monkeypatch.setattr(module, "_extractable", lambda _p: True)

    class Document:
        text = "Costs\tSchedule"
        segments = ()

    monkeypatch.setattr("app.extract.base.extract", lambda _p: iter([Document()]))

    target = tmp_path / "legacy.xls"
    target.write_bytes(b"stub")

    preview = load_preview(str(target))

    assert preview.kind == KIND_TEXT
    assert "Costs" in preview.body


def test_xls_reads_through_the_converter_when_one_is_present(tmp_path, monkeypatch):
    """The converter route is exercised without actually shelling out to
    LibreOffice: `_read_xls_via_converter` is the one seam, and this proves
    `_spreadsheet_preview` uses whatever it returns rather than the fallback."""
    import app.ui.preview_loader as module

    fake = [SheetGrid(name="Costs", rows=(("Pump housing", "4200"),))]
    monkeypatch.setattr(module, "_read_xls_via_converter", lambda _p: fake)

    target = tmp_path / "legacy.xls"
    target.write_bytes(b"stub")

    preview = load_preview(str(target))

    assert preview.kind == KIND_SPREADSHEET
    assert preview.meta["sheets"] == fake


# --- widget ------------------------------------------------------------------

def test_the_widget_draws_one_tab_per_sheet():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from app.ui.widgets.spreadsheet_view import SpreadsheetView

    QApplication.instance() or QApplication([])
    sheets = [
        SheetGrid(name="Costs", rows=(("Pump housing", "4200"),)),
        SheetGrid(name="Schedule", rows=(("Autumn shutdown",),)),
    ]

    view = SpreadsheetView()
    view.show_sheets(sheets)

    assert view.count() == 2
    assert view.tabText(0) == "Costs"
    assert view.tabText(1) == "Schedule"
    first_model = view.widget(0).model()
    assert first_model.data(first_model.index(0, 0)) == "Pump housing"
    assert first_model.headerData(0, Qt.Orientation.Horizontal) == "A"


def test_rebuilding_the_widget_replaces_rather_than_accumulates():
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
    from PySide6.QtWidgets import QApplication

    from app.ui.widgets.spreadsheet_view import SpreadsheetView

    QApplication.instance() or QApplication([])
    view = SpreadsheetView()
    view.show_sheets([SheetGrid(name="One", rows=())])
    view.show_sheets([SheetGrid(name="Two", rows=())])

    assert view.count() == 1
    assert view.tabText(0) == "Two"
