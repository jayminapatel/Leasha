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

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

from app.core.errors import AppError, make_error
from app.core.logging import logger

__all__ = ["Preview", "KIND_TEXT", "KIND_HTML", "KIND_PDF", "KIND_IMAGE",
           "KIND_NONE", "kind_for", "load_preview", "load_preview_for", "stored_text",
           "CAPS"]

_log = logger.bind(component="ui.preview")

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

#: Characters of an extracted document worth showing. Lower than the text cap
#: because extraction has already cost a zip open and an XML parse, and a Word
#: document long enough to reach this is one somebody should open properly.
EXTRACTED_CAP = 128 * 1024

#: Types that reach the extractor rather than the "no preview" card. **Not a
#: list of Office extensions**: it is "everything the registry can read", asked
#: at call time, so a file type added through the file-types UI becomes
#: previewable at the same moment it becomes searchable. A preview that lags
#: behind the index is a second list to keep in step, and it would drift.
def _extractable(path: Path) -> bool:
    """Whether the index's own extractor claims this file.

    Imported inside the function: `preview_loader` is on the window's startup
    path, and `app.extract` pulls in a registry that imports optional libraries.
    Paying for that before anybody has selected a result is the wrong trade.
    """
    try:
        from app.extract.base import extractor_for, reads_externally

        if reads_externally(path):
            # A Tier 2 converter shells out to another program. That is a
            # reasonable thing to do while indexing a folder overnight; it is
            # not a reasonable thing to do because somebody pressed the down
            # arrow. The card says the file can be opened instead.
            return False
        return extractor_for(path) is not None
    except Exception:                        # noqa: BLE001
        return False

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


def _extracted(path: Path, *, title: str, subtitle: str) -> Preview:
    """A Word, Excel, PowerPoint, OpenDocument or drawing file, as its text.

    **The same extractor the index uses, and that is the point.** Rendering a
    `.docx` faithfully means a word processor; showing what the *index* holds
    means reusing thirty lines of `app/extract`. The second is not a compromise
    of the first - it is a different and more useful thing in a search tool,
    because what appears in the pane is exactly what was searched. A result you
    cannot find the match in is the complaint this answers.

    The pane says so in the notice line rather than letting somebody conclude
    their formatting has been lost.

    Segments carry labels - "Slide 3", a sheet name - so a spreadsheet does not
    arrive as one undifferentiated wall of cells. Where an extractor provides
    them they become headings; where it does not, the flat text is used.
    """
    from app.core.errors import AppErrorException
    from app.extract.base import extract

    try:
        documents = list(extract(path))
    except AppErrorException as exc:
        # ERR_NO_TEXT_LAYER for a scanned PDF or a slide deck of pictures,
        # ERR_FILE_CORRUPT, ERR_FILE_LOCKED - all already carry a fix line.
        return Preview(kind=KIND_NONE, path=str(path), title=title,
                       subtitle=subtitle, error=exc.error)
    except Exception as exc:                 # noqa: BLE001
        return Preview(
            kind=KIND_NONE, path=str(path), title=title, subtitle=subtitle,
            error=make_error("ERR_UNEXPECTED", "ui.preview",
                             details=f"{type(exc).__name__}: {exc}"),
        )

    body = "\n\n".join(_labelled(document) for document in documents).strip()
    truncated = len(body) > EXTRACTED_CAP
    return Preview(
        kind=KIND_TEXT,
        body=body[:EXTRACTED_CAP],
        path=str(path),
        title=title,
        subtitle=subtitle,
        truncated=truncated,
        notice="Text extracted from the document - this is what was indexed, "
               "not how the file looks. Open it to see the formatting.",
    )


def _labelled(document: Any) -> str:
    """Segment labels as headings, where the extractor gave any.

    A spreadsheet without them is a wall of cells with no way to tell which
    sheet a number came from, which is precisely the question somebody
    previewing a spreadsheet is asking.
    """
    labels = [segment for segment in document.segments if segment.label]
    if not labels:
        return document.text
    return "\n\n".join(
        f"{segment.label}\n{'-' * len(segment.label)}\n{segment.text}".strip()
        for segment in labels
    )


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

    if kind == KIND_NONE and _extractable(path):
        return _extracted(path, title=title, subtitle=subtitle)

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


