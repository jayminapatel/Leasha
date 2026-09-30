r"""Read enough of a file to preview it, off the interface thread.

Layer: L5

**Every line here runs on a worker**, which is why it is a module of its own
rather than methods on the pane. Reading the first megabyte of a file on a
sleeping external drive takes seconds; doing that between two presses of the
down arrow is the freeze this application has a standing rule against.

The pane decides how to draw. This decides *what to read* and, more importantly,
**how much** - a preview of a 400MB log is the first page of it, and reading the
rest to show a screenful would be the most expensive thing the window ever does.

Nothing imports Qt, so every decision here is testable without a display.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from app.core.errors import AppError, make_error
from app.core.logging import logger

__all__ = ["Preview", "KIND_TEXT", "KIND_HTML", "KIND_PDF", "KIND_IMAGE",
           "KIND_MARKDOWN", "KIND_SPREADSHEET", "KIND_EPUB", "KIND_NONE",
           "kind_for", "load_preview", "load_preview_for", "stored_text",
           "MailPreview", "mail_preview", "NO_MESSAGE_TEXT",
           "CAPS", "SheetGrid", "SHEET_PREVIEW_MAX_ROWS",
           "SHEET_PREVIEW_MAX_COLUMNS", "EpubChapter",
           "office_converter_available", "office_pdf_cache_path",
           "ensure_office_pdf", "OFFICE_CONVERTER_MISSING_NOTE",
           "DWG_BINARY", "DWG_PREVIEW_NOTE", "DWG_CONVERTER_MISSING_NOTE",
           "DWG_PIN_TO_SEE_NOTE", "dwg_preview_available",
           "dwg_svg_cache_path", "ensure_dwg_svg",
           "offline_volume_subtitle", "volume_preview"]

_log = logger.bind(component="ui.preview")

KIND_TEXT = "text"
KIND_HTML = "html"
KIND_PDF = "pdf"
KIND_IMAGE = "image"
#: Workspace §4a. Rendered through `QTextDocument.setMarkdown` rather than as
#: raw text - a `.md` file is prose with structure in it, and showing the
#: literal `#`, `**` and `-` characters is showing the markup rather than the
#: document. A kind of its own rather than a flag on `KIND_TEXT`, because the
#: pane's dispatch is already a kind-by-kind `if`/`elif` and a boolean bolted
#: onto one branch is the first place the next reader looks past.
KIND_MARKDOWN = "markdown"
#: Workspace §4b. `.xlsx` and `.xls` as a real grid rather than a wall of
#: tab-separated text - the biggest preview upgrade in the order.
KIND_SPREADSHEET = "spreadsheet"
#: Workspace §4c. A chapter list beside the existing HTML renderer, rather
#: than the index's flattened text - an EPUB is a zip of XHTML, and both
#: halves of this already exist.
KIND_EPUB = "epub"
KIND_NONE = "none"          # nothing to render; show the card

#: How much of each kind is worth reading, in bytes.
#:
#: **A ceiling per kind, not one number.** They fail differently: a 40MB text
#: file is a wall of characters the widget lays out one by one, an image is
#: decoded whole into memory whatever the pane shows, and a PDF is paged so its
#: size barely matters. The numbers are what fills a pane several times over,
#: chosen so the cost is bounded rather than so the preview is complete.
CAPS: dict[str, int] = {
    KIND_TEXT: 256 * 1024,
    KIND_HTML: 512 * 1024,
    KIND_IMAGE: 25 * 1024 * 1024,
    KIND_PDF: 0,                     # paged by the viewer; never read here
}
#: Markdown is prose, so it reads the same amount as plain text - a second
#: number here would be two caps to keep in step for no reason.
CAPS[KIND_MARKDOWN] = CAPS[KIND_TEXT]
#: Paged by `_spreadsheet_preview` itself, in rows rather than bytes - see
#: `SHEET_PREVIEW_MAX_ROWS`. Present so `CAPS` still names every kind, exactly
#: as `KIND_PDF`'s `0` does for the same reason.
CAPS[KIND_SPREADSHEET] = 0
#: Capped by chapter count in `_epub_preview` (`EPUB_MAX_CHAPTERS`), not bytes.
CAPS[KIND_EPUB] = 0

#: Rows and columns a preview grid builds, deliberately smaller than the
#: index's own caps (`office.MAX_SHEET_ROWS`/`MAX_SHEET_COLUMNS`, 5,000 / 64).
#: A screenful is what a preview is for; a `QTableView` model holding every
#: row of a serious workbook is exactly the wrong thing to build behind a
#: down-arrow, two hundred milliseconds after the selection moved on.
SHEET_PREVIEW_MAX_ROWS = 200
SHEET_PREVIEW_MAX_COLUMNS = 40

#: Characters of an extracted document worth showing. Lower than the text cap
#: because extraction has already cost a zip open and an XML parse, and a Word
#: document long enough to reach this is one somebody should open properly.
EXTRACTED_CAP = 128 * 1024

#: Types that reach the extractor rather than the "no preview" card. **Not a
#: list of Office extensions**: it is "everything the registry can read", asked
#: at call time, so a file type added through the file-types UI becomes
#: previewable at the same moment it becomes searchable. A preview that lags
#: behind the index is a second list to keep in step, and it would drift.
def _extractable(path: Path) -> bool:
    """Whether the index's own extractor claims this file.

    Imported inside the function: `preview_loader` is on the window's startup
    path, and `app.extract` pulls in a registry that imports optional libraries.
    Paying for that before anybody has selected a result is the wrong trade.
    """
    try:
        from app.extract.base import extractor_for, reads_externally

        if reads_externally(path):
            # A Tier 2 converter shells out to another program. That is a
            # reasonable thing to do while indexing a folder overnight; it is
            # not a reasonable thing to do because somebody pressed the down
            # arrow. The card says the file can be opened instead.
            return False
        return extractor_for(path) is not None
    except Exception:                        # noqa: BLE001
        return False

_TEXT_SUFFIXES = frozenset({
    ".txt", ".rst", ".log", ".csv", ".tsv", ".json",
    ".yaml", ".yml", ".xml", ".ini", ".cfg", ".toml", ".py", ".js", ".ts",
    ".sql", ".ps1", ".bat", ".cmd", ".sh", ".c", ".h", ".cpp", ".cs", ".java",
    ".go", ".rs", ".rb", ".php", ".css", ".env", ".conf", ".properties",
    ".tex", ".bib", ".adoc", ".org", ".srt", ".vtt", ".jsonl", ".ndjson",
})
#: Workspace §4a: rendered through `QTextDocument.setMarkdown` rather than as
#: raw text. Its own set, checked before `_TEXT_SUFFIXES`, rather than a flag -
#: see `KIND_MARKDOWN`.
_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})
_HTML_SUFFIXES = frozenset({".html", ".htm", ".eml", ".msg"})
#: **Kept equal to `OcrExtractor.extensions` in `app/extract/ocr.py`.**
#: That is the indexer's own list of what counts as an image - OCR is what
#: reads one, whatever else it also has an extractor for - and a preview that
#: drifts from it is exactly the `.tiff` class of bug the order names: a type
#: the index reads happily shown as "no preview" because this set forgot it.
#: `test_viewer_suffixes.py` pins the two sets equal.
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
                             ".tif", ".tiff", ".svg", ".heic", ".heif"})
#: Workspace §4b. **Kept equal to `XlsxExtractor.extensions`** in
#: `app/extract/office.py` - `.xlsm` and `.xltx` open in Excel exactly like a
#: `.xlsx` and deserve the same grid, not a text wall because this set forgot
#: them.
_XLSX_SUFFIXES = frozenset({".xlsx", ".xlsm", ".xltx"})
#: **Kept equal to `XlsExtractor.extensions`** in `app/extract/xls.py`. Read
#: through the LibreOffice converter route rather than directly - see
#: `_read_xls_via_converter`.
_XLS_SUFFIXES = frozenset({".xls", ".xlt"})
_SPREADSHEET_SUFFIXES = _XLSX_SUFFIXES | _XLS_SUFFIXES
#: Workspace §5c. **Not in `kind_for`'s table**, and that is deliberate: every
#: other kind there is decided by the extension alone, and a `.dwg` is not -
#: whether it can be shown at all depends on a converter being installed on
#: this machine. `load_preview` asks that question separately, in
#: `_drawing_preview`, and the answer travels in `meta` rather than in `kind`.
#: `.dxf` is absent for the opposite reason: `dwg2SVG` reads DWG, and a `.dxf`
#: already previews as extracted text through the registered `cad` extractor.
_DRAWING_SUFFIXES = frozenset({".dwg"})


@dataclass(frozen=True, slots=True)
class Preview:
    """What the pane should draw, or why it cannot."""

    kind: str
    #: Text or sanitised markup. Empty for images and PDFs, which are drawn
    #: from the path.
    body: str = ""
    path: str = ""
    title: str = ""
    subtitle: str = ""
    #: Set when only part of the file was read, so the pane can say so rather
    #: than implying the document simply ends there.
    truncated: bool = False
    #: Remote content that was removed, for the pane's notice line.
    notice: str = ""
    error: Optional[AppError] = None
    #: The page a PDF should open at, when the hit knows one.
    page: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


def kind_for(path: Path) -> str:
    """How this file should be previewed, from its extension alone.

    Extension rather than content sniffing: sniffing means opening the file,
    and the decision is needed before anything has been read - including for
    files on a drive that is not there.
    """
    suffix = path.suffix.lower()
    if suffix in _HTML_SUFFIXES:
        return KIND_HTML
    if suffix in _MARKDOWN_SUFFIXES:
        return KIND_MARKDOWN
    if suffix in _TEXT_SUFFIXES:
        return KIND_TEXT
    if suffix in _IMAGE_SUFFIXES:
        return KIND_IMAGE
    if suffix in _SPREADSHEET_SUFFIXES:
        return KIND_SPREADSHEET
    if suffix == ".epub":
        return KIND_EPUB
    if suffix == ".pdf":
        return KIND_PDF
    return KIND_NONE


def _describe(path: Path) -> str:
    """Size and modification date, for the card and the subtitle."""
    try:
        stat = path.stat()
    except OSError:
        return ""
    from app.ui.presenter import format_size, format_when

    return f"{format_size(stat.st_size)} · {format_when(stat.st_mtime_ns)}"


def _decode(raw: bytes) -> str:
    """Bytes to text, on the ladder `plaintext.py` uses and for the same reason.

    UTF-8, then cp1252, then latin-1 which cannot fail. A preview that refuses
    to show a legacy file is worse than one showing an occasional wrong dash.
    """
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def _extracted(path: Path, *, title: str, subtitle: str) -> Preview:
    """A Word, Excel, PowerPoint, OpenDocument or drawing file, as its text.

    **The same extractor the index uses, and that is the point.** Rendering a
    `.docx` faithfully means a word processor; showing what the *index* holds
    means reusing thirty lines of `app/extract`. The second is not a compromise
    of the first - it is a different and more useful thing in a search tool,
    because what appears in the pane is exactly what was searched. A result you
    cannot find the match in is the complaint this answers.

    The pane says so in the notice line rather than letting somebody conclude
    their formatting has been lost.

    Segments carry labels - "Slide 3", a sheet name - so a spreadsheet does not
    arrive as one undifferentiated wall of cells. Where an extractor provides
    them they become headings; where it does not, the flat text is used.
    """
    from app.core.errors import AppErrorException
    from app.extract.base import extract

    try:
        documents = list(extract(path))
    except AppErrorException as exc:
        # ERR_NO_TEXT_LAYER for a scanned PDF or a slide deck of pictures,
        # ERR_FILE_CORRUPT, ERR_FILE_LOCKED - all already carry a fix line.
        return Preview(kind=KIND_NONE, path=str(path), title=title,
                       subtitle=subtitle, error=exc.error)
    except Exception as exc:                 # noqa: BLE001
        return Preview(
            kind=KIND_NONE, path=str(path), title=title, subtitle=subtitle,
            error=make_error("ERR_UNEXPECTED", "ui.preview",
                             details=f"{type(exc).__name__}: {exc}"),
        )

    body = "\n\n".join(_labelled(document) for document in documents).strip()
    truncated = len(body) > EXTRACTED_CAP
    return Preview(
        kind=KIND_TEXT,
        body=body[:EXTRACTED_CAP],
        path=str(path),
        title=title,
        subtitle=subtitle,
        truncated=truncated,
        notice="Text extracted from the document - this is what was indexed, "
               "not how the file looks. Open it to see the formatting.",
        # **A flag, not a sentence to parse.** Workspace §2g puts a line in
        # the pop-out for exactly these kinds, and the rule this codebase set
        # for notices applies: nothing reads a message string to decide
        # anything. `.txt` shown as text has no layout to be missing; a
        # `.docx` shown as text does.
        #
        # **§4e's detection travels here, not into the pane.** Whether
        # LibreOffice is on this machine is answered by walking Program
        # Files - real filesystem work, which belongs on the worker this
        # function already runs on, never in a Qt slot on the UI thread.
        meta={"extracted": True,
             "office_converter_available": office_converter_available()},
    )


def _labelled(document: Any) -> str:
    """Segment labels as headings, where the extractor gave any.

    A spreadsheet without them is a wall of cells with no way to tell which
    sheet a number came from, which is precisely the question somebody
    previewing a spreadsheet is asking.
    """
    labels = [segment for segment in document.segments if segment.label]
    if not labels:
        return document.text
    return "\n\n".join(
        f"{segment.label}\n{'-' * len(segment.label)}\n{segment.text}".strip()
        for segment in labels
    )


@dataclass(frozen=True, slots=True)
class SheetGrid:
    """One worksheet, already capped, for `SpreadsheetView` to draw.

    **Plain data, no Qt.** Built here, on the worker, so the widget's model
    construction is arithmetic over a list already in hand rather than a
    second read of the file - the same split `render_page` draws between
    decoding and drawing.
    """

    name: str
    rows: tuple[tuple[str, ...], ...] = ()
    #: Set when the sheet had more rows or columns than the preview cap kept.
    #: Two flags rather than one: a sheet can be wide and short, or the other
    #: way round, and "large sheet" alone would not say which limit was hit.
    truncated_rows: bool = False
    truncated_columns: bool = False

    @property
    def truncated(self) -> bool:
        return self.truncated_rows or self.truncated_columns


def _sheet_grid(name: str, rows: Any, *, max_rows: int = SHEET_PREVIEW_MAX_ROWS,
                max_columns: int = SHEET_PREVIEW_MAX_COLUMNS) -> SheetGrid:
    """`rows` is an iterable of cell tuples - openpyxl's `iter_rows(values_only=True)`
    or the xlrd-reading equivalent. Every cell becomes a string: a preview
    grid is something to look at, not something to calculate with.
    """
    kept: list[tuple[str, ...]] = []
    truncated_rows = False
    truncated_columns = False
    for index, row in enumerate(rows):
        if index >= max_rows:
            truncated_rows = True
            break
        cells = list(row)
        if len(cells) > max_columns:
            truncated_columns = True
            cells = cells[:max_columns]
        kept.append(tuple("" if cell is None else str(cell) for cell in cells))
    return SheetGrid(name=name, rows=tuple(kept), truncated_rows=truncated_rows,
                     truncated_columns=truncated_columns)


def _spreadsheet_notice(sheets: list[SheetGrid]) -> str:
    """§4b's line: "large sheet - showing first N rows." Named, not general -
    a workbook of six sheets where only one is huge should not read as if
    every sheet were cut down."""
    large = [sheet.name for sheet in sheets if sheet.truncated]
    if not large:
        return ""
    names = ", ".join(f"'{name}'" for name in large)
    return (
        f"Large sheet - showing the first {SHEET_PREVIEW_MAX_ROWS} rows and "
        f"{SHEET_PREVIEW_MAX_COLUMNS} columns of {names}. Open the file for "
        f"the rest."
    )


def _read_xlsx_sheets(path: Path) -> list[SheetGrid]:
    """Every sheet of a modern workbook, read with `openpyxl` - already a
    dependency, per §4b. `read_only` and `data_only`: a formula's last
    *result* is what belongs in a grid somebody is looking at, not
    `=VLOOKUP(...)`, and the same choice `office.XlsxExtractor` already made."""
    import warnings

    import openpyxl

    with warnings.catch_warnings():
        # See `office.XlsxExtractor.extract` for why this is suppressed here
        # and nowhere broader: real workbooks trigger it constantly and none
        # of it is actionable from a preview pane.
        warnings.simplefilter("ignore", UserWarning)
        workbook = openpyxl.load_workbook(
            str(path), read_only=True, data_only=True, keep_links=False)
    try:
        sheets = []
        for name in workbook.sheetnames:
            sheet = workbook[name]
            values = sheet.iter_rows(max_row=SHEET_PREVIEW_MAX_ROWS + 1,
                                     values_only=True)
            sheets.append(_sheet_grid(name, values))
        return sheets
    finally:
        workbook.close()


def _read_xls_via_converter(path: Path) -> Optional[list[SheetGrid]]:
    """A legacy workbook, read as a grid by converting it through the
    **existing LibreOffice converter route** and then reading the result the
    same way as a native `.xlsx` - one grid-building code path for both
    kinds, per §4b, rather than a second reader for `xlrd`'s cell types.

    `None` means no converter is on this machine. The caller falls back to
    the flat text table `_extracted` already produced before this feature
    existed - a spreadsheet that cannot become a grid still previews as
    something, not as a hole where a preview used to be.
    """
    from app.core.formats import ConverterRule
    from app.extract.converter import available_binaries, convert

    binaries = available_binaries()
    binary = "soffice" if binaries.get("soffice") else (
        "libreoffice" if binaries.get("libreoffice") else "")
    if not binary:
        return None

    rule = ConverterRule(
        extension=path.suffix.lower(),
        command=(binary, "--headless", "--convert-to", "xlsx",
                "--outdir", "{outdir}", "{input}"),
        produces="{stem}.xlsx",
        then="xlsx",
        enabled=True,
    )
    try:
        with convert(path, rule) as result:
            return _read_xlsx_sheets(result.path)
    except Exception as exc:                      # noqa: BLE001 - see docstring
        _log.debug("could not convert {} for a grid preview: {}", path, exc)
        return None


def _spreadsheet_preview(path: Path, *, title: str, subtitle: str) -> Preview:
    """§4b: `.xlsx`/`.xls` as a real grid. Never raises - a workbook that
    cannot be parsed falls back to the flat text `_extracted` already gives,
    which worked before this feature existed and still does."""
    suffix = path.suffix.lower()
    try:
        if suffix in _XLS_SUFFIXES:
            sheets = _read_xls_via_converter(path)
            if sheets is None:
                return _extracted(path, title=title, subtitle=subtitle)
        else:
            sheets = _read_xlsx_sheets(path)
    except Exception as exc:                      # noqa: BLE001 - never raise
        _log.debug("grid preview failed for {}: {}", path, exc)
        return _extracted(path, title=title, subtitle=subtitle)

    if not sheets:
        return _extracted(path, title=title, subtitle=subtitle)

    return Preview(
        kind=KIND_SPREADSHEET, path=str(path), title=title, subtitle=subtitle,
        notice=_spreadsheet_notice(sheets), meta={"sheets": sheets},
    )


#: Workspace §4c. Zip-bomb and runaway-book guards, matching the reasoning
#: `extract/ebook.py` already gives for the same numbers: generous for a real
#: book, ruinous for an archive built to expand or an index of ten thousand
#: fragments.
EPUB_MAX_MEMBER_BYTES = 16 * 1024 * 1024
EPUB_MAX_CHAPTERS = 400

#: Where an EPUB is required to say which file describes it - the only fixed
#: path in the format, same as `extract/ebook.py`.
_EPUB_CONTAINER_MEMBER = "META-INF/container.xml"


@dataclass(frozen=True, slots=True)
class EpubChapter:
    """One chapter, in reading order, for `EpubView` to draw."""

    title: str
    html: str


def _epub_local_tag(tag: str) -> str:
    """`{namespace}name` -> `name` - EPUB's namespace URI varies by writer."""
    return tag.rpartition("}")[2]


