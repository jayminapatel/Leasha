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


# ---------------------------------------------------------------------------
# 2d: the ladder is the router for scanned-PDF pages too, per-page
# ---------------------------------------------------------------------------

def test_only_the_scanned_page_among_text_pages_pays_for_recognition(tmp_path, monkeypatch):
    """§4 'per-page PDF probe:' - a fixture PDF with one scanned page among
    text pages. Before 2d, a mostly-text PDF like this one indexed the text
    pages and only ever *warned* about the rest - it never got the OCR budget
    at all, because `result.is_empty` was False. Now the specific page with no
    text layer gets its own shot at `ocr_image()`, and only that page pays."""
    import pymupdf

    from app.extract import ocr as ocr_module
    from app.extract.pdf import PDF_OCR_PAGES_VAR, PdfExtractor

    document = pymupdf.open()
    page_one = document.new_page()
    page_one.insert_text((72, 96), "Page one has real text.", fontsize=12)
    page_two = document.new_page()  # the scanned page: an image, no text layer
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 160, 160))
    pixmap.set_rect(pixmap.irect, (210, 210, 210))
    page_two.insert_image(pymupdf.Rect(72, 72, 232, 232), pixmap=pixmap)
    page_three = document.new_page()
    page_three.insert_text((72, 96), "Page three has real text.", fontsize=12)
    pdf_path = tmp_path / "mostly_text_one_scanned_page.pdf"
    document.save(str(pdf_path))
    document.close()

    monkeypatch.setenv(PDF_OCR_PAGES_VAR, "10")
    monkeypatch.setattr(ocr_module, "available", lambda: True)

    calls: list[object] = []

    def fake_ocr_image(source):
        calls.append(source)
        return ocr_module.OcrResult(text="Page two, read by OCR.", lines=1, elapsed_s=0.01)

    monkeypatch.setattr(ocr_module, "ocr_image", fake_ocr_image)

    documents = list(PdfExtractor().extract(pdf_path))

    assert len(calls) == 1, "only the one page with no text layer should pay for recognition"
    assert documents, "the document should still be indexed, not treated as unreadable"
    text = documents[0].text
    assert "Page one has real text." in text
    assert "Page three has real text." in text
    assert "Page two, read by OCR." in text
    assert documents[0].meta["ocr_pages"] == 1
    assert not documents[0].warnings, "the recovered page must not still be warned about"


def test_a_scanned_page_the_budget_does_not_cover_still_warns(tmp_path, monkeypatch):
    """The other half: when OCR is off (budget 0, the default), a mostly-text
    PDF's scanned page behaves exactly as it always did - warned about, not
    silently dropped. `PDF_OCR_PAGES` semantics are unchanged for this case."""
    import pymupdf

    from app.extract.pdf import PDF_OCR_PAGES_VAR, PdfExtractor

    document = pymupdf.open()
    page_one = document.new_page()
    page_one.insert_text((72, 96), "Page one has real text.", fontsize=12)
    page_two = document.new_page()
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 160, 160))
    pixmap.set_rect(pixmap.irect, (210, 210, 210))
    page_two.insert_image(pymupdf.Rect(72, 72, 232, 232), pixmap=pixmap)
    pdf_path = tmp_path / "mostly_text_no_ocr_budget.pdf"
    document.save(str(pdf_path))
    document.close()

    monkeypatch.delenv(PDF_OCR_PAGES_VAR, raising=False)

    documents = list(PdfExtractor().extract(pdf_path))

    assert documents
    assert "Page one has real text." in documents[0].text
    assert documents[0].warnings, "the unreadable page must still be reported, not silently dropped"
    assert documents[0].warnings[-1].code == "ERR_NO_TEXT_LAYER"
