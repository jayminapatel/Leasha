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
invisible failure into a number on the skipped-files panel - and into the
evidence for whether OCR is worth adding after V2.

A partly-scanned PDF - a born-digital report with scanned appendices - indexes
the pages that have text and warns about the ones that do not.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Iterable

from app.core.errors import make_error, raise_error
from app.extract.base import Document, DocumentBuilder, normalise_whitespace, register

__all__ = ["PdfExtractor"]

#: A page with fewer than this many characters is treated as having no text
#: layer. Scanned pages often yield a stray ligature or a page number from a
#: header stamp, which is not a text layer in any useful sense.
MIN_PAGE_CHARS = 8


class PdfExtractor:
    name = "pdf"
    extensions = frozenset({".pdf"})

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
                raise_error(
                    "ERR_NO_TEXT_LAYER",
                    "extract.pdf",
                    path=str(path),
                    details=(
                        f"All {document.page_count} page(s) are images with no text layer - "
                        "the document was almost certainly scanned or photographed."
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
