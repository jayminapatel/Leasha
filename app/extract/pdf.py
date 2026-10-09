"""PDF extraction via PyMuPDF, page by page.

Layer: L2

Page numbers are kept because a search result that says "page 47 of the 300-page
manual" is useful and one that says "somewhere in the manual" is not. Each page
becomes a `Segment` carrying its 1-based number, and the chunker stamps every
chunk with the page its text starts on.

**Scanned PDFs are the trap.** A photographed or scanned document is a sequence
of images: PyMuPDF extracts an empty string from every page, and without a check
that indexes perfectly cleanly as a file containing nothing. It would then be
permanently unfindable while appearing successfully indexed. So a PDF with no
text on any page is skipped as `ERR_NO_TEXT_LAYER` and *counted*, which turns an
invisible failure into a number on the skipped-files panel.

**And it can now be read, if the time is worth spending.** That skip message
said "there is nothing to index without OCR - which V2 does not do", while the
OCR engine sat loaded in the same process reading `.png` files. Both halves were
true: OCR was wired to image *extensions*, and nothing here ever called it. Set
`LEASHA_PDF_OCR_PAGES` to a page budget and an image-only PDF is rendered and
read - see `_ocr_pages`, and the arithmetic beside `PDF_OCR_PAGES_VAR` for why
it is a budget rather than a switch.

A partly-scanned PDF - a born-digital report with scanned appendices - indexes
the pages that have text and warns about the ones that do not.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Iterable

from app.core.errors import make_error, raise_error
from app.core.format_health import Requirement
from app.core.logging import logger
from app.extract.base import (
    Document, DocumentBuilder, in_reader_process, normalise_whitespace, register,
)

__all__ = ["PdfExtractor", "PDF_OCR_PAGES_VAR", "OCR_RENDER_DPI"]

log = logger.bind(component="extract.pdf")

#: A page with fewer than this many characters is treated as having no text
#: layer. Scanned pages often yield a stray ligature or a page number from a
#: header stamp, which is not a text layer in any useful sense.
MIN_PAGE_CHARS = 8

#: How many pages of one image-only PDF are worth OCR-ing. 0 turns it off.
#:
#: **Off by default because of arithmetic, not caution.** OCR is measured here
#: at 3.6 seconds a page. The manuals in a real corpus run to hundreds of pages:
#: `PI Server Reference Guide.pdf` alone would be half an hour, and there were
#: dozens beside it. Turning this on globally converts an index run into an OCR
#: run, which is the whole argument for the separate `--only-ocr` pass.
#:
#: A cap rather than a switch, because the first pages of a scanned manual are
#: the title, the contents and the introduction - which is most of what makes it
#: findable. Twenty pages at 3.6 seconds is roughly a minute a document, and a
#: document that is findable by its first twenty pages is enormously better than
#: one that is not findable at all.
PDF_OCR_PAGES_VAR = "LEASHA_PDF_OCR_PAGES"

#: Rendering resolution. 200dpi is the usual floor for reliable OCR on a
#: 300dpi scan; higher costs time quadratically for very little accuracy.
OCR_RENDER_DPI = 200

#: How a rendered page is handed to OCR: uncompressed PNM, not PNG.
#:
#: **2026-09-29, measured** on a 200 dpi A4 scan (1653x2339): encoding the page
#: as PNG took about 200 ms, and it was then decoded twice more (the ladder's
#: thumbnail, and the engine) at about 35 ms each - roughly 270 ms a page spent
#: compressing a picture only to uncompress it again. PNM is about 10 ms to
#: write and 7-10 ms to read. The bytes are larger (11.6 MB against 1 MB for
#: that page) but live only for the one page being read. Pillow, which both the
#: ladder and RapidOCR use to open bytes, reads PNM natively.
RENDER_FORMAT = "pnm"


class PdfExtractor:
    """PDF files through PyMuPDF, page by page, with OCR for scanned pages when
    a page budget allows it."""

    name = "pdf"
    extensions = frozenset({".pdf"})
    #: Hard, and pinned in requirements.txt rather than optional - declared here
    #: so a broken install reports "PDFs cannot be read, install pymupdf"
    #: rather than failing once per PDF in a 100GB corpus.
    requires = (
        Requirement("fitz", "pymupdf", provides="all PDF text", hard=True),
    )

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        """One document, a segment per page with its 1-based number.

        Cannot open, or password-protected: `ERR_FILE_CORRUPT`. No text on any
        page: OCR of the first N pages when `PDF_OCR_PAGES` allows, else
        `ERR_NO_TEXT_LAYER` (a skip the pictures pass reads back). Pages without
        text in a mixed document become a warning. Reads only; closes the file.
        """
        import pymupdf  # imported lazily: ~40MB of C library

        # MuPDF writes its own diagnostics straight to stderr from C. Across a
        # 100GB run that is thousands of lines nobody asked for, interleaved with
        # the progress output and absent from the log file. Our AppError already
        # reports the same failures, with a fix attached.
        with contextlib.suppress(Exception):               # a nicety, never fatal
            pymupdf.TOOLS.mupdf_display_errors(False)

        try:
            document = pymupdf.open(path)
        except Exception as exc:                          # noqa: BLE001 - any failure to open
            raise_error("ERR_FILE_CORRUPT", "extract.pdf", path=str(path), details=str(exc))
            return

        try:
            if document.needs_pass:
                raise_error(
                    "ERR_FILE_CORRUPT",
                    "extract.pdf",
                    path=str(path),
                    details="The PDF is password-protected, so its contents cannot be read.",
                )
                return

            builder = DocumentBuilder(path)
            builder.meta["page_count"] = document.page_count
            empty_pages: list[int] = []

            for number in range(document.page_count):
                try:
                    text = document.load_page(number).get_text("text")
                except Exception as exc:                  # noqa: BLE001 - one bad page, not a bad file
                    empty_pages.append(number + 1)
                    builder.warn(
                        make_error(
                            "ERR_FILE_CORRUPT",
                            "extract.pdf",
                            path=str(path),
                            details=f"Page {number + 1} could not be read: {exc}",
                        )
                    )
                    continue

                cleaned = normalise_whitespace(text)
                if len(cleaned) < MIN_PAGE_CHARS:
                    empty_pages.append(number + 1)
                    continue
                builder.add(cleaned, page=number + 1)

            if empty_pages:
                builder.meta["pages_without_text"] = empty_pages

            result = builder.build()
            if result.is_empty:
                # **The scanned PDF, and the gap this closes.**
                #
                # A real run produced dozens of these - ISA-95 standards, PI
                # Server manuals, MESA papers - each saying "there is nothing to
                # index without OCR, which V2 does not do" *while the OCR engine
                # was loaded and running*. Both halves were true and together
                # they read as a contradiction: OCR was wired to `.png` and
                # `.jpg` through `ocr.py`, and this module never called it. A
                # scanned PDF is a picture in an envelope the OCR reader never
                # opens, because the envelope has a `.pdf` on it.
                ocr_result = _ocr_pages(document, path, builder)
                if ocr_result is not None:
                    yield ocr_result
                    return

                raise_error(
                    "ERR_NO_TEXT_LAYER",
                    "extract.pdf",
                    path=str(path),
                    details=(
                        f"All {document.page_count} page(s) are images with no text layer - "
                        "the document was almost certainly scanned or photographed."
                    ),
                    suggestion=(
                        "Its pages are left for the pictures pass, which reads "
                        "scanned pages after the text is indexed."
                        if _pdf_ocr_pages() and _pictures_held() else
                        "Reading it needs OCR on every page, at seconds per page. "
                        "Turn it on with LEASHA_PDF_OCR_PAGES=<n>, where n is how "
                        "many pages of one document are worth that time."
                        if not _pdf_ocr_pages() else
                        "OCR is on but produced no usable text - the scan may be "
                        "too faint, rotated, or in a language the reader does not "
                        "have. The file is indexed by name either way."
                    ),
                )
                return

            if empty_pages:
                # **2d: the ladder is the router for scanned-PDF pages too,
                # even inside a document that is mostly born-digital.** Before
                # this, a report with one scanned appendix indexed the text
                # pages and just warned about the rest - the same budget
                # (`PDF_OCR_PAGES`) that already reads a wholly-scanned
                # manual's first pages had never been offered this document at
                # all, because `result.is_empty` was False. `_ocr_specific_pages`
                # gives each of *these specific* pages the same `ocr_image()`
                # call - and therefore the same ladder - a mostly-text PDF
                # stops paying for the pages that turn out to have no text
                # boxes, exactly like any other image would.
                read_pages, ocr_elapsed = _ocr_specific_pages(
                    document, path, builder, empty_pages)
                if read_pages:
                    builder.meta["ocr_pages"] = len(read_pages)
                    builder.meta["ocr_seconds"] = round(ocr_elapsed, 2)
                    empty_pages = [p for p in empty_pages if p not in read_pages]
                    result = builder.build()

            if empty_pages:
                result.warnings = (
                    *result.warnings,
                    make_error(
                        "ERR_NO_TEXT_LAYER",
                        "extract.pdf",
                        path=str(path),
                        details=(
                            f"{len(empty_pages)} of {document.page_count} pages have no text "
                            f"layer and were not indexed: "
                            f"{_summarise(empty_pages)}. The rest of the document was indexed."
                        ),
                    ),
                )

            yield result
        finally:
            document.close()


def _summarise(pages: list[int], limit: int = 10) -> str:
    """'1, 2, 3 and 47 more' - a page list that cannot itself fill the log."""
    head = ", ".join(str(page) for page in pages[:limit])
    remainder = len(pages) - limit
    return f"{head} and {remainder} more" if remainder > 0 else head


register(PdfExtractor())


def _pdf_ocr_pages() -> int:
    """How many pages of one scanned PDF to OCR. 0 means do not.

    Read per call rather than cached, so turning it on does not need a restart -
    and so a test can set it without reloading the module. Anything unparseable
    is 0: a typo in an environment variable must not silently start a job that
    takes days.
    """
    raw = os.environ.get(PDF_OCR_PAGES_VAR)
    if raw is None:
        # **The environment variable is the override, not the only way in.**
        # `LEASHA_PDF_OCR_PAGES` was the whole interface, which is
        # non-negotiable 11 broken: a tunable with no control. The setting is
        # the ordinary route; the variable still wins so a single run can be
        # given a different budget without touching anybody's configuration.
        return _pages_from_settings()
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


_SETTINGS_PAGES: object = None


def _pages_from_settings() -> int:
    """`PDF_OCR_PAGES` from Settings. Cached, and never raises.

    Cached because this is asked once per scanned PDF on a worker thread, and
    re-reading `.env` per document is the shape of cost that turns a run into
    an afternoon. A missing configuration is 0 - off - which is what this did
    before it was configurable.
    """
    global _SETTINGS_PAGES
    if _SETTINGS_PAGES is None:
        try:
            from app.core.config import load_settings

            settings = load_settings(create_dirs=False, check_writable=False)
            _SETTINGS_PAGES = max(0, int(getattr(settings, "pdf_ocr_pages", 0) or 0))
        except Exception:                        # noqa: BLE001 - a budget
            _SETTINGS_PAGES = 0
    return int(_SETTINGS_PAGES)


def _pictures_held() -> bool:
    """Is this read the text-first pass, which leaves scanned pages for later?

    **2026-09-29, measured.** `PDF_OCR_PAGES` is described in Settings as
    "Only the images pass uses this", but the text-first pass (`INDEX_OCR_MODE`
    text, or "after the run") OCR'd a wholly scanned PDF inline all the same:
    on a 250-document corpus with 20 scanned PDFs and a budget of 5 pages, the
    text pass took 129 s instead of 3 s, all of it OCR - the same trap order 0z
    lane C found for pictures inside a `.pst`. `app.extract.reading` carries
    the pass's rule to the reader; outside a pipeline (a test, `app.cli
    extract`) it is "read", which is exactly the old behaviour.

    Only the *wholly* scanned PDF is held. A partly scanned one is indexed on
    this pass from its text pages, so no queue entry would ever bring the
    pictures pass back to its scanned pages; those stay read inline, as before.
    """
    try:
        from app.extract import reading

        return reading.current().images == reading.IMAGES_HOLD
    except Exception:                            # noqa: BLE001 - never blocks a read
        return False


def _ocr_specific_pages(
    document: object, path: Path, builder: object, pages: list[int],
) -> tuple[list[int], float]:
    """OCR each page in `pages` (1-based) that is worth it, budget allowing.

    2d's own half: `_ocr_pages` below handles the *wholly*-scanned PDF, trying
    the first N pages of a document that had no text layer anywhere. This
    handles the *partly*-scanned one - a born-digital report with one scanned
    appendix - where `pages` is exactly the page numbers that had no text
    layer, not the first N of the document. Same budget
    (`_pdf_ocr_pages()`/`PDF_OCR_PAGES`), same `ocr_image()` call, and
    therefore the same ladder, as `_ocr_pages` - kept as a separate function
    rather than a shared one because `_ocr_pages`'s own whitebox tests
    (`test_pdf_ocr.py`) assert on literal calls in *its* source specifically.

    Returns `(page_numbers_read, elapsed_seconds)`. An empty list means
    nothing usable came back - OCR not installed, the budget is 0, or every
    one of these pages genuinely has no text either way.

    **Never raises.** Runs inside an extraction worker on a corpus of unknown
    provenance - one unreadable page costs one page, not the file, exactly
    like `_ocr_pages`.
    """
    limit = _pdf_ocr_pages()
    if limit <= 0 or in_reader_process():
        # In a reader process the OCR models stay with the indexer (order
        # `reader-process-isolation`, 2026-10-09): the pages are left in the
        # "pages without text" warning, as when the budget is 0.
        return [], 0.0

    from app.extract.ocr import available, ocr_image

    if not available():
        return [], 0.0

    import time

    started = time.monotonic()
    read: list[int] = []
    for number in pages[:limit]:
        try:
            page = document.load_page(number - 1)               # type: ignore[attr-defined]
            image = page.get_pixmap(dpi=OCR_RENDER_DPI).tobytes(RENDER_FORMAT)
        except Exception as exc:                                 # noqa: BLE001
            log.debug("could not render page {} of {}: {}", number, path, exc)
            continue

        result = ocr_image(image)
        text = normalise_whitespace(getattr(result, "text", "") or "")
        if len(text) >= MIN_PAGE_CHARS:
            builder.add(text, page=number)                       # type: ignore[attr-defined]
            read.append(number)

    return read, time.monotonic() - started


def _ocr_pages(document: object, path: Path, builder: object) -> object:
    """OCR the first N pages of an image-only PDF, or None if not doing that.

    Returns a built `Document` when OCR produced usable text, and `None` in
    every other case - not installed, switched off, or read nothing - so the
    caller falls through to the honest `ERR_NO_TEXT_LAYER` it always raised.

    **Never raises.** This runs inside an extraction worker on a corpus of
    unknown provenance; one unreadable scan must cost one file, not the run.
    """
    limit = _pdf_ocr_pages()
    if limit <= 0:
        return None

    if _pictures_held() or in_reader_process():
        # The text-first pass. Declining here gives the caller its ordinary
        # `ERR_NO_TEXT_LAYER`, and that row is exactly the queue the pictures
        # pass reads its scanned PDFs from (`Pipeline._is_deferred`).
        #
        # And a reader process (order `reader-process-isolation`, 2026-10-09):
        # the OCR models, their memory and the GPU lock belong to the indexer,
        # so a child declines for the same reason, with the same result - the
        # pictures pass, in the parent, reads the row back.
        return None

    from app.extract.ocr import available, ocr_image

    if not available():
        return None

    import time

    started = time.monotonic()
    pages = min(limit, getattr(document, "page_count", 0))
    read = 0

    for number in range(pages):
        try:
            page = document.load_page(number)                 # type: ignore[attr-defined]
            # `tobytes("png")` rather than a temp file: the OCR reader takes
            # bytes, and a corpus of scanned manuals would otherwise write and
            # delete a few hundred thousand temporary images.
            # 2026-09-29: `RENDER_FORMAT` (uncompressed) rather than "png" -
            # see there; still bytes, still no temp file.
            image = page.get_pixmap(dpi=OCR_RENDER_DPI).tobytes(RENDER_FORMAT)
        except Exception as exc:                              # noqa: BLE001
            log.debug("could not render page {} of {}: {}", number + 1, path, exc)
            continue

        result = ocr_image(image)
        text = normalise_whitespace(getattr(result, "text", "") or "")
        if len(text) >= MIN_PAGE_CHARS:
            builder.add(text, page=number + 1)                # type: ignore[attr-defined]
            read += 1

    if not read:
        return None

    elapsed = time.monotonic() - started
    out = builder.build()                                     # type: ignore[attr-defined]
    out.meta["ocr_pages"] = read
    out.meta["ocr_seconds"] = round(elapsed, 1)
    # **Say that this text was read by looking at it.** OCR output has errors
    # that a text layer does not, and a search result nobody can account for is
    # a search result nobody trusts.
    out.meta["read_by"] = "ocr"

    total = getattr(document, "page_count", 0)
    if total > pages:
        out.warnings = (
            *out.warnings,
            make_error(
                "ERR_FILE_TRUNCATED", "extract.pdf", path=str(path),
                details=f"OCR read the first {pages} of {total} pages.",
                suggestion=(
                    f"This scan has {total} pages and OCR runs at seconds per "
                    f"page, so the first {pages} were read - normally the title, "
                    f"contents and introduction, which is most of what makes it "
                    f"findable. Raise {PDF_OCR_PAGES_VAR} if the rest matters."
                ),
            ),
        )
    log.info("OCR read {} page(s) of {} in {:.1f}s", read, path.name, elapsed)
    return out
