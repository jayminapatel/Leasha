r"""Scanned PDFs, and the contradiction that led here.

A real run logged dozens of lines like:

    'ISA 95 Part 1 October 2012.pdf' contains no text that can be extracted.
    | Scanned documents hold text as pixels... there is nothing to index
      without OCR - which V2 does not do.

...immediately after:

    OCR engine loaded in 0.8s

Both were true. OCR was wired to `.png` and `.jpg` through `ocr.py`, and
`pdf.py` never called it - so a scanned PDF was a picture in an envelope the
OCR reader never opened, because the envelope had a `.pdf` on it.
"""

from __future__ import annotations

import pytest

from app.extract.pdf import PDF_OCR_PAGES_VAR, _pdf_ocr_pages


def test_it_is_off_unless_a_page_budget_is_given(monkeypatch):
    """**Arithmetic, not caution.** 3.6 seconds a page against manuals of
    several hundred pages turns an index run into an OCR run."""
    monkeypatch.delenv(PDF_OCR_PAGES_VAR, raising=False)
    assert _pdf_ocr_pages() == 0


def test_a_budget_is_a_number_of_pages_not_a_switch(monkeypatch):
    """The first pages of a scanned manual are the title, contents and
    introduction - most of what makes it findable, for about a minute a file."""
    monkeypatch.setenv(PDF_OCR_PAGES_VAR, "20")
    assert _pdf_ocr_pages() == 20


@pytest.mark.parametrize("value", ["", "yes", "-5", "3.5"])
def test_an_unparseable_budget_means_off(monkeypatch, value):
    """A typo in an environment variable must not silently start a job that
    runs for days."""
    monkeypatch.setenv(PDF_OCR_PAGES_VAR, value)
    assert _pdf_ocr_pages() == 0


def test_the_reader_is_actually_called_now():
    """The bug was an absence, so the test is for a presence: `pdf.py` had no
    reference to OCR anywhere except a comment wondering whether to add it."""
    import inspect

    from app.extract import pdf

    source = inspect.getsource(pdf)
    assert "from app.extract.ocr import" in source
    assert "ocr_image(" in source


def test_ocr_text_is_labelled_as_such():
    """OCR output carries errors a text layer does not. A result nobody can
    account for is a result nobody trusts."""
    import inspect

    from app.extract import pdf

    assert '"read_by"' in inspect.getsource(pdf._ocr_pages)


def test_a_capped_scan_says_how_much_it_read():
    """Reading 20 pages of a 400-page manual and saying nothing would be the
    same silent half-answer this project keeps finding."""
    import inspect

    body = inspect.getsource(__import__("app.extract.pdf", fromlist=["_ocr_pages"])._ocr_pages)
    assert "ERR_FILE_TRUNCATED" in body
    assert "ocr_pages" in body and "ocr_seconds" in body


def test_nothing_happens_when_ocr_is_not_installed(monkeypatch):
    """`available()` is checked before any page is rendered - rendering a
    400-page scan and then discovering there is no reader is pure waste."""
    import inspect

    from app.extract import pdf

    body = inspect.getsource(pdf._ocr_pages)
    assert body.index("available()") < body.index("get_pixmap")
