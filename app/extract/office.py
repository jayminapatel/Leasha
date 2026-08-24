"""DOCX, XLSX and PPTX.

Layer: L2

Each format has one thing that is easy to get wrong:

* **DOCX** - `doc.paragraphs` and `doc.tables` are separate collections, so
  reading them in turn silently reorders the document: every table ends up after
  every paragraph. A contract whose obligations are in a table then reads as if
  they came after the signature block. The body's XML children are walked in
  document order instead.
* **XLSX** - opened read-only and `data_only`, so a formula contributes its last
  cached *result* rather than the string `=VLOOKUP(...)`, which is what a person
  searching for a number is actually looking for. Sheet names are written into
  the text because there is no column for them and "which sheet was that on?" is
  a real question.
* **PPTX** - the speaker notes usually contain the sentences, while the slide
  contains three words and a chart. Both are extracted; notes are labelled so a
  result can say where it came from.

The pre-2007 binary formats (.doc, .xls, .ppt) are deliberately unregistered:
they need a completely different parser, and `ERR_UNSUPPORTED_TYPE` already tells
the user to re-save them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Iterator

from app.core.errors import make_error, raise_error
from app.extract.base import Document, DocumentBuilder, normalise_whitespace, register

__all__ = ["DocxExtractor", "XlsxExtractor", "PptxExtractor", "MAX_SHEET_ROWS"]

#: A spreadsheet used as a database can hold a million rows. Left uncapped, one
#: such file produces tens of thousands of chunks and dominates both the index
#: and the embedding queue for hours. The cap is loud - it raises a warning
#: naming the file and the row count - rather than silent.
MAX_SHEET_ROWS = 5_000

#: Cells per row is capped for the same reason, and because a sheet padded to
#: column XFD is almost always padding rather than data.
MAX_SHEET_COLUMNS = 64


def _fail(component: str, path: Path, exc: BaseException) -> None:
    """Map a parser exception onto the right skip code.

    A locked file and a corrupt one need different advice - "close Excel" versus
    "this file is damaged" - and guessing wrong sends the user somewhere useless.
    """
    if isinstance(exc, PermissionError):
        raise_error("ERR_FILE_LOCKED", component, path=str(path), details=str(exc))
    raise_error("ERR_FILE_CORRUPT", component, path=str(path), details=f"{type(exc).__name__}: {exc}")


class DocxExtractor:
    name = "docx"
    extensions = frozenset({".docx", ".docm"})

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        import docx
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        try:
            source = docx.Document(str(path))
        except Exception as exc:                          # noqa: BLE001
            _fail("extract.docx", path, exc)
            return

        builder = DocumentBuilder(path)

        def blocks() -> Iterator[Any]:
            for child in source.element.body.iterchildren():
                if child.tag == qn("w:p"):
                    yield Paragraph(child, source)
                elif child.tag == qn("w:tbl"):
                    yield Table(child, source)

        for block in blocks():
            if isinstance(block, Paragraph):
                builder.add(block.text)
            else:
                builder.add(_table_text(block))

        yield builder.build()


def _table_text(table: Any) -> str:
    """Rows as tab-separated lines, so columns stay adjacent to their headers."""
    lines: list[str] = []
    for row in table.rows:
        cells = [" ".join(cell.text.split()) for cell in row.cells]
        # A merged cell repeats across the span; collapse the repeats.
        deduped: list[str] = []
        for cell in cells:
            if not deduped or deduped[-1] != cell:
                deduped.append(cell)
        line = "\t".join(deduped).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


class XlsxExtractor:
    name = "xlsx"
    extensions = frozenset({".xlsx", ".xlsm", ".xltx"})

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        import openpyxl

        try:
            workbook = openpyxl.load_workbook(
                str(path), read_only=True, data_only=True, keep_links=False
            )
        except Exception as exc:                          # noqa: BLE001
            _fail("extract.xlsx", path, exc)
            return

        try:
            builder = DocumentBuilder(path)
            builder.meta["sheets"] = list(workbook.sheetnames)

            for index, name in enumerate(workbook.sheetnames, start=1):
                sheet = workbook[name]
                lines: list[str] = []
                truncated = False

                for row_number, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                    if row_number > MAX_SHEET_ROWS:
                        truncated = True
                        break
                    values = [
                        str(value).strip()
                        for value in row[:MAX_SHEET_COLUMNS]
                        if value is not None and str(value).strip()
                    ]
                    if values:
                        lines.append("\t".join(values))

                if truncated:
                    builder.warn(
                        make_error(
                            "ERR_FILE_CORRUPT",
                            "extract.xlsx",
                            path=str(path),
                            suggestion=(
                                f"Sheet '{name}' has more than {MAX_SHEET_ROWS:,} rows. The first "
                                f"{MAX_SHEET_ROWS:,} were indexed; the rest were not, to stop one "
                                "spreadsheet from dominating the index. Split it, or export the "
                                "table to CSV, if the later rows matter."
                            ),
                            details=f"Sheet '{name}' truncated at {MAX_SHEET_ROWS} rows.",
                        )
                    )

                builder.add(
                    "\n".join(lines),
                    page=index,
                    label=f"Sheet: {name}",
                    prefix_label=True,
                )

            yield builder.build()
        finally:
            workbook.close()


class PptxExtractor:
    name = "pptx"
    extensions = frozenset({".pptx", ".pptm"})

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        from pptx import Presentation

        try:
            deck = Presentation(str(path))
        except Exception as exc:                          # noqa: BLE001
            _fail("extract.pptx", path, exc)
            return

        builder = DocumentBuilder(path)
        builder.meta["slide_count"] = len(deck.slides)

        for number, slide in enumerate(deck.slides, start=1):
            parts = [
                shape.text_frame.text
                for shape in slide.shapes
                if getattr(shape, "has_text_frame", False) and shape.text_frame.text.strip()
            ]
            builder.add(
                normalise_whitespace("\n".join(parts)),
                page=number,
                label=f"Slide {number}",
                prefix_label=True,
            )

            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame
                if notes is not None and notes.text.strip():
                    builder.add(
                        normalise_whitespace(notes.text),
                        page=number,
                        label=f"Slide {number} speaker notes",
                        prefix_label=True,
                    )

        yield builder.build()


register(DocxExtractor())
register(XlsxExtractor())
register(PptxExtractor())
