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
from app.extract.base import Document, DocumentBuilder, normalise_whitespace, register

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


class PdfExtractor:
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
    try:
        return max(0, int(os.environ.get(PDF_OCR_PAGES_VAR, "0")))
    except ValueError:
        return 0


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
            image = page.get_pixmap(dpi=OCR_RENDER_DPI).tobytes("png")
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
