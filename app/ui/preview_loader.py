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

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.core.errors import AppError, make_error

__all__ = ["Preview", "KIND_TEXT", "KIND_HTML", "KIND_PDF", "KIND_IMAGE",
           "KIND_NONE", "kind_for", "load_preview", "CAPS"]

KIND_TEXT = "text"
KIND_HTML = "html"
KIND_PDF = "pdf"
KIND_IMAGE = "image"
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

_TEXT_SUFFIXES = frozenset({
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json",
    ".yaml", ".yml", ".xml", ".ini", ".cfg", ".toml", ".py", ".js", ".ts",
    ".sql", ".ps1", ".bat", ".cmd", ".sh", ".c", ".h", ".cpp", ".cs", ".java",
    ".go", ".rs", ".rb", ".php", ".css", ".env", ".conf", ".properties",
    ".tex", ".bib", ".adoc", ".org", ".srt", ".vtt", ".jsonl", ".ndjson",
})
_HTML_SUFFIXES = frozenset({".html", ".htm", ".eml", ".msg"})
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp",
                             ".tif", ".tiff"})


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
    if suffix in _TEXT_SUFFIXES:
        return KIND_TEXT
    if suffix in _IMAGE_SUFFIXES:
        return KIND_IMAGE
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

    return Preview(kind=KIND_TEXT, body=text, path=path_text, title=title,
                   subtitle=subtitle, truncated=truncated)