def stored_text(store: Any, file_id: Any) -> str:
    """What the index holds for one file, reassembled. **Runs on a worker.**

    The `body_provider` Mail installs. Read from `chunks` rather than from the
    file because the "file" is a PST holding a hundred thousand messages and
    there is nothing on disk that is *this* message - the text was extracted
    once, at index time, and this is where it went.

    `chunks_for_file` is indexed on `file_id`, which is the whole reason it is
    affordable on every arrow key.
    """
    try:
        chunks = store.chunks_for_file(int(file_id or 0))
    except Exception as exc:                    # noqa: BLE001
        _log.debug("no stored text for file {}: {}", file_id, exc)
        return ""
    return "\n\n".join(chunk.text for chunk in chunks)


def load_preview_for(row: Any, *, body_provider: Any = None) -> Preview:
    """`load_preview` for a result row, whatever kind of row it is.

    **Which fields of a row become which arguments is a decision, so it is
    made here rather than in the widget.** The pane used to reach into the row
    with three `getattr` calls, which meant the rule was untestable and silently
    wrong for anything that did not look like a search result - a Mail row has
    no `preview_text`, so every message previewed as ERR_FILE_MISSING against a
    synthetic path nobody could have opened.

    `body_provider` is how a view supplies text the row does not carry: Mail
    reads the message from the store. **It is called here, on the worker**, for
    the same reason nothing else in this module is called anywhere else - a
    store read on the interface thread between two presses of the down arrow is
    the freeze this application has a standing rule against.
    """
    body = str(getattr(row, "preview_text", "") or "")
    if not body and body_provider is not None:
        try:
            body = str(body_provider(row) or "")
        except Exception as exc:            # noqa: BLE001 - see the docstring
            # A body that cannot be fetched is a preview without one, never a
            # traceback for a row somebody arrowed past.
            _log.debug("no body for the selected row: {}", exc)
            body = ""

    # **A message with no stored text says so.** Reported as "mails are not
    # previewing", and the pane could not tell anybody why: a message has no
    # file of its own, so with no body it fell through to `load_preview` with a
    # synthetic path, which reported the *path* as missing - true, useless, and
    # not the reason. The reason is that this message has no rows in `chunks`:
    # indexed before bodies were stored, or skipped, or an empty message.
    #
    # Checked on `file_id` rather than on the path, because that is what makes
    # a row a message rather than a file.
    if not body and getattr(row, "file_id", None) and body_provider is not None:
        return Preview(
            kind=KIND_NONE,
            path=str(getattr(row, "path", "") or ""),
            title=str(getattr(row, "name", "") or "This message"),
            body=(
                "No text was stored for this message, so there is nothing to "
                "preview.\n\nA message is previewed from the text extracted when "
                "it was indexed - it has no file of its own to re-read. This "
                "usually means it was indexed before message bodies were kept, "
                "or the message is empty.\n\nRe-indexing the archive fills it in."
            ),
        )

    # **`full_path` first.** A Code row's `path` is shortened for its column and
    # cannot be opened - `full_path` is the real one. Reading `path` blindly
    # previewed every repository file as ERR_FILE_MISSING, which is a plausible
    # enough message that nobody would have questioned it.
    preview = load_preview(
        str(getattr(row, "full_path", "") or getattr(row, "path", "")),
        page=int(getattr(row, "page", 0) or 0),
        mail_body=body,
    )
    # A message's title is its subject. `load_preview` cannot know that - it is
    # given text and a synthetic path - and "Message" above every message is a
    # heading that says nothing the pane has not already said.
    name = str(getattr(row, "name", "") or "")
    if body and name:
        preview = replace(preview, title=name)
    return preview
