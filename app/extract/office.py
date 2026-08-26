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
* **PPTX** - `slide.shapes` yields **top-level shapes only**, and a group is one
  opaque shape with no text frame of its own. Reading `has_text_frame` alone
  therefore loses every word inside every group - and grouping is how slides get
  built, so on a deck-heavy corpus that is not an edge case, it is most of the
  content. Groups are recursed into; tables and charts, which have no text frame
  either, are read for their cells and their category and series labels. The
  speaker notes usually contain the sentences while the slide contains three
  words and a chart, so both are extracted and the notes are labelled.

The pre-2007 binary formats (.doc, .xls, .ppt) are deliberately unregistered:
they need a completely different parser, and `ERR_UNSUPPORTED_TYPE` already tells
the user to re-save them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Iterator

from app.core.errors import make_error, raise_error
from app.core.format_health import Requirement
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
    requires = (Requirement("docx", "python-docx",
                            provides="Word document text", hard=True),)

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
    requires = (Requirement("openpyxl", "openpyxl",
                            provides="spreadsheet cell text", hard=True),)

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        import warnings

        import openpyxl

        # **openpyxl warns on stderr, straight through the progress line.**
        #
        # "Unknown extension is not supported and will be removed", "Data
        # Validation extension...", "Print area cannot be set to Defined
        # name..." - none of which a person indexing a corpus can act on, and
        # all of which are perfectly normal for a real spreadsheet. On a long
        # run they arrive mid-line and split the progress display in half,
        # which is how an index that is working looks broken:
        #
        #     1,880 docs ... 37,063 chunks | 4/min | nov07pilot.ppt [59
        #     venv\Lib\site-packages\openpyxl\...\_reader.py:329: UserWarning:
        #
        # Suppressed here rather than globally: this is the library that emits
        # them, this is the call that triggers them, and a blanket filter would
        # also hide a warning worth reading from somewhere else.
        # **`yield from`, not `return`.** This is a generator: `return` would
        # hand back an unstarted iterator and leave the `with` block before a
        # single row was read, so every warning would fire anyway. The filter
        # has to span the iteration, which is what `yield from` inside the
        # block does.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            yield from self._read(path, openpyxl)

    def _read(self, path: Path, openpyxl: Any) -> Iterable[Document]:
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
                            # **Not ERR_FILE_CORRUPT.** That renders as "Cannot
                            # read '<path>' - it is encrypted or damaged", which
                            # is then followed by a detail line saying the first
                            # 5,000 rows WERE indexed. The two contradict each
                            # other, and the headline is the false one: the file
                            # read perfectly and was capped on purpose.
                            "ERR_FILE_TRUNCATED",
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
    requires = (Requirement("pptx", "python-pptx",
                            provides="slide and notes text", hard=True),)

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
        total_chars = 0

        for number, slide in enumerate(deck.slides, start=1):
            parts: list[str] = []
            for shape in slide.shapes:
                parts.extend(_shape_text(shape))

            body = normalise_whitespace("\n".join(parts))
            total_chars += len(body)
            builder.add(
                body,
                page=number,
                label=f"Slide {number}",
                prefix_label=True,
            )

            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame
                if notes is not None and notes.text.strip():
                    notes_body = normalise_whitespace(notes.text)
                    total_chars += len(notes_body)
                    builder.add(
                        notes_body,
                        page=number,
                        label=f"Slide {number} speaker notes",
                        prefix_label=True,
                    )

        try:
            size_bytes = path.stat().st_size
        except OSError:
            size_bytes = 0
        _warn_if_mostly_pictures(builder, path, total_chars, len(deck.slides), size_bytes)

        yield builder.build()


