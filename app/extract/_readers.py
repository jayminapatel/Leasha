"""Every extractor module, imported for one reason: `register()` runs.

Layer: L2

**This file is the old top of `app/extract/__init__.py`, moved one step
sideways so that importing the package no longer imports twenty-four parsers.**
Nothing imports this module directly. `app.extract.load_all_extractors()` does,
the first time anything reads `REGISTRY`, `NAME_REGISTRY` or anything derived
from them - which is every question about what Leasha can read, and none of the
questions the first paint of the window asks. The start-up saving is the whole
point: `app.ui.widgets.spreadsheet_view` imports `app.extract.cells`,
`app.core.code_types` imports `app.extract.source_types`, `app.core.media_open`
imports `app.extract.timecode`, and each of those used to drag the entire
parser set - `email`, `mailbox`, `html`, `xml` and the rest - in behind it,
before the window was on screen.

The import lines keep the exact shape they had: one per module, alphabetical,
`from app.extract import x as x`, which is what `scripts/scaffold_extractor.py`
inserts into and what `test_scaffold` asserts.

`email_pst` is imported defensively: it needs pywin32 and a live Outlook, and
must never prevent the rest of the set from loading on a machine without them.
"""

from __future__ import annotations

from app.extract import archive as archive  # noqa: F401,E402
from app.extract import cad as cad  # noqa: F401,E402
from app.extract import cloudstub as cloudstub  # noqa: F401,E402
from app.extract import diagrams as diagrams  # noqa: F401,E402
from app.extract import doc as doc  # noqa: F401,E402
from app.extract import ebook as ebook  # noqa: F401,E402
from app.extract import email_emlx as email_emlx  # noqa: F401,E402
from app.extract import email_files as email_files  # noqa: F401,E402
from app.extract import email_mbox as email_mbox  # noqa: F401,E402
from app.extract import email_olm as email_olm  # noqa: F401,E402
from app.extract import iwork as iwork  # noqa: F401,E402
from app.extract import media as media  # noqa: F401,E402
from app.extract import mobi as mobi  # noqa: F401,E402
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
from app.extract import ppt as ppt  # noqa: F401,E402
from app.extract import publisher as publisher  # noqa: F401,E402
from app.extract import raw as raw  # noqa: F401,E402
from app.extract import rtf as rtf  # noqa: F401,E402
from app.extract import xls as xls  # noqa: F401,E402

try:  # pragma: no cover - Windows + Outlook only
    from app.extract import email_pst as email_pst  # noqa: F401,E402
except Exception:  # noqa: BLE001 - absence of Outlook is normal, not an error
    email_pst = None  # type: ignore[assignment]
