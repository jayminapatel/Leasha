"""Previewing what has no renderer of its own: Office, OpenDocument, drawings.

**The preview reuses the index's extractor rather than owning a list.** Faithful
rendering of a `.docx` means a word processor; showing what the *index* holds
means reusing `app/extract`. In a search tool the second is the more useful
thing, because what appears in the pane is exactly what was searched - "I found
it but I cannot see why" is the complaint this answers.

It also means preview coverage cannot drift from index coverage. A file type
added through the file-types UI becomes previewable at the moment it becomes
searchable, with nothing to keep in step.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.ui.preview_loader import (
    EXTRACTED_CAP, KIND_NONE, KIND_TEXT, _extractable, load_preview,
)


@pytest.fixture()
def docx_file(tmp_path: Path) -> Path:
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("Northern pump station commissioning report.")
    document.add_paragraph("Valve replacement scheduled for the autumn.")
    path = tmp_path / "report.docx"
    document.save(path)
    return path


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


# -- which files get here ----------------------------------------------------

def test_a_word_document_is_claimed_by_the_extractor(docx_file: Path) -> None:
    assert _extractable(docx_file)


def test_a_file_nothing_can_read_is_not(tmp_path: Path) -> None:
    """It should still get the card, not an empty pane."""
    unknown = tmp_path / "thing.zzz"
    unknown.write_bytes(b"\x00\x01")
    assert not _extractable(unknown)
    assert load_preview(str(unknown)).kind == KIND_NONE


def test_a_type_needing_an_external_program_is_left_alone(monkeypatch) -> None:
    """A Tier 2 converter shells out. That is fine overnight; not on an arrow key.

    Indexing a folder can afford to launch another program per file. A preview
    fired by the down arrow, two hundred milliseconds after a selection moved,
    cannot - and the card offering to open the file is the better answer.
    """
    import app.extract.base as base

    monkeypatch.setattr(base, "reads_externally", lambda _path: True)
    assert not _extractable(Path("drawing.dwg"))


def test_the_check_survives_a_registry_that_will_not_import(monkeypatch) -> None:
    """A broken optional library must not take the preview pane down with it.

    `_extractable` runs before anything is read, on every selection. If a
    half-installed dependency makes the registry raise on import, the honest
    outcome is "no preview for this one", not a dead pane.
    """
    import app.extract.base as base

    def explode(_path):
        raise RuntimeError("half-installed optional dependency")

    monkeypatch.setattr(base, "extractor_for", explode)
    assert _extractable(Path("x.docx")) is False


# -- what comes out ----------------------------------------------------------

def test_a_word_document_previews_as_its_text(docx_file: Path) -> None:
    preview = load_preview(str(docx_file))

    assert preview.kind == KIND_TEXT
    assert "Northern pump station" in preview.body
    assert "Valve replacement" in preview.body


def test_the_pane_says_this_is_the_text_not_the_document(docx_file: Path) -> None:
    """Otherwise somebody concludes their formatting has been lost."""
    notice = load_preview(str(docx_file)).notice

    assert "extracted" in notice.lower()
    assert "open" in notice.lower(), "say where the formatting is"


def test_a_spreadsheet_keeps_its_sheet_names(xlsx_file: Path) -> None:
    """A wall of cells with no sheet names answers the wrong question.

    Which sheet a number came from is most of what somebody previewing a
    spreadsheet wants to know, and `Segment.label` already carries it.
    """
    body = load_preview(str(xlsx_file)).body

    assert "Costs" in body
    assert "Schedule" in body
    assert "Pump housing" in body


def test_a_long_document_is_cut_and_says_so(tmp_path: Path, monkeypatch) -> None:
    """Truncated silently, a document appears to simply end where it does not."""
    import app.ui.preview_loader as module

    class Document:
        text = "x" * (EXTRACTED_CAP + 500)
        segments = ()

    monkeypatch.setattr(module, "_extractable", lambda _p: True)
    monkeypatch.setattr("app.extract.base.extract", lambda _p: iter([Document()]))

    target = tmp_path / "long.docx"
    target.write_bytes(b"stub")
    preview = load_preview(str(target))

    assert preview.truncated
    assert len(preview.body) == EXTRACTED_CAP


# -- failures ----------------------------------------------------------------

def test_a_corrupt_document_reports_why_rather_than_raising(tmp_path: Path) -> None:
    """A preview that throws shows a traceback for a file somebody arrowed past."""
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"this is not a zip archive")

    preview = load_preview(str(broken))

    assert preview.kind == KIND_NONE
    assert preview.error is not None
    assert preview.error.render(), "an error with no sentence in it is not one"


def test_an_extractor_error_keeps_its_own_code(tmp_path: Path, monkeypatch) -> None:
    """`ERR_NO_TEXT_LAYER` already carries a fix line. Wrapping it discards one."""
    from app.core.errors import AppErrorException, make_error

    import app.ui.preview_loader as module

    def refuse(_path):
        raise AppErrorException(make_error(
            "ERR_NO_TEXT_LAYER", "extract.pdf", path="x"))

    monkeypatch.setattr(module, "_extractable", lambda _p: True)
    monkeypatch.setattr("app.extract.base.extract", refuse)

    target = tmp_path / "scan.docx"
    target.write_bytes(b"stub")
    preview = load_preview(str(target))

    assert preview.error is not None
    assert preview.error.code == "ERR_NO_TEXT_LAYER"


def test_a_missing_file_is_still_reported_before_anything_is_extracted() -> None:
    preview = load_preview(r"D:\nowhere\absent.docx")

    assert preview.kind == KIND_NONE
    assert preview.error is not None
    assert preview.error.code == "ERR_FILE_MISSING"
