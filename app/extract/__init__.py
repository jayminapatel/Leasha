"""Layer 2 — extraction: any supported file in, clean text and chunks out.

Layer: L2

**Importing this package no longer imports every extractor.** It used to, and
that cost about a quarter of a second before the window was on screen: three
modules the first paint does need (`app.extract.cells` for the spreadsheet
view, `app.extract.source_types` for code types, `app.extract.timecode` for
media links) are submodules of this package, so reaching any one of them ran
this file, and this file imported twenty-four parsers plus `email`, `mailbox`,
`html` and `xml` behind them. Work order 0r item 2b.

Registration is instead deferred to the first question that needs an answer:
`REGISTRY` and `NAME_REGISTRY` in `app/extract/base.py` import
`app.extract._readers` - which holds the same import lines this file used to -
the first time either is read. Every reader of the registry (`REGISTRY[".pdf"]`,
`supported_extensions()`, `extractor_for()`, `extract()`, `format_health`,
`doctor`, `app.cli formats`, the walker) therefore sees exactly what it always
saw, and a duplicate claim still raises from `register()`; the difference is
only *when* the modules are imported, never *whether*.

The heavy parsers (PyMuPDF, python-docx, openpyxl, python-pptx) are still
imported lazily *inside* `extract()` - so even loading the registry to ask "is
.pdf supported?" costs no parser, and a machine missing one optional parser can
still index every other type.
"""

from __future__ import annotations

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


def load_all_extractors() -> None:
    """Import every extractor module, so each one's `register()` has run.

    Idempotent - the second call is a `sys.modules` hit and nothing more.
    Called by `app/extract/base.py` the first time the registry is read;
    callers that want the registry should read the registry, not call this.
    """
    from app.extract import _readers  # noqa: F401 - registration side effects


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
    "load_all_extractors",
    "register",
    "supported_extensions",
]