def _shape_text(shape: Any) -> list[str]:
    """Every piece of text a slide shape holds, recursing into groups.

    `slide.shapes` yields **top-level shapes only**, and a group is one opaque
    shape with no text frame of its own. Reading only `has_text_frame` therefore
    loses every word inside every group - and grouping is how people build
    slides. On a deck-heavy corpus that is not an edge case, it is most of the
    content.

    Tables and charts are the same story in a different shape: a comparison
    table or a chart's category labels are exactly the text someone searches
    for, and neither has a text frame.
    """
    found: list[str] = []

    # 6 == MSO_SHAPE_TYPE.GROUP. Compared numerically so a python-pptx version
    # that moves the enum cannot silently turn this back into the old bug.
    if getattr(shape, "shape_type", None) == 6 or hasattr(shape, "shapes"):
        for child in getattr(shape, "shapes", ()):
            found.extend(_shape_text(child))
        return found

    if getattr(shape, "has_text_frame", False):
        text = shape.text_frame.text
        if text.strip():
            found.append(text)

    if getattr(shape, "has_table", False):
        try:
            found.append(_pptx_table_text(shape.table))
        except Exception:                                 # noqa: BLE001 - one odd table
            pass

    if getattr(shape, "has_chart", False):
        try:
            found.extend(_chart_text(shape.chart))
        except Exception:                                 # noqa: BLE001 - chart XML varies wildly
            pass

    return [part for part in found if part and part.strip()]


def _pptx_table_text(table: Any) -> str:
    lines = []
    for row in table.rows:
        cells = [" ".join(cell.text.split()) for cell in row.cells]
        line = "\t".join(cells).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _chart_text(chart: Any) -> list[str]:
    """Title, category labels and series names - the words, not the numbers."""
    found: list[str] = []
    if getattr(chart, "has_title", False) and chart.chart_title.has_text_frame:
        found.append(chart.chart_title.text_frame.text)
    for plot in getattr(chart, "plots", ()):
        categories = [str(c) for c in getattr(plot, "categories", ()) if c is not None]
        if categories:
            found.append(" ".join(categories))
    for series in getattr(chart, "series", ()):
        name = getattr(series, "name", None)
        if name:
            found.append(str(name))
    return found


#: Below this many characters per megabyte, a deck is mostly pictures. Chosen
#: from real decks: a text-carrying slide runs into the thousands per MB, while
#: an infographic exported to PowerPoint lands in the low hundreds.
MIN_PPTX_CHARS_PER_MB = 400


def _warn_if_mostly_pictures(
    builder: DocumentBuilder, path: Path, characters: int, slides: int, size_bytes: int
) -> None:
    """Flag a deck whose content is images, the PPTX analogue of a scanned PDF.

    A PDF with no text at all is skipped outright. A deck is rarely *entirely*
    pictures - there is always a title - so it indexes successfully while most
    of what it says stays unsearchable. Without this the file looks fine and the
    absence only shows up as a search that should have matched and didn't.
    """
    megabytes = size_bytes / 1_048_576
    if megabytes < 1 or slides == 0:
        return

    density = characters / megabytes
    if density >= MIN_PPTX_CHARS_PER_MB:
        return

    # **Its own code, not `ERR_NO_TEXT_LAYER`.** They read the same to a person
    # and mean opposite things to the indexer: a scanned PDF is work the images
    # pass should retry, and a picture-heavy deck is work the OCR strategy
    # explicitly declines. Sharing the code made the second look like the first,
    # so `--only-ocr` would have queued every infographic in the corpus. It also
    # gives §5 its count for free - the two are now countable apart.
    builder.warn(
        make_error(
            "ERR_MOSTLY_PICTURES",
            "extract.pptx",
            path=str(path),
            suggestion=(
                "Most of this deck's content appears to be images rather than text, so most "
                "of it will not be findable by searching. That is expected for an exported "
                "infographic. If it should be searchable, the text has to exist as text on "
                "the slide - V2 does not read words out of pictures."
            ),
            details=(
                f"{characters:,} characters across {slides} slide(s) in a "
                f"{megabytes:,.0f} MB file ({density:,.0f} chars/MB). Indexed anyway."
            ),
        )
    )


register(DocxExtractor())
register(XlsxExtractor())
register(PptxExtractor())
