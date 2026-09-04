"""Layer 2 — extraction: any supported file in, clean text and chunks out.

Importing this package populates the extractor registry. Each extractor module
registers itself on import, and the heavy parsers (PyMuPDF, python-docx,
openpyxl, python-pptx) are imported lazily *inside* `extract()` - so importing
`app.extract` to ask "is .pdf supported?" costs nothing, and a machine missing
one optional parser can still index every other type.

`email_pst` is imported defensively: it needs pywin32 and a live Outlook, and
must never prevent the rest of the package from loading on a machine without
them.
"""

from __future__ import annotations

from app.extract import archive as archive  # noqa: F401,E402
from app.extract import cad as cad  # noqa: F401,E402
from app.extract import cloudstub as cloudstub  # noqa: F401,E402
from app.extract import diagrams as diagrams  # noqa: F401,E402
from app.extract import ebook as ebook  # noqa: F401,E402
from app.extract import email_files as email_files  # noqa: F401,E402
from app.extract import email_mbox as email_mbox  # noqa: F401,E402
from app.extract import ocr as ocr  # noqa: F401,E402
from app.extract import odf as odf  # noqa: F401,E402
from app.extract import office as office  # noqa: F401,E402
# Every one of these registers itself on import. Order is irrelevant - a
# duplicate claim raises - so they are kept alphabetical, which is what
# `scripts/scaffold_extractor.py` inserts into and what `test_scaffold`
# asserts. `plaintext` sat below this block, out of order, and the generator
# was blamed for the file it was reading.
from app.extract import pdf as pdf  # noqa: F401,E402
from app.extract import plaintext as plaintext  # noqa: F401,E402
from app.extract import raw as raw  # noqa: F401,E402
from app.extract import rtf as rtf  # noqa: F401,E402
from app.extract import xls as xls  # noqa: F401,E402
from app.extract.base import (  # noqa: F401
    Document,
    DocumentBuilder,
    Extractor,
    Segment,
    SourceKind,
    extract,
    extractor_for,
    register,
    supported_extensions,
)
from app.extract.chunker import Chunk, chunk_document, chunk_text  # noqa: F401

try:  # pragma: no cover - Windows + Outlook only
    from app.extract import email_pst as email_pst  # noqa: F401,E402
except Exception:  # noqa: BLE001 - absence of Outlook is normal, not an error
    email_pst = None  # type: ignore[assignment]

__all__ = [
    "Chunk",
    "Document",
    "DocumentBuilder",
    "Extractor",
    "Segment",
    "SourceKind",
    "chunk_document",
    "chunk_text",
    "extract",
    "extractor_for",
    "register",
    "supported_extensions",
]
