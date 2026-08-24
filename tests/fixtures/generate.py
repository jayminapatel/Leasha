"""Build the Layer 2 fixture corpus deterministically.

Layer: L2 (test support)

Fixtures are **generated, not committed**. Binary test files in git rot: nobody
can review a diff of a .docx, nobody remembers which byte was corrupted on
purpose, and a "helpful" editor that opens and re-saves one silently destroys the
property it was testing. A generator is reviewable, and every corruption is
deliberate and explained in the line that causes it.

`ensure_fixtures()` is idempotent and cheap, so `conftest.py` can call it on every
session. Run it by hand with:

    venv\\Scripts\\python.exe -m scripts.make_fixtures
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["ensure_fixtures", "FIXTURE_ROOT", "PDF_PAGE_TEXT", "DOCX_TABLE"]

FIXTURE_ROOT = Path(__file__).resolve().parent

#: Distinctive strings, so a test can assert that *this* page's text came back
#: rather than merely that some text did.
PDF_PAGE_TEXT = {
    1: "Commissioning report for the northern pump station.",
    2: "Flow rates were measured at three points across the manifold.",
    3: "The survey concluded on the fourteenth of March.",
}

DOCX_TABLE = [
    ["Item", "Quantity", "Unit cost"],
    ["Centrifugal pump", "2", "4150.00"],
    ["Isolation valve", "8", "212.50"],
]

XLSX_SHEETS = {
    "Costs": [["Item", "Cost"], ["Pump", 4150], ["Valve", 212.5]],
    "Notes": [["Observation"], ["Vibration within tolerance"]],
}

PPTX_SLIDES = [
    ("Quarterly review", "Delivery is on schedule for the March handover."),
    ("Risks", "Long-lead valve delivery remains the critical path item."),
]


def ensure_fixtures(root: Path | None = None) -> Path:
    """Create every fixture that is missing. Returns the fixture root."""
    root = root or FIXTURE_ROOT
    for folder in ("pdf", "office", "email", "plaintext", "corrupt"):
        (root / folder).mkdir(parents=True, exist_ok=True)

    _plaintext(root / "plaintext")
    _corrupt(root / "corrupt")
    _email(root / "email")
    _pdf(root / "pdf")
    _office(root / "office")
    return root


def _write_if_missing(path: Path, data: bytes) -> None:
    if not path.exists():
        path.write_bytes(data)


# --- plaintext --------------------------------------------------------------

def _plaintext(folder: Path) -> None:
    body = (
        "Site survey notes\n\n"
        "The northern plant was inspected on Tuesday. Access was granted at 08:00.\n"
        "Two isolation valves require replacement before the next shutdown.\n"
    )
    _write_if_missing(folder / "utf8.txt", body.encode("utf-8"))
    _write_if_missing(folder / "utf8_bom.txt", b"\xef\xbb\xbf" + body.encode("utf-8"))

    # cp1252-only bytes: curly quotes, an en dash and a sterling sign. These are
    # invalid UTF-8, so a naive reader either raises or produces mojibake.
    legacy = "Cost – “50 units” at £4,150 each.\n"
    _write_if_missing(folder / "cp1252.txt", legacy.encode("cp1252"))

    # Valid in neither UTF-8 nor cp1252: 0x81 and 0x90 are unmapped in cp1252,
    # so decoding must fall all the way through to latin-1 and warn.
    _write_if_missing(folder / "undecodable.txt", b"Header\n\x81\x90\x8d rest of the line\n")

    # A binary file wearing a text extension.
    _write_if_missing(folder / "binary.log", b"\x00\x01\x02\x00" * 512 + b"not really a log")

    _write_if_missing(
        folder / "table.csv",
        b"item,quantity,cost\npump,2,4150.00\nvalve,8,212.50\n",
    )


def _corrupt(folder: Path) -> None:
    # The extension lies: plain text calling itself a PDF.
    _write_if_missing(folder / "lies.pdf", b"This is not a PDF. It is a sentence.\n")
    # Zero bytes, which every parser must reject cleanly rather than crash on.
    _write_if_missing(folder / "empty.docx", b"")
    # A valid zip that is not an OOXML package.
    _write_if_missing(folder / "notoffice.xlsx", b"PK\x03\x04 truncated zip header only")
    _write_if_missing(folder / "unsupported.xyz", b"nobody handles this extension\n")


# --- email ------------------------------------------------------------------

def _email(folder: Path) -> None:
    root_message = (
        "Message-ID: <thread-root@example.com>\r\n"
        "From: Priya Anand <priya@example.com>\r\n"
        "To: Sam Okoro <sam@example.com>, Dev Team <dev@example.com>\r\n"
        "Subject: Site survey findings\r\n"
        "Date: Tue, 03 Mar 2026 09:14:00 +0000\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "The survey found two valves needing replacement before the shutdown.\r\n"
        "Costs are in the attached sheet.\r\n"
    )
    _write_if_missing(folder / "thread_root.eml", root_message.encode("utf-8"))

    reply = (
        "Message-ID: <reply-one@example.com>\r\n"
        "In-Reply-To: <thread-root@example.com>\r\n"
        "References: <thread-root@example.com>\r\n"
        "From: Sam Okoro <sam@example.com>\r\n"
        "To: Priya Anand <priya@example.com>\r\n"
        "Subject: Re: Site survey findings\r\n"
        "Date: Tue, 03 Mar 2026 11:02:00 +0000\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        "\r\n"
        "Agreed. I will raise the purchase order this week.\r\n"
    )
    _write_if_missing(folder / "thread_reply.eml", reply.encode("utf-8"))

    html_only = (
        "Message-ID: <html-only@example.com>\r\n"
        "From: noreply@example.com\r\n"
        "Subject: Monthly summary\r\n"
        "Date: Wed, 04 Mar 2026 07:00:00 +0000\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "\r\n"
        "<html><head><style>p{color:red}</style></head><body>"
        "<p>Throughput rose by 4%.</p><p>No incidents were &amp; recorded.</p>"
        "<script>alert('x')</script></body></html>\r\n"
    )
    _write_if_missing(folder / "html_only.eml", html_only.encode("utf-8"))

    # Headers with no body and no subject: nothing worth indexing.
    _write_if_missing(folder / "empty_body.eml", b"X-Nothing: here\r\n\r\n")


# --- pdf --------------------------------------------------------------------

def _pdf(folder: Path) -> None:
    healthy = folder / "healthy.pdf"
    scanned = folder / "scanned.pdf"
    encrypted = folder / "encrypted.pdf"
    truncated = folder / "truncated.pdf"

    if healthy.exists() and scanned.exists() and encrypted.exists() and truncated.exists():
        return

    import pymupdf

    if not healthy.exists():
        document = pymupdf.open()
        for number in sorted(PDF_PAGE_TEXT):
            page = document.new_page()
            page.insert_text((72, 96), PDF_PAGE_TEXT[number], fontsize=12)
        document.save(str(healthy))
        document.close()

    if not truncated.exists():
        # A valid PDF header followed by rubbish. Truncating a real PDF in half
        # is not enough: MuPDF reconstructs the xref and recovers the first page,
        # which is admirable but tests nothing. This is unambiguously unreadable.
        truncated.write_bytes(b"%PDF-1.7\n" + bytes(range(256)) * 4)

    if not encrypted.exists():
        document = pymupdf.open()
        page = document.new_page()
        page.insert_text((72, 96), "Confidential rates schedule.", fontsize=12)
        document.save(
            str(encrypted),
            encryption=pymupdf.PDF_ENCRYPT_AES_256,
            owner_pw="owner-secret",
            user_pw="user-secret",
        )
        document.close()

    if not scanned.exists():
        # One page holding an image and no text at all - what a scan looks like.
        document = pymupdf.open()
        page = document.new_page()
        pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 160, 160))
        pixmap.set_rect(pixmap.irect, (210, 210, 210))
        page.insert_image(pymupdf.Rect(72, 72, 232, 232), pixmap=pixmap)
        document.save(str(scanned))
        document.close()


# --- office -----------------------------------------------------------------

def _office(folder: Path) -> None:
    _docx(folder / "healthy.docx")
    _xlsx(folder / "healthy.xlsx")
    _pptx(folder / "healthy.pptx")


def _docx(path: Path) -> None:
    if path.exists():
        return
    import docx

    document = docx.Document()
    document.add_heading("Commissioning summary", level=1)
    document.add_paragraph(
        "The northern pump station was commissioned in March. "
        "All acceptance criteria were met."
    )
    table = document.add_table(rows=0, cols=3)
    for row in DOCX_TABLE:
        cells = table.add_row().cells
        for cell, value in zip(cells, row):
            cell.text = value
    # A paragraph *after* the table, so a reader that splits paragraphs from
    # tables will visibly reorder the document and the test will catch it.
    document.add_paragraph("Signed off by the site engineer on completion.")
    document.save(str(path))


def _xlsx(path: Path) -> None:
    if path.exists():
        return
    import openpyxl

    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, rows in XLSX_SHEETS.items():
        sheet = workbook.create_sheet(title=name)
        for row in rows:
            sheet.append(row)
    # A formula: read with data_only=True it yields the cached value, which is
    # None here because nothing has ever calculated it. That is the real-world
    # case worth having in the corpus.
    workbook["Costs"]["D1"] = "=SUM(B2:B3)"
    workbook.save(str(path))


def _pptx(path: Path) -> None:
    if path.exists():
        return
    from pptx import Presentation

    deck = Presentation()
    layout = deck.slide_layouts[5]                        # title only
    for title, notes in PPTX_SLIDES:
        slide = deck.slides.add_slide(layout)
        slide.shapes.title.text = title
        slide.notes_slide.notes_text_frame.text = notes
    deck.save(str(path))


if __name__ == "__main__":  # pragma: no cover
    print(f"Fixtures written to {ensure_fixtures()}")