def _epub_member(archive: Any, name: str) -> bytes:
    """One archive member, size-checked from its header before reading."""
    info = archive.getinfo(name)                      # KeyError if absent
    if info.file_size > EPUB_MAX_MEMBER_BYTES:
        raise ValueError(f"{name} expands to {info.file_size} bytes")
    return archive.read(name)


def _epub_opf_path(archive: Any) -> Optional[str]:
    """The package document's path, from `META-INF/container.xml`."""
    from xml.etree import ElementTree

    container = ElementTree.fromstring(_epub_member(archive, _EPUB_CONTAINER_MEMBER))
    for element in container.iter():
        if _epub_local_tag(element.tag) == "rootfile":
            full_path = element.get("full-path")
            if full_path:
                return full_path
    return None


def _epub_chapter_title(markup: str, *, fallback: str) -> str:
    """The chapter's own `<title>`, or its first heading, or `fallback`.

    A regex over the raw markup rather than a second parse: this is read
    once per chapter purely to label a row in a list, and the pane already
    parses the same markup properly (via Qt) to display it.
    """
    import html as html_module
    import re

    for pattern in (r"<title[^>]*>(.*?)</title>", r"<h[1-3][^>]*>(.*?)</h[1-3]>"):
        match = re.search(pattern, markup, re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        text = html_module.unescape(re.sub(r"<[^>]+>", " ", match.group(1)))
        text = " ".join(text.split())
        if text:
            return text
    return fallback


def _epub_chapters(path: Path) -> list[EpubChapter]:
    r"""Chapters in spine order, with their raw XHTML. Never raises - the
    caller degrades to the file card.

    **Deliberately independent of `extract.ebook.EpubExtractor`.** That reads
    plain text, flattened, for the index; this keeps the markup, one chapter
    at a time, for the pane's existing HTML renderer - a different enough
    shape that sharing the method was not worth the coupling. The read
    itself - container.xml, then the OPF, then the spine - mirrors it, since
    it is the one correct way to walk an EPUB.
    """
    import posixpath
    import zipfile
    from xml.etree import ElementTree

    with zipfile.ZipFile(path) as archive:
        opf_name = _epub_opf_path(archive)
        if not opf_name:
            return []
        opf_root = ElementTree.fromstring(_epub_member(archive, opf_name))
        base = posixpath.dirname(opf_name)

        hrefs: dict[str, str] = {}
        for element in opf_root.iter():
            if _epub_local_tag(element.tag) != "item":
                continue
            item_id = element.get("id")
            href = element.get("href")
            media = (element.get("media-type") or "").lower()
            if item_id and href and ("html" in media or not media):
                hrefs[item_id] = href

        order: list[str] = []
        for element in opf_root.iter():
            if _epub_local_tag(element.tag) != "itemref":
                continue
            ref = element.get("idref")
            if ref and ref in hrefs:
                order.append(hrefs[ref])
        if not order:
            # A spine-less book is malformed, but its chapters are still
            # there - manifest order is a poor second and much better than
            # an empty preview.
            order = list(hrefs.values())

        chapters: list[EpubChapter] = []
        for index, href in enumerate(order[:EPUB_MAX_CHAPTERS], start=1):
            member = posixpath.normpath(posixpath.join(base, href)) if base else href
            try:
                raw = _epub_member(archive, member)
            except (KeyError, ValueError):
                # A manifest entry pointing at a missing file, or one too
                # large to be worth it. Skip the chapter, keep the book.
                continue
            markup = raw.decode("utf-8", errors="replace")
            chapters.append(EpubChapter(
                title=_epub_chapter_title(markup, fallback=f"Chapter {index}"),
                html=markup,
            ))
        return chapters


def _epub_preview(path: Path, *, title: str, subtitle: str) -> Preview:
    """§4c: a chapter list beside the existing HTML renderer.

    Every chapter is sanitised through the same cleaner `KIND_HTML` already
    uses - remote references and script have no more business in a novel
    than in an email.
    """
    from app.ui.sanitise import sanitise_email_html

    try:
        raw_chapters = _epub_chapters(path)
    except Exception as exc:                      # noqa: BLE001 - never raise
        return Preview(
            kind=KIND_NONE, path=str(path), title=title, subtitle=subtitle,
            error=make_error(
                "ERR_FILE_CORRUPT", "ui.preview", path=str(path),
                details=f"{type(exc).__name__}: {exc}",
            ),
        )

    if not raw_chapters:
        return Preview(
            kind=KIND_NONE, path=str(path), title=title, subtitle=subtitle,
            error=make_error(
                "ERR_FILE_CORRUPT", "ui.preview", path=str(path),
                details="not a readable EPUB archive, or it names no chapters",
            ),
        )

    chapters = [
        EpubChapter(title=chapter.title, html=sanitise_email_html(chapter.html).html)
        for chapter in raw_chapters
    ]
    return Preview(kind=KIND_EPUB, path=str(path), title=title, subtitle=subtitle,
                   meta={"chapters": chapters})


def load_preview(path_text: str, *, page: int = 0, mail_body: str = "") -> Preview:
    """Everything the pane needs for one result. **Never raises.**

    A preview that throws takes the worker's error path and shows a traceback
    for a file somebody merely arrowed past. Every failure here is a `Preview`
    carrying an `AppError` instead, which the pane renders as a sentence.

    `mail_body` short-circuits the file entirely: a message has no file of its
    own - its path is synthetic - so the text comes from the store.
    """
    if mail_body:
        return Preview(
            kind=KIND_TEXT, body=mail_body, path=path_text,
            title="Message", subtitle="",
        )

    path = Path(path_text)
    kind = kind_for(path)

    try:
        exists = path.is_file()
    except OSError:
        exists = False

    if not exists:
        return Preview(
            kind=KIND_NONE, path=path_text, title=path.name,
            error=make_error(
                "ERR_FILE_MISSING", "ui.preview", path=path_text,
            ),
        )

    title = path.name
    subtitle = _describe(path)

    # §5c, before `_extractable`: a `.dwg` has no registered extractor (see
    # `cad.py`'s docstring - claiming the extension there would disable the
    # only route that can read it), so it would otherwise fall all the way
    # through to the "no preview for this type" card.
    if path.suffix.lower() in _DRAWING_SUFFIXES:
        return _drawing_preview(path, title=title, subtitle=subtitle)

    if kind == KIND_NONE and _extractable(path):
        return _extracted(path, title=title, subtitle=subtitle)

    if kind == KIND_SPREADSHEET:
        return _spreadsheet_preview(path, title=title, subtitle=subtitle)

    if kind == KIND_EPUB:
        return _epub_preview(path, title=title, subtitle=subtitle)

    if kind in (KIND_PDF, KIND_IMAGE, KIND_NONE):
        # Drawn from the path by the widget that knows how - a PDF is paged by
        # the viewer, and an image is decoded by Qt. Reading either here would
        # pull the whole thing into memory to hand it straight back.
        if kind == KIND_IMAGE:
            try:
                if path.stat().st_size > CAPS[KIND_IMAGE]:
                    return Preview(
                        kind=KIND_NONE, path=path_text, title=title,
                        subtitle=subtitle,
                        error=make_error(
                            "ERR_FILE_TOO_LARGE", "ui.preview",
                            path=path_text,
                            details=f"images are previewed up to "
                                    f"{CAPS[KIND_IMAGE] // (1 << 20)}MB; the "
                                    f"file itself is untouched",
                        ),
                    )
            except OSError:
                pass
        return Preview(kind=kind, path=path_text, title=title,
                       subtitle=subtitle, page=max(0, int(page)))

    cap = CAPS[kind]
    try:
        with path.open("rb") as handle:
            raw = handle.read(cap + 1)
    except PermissionError:
        return Preview(
            kind=KIND_NONE, path=path_text, title=title, subtitle=subtitle,
            error=make_error("ERR_FILE_LOCKED", "ui.preview", path=path_text),
        )
    except OSError as exc:
        return Preview(
            kind=KIND_NONE, path=path_text, title=title, subtitle=subtitle,
            error=make_error(
                "ERR_UNEXPECTED", "ui.preview",
                details=f"{type(exc).__name__}: {exc}",
            ),
        )

    truncated = len(raw) > cap
    text = _decode(raw[:cap])

    if kind == KIND_HTML:
        from app.ui.sanitise import sanitise_email_html

        cleaned = sanitise_email_html(text)
        return Preview(
            kind=KIND_HTML, body=cleaned.html, path=path_text, title=title,
            subtitle=subtitle, truncated=truncated, notice=cleaned.notice(),
        )

    return Preview(kind=kind, body=text, path=path_text, title=title,
                   subtitle=subtitle, truncated=truncated)


def stored_text(store: Any, file_id: Any) -> str:
    """What the index holds for one file, reassembled. **Runs on a worker.**

    The `body_provider` Mail installs. Read from `chunks` rather than from the
    file because the "file" is a PST holding a hundred thousand messages and
    there is nothing on disk that is *this* message - the text was extracted
    once, at index time, and this is where it went.

    `chunks_for_file` is indexed on `file_id`, which is the whole reason it is
    affordable on every arrow key.
    """
    try:
        chunks = store.chunks_for_file(int(file_id or 0))
    except Exception as exc:                    # noqa: BLE001
        _log.debug("no stored text for file {}: {}", file_id, exc)
        return ""
    return join_chunks(chunks)


def join_chunks(chunks: Any) -> str:
    r"""One document's chunks back into one body. **No invented paragraphs.**

    This was `"\n\n".join(...)`, which put a blank line at every chunk
    boundary - so a long message read as arbitrarily broken paragraphs, in
    places decided by a 512-token window rather than by whoever wrote it.
    Chunking is an indexing decision and has no business being visible.

    Chunks are contiguous slices of the original, so joining them with nothing
    restores the text as extracted, including its real paragraph breaks. A
    single newline is inserted only where the seam would otherwise run two
    words together, which happens when a chunker trims trailing whitespace.
    """
    out: list[str] = []
    for chunk in chunks or ():
        text = str(getattr(chunk, "text", "") or "")
        if not text:
            continue
        if out and not out[-1].endswith(("\n", " ")) and not text.startswith(("\n", " ")):
            out.append("\n")
        out.append(text)
    return "".join(out)


#: Field order for the header block above a message body.
#:
#: **From, To, Sent, Subject - the order every mail client uses**, so it is read
#: without being studied. They are columns in the table already; the preview had
#: none of them, so a message opened on its own had no context at all.
MAIL_HEADERS: tuple[tuple[str, str], ...] = (
    ("From", "sender"),
    ("To", "recipients"),
    ("Sent", "sent"),
    ("Subject", "subject"),
)


def mail_header(row: Any) -> str:
    """The block above the body. Everything comes off the row already."""
    lines = []
    for label, field in MAIL_HEADERS:
        value = str(getattr(row, field, "") or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    attached = str(getattr(row, "attachment", "") or "").strip()
    if attached:
        lines.append(f"Attached: {attached}")
    return "\n".join(lines)


def quoted_notice(removed: Any) -> str:
    r"""What the preview is not showing, and how much of it.

    **The honesty this pane owed and did not pay.** A mail preview draws the
    *indexed* text, and quoted replies and signatures are stripped at index
    time - correctly, because a thread quoted twenty times would otherwise be
    indexed twenty times. The consequence is that a reply appears with the
    conversation it is replying to gone, and with nothing saying so it reads as
    a message that was sent without context.

    `None` is not zero. A message indexed before schema v12 genuinely does not
    know, and claiming "nothing was removed" would be an invention.
    """
    try:
        count = int(removed)
    except (TypeError, ValueError):
        return ""
    if count <= 0:
        return ""
    return (f"Quoted reply and signature removed — {count:,} characters. "
            f"The index holds only what this message itself added.")


def attachment_notice(name: str) -> str:
    """What the pane says above an attachment shown under its message."""
    return (f"'{name}' is attached to this message. The text below is the "
            f"attachment's own, as the index read it.")


def mail_body(store: Any, row: Any) -> str:
    """Header block, then the message. Runs on a worker - see `stored_text`."""
    body = stored_text(store, getattr(row, "file_id", 0))
    header = mail_header(row)
    if not header:
        return body
    return f"{header}\n\n{'-' * 40}\n\n{body}" if body else header


# ---------------------------------------------------------------------------
# Order 0y section 4: a message as a message - the header card's words, the
# message's own text, and (4c, 4d) its conversation and its original.
# ---------------------------------------------------------------------------

#: What stands where a message's text would be when the index holds none.
#: One sentence, used by both routes to a message preview.
NO_MESSAGE_TEXT = (
    "No text was stored for this message, so there is nothing to "
    "preview.\n\nA message is previewed from the text extracted when "
    "it was indexed - it has no file of its own to re-read. This "
    "usually means it was indexed before message bodies were kept, "
    "or the message is empty.\n\nRe-indexing the archive fills it in."
)

#: Between the plain header block and the message, in what Copy produces -
#: the line `mail_body` has always drawn there.
_COPY_RULE = f"\n\n{'-' * 40}\n\n"


@dataclass(frozen=True, slots=True)
class MailPreview:
    """One message, read for the pane. Rides in `Preview.meta["mail"]`."""

    file_id: int
    #: `presenter.mail.MailCard` - what the header card says.
    card: Any
    #: The message's own words: the stored text without the index's header lines.
    body: str = ""
    #: The plain `From: ...` block and its rule. `copy_header + body` is what
    #: Copy produces, and is `Preview.body`.
    copy_header: str = ""
    #: `messages.quoted_removed`: how much quoted text the index left out, or
    #: `None` when the message does not know - see `quoted_notice`.
    quoted_removed: Optional[int] = None
    #: 4c: `presenter.mail.ConversationLine`s, oldest first - empty for a
    #: message on its own - and the line above them.
    conversation: tuple = ()
    conversation_heading: str = ""
    #: 4d: `presenter.mail.OriginalTarget` - where the full message can be
    #: opened - or `None` when nowhere can. Deciding it opens nothing.
    original: Any = None
    #: Order 0z F2: set when the row previewed is an **attachment** - its name.
    #: The card, conversation and original are then its parent message's, and
    #: `body` is the attachment's own text.
    attachment: str = ""


def _conversation(store: Any, message: Any, file_id: int) -> tuple[tuple, str]:
    """`(lines, heading)` for the list under the card. **Worker. Never raises.**

    One query (`SqliteStore.conversation_messages`), asked for one row more
    than the list shows so the heading can say when there were more.
    """
    from app.ui.presenter.mail import (
        CONVERSATION_SHOWN, conversation_heading, conversation_lines,
    )

    try:
        rows = store.conversation_messages(
            message.get("conversation"), limit=CONVERSATION_SHOWN + 1)
    except Exception as exc:                    # noqa: BLE001 - the list, not the message
        _log.debug("no conversation for file {}: {}", file_id, exc)
        return (), ""
    heading = conversation_heading(len(rows))
    if not heading:
        return (), ""
    return conversation_lines(rows, file_id), heading


def mail_preview(store: Any, row: Any) -> Optional[MailPreview]:
    r"""`row` as a message, or `None` when it is not one. **Worker. Never raises.**

    Asked of the store rather than guessed from the row: a Mail row, a search
    result and a row clicked in a conversation are three shapes, and what makes
    any of them a message is a row in `messages` under its `file_id` - one
    lookup by primary key. So a message previews the same way from every list
    that hands the pane a store.
    """
    try:
        file_id = int(getattr(row, "file_id", 0) or 0)
    except (TypeError, ValueError):
        return None
    if store is None or file_id <= 0:
        return None
    try:
        message = store.get_message(file_id)
    except Exception as exc:                    # noqa: BLE001 - the card, not the preview
        _log.debug("no message row for file {}: {}", file_id, exc)
        return None
    # Order 0z F2: **an attachment is shown under the message it is attached
    # to.** It has no row in `messages` - its parent has - so the card, the
    # conversation and the original are the parent's, and the text under them
    # is the attachment's own, which is where the searched words are.
    path = str(getattr(row, "path", "") or "")
    attachment = ""
    own_text = ""
    if not message:
        parent = _attachment_parent(store, path)
        if parent is None:
            return None
        message, path, attachment = parent
        own_text = stored_text(store, file_id)
        file_id = int(message["file_id"])

    from app.ui.presenter.mail import (
        UNNAMED_ATTACHMENT, mail_card, original_target, split_index_headers,
    )
    from app.ui.presenter.rows import mail_rows

    stored = stored_text(store, file_id)
    card = mail_card(message, stored)
    _headers, body = split_index_headers(stored)
    if attachment:
        body = own_text

    # The plain block is the one `mail_header` has always written, from the
    # same row shape the Mail list uses - with the attachments named where the
    # index knows their names, since the index's own header lines (which used
    # to follow it and carried them) are no longer typed under it.
    listed = mail_rows([{**message, "path": path}])[0]
    named = ", ".join(name for name in card.attachments if name != UNNAMED_ATTACHMENT)
    if named and listed.attachment:
        listed = replace(listed, attachment=named)
    header = mail_header(listed)
    lines, heading = _conversation(store, message, file_id)
    return MailPreview(
        file_id=file_id, card=card, body=body or NO_MESSAGE_TEXT,
        copy_header=f"{header}{_COPY_RULE}" if header else "",
        quoted_removed=message.get("quoted_removed"),
        conversation=lines, conversation_heading=heading,
        original=original_target(message, listed.path),
        attachment=attachment,
    )


def _attachment_parent(store: Any, path: str) -> Optional[tuple[Any, str, str]]:
    """`(the parent's messages row, its path, the attachment's name)` for a
    file that is an attachment of an indexed message, else `None`. **Worker.**

    The link is the path - `<message>/attachments/<name>`, the convention the
    Search list already reads (`presenter.mail.attachment_of`) - so this is two
    lookups by key, and only for a row that is not itself a message.
    """
    from app.ui.presenter.mail import attachment_of

    parent_path, name = attachment_of(path)
    if not parent_path:
        return None
    try:
        record = store.get_file(parent_path)
        message = store.get_message(record.id) if record is not None else None
    except Exception as exc:                    # noqa: BLE001 - the card, not the preview
        _log.debug("no parent message for {}: {}", path, exc)
        return None
    if not message:
        return None
    return message, parent_path, name


def offline_volume_subtitle(store: Any, row: Any) -> str:
    r"""Offline Media §3a's own sentence for one row's volume, reused rather
    than recomputed - see `presenter.offline_volume_note`. `""` when the
    volume record cannot be read (a deleted source, a store error): a preview
    that cannot name the drive still owes the person a preview.
    """
    volume_id = getattr(row, "volume_id", None)
    if volume_id is None:
        return ""
    try:
        record = store.get_volume(int(volume_id))
    except Exception as exc:                      # noqa: BLE001 - a subtitle
        _log.debug("could not read volume {} for a preview subtitle: {}",
                   volume_id, exc)
        return ""
    if record is None:
        return ""
    from app.ui.presenter import offline_volume_note

    scanned_at = int(getattr(record, "last_scanned_at", 0) or 0)
    mark = {
        "name": record.name,
        "scanned": _when(scanned_at) if scanned_at else "",
    }
    return offline_volume_note(mark)


def _when(seconds: int) -> str:
    """`format_when` wants nanoseconds; every timestamp this module reads off
    a `VolumeRecord` is Unix seconds - one place to do the conversion rather
    than three call sites each getting the multiplier right or wrong."""
    from app.ui.presenter import format_when

    return format_when(seconds * 1_000_000_000)


def volume_preview(row: Any, store: Any) -> Preview:
    r"""A row on a catalogued Offline Media volume, online or not. **Worker
    thread. Never raises.**

    **Online**: resolved through the volume's current mount point - the same
    `app.index.offline_media.resolve_file_path` that Open/Reveal already use
    (1b) - and previewed exactly like an ordinary file. Nothing about the
    rest of this module changes: a photo still decodes, a PDF still pages, a
    spreadsheet still grids.

    **Offline**: the drive is not here to read, so this shows what the index
    already holds - §3b's own wording, "the mail synthetic-path pattern:
    stored text + segments". A row with no stored text (a photograph with no
    OCR text, most of them) says plainly that the drive is not connected,
    which is true, rather than reporting the file "missing", which is not -
    it is sitting in a drawer, not gone.

    **Not built here**: §3b's "images show cached thumbnail when 0510's
    thumbnails exist" clause. Checked against the code rather than assumed -
    `app.ui.thumbnail_loader.decode_thumbnail` decodes straight from the
    original file path on every call; nothing in this tree persists a
    thumbnail anywhere a offline row's bytes could still be read from. There
    is no cache to reach for, so an offline photo gets the same honest
    subtitle as anything else with no stored text, not an invented cache.
    """
    from app.index.offline_media import resolve_file_path

    title = str(getattr(row, "name", "") or "").strip()
    if not title:
        relative = str(getattr(row, "relative_path", "") or getattr(row, "path", ""))
        title = Path(relative).name or relative

    try:
        resolved = resolve_file_path(store, row)
    except Exception as exc:                      # noqa: BLE001 - never raise
        _log.debug("could not resolve a volume-backed preview path: {}", exc)
        resolved = None

    if resolved is not None:
        return load_preview(str(resolved), page=int(getattr(row, "page", 0) or 0))

    subtitle = offline_volume_subtitle(store, row) or \
        "This drive is not plugged in right now."
    body = stored_text(store, getattr(row, "file_id", 0))
    if body:
        return Preview(
            kind=KIND_TEXT, body=body, title=title, subtitle=subtitle,
            notice="Shown from the index - the drive itself is not "
                  "connected right now. This is the text Leasha already "
                  "read from it.",
        )
    return Preview(
        kind=KIND_NONE, title=title, subtitle=subtitle,
        error=make_error(
            "ERR_FILE_CORRUPT", "ui.preview",
            suggestion="Plug the drive in to see this file.",
            details="Volume not currently connected, and nothing was "
                    "stored from it to preview from the index.",
        ),
    )


def load_preview_for(row: Any, *, body_provider: Any = None,
                     notice_provider: Any = None, store: Any = None) -> Preview:
    """`load_preview` for a result row, whatever kind of row it is.

    **Which fields of a row become which arguments is a decision, so it is
    made here rather than in the widget.** The pane used to reach into the row
    with three `getattr` calls, which meant the rule was untestable and silently
    wrong for anything that did not look like a search result - a Mail row has
    no `preview_text`, so every message previewed as ERR_FILE_MISSING against a
    synthetic path nobody could have opened.

    `body_provider` is how a view supplies text the row does not carry: Mail
    reads the message from the store. **It is called here, on the worker**, for
    the same reason nothing else in this module is called anywhere else - a
    store read on the interface thread between two presses of the down arrow is
    the freeze this application has a standing rule against.

    **Offline Media §3b, before anything else.** `row.path` for a row on a
    catalogued volume (`row.volume_id is not None`) is never a real filesystem
    path - it is the letter-free key `volume_synthetic_path` builds, the same
    fact `presenter.resolve_open_path` already handles for Open/Reveal. A
    volume-backed row is resolved and handed to `volume_preview` before any of
    the ordinary, path-based logic below ever sees it: online, that means the
    file's *current* mount point; offline, it means what the index already
    holds - the mail synthetic-path pattern this item names. `store` is
    optional so every existing caller (and every test that built a row by
    hand) keeps working exactly as before; without one a volume-backed row
    falls through unresolved, which is the pre-existing behaviour rather than
    a new failure mode.
    """
    if store is not None and getattr(row, "volume_id", None) is not None:
        return volume_preview(row, store)

    # **Order 0y section 4: a message is previewed as a message**, from whichever
    # list it was selected in. `body` is what Copy produces (the plain block and
    # the message); the pane draws the card from `meta["mail"]` and types only
    # the message's own words under it. The quoted-text notice stays.
    mail = mail_preview(store, row)
    if mail is not None:
        return Preview(
            kind=KIND_TEXT, body=mail.copy_header + mail.body,
            path=str(getattr(row, "path", "") or ""), title=mail.card.subject,
            notice=(attachment_notice(mail.attachment) if mail.attachment
                    else quoted_notice(mail.quoted_removed)),
            meta={"mail": mail},
        )

    body = str(getattr(row, "preview_text", "") or "")
    if not body and body_provider is not None:
        try:
            body = str(body_provider(row) or "")
        except Exception as exc:            # noqa: BLE001 - see the docstring
            # A body that cannot be fetched is a preview without one, never a
            # traceback for a row somebody arrowed past.
            _log.debug("no body for the selected row: {}", exc)
            body = ""

    # **A message with no stored text says so.** Reported as "mails are not
    # previewing", and the pane could not tell anybody why: a message has no
    # file of its own, so with no body it fell through to `load_preview` with a
    # synthetic path, which reported the *path* as missing - true, useless, and
    # not the reason. The reason is that this message has no rows in `chunks`:
    # indexed before bodies were stored, or skipped, or an empty message.
    #
    # Checked on `file_id` rather than on the path, because that is what makes
    # a row a message rather than a file.
    if not body and getattr(row, "file_id", None) and body_provider is not None:
        return Preview(
            kind=KIND_NONE,
            path=str(getattr(row, "path", "") or ""),
            title=str(getattr(row, "name", "") or "This message"),
            body=NO_MESSAGE_TEXT,
        )

    # **`full_path` first.** A Code row's `path` is shortened for its column and
    # cannot be opened - `full_path` is the real one. Reading `path` blindly
    # previewed every repository file as ERR_FILE_MISSING, which is a plausible
    # enough message that nobody would have questioned it.
    preview = load_preview(
        str(getattr(row, "full_path", "") or getattr(row, "path", "")),
        page=int(getattr(row, "page", 0) or 0),
        mail_body=body,
    )
    # A message's title is its subject. `load_preview` cannot know that - it is
    # given text and a synthetic path - and "Message" above every message is a
    # heading that says nothing the pane has not already said.
    name = str(getattr(row, "name", "") or "")
    if body and name:
        preview = replace(preview, title=name)

    # **What the body is not showing.** The pane already has a notice line for
    # "remote content was removed"; mail owes the same honesty about a quoted
    # thread stripped at index time. Appended rather than replacing, because a
    # message could legitimately have both.
    if notice_provider is not None:
        try:
            note = str(notice_provider(row) or "")
        except Exception as exc:            # noqa: BLE001 - a notice, not a body
            _log.debug("no notice for the selected row: {}", exc)
            note = ""
        if note:
            joined = f"{preview.notice}  {note}" if preview.notice else note
            preview = replace(preview, notice=joined)
    return preview


#: Workspace §4d. Qt has no built-in decoder for these - `app/extract/ocr.py`
#: already declares `pillow_heif` as the same optional, soft dependency for
#: the same two extensions, and this extends that pipeline rather than
#: building a second one.
_HEIF_SUFFIXES = frozenset({".heic", ".heif"})


#: 3b. `Qt` reads the pixels but not the orientation tag - `QImageReader`
#: only auto-rotates when told to, and `QImage(path)` never asks. A camera
#: held upright writes Orientation 6 or 8 into a landscape sensor frame, and
#: without this a portrait photo previews on its side. `read_orientation`
#: already existed in `app/extract/exif.py`; nothing called it anywhere in
#: the tree - this is that wiring, in the one place every displayed image
#: (this function's two branches) passes through.
#:
#: The standard EXIF 1-8 orientation table, expressed as the correction each
#: code needs (not the rotation the camera applied - the inverse of it).
#: 1 and any unrecognised code are absent on purpose: "do nothing" is the
#: fallback in `_apply_orientation` itself, not an entry here.
def _apply_orientation(image: Any, orientation: int) -> Any:
    """Rotate/mirror a decoded `QImage` to the upright EXIF recorded.

    `orientation` is the raw EXIF tag (1-8) from `read_orientation`. 1 (or
    anything unrecognised) is returned unchanged - the common case, and the
    safe default for a tag that could not be read.
    """
    if orientation not in range(2, 9):
        return image

    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QTransform

    transform = QTransform()
    if orientation == 2:                 # mirrored horizontally
        transform.scale(-1, 1)
    elif orientation == 3:               # rotated 180
        transform.rotate(180)
    elif orientation == 4:               # mirrored vertically
        transform.scale(1, -1)
    elif orientation == 5:               # mirrored + rotated 90 CW
        transform.rotate(90)
        transform.scale(-1, 1)
    elif orientation == 6:               # rotated 90 CW
        transform.rotate(90)
    elif orientation == 7:               # mirrored + rotated 90 CCW
        transform.rotate(-90)
        transform.scale(-1, 1)
    elif orientation == 8:               # rotated 90 CCW
        transform.rotate(-90)
    return image.transformed(transform, Qt.TransformationMode.SmoothTransformation)


def decode_image(path: str):
    """Read and decode an image file to a `QImage`. **Worker thread only.**

    `QPixmap` cannot be built off the UI thread - Qt refuses, and on some
    platforms crashes - but `QImage` can, and converting one to the other on
    the UI thread afterwards is a wrap rather than a second decode. That split
    is the whole reason this function exists separately from `_show_image`.

    Returns None rather than raising: an unreadable image is an ordinary state -
    a truncated download, a `.png` that is really HTML - and the pane says so in
    the card. Nothing here is worth an error dialog.
    """
    if Path(str(path)).suffix.lower() in _HEIF_SUFFIXES:
        # Qt has no HEIC/HEIF image plugin at all - unlike SVG, where the
        # plugin this build ships already covers it - so these go through
        # PIL instead of the usual `QImage(path)` one-liner.
        return _decode_heif(path)

    from PyQt6.QtGui import QImage

    try:
        image = QImage(str(path))
    except Exception:                            # noqa: BLE001 - boundary
        return None
    if image.isNull():
        return None

    from app.extract.exif import read_orientation
    orientation = read_orientation(Path(str(path)))
    return _apply_orientation(image, orientation)


def _decode_heif(path: str):
    """A HEIC/HEIF photo, via `pillow-heif` and PIL, to a `QImage`.

    **The same optional dependency `extract/ocr.py` already declares**, with
    the same soft-degrade posture: if it or PIL is not installed, this
    returns None exactly as any other undecodable image does, and the pane's
    existing "this image could not be read" card is the honest answer -
    `format_health` already tells the Settings/doctor story for the index
    side of this same gap.

    `register_heif_opener` is called every time rather than once at import:
    it is idempotent and cheap, and this module's own docstring is explicit
    that nothing here runs until a worker calls it - there is no startup
    path to hang it from instead.
    """
    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except Exception:                             # noqa: BLE001 - not installed
        return None

    try:
        from PIL import Image
        from PyQt6.QtGui import QImage

        with Image.open(str(path)) as opened:
            frame = opened.convert("RGBA")
            data = frame.tobytes("raw", "RGBA")
            image = QImage(data, frame.width, frame.height,
                           QImage.Format.Format_RGBA8888)
            # `.copy()`: `data` is a local `bytes` object PIL is done with the
            # moment this function returns, and `QImage` over a raw buffer
            # does not take ownership of it - the same use-after-free
            # `render_page._pdf_page` already guards against for the same
            # reason.
            image = image.copy()
    except Exception as exc:                       # noqa: BLE001 - see docstring
        _log.debug("could not decode HEIC/HEIF {}: {}", path, exc)
        return None

    # 3b: HEIC/HEIF phones carry the same Orientation tag a JPEG does, and
    # PIL's plain `Image.open` does not auto-rotate any more than Qt does -
    # this needs the same correction the generic branch above applies.
    from app.extract.exif import read_orientation
    orientation = read_orientation(Path(str(path)))
    return _apply_orientation(image, orientation)


# ---------------------------------------------------------------------------
# Workspace §4e: "Show full layout", on demand
# ---------------------------------------------------------------------------
#
# A text-rendered Office/ODF preview (`_extracted`, `meta["extracted"]`) can
# ask, once, to see the real thing - converted to PDF through the existing
# LibreOffice converter route and cached beside the index, keyed by a hash of
# the file's own bytes so an edited-then-reverted document converts again and
# an unchanged one never pays twice. Never at index time: this only runs when
# a person presses the button.

#: Where the cache lives, under the app's own cache directory - never beside
#: the user's file, and never the document itself. `office_pdf_cache_path`
#: joins this onto `settings.cache_path`.
OFFICE_PDF_CACHE_DIRNAME = "office_preview"

#: §4e's sentence for when no converter is on this machine - the button is
#: hidden and this replaces it, in the same shape `format_health`'s own
#: converter rows already use for the identical fact.
OFFICE_CONVERTER_MISSING_NOTE = (
    "Install LibreOffice to see the full layout of this document: "
    "winget install --id TheDocumentFoundation.LibreOffice -e"
)


def office_converter_available(binaries: Optional[dict] = None) -> bool:
    """Is LibreOffice on this machine, under either of its two binary names?

    **One question, one answer**, shared with the index's own detection: the
    button asks exactly what `format_health._converter_status` already asks
    for `.doc`/`.ppt`/etc, so a machine that has LibreOffice installed gets
    the same answer in Settings and in this window - two separate probes
    would risk two different answers to "is it installed".
    """
    if binaries is None:
        return _office_converter_default()
    return bool((binaries or {}).get("soffice") or (binaries or {}).get("libreoffice"))


@lru_cache(maxsize=1)
def _office_converter_default() -> bool:
    """The real probe, cached. It walks `PATH` and Program Files - real
    filesystem work, worth doing once per process rather than once per
    preview render, since the answer cannot change while this process is
    running an older install. `format_health.module_present` caches its own
    probe for the identical reason.
    """
    try:
        from app.extract.converter import available_binaries
        binaries = available_binaries()
    except Exception:                              # noqa: BLE001 - absence is an answer
        binaries = {}
    return bool(binaries.get("soffice") or binaries.get("libreoffice"))


def office_pdf_cache_path(path: Path, *, cache_root: Optional[Path] = None) -> Path:
    """Where a converted PDF for `path` would live, keyed by content hash.

    **A hash of the bytes, not the path or the mtime.** Two different files
    that happen to render the same PDF share a cache entry for free, and a
    file edited and then reverted converts again rather than trusting a
    modification time that copying a file around can make meaningless.
    `content_hash` is `app.index.walker`'s own - the one the indexer already
    uses to answer "are these the same bytes?" - reused rather than a second
    hashing scheme invented for one button.
    """
    from app.index.walker import content_hash

    if cache_root is None:
        from app.core.config import load_settings

        cache_root = load_settings().cache_path / OFFICE_PDF_CACHE_DIRNAME
    digest = content_hash(Path(path))
    return Path(cache_root) / f"{digest}.pdf"


def ensure_office_pdf(path_text: str) -> Preview:
    """§4e: convert to PDF through the existing LibreOffice converter route,
    caching the result beside the index. **Worker thread. Never raises.**

    A cache hit skips the conversion entirely - "paid once per document a
    user actually opens" is the item's own wording, and the check is a
    single `is_file()` before anything else happens.
    """
    path = Path(path_text)
    title = path.name

    try:
        cache_file = office_pdf_cache_path(path)
    except Exception as exc:                      # noqa: BLE001 - never raise
        return Preview(
            kind=KIND_NONE, path=path_text, title=title,
            error=make_error("ERR_UNEXPECTED", "ui.preview",
                             details=f"{type(exc).__name__}: {exc}"),
        )

    if not cache_file.is_file():
        from app.core.errors import AppErrorException
        from app.core.formats import ConverterRule
        from app.extract.converter import available_binaries, convert

        binaries = available_binaries()
        binary = ("soffice" if binaries.get("soffice")
                 else "libreoffice" if binaries.get("libreoffice") else "")
        if not binary:
            return Preview(
                kind=KIND_NONE, path=path_text, title=title,
                error=make_error("ERR_CONVERTER_MISSING", "ui.preview",
                                 binary="soffice", ext=path.suffix,
                                 path=path_text),
            )

        # A rule built here rather than read from `extractors.toml` - this is
        # a preview action a person asked for, not an indexing route, and
        # `.doc`/`.xlsx`/etc already have *different* converter rules there
        # (to plain text or CSV, for the index). One extra rule, to PDF, for
        # this one button - still `soffice`/`libreoffice`, still on the same
        # allow-list, still run through the same `convert()`.
        rule = ConverterRule(
            extension=path.suffix.lower(),
            command=(binary, "--headless", "--convert-to", "pdf",
                    "--outdir", "{outdir}", "{input}"),
            produces="{stem}.pdf",
            then="pdf",
            enabled=True,
        )
        try:
            with convert(path, rule) as result:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_bytes(Path(result.path).read_bytes())
        except AppErrorException as exc:
            return Preview(kind=KIND_NONE, path=path_text, title=title,
                           error=exc.error)
        except OSError as exc:
            return Preview(
                kind=KIND_NONE, path=path_text, title=title,
                error=make_error("ERR_UNEXPECTED", "ui.preview",
                                 details=f"{type(exc).__name__}: {exc}"),
            )

    return Preview(kind=KIND_PDF, path=str(cache_file), title=title,
                  subtitle=_describe(cache_file))


# ---------------------------------------------------------------------------
# Workspace §5c: a DWG drawing as a simplified view, on demand
# ---------------------------------------------------------------------------
#
# The same shape as §4e directly above, for the same reasons, against a
# different program: `dwg2SVG` turns the drawing into SVG, the SVG is cached
# under the app's own cache directory keyed by a hash of the drawing's bytes,
# and the pop-out is told to show an image at that path - which is the route
# §4a left behind for `.svg` (Qt's own `qsvg` image-format plugin decodes one
# through `QImage` exactly like a raster file), so nothing new draws it.
#
# **Line-work and text, not a plot.** `dwg2SVG` renders LINE, CIRCLE, ARC,
# TEXT, POINT, ELLIPSE, SOLID, 3DFACE, POLYLINE_2D, LWPOLYLINE, INSERT, RAY
# and XLINE, and nothing else - no hatching, no dimensions, no 3D. That is
# what the item asks for and says why: this answers "is this the right
# drawing?", and for that it is enough. The notice on the preview says so in
# the person's own words rather than letting a thin-looking drawing read as a
# damaged one.
#
# **Subprocess only, never LibreDWG's Python bindings.** Running a GPL program
# is mere aggregation; importing its bindings would put this MIT application
# under the GPL. Nothing is bundled - the user installs LibreDWG - so Leasha
# carries no licence obligation at all.
# `test_cad.test_no_module_imports_libredwgs_python_bindings` enforces it over
# every module under `app/`.

#: LibreDWG's SVG program. Its own name on the allow-list, beside `dwg2dxf`.
DWG_BINARY = "dwg2SVG"

#: Where the cache lives, under the app's own cache directory - never beside
#: the drawing, which is never opened for writing at all.
DWG_SVG_CACHE_DIRNAME = "dwg_preview"

#: §5c's label, in the notice line the pane and the pop-out already have.
DWG_PREVIEW_NOTE = (
    "Simplified view - line-work and text only, drawn from the file to show "
    "which drawing this is. Open it for the real thing."
)

#: What to say when the drawing cannot be shown because nothing here can draw
#: it. **Names LibreDWG alone, and that is not an oversight**: §5b's message
#: names the ODA File Converter too because either one produces the DXF the
#: *index* reads, but only LibreDWG produces an SVG, so offering the other
#: here would send somebody to install software that would not help.
DWG_CONVERTER_MISSING_NOTE = (
    "Install LibreDWG to see a simplified view of this drawing: "
    "https://www.gnu.org/software/libredwg/"
)

#: Where the picture is, for the in-app pane - which has no button of its own,
#: for the reason §4e's note gives for its own: rotate, zoom and print live in
#: the pop-out and nowhere else, so that is where a rendered drawing belongs.
#: **It quotes the pane's existing button by its exact label**, "Pin in a
#: window", rather than describing it - a sentence naming a control somebody
#: cannot then find is worse than no sentence.
DWG_PIN_TO_SEE_NOTE = (
    "Pin in a window to see a simplified view of this drawing."
)

#: How large a produced SVG is worth drawing. An SVG is parsed and rasterised
#: whole, so a dense site plan can cost far more than the image cap suggests
#: for a file of that size - and past a point this stops being the glance the
#: item asks for. Smaller than `CAPS[KIND_IMAGE]` deliberately.
DWG_SVG_MAX_BYTES = 8 * 1024 * 1024


def dwg_preview_available() -> bool:
    """Is `dwg2SVG` on this machine? **Worker thread** - it walks Program Files.

    Cached for the process, exactly as `_office_converter_default` is and for
    the identical reason: the answer cannot change while this process is
    running, and the walk is real filesystem work that would otherwise be
    repeated on every arrow key that landed on a drawing.
    """
    return _dwg_converter_default()


@lru_cache(maxsize=1)
def _dwg_converter_default() -> bool:
    try:
        from app.extract.converter import resolve_binary

        return bool(resolve_binary(DWG_BINARY))
    except Exception:                              # noqa: BLE001 - absence is an answer
        return False


def dwg_svg_cache_path(path: Path, *, cache_root: Optional[Path] = None) -> Path:
    """Where the converted SVG for `path` would live, keyed by content hash.

    `content_hash` is `app.index.walker`'s own, reused rather than reinvented -
    the same argument `office_pdf_cache_path` makes just above.
    """
    from app.index.walker import content_hash

    if cache_root is None:
        from app.core.config import load_settings

        cache_root = load_settings().cache_path / DWG_SVG_CACHE_DIRNAME
    digest = content_hash(Path(path))
    return Path(cache_root) / f"{digest}.svg"


def _drawing_body(path: Path) -> str:
    """The drawing's own header line: which AutoCAD release wrote it.

    **`cad.dwg_release`, the function the indexer already uses** - §5a's
    fallback for a drawing that could not be converted, reused here so the
    preview and the index say the same thing about the same file rather than
    two things. Never raises: it reads six bytes and answers None for
    anything it does not recognise.
    """
    try:
        from app.extract.cad import dwg_release

        release = dwg_release(path)
    except Exception as exc:                       # noqa: BLE001 - a header read
        _log.debug("could not read the DWG header of {}: {}", path, exc)
        release = None
    return f"AutoCAD drawing, {release}" if release else "AutoCAD drawing."


def _drawing_preview(path: Path, *, title: str, subtitle: str,
                     notice: str = "") -> Preview:
    """A `.dwg` before anybody has asked to see it drawn. §5c's fallback.

    Text, not a card: the release line is a real answer to "what is this
    file", and it is the same answer the index holds. The simplified view is
    a button away, and `meta` carries whether pressing it could work -
    asked here, on the worker, exactly as §4e asks its own question, never in
    a Qt slot.
    """
    available = dwg_preview_available()
    return Preview(
        kind=KIND_TEXT,
        body=_drawing_body(path),
        path=str(path),
        title=title,
        subtitle=subtitle,
        notice=notice or (DWG_PIN_TO_SEE_NOTE if available
                          else DWG_CONVERTER_MISSING_NOTE),
        meta={"drawing": True, "dwg_preview_available": available},
    )


def ensure_dwg_svg(path_text: str) -> Preview:
    """§5c: the drawing as a cached SVG. **Worker thread. Never raises.**

    A cache hit skips the conversion entirely. Anything that goes wrong -
    LibreDWG absent, a drawing it refuses, an SVG too large to be worth
    drawing - comes back as the drawing's own release line with a sentence
    saying what happened and what to do, never as a traceback and never as a
    silent nothing.
    """
    path = Path(path_text)
    title = path.name
    subtitle = _describe(path)

    try:
        cache_file = dwg_svg_cache_path(path)
    except Exception as exc:                       # noqa: BLE001 - never raise
        return _drawing_preview(
            path, title=title, subtitle=subtitle,
            notice=make_error("ERR_UNEXPECTED", "ui.preview",
                              details=f"{type(exc).__name__}: {exc}").render(),
        )

    if not cache_file.is_file():
        failed = _convert_dwg_to_svg(path, cache_file)
        if failed:
            return _drawing_preview(path, title=title, subtitle=subtitle,
                                    notice=failed)

    try:
        oversized = cache_file.stat().st_size > DWG_SVG_MAX_BYTES
    except OSError:
        oversized = False
    if oversized:
        return _drawing_preview(
            path, title=title, subtitle=subtitle,
            notice=(
                f"This drawing is too detailed to show as a simplified view - "
                f"it comes out larger than "
                f"{DWG_SVG_MAX_BYTES // (1 << 20)}MB. Open it to look at it."
            ),
        )

    return Preview(
        kind=KIND_IMAGE, path=str(cache_file), title=title, subtitle=subtitle,
        notice=DWG_PREVIEW_NOTE, meta={"drawing": True, "simplified": True},
    )


def _convert_dwg_to_svg(path: Path, cache_file: Path) -> str:
    """Run `dwg2SVG` once and keep what it printed. `""` means it worked.

    **`dwg2SVG` writes the SVG to standard output** - `dwg2SVG DRAWING.dwg
    >DRAWING.svg`, no `-o` option exists - which is why `convert()` is called
    with `stdout_to`. That parameter lives in `converter.py` rather than here
    so this stays one call into the one audited place that starts a process:
    the allow-list, `shell=False`, the timeout ceiling and the temporary
    directory removed in a `finally` all apply unchanged.
    """
    from app.core.errors import AppErrorException
    from app.core.formats import ConverterRule
    from app.extract.converter import convert, resolve_binary

    if not resolve_binary(DWG_BINARY):
        return make_error(
            "ERR_CONVERTER_MISSING", "ui.preview",
            binary=DWG_BINARY, ext=path.suffix, path=str(path),
            suggestion=DWG_CONVERTER_MISSING_NOTE,
            # The registry's own payload is LibreOffice's winget command,
            # which is the right answer for `.doc` and the wrong one here.
            action_payload="",
        ).render()

    rule = ConverterRule(
        extension=path.suffix.lower(),
        command=(DWG_BINARY, "{input}"),
        produces="{stem}.svg",
        # Nothing extracts text from this: it is drawn, not read, so
        # `convert()` is called directly and `extract_via_converter` - the
        # function `then` exists for - never sees it.
        then="",
        enabled=True,
    )
    try:
        with convert(path, rule, stdout_to="{stem}.svg") as result:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(Path(result.path).read_bytes())
    except AppErrorException as exc:
        return exc.error.render()
    except OSError as exc:
        return make_error("ERR_UNEXPECTED", "ui.preview",
                          details=f"{type(exc).__name__}: {exc}").render()
    return ""
