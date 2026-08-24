"""The extraction contract: what every parser returns, and how one is chosen.

Layer: L2

Every extractor turns a path into `Document`s. One file usually means one
Document; a PST means one per message, which is why `extract` returns an
iterable rather than a single value.

**The offset invariant.** A `Document` holds one flat `text` string, and every
`Segment` carries the exact `[char_start, char_end)` range it occupies within it.
The chunker slices that same string, so a chunk's `char_start`/`char_end` point
into it too. That is what lets Layer 5 highlight a hit inside the original
document instead of guessing where it came from. The invariant is:

    document.text[segment.char_start:segment.char_end] == segment.text

`DocumentBuilder` exists so that no extractor has to maintain that by hand -
appending text and recording its offsets is one operation, not two that can
drift apart.

**Failure is a value, not an exception.** A corrupt file is not exceptional in a
100GB run; it is Tuesday. Extractors raise `AppErrorException` for a file that
cannot be read at all, and attach non-fatal problems to `Document.warnings`
(a file that decoded with replacement characters is still worth indexing).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional, Protocol, runtime_checkable

from app.core.errors import AppError, raise_error

__all__ = [
    "Segment",
    "Document",
    "DocumentBuilder",
    "Extractor",
    "SourceKind",
    "register",
    "extractor_for",
    "supported_extensions",
    "extract",
    "normalise_whitespace",
    "REGISTRY",
]


class SourceKind:
    """Mirrors the `files.source_kind` column in schema.sql."""

    FILE = "file"
    PST_MESSAGE = "pst_message"
    EML = "eml"


@dataclass(frozen=True)
class Segment:
    """One naturally-bounded piece of a document, located in `Document.text`.

    `page` is what reaches the `chunks.page` column: a PDF page, a slide number,
    or a 1-based sheet index. `label` is the human name for it ("Costs", "Slide 3
    notes"); it has no column of its own, so an extractor that wants a label to be
    searchable writes it into the text as well - which is exactly what the sheet
    and slide extractors do.
    """

    text: str
    char_start: int
    char_end: int
    page: Optional[int] = None
    label: Optional[str] = None


@dataclass
class Document:
    """One extracted document: flat text, located segments, and metadata."""

    path: Path
    text: str
    segments: tuple[Segment, ...] = ()
    source_kind: str = SourceKind.FILE
    meta: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[AppError, ...] = ()
    #: Set only when one path yields many documents (PST messages), in which case
    #: it is the synthetic per-message path used as the `files.path` key.
    virtual_path: Optional[str] = None

    @property
    def key(self) -> str:
        """What `files.path` should hold for this document."""
        return self.virtual_path or str(self.path)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()

    def page_for_offset(self, offset: int) -> Optional[int]:
        """The page containing `offset`, for stamping a chunk with its page.

        Segments are ordered and non-overlapping, so a linear scan is correct;
        callers doing this per chunk over a long document should use
        `page_lookup()` instead.
        """
        for segment in self.segments:
            if segment.char_start <= offset < segment.char_end:
                return segment.page
        return self.segments[-1].page if self.segments else None

    def page_lookup(self) -> Callable[[int], Optional[int]]:
        """A binary-search page lookup, for mapping many chunks at once."""
        import bisect

        starts = [segment.char_start for segment in self.segments]
        pages = [segment.page for segment in self.segments]
        if not starts:
            return lambda _offset: None

        def lookup(offset: int) -> Optional[int]:
            position = bisect.bisect_right(starts, offset) - 1
            return pages[max(position, 0)]

        return lookup


def normalise_whitespace(text: str) -> str:
    """Collapse runs of blank lines and strip trailing spaces, in place.

    Length-preserving operations only would be ideal, but this runs *before* a
    document is built, never after - so offsets are computed against the result
    and the invariant holds. PDFs and slide decks are full of ragged whitespace
    that would otherwise become chunk boundaries in their own right.
    """
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    out: list[str] = []
    blanks = 0
    for line in lines:
        if line:
            blanks = 0
            out.append(line)
        else:
            blanks += 1
            if blanks <= 2:  # one blank line separates paragraphs; two is a section break
                out.append(line)
    return "\n".join(out).strip()


class DocumentBuilder:
    """Accumulates segments and their offsets together, so they cannot drift.

        builder = DocumentBuilder(path)
        builder.add("page one text", page=1)
        builder.add("page two text", page=2)
        document = builder.build()
    """

    #: Written between segments. Two newlines make a paragraph break, which is
    #: the boundary the chunker prefers to split on.
    SEPARATOR = "\n\n"

    def __init__(self, path: Path, *, source_kind: str = SourceKind.FILE) -> None:
        self.path = path
        self.source_kind = source_kind
        self.meta: dict[str, Any] = {}
        self.warnings: list[AppError] = []
        self._parts: list[str] = []
        self._segments: list[Segment] = []
        self._cursor = 0

    def add(
        self,
        text: str,
        *,
        page: Optional[int] = None,
        label: Optional[str] = None,
        prefix_label: bool = False,
    ) -> None:
        """Append one segment. Empty and whitespace-only text is dropped.

        `prefix_label=True` writes the label into the indexed text as well, which
        is how a spreadsheet's sheet name or a slide's notes marker become
        searchable - there is no column for them.
        """
        body = text.strip()
        if not body:
            return
        if prefix_label and label:
            body = f"{label}\n{body}"

        if self._parts:
            self._parts.append(self.SEPARATOR)
            self._cursor += len(self.SEPARATOR)

        start = self._cursor
        self._parts.append(body)
        self._cursor += len(body)
        self._segments.append(
            Segment(text=body, char_start=start, char_end=self._cursor, page=page, label=label)
        )

    def warn(self, error: AppError) -> None:
        """Record a non-fatal problem. The document is still indexed."""
        self.warnings.append(error)

    def build(self) -> Document:
        return Document(
            path=self.path,
            text="".join(self._parts),
            segments=tuple(self._segments),
            source_kind=self.source_kind,
            meta=dict(self.meta),
            warnings=tuple(self.warnings),
        )


@runtime_checkable
class Extractor(Protocol):
    """What every parser implements."""

    name: str
    extensions: frozenset[str]

    def supports(self, path: Path) -> bool: ...

    def extract(self, path: Path) -> Iterable[Document]: ...


#: extension (lowercase, with dot) -> extractor
REGISTRY: dict[str, Extractor] = {}


def register(extractor: Extractor) -> Extractor:
    """Register an extractor for each of its extensions.

    Registering the same extension twice is a programming error and says so:
    silently overwriting would mean a file type is parsed by whichever module
    happened to import last, which is not something to discover in production.
    """
    for ext in extractor.extensions:
        key = ext.lower()
        existing = REGISTRY.get(key)
        if existing is not None and existing is not extractor:
            raise ValueError(
                f"'{key}' is already handled by {existing.name!r}; "
                f"{extractor.name!r} cannot also claim it"
            )
        REGISTRY[key] = extractor
    return extractor


def extractor_for(path: Path) -> Optional[Extractor]:
    """The extractor for this path, or None if the type is unsupported."""
    return REGISTRY.get(path.suffix.lower())


def supported_extensions() -> frozenset[str]:
    return frozenset(REGISTRY)


def extract(path: Path) -> Iterator[Document]:
    """Extract one path through the registry.

    Raises `AppErrorException` with a precise code - `ERR_UNSUPPORTED_TYPE`,
    `ERR_FILE_CORRUPT`, `ERR_FILE_LOCKED`, `ERR_NO_TEXT_LAYER` - so the caller
    records why, marks the file, and carries on. It never returns an empty
    document: nothing to index is a skip reason, not a success.
    """
    extractor = extractor_for(path)
    if extractor is None:
        raise_error(
            "ERR_UNSUPPORTED_TYPE",
            "extract",
            path=str(path),
            ext=path.suffix.lower() or "(no extension)",
        )
        return

    produced = False
    for document in extractor.extract(path):
        if document.is_empty:
            continue
        produced = True
        yield document

    if not produced:
        raise_error("ERR_NO_TEXT_LAYER", f"extract.{extractor.name}", path=str(path))
