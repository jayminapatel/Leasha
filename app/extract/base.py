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
from typing import (
    Any, Callable, Iterable, Iterator, Optional, Protocol, Sequence,
    runtime_checkable,
)

from app.core.errors import AppError, raise_error

__all__ = [
    "Segment",
    "Document",
    "DocumentBuilder",
    "Extractor",
    "SourceKind",
    "register",
    "extractor_for",
    "extractor_by_name",
    "extractor_names",
    "supported_extensions",
    "extract",
    "normalise_whitespace",
    "reads_by_ocr",
    "REGISTRY",
]


class SourceKind:
    """Mirrors the `files.source_kind` column in schema.sql."""

    FILE = "file"
    PST_MESSAGE = "pst_message"
    EML = "eml"
    #: A file *inside* an archive, and the archive's own marker row.
    #:
    #: **Not `FILE`, and the reason is `pipeline._prune_missing`.** That deletes
    #: any `file` row whose path is not on disk, and a member's path -
    #: `backup.zip/q3/report.docx` - never is. Members written as `file` were
    #: therefore indexed and then deleted at the end of the same run, leaving
    #: four documents indexed, one chunk, and nothing findable. `.pst` avoids it
    #: with `PST_MESSAGE` for exactly the same reason.
    ARCHIVE = "archive"


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
    #: `(char offset, locator)` landmarks inside the text, ascending. Adoptions
    #: §6a: a spreadsheet's rows, as `Q3!A14`. Empty for everything else, which
    #: is nearly every document - a tuple of nothing costs nothing.
    anchors: tuple[tuple[int, str], ...] = ()

    @property
    def key(self) -> str:
        """What `files.path` should hold for this document."""
        return self.virtual_path or str(self.path)

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()

    def page_for_offset(self, offset: int) -> Optional[int]:
        """The page of the last segment starting at or before `offset`.

        Defined by *start*, not containment, because the separators written
        between segments belong to no segment at all - and an offset landing in
        one must resolve to the page it follows, not to the last page of the
        document. `page_lookup()` is the same function with a binary search, for
        callers mapping many chunks at once; the two must agree.
        """
        page: Optional[int] = None
        for segment in self.segments:
            if segment.char_start > offset:
                break
            page = segment.page
        return page

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

    def anchor_lookup(self) -> Callable[[int], Optional[str]]:
        r"""The landmark at or before an offset, or None. Adoptions §6a.

        Same shape and same rule as `page_lookup` - *last one starting at or
        before* - so a chunk that begins between two rows is reported as
        starting at the earlier one, which is the row a person would see at
        the top of the snippet.

        Returns a lookup that always answers None when there are no anchors,
        which is every document that is not a spreadsheet.
        """
        import bisect

        if not self.anchors:
            return lambda _offset: None

        starts = [offset for offset, _locator in self.anchors]
        locators = [locator for _offset, locator in self.anchors]

        def lookup(offset: int) -> Optional[str]:
            position = bisect.bisect_right(starts, offset) - 1
            return locators[position] if position >= 0 else None

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
    blank_run = False
    for line in lines:
        if line:
            blank_run = False
            out.append(line)
        elif not blank_run:
            # One blank line is a paragraph break, which the chunker splits on.
            # More than one carries no extra meaning and only makes the text
            # longer, so any run collapses to exactly one.
            blank_run = True
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
        #: `(absolute offset, locator)`, in the order added. See `add`.
        self._anchors: list[tuple[int, str]] = []
        self._cursor = 0

    def add(
        self,
        text: str,
        *,
        page: Optional[int] = None,
        label: Optional[str] = None,
        prefix_label: bool = False,
        anchors: Sequence[tuple[int, str]] = (),
    ) -> None:
        r"""Append one segment. Empty and whitespace-only text is dropped.

        `prefix_label=True` writes the label into the indexed text as well, which
        is how a spreadsheet's sheet name or a slide's notes marker become
        searchable - there is no column for them.

        `anchors` are `(offset_into_text, locator)` pairs marking places
        *inside* one segment that a person could be pointed at - the row a
        spreadsheet's line came from, in `Q3!A14` form. **Offsets are relative
        to `text` as passed in**, before the label prefix and before the
        separator, because that is the only frame the caller knows; both
        shifts are applied here, where they cannot be forgotten.

        A segment is the unit of *structure*; an anchor is a landmark within
        one. Adding a segment per row instead would have been the obvious
        move and is wrong twice over: `SEPARATOR` is a paragraph break, so the
        chunker would split on every row, and a five-thousand-row sheet would
        become five thousand chunks.
        """
        body = text.strip()
        if not body:
            return
        # `strip()` moved the text under the offsets the caller gave, so the
        # anchors move with it. Leading whitespace is the common case - a sheet
        # that starts with a blank row - and an anchor that is off by that much
        # points at the wrong row for the whole segment.
        shift = -(len(text) - len(text.lstrip()))
        if prefix_label and label:
            body = f"{label}\n{body}"
            shift += len(label) + 1

        if self._parts:
            self._parts.append(self.SEPARATOR)
            self._cursor += len(self.SEPARATOR)

        start = self._cursor
        self._parts.append(body)
        self._cursor += len(body)
        self._segments.append(
            Segment(text=body, char_start=start, char_end=self._cursor, page=page, label=label)
        )
        for position, (offset, locator) in enumerate(anchors or ()):
            place = start + int(offset) + shift
            # **The first anchor claims the top of its segment**, heading and
            # all. Without this the label line `Sheet: Q3` sits above every
            # anchor, so the *first chunk of every sheet* - which starts at
            # offset zero - resolved to nothing and showed no locator at all.
            # A heading belongs to the rows under it.
            if position == 0:
                place = start
            # Clamped rather than dropped: an anchor a little before its
            # segment - a caller counting from the unstripped text in a way
            # this has not thought of - should still point at the right sheet.
            self._anchors.append((max(start, min(place, self._cursor)), str(locator)))

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
            anchors=tuple(self._anchors),
        )


@runtime_checkable
class Extractor(Protocol):
    """What every parser implements."""

    name: str
    extensions: frozenset[str]

    #: True when the file is read through another application rather than by
    #: reading its bytes - `.pst` through Outlook being the only case today.
    #:
    #: Two consequences, and both bit on the first real run. The file is **held
    #: open by that application**, so reading it to compute a content hash fails
    #: with a permission error; and the hash would be meaningless anyway, because
    #: nothing here parses those bytes. Change detection falls back to mtime and
    #: size, which is all that is available and all that is needed.
    reads_externally: bool

    def supports(self, path: Path) -> bool: ...

    def extract(self, path: Path) -> Iterable[Document]: ...

    #: Optional. Left unset (or `False`), an extractor is exactly as this
    #: Protocol describes it - `extract(path)` and nothing else - and the
    #: module-level `extract()` below never touches it. Set `True` and add a
    #: keyword-only `resume_from: int = 0` to `extract`, and a caller that
    #: knows the last durable position within this file's own stable
    #: ordering can pass it back in to skip straight past what a prior run
    #: already finished. Not part of the formal `Protocol` shape above,
    #: because adding a required attribute here would mean every other
    #: extractor - none of which have any use for it - would have to declare
    #: it too. `email_mbox.MboxExtractor` is the one implementation.
    #: supports_resume: bool


#: extension (lowercase, with dot) -> extractor
REGISTRY: dict[str, Extractor] = {}


#: Whole filenames an extractor claims, lower-cased: `makefile`, `.gitignore`.
#:
#: **A second registry, and it has to be.** `Path("Makefile").suffix` is `""`
#: and so is `Path(".gitignore").suffix` - Python reads a leading dot as the
#: start of the stem, not as a separator. Neither can be a key in an
#: extension-keyed map, so a build file and every dotfile in a repository were
#: invisible to the walk. Filled by `register` from an extractor's optional
#: `names`, which keeps one source of truth per extractor.
NAME_REGISTRY: "dict[str, Extractor]" = {}


def register(extractor: Extractor) -> Extractor:
    """Register an extractor for each of its extensions, and any whole names.

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

    for name in getattr(extractor, "names", ()) or ():
        NAME_REGISTRY[str(name).lower()] = extractor
    return extractor


def extractor_for(path: Path) -> Optional[Extractor]:
    """The extractor for this path, or None if the type is unsupported.

    Extension first, then the whole name. The order matters only in that the
    common case stays one dictionary lookup: a file with a suffix never touches
    the name map.
    """
    found = REGISTRY.get(path.suffix.lower())
    if found is not None:
        return found
    return NAME_REGISTRY.get(path.name.lower())


def extractor_names() -> frozenset[str]:
    """Every registered extractor's `name`, for validating configuration.

    Config names an extractor by name rather than by import path, so a typo is
    caught while the app is starting rather than on one file three hours into a
    run. This is the set that check is made against.
    """
    return frozenset(
        name for name in (getattr(e, "name", "") for e in REGISTRY.values()) if name
    )


def extractor_by_name(name: str) -> Optional[Extractor]:
    """Look an extractor up by name. The reverse of the registry's own key.

    Deliberately derived from `REGISTRY` on each call rather than kept as a
    second dictionary: two structures describing the same set is how an
    extractor ends up findable by name but not by extension.
    """
    for extractor in REGISTRY.values():
        if getattr(extractor, "name", None) == name:
            return extractor
    return None


def supported_extensions() -> frozenset[str]:
    return frozenset(REGISTRY)


def supported_names() -> frozenset[str]:
    """Whole filenames that are indexed - `makefile`, `dockerfile`, dotfiles.

    Separate from `supported_extensions` because the walk filters on suffix and
    has to ask this second question explicitly. Folding them into one set would
    mean the walker comparing `""` against a set that contains `""`, which
    admits every extensionless file on the disk.
    """
    return frozenset(NAME_REGISTRY)


def reads_externally(path: Path) -> bool:
    """True if this file is read through another application, not by its bytes.

    Callers use it to skip hashing: the owning application holds the file open,
    so the read fails, and the bytes are not what gets parsed anyway.
    """
    extractor = extractor_for(path)
    return bool(getattr(extractor, "reads_externally", False))


def reads_by_ocr(path: Path) -> bool:
    """True if the only way to read this file is to look at it.

    **The tier whose cost is measured in seconds per page rather than
    milliseconds.** Measured here at 3.6 seconds a page, roughly eight times
    what embedding a passage costs - so at a terabyte OCR is not a feature of
    an index run, it *is* the schedule. The pipeline asks this to hold images
    back for a second pass, and `app.cli scan` asks it to say how many days
    that pass will take.

    Asked of the resolved extractor rather than of a hard-coded extension list,
    so a route added in `extractors.toml` that lands on the OCR reader is
    counted with the rest.
    """
    extractor = extractor_for(path)
    return str(getattr(extractor, "name", "")) == "ocr"


def _try_converter(path: Path) -> Optional[Iterator[Document]]:
    """An enabled Tier 2 converter for this extension, or None.

    **Only if it is switched on.** Converters ship disabled because the binary
    may not be installed, and a format that fails on every file is worse than
    one that says plainly it is off - so an unconfigured `.doc` stays
    ERR_UNSUPPORTED_TYPE rather than becoming ERR_CONVERTER_MISSING on every
    file in the corpus.

    Imported lazily. `converter.py` runs external programs, and nothing should
    be able to reach that by importing the extraction package to ask whether
    `.pdf` is supported.
    """
    try:
        from app.core.formats import load_rules
        from app.extract.converter import extract_via_converter
    except ImportError:                          # pragma: no cover - partial install
        return None

    try:
        rule = load_rules().converter_for(path.suffix.lower())
    except Exception:                            # noqa: BLE001 - bad config is not this
        return None                              # path's problem; `formats` reports it

    if rule is None or not rule.enabled:
        return None
    return iter(extract_via_converter(path, rule))


def extract(path: Path, *, resume_from: int = 0) -> Iterator[Document]:
    """Extract one path through the registry.

    Raises `AppErrorException` with a precise code - `ERR_UNSUPPORTED_TYPE`,
    `ERR_FILE_CORRUPT`, `ERR_FILE_LOCKED`, `ERR_NO_TEXT_LAYER` - so the caller
    records why, marks the file, and carries on. It never returns an empty
    document: nothing to index is a skip reason, not a success.

    `resume_from` is forwarded only to an extractor that opts in by setting
    `supports_resume = True` - every other extractor's `extract(path)` is
    called exactly as before, so this is silently ignored for the files it
    does not apply to rather than a signature every extractor must accept.
    See `email_mbox.MboxExtractor` for the one that uses it: a resumed run
    skips straight to the message index a prior run last confirmed durable,
    instead of re-parsing everything before it.
    """
    extractor = extractor_for(path)
    if extractor is None:
        # No extractor claims this type in code. Before calling it unsupported,
        # ask whether an *enabled* Tier 2 converter covers it - that is the
        # whole point of Tier 2, and checking here means every caller gets it
        # without knowing converters exist.
        converted = _try_converter(path)
        if converted is not None:
            yield from converted
            return

        raise_error(
            "ERR_UNSUPPORTED_TYPE",
            "extract",
            path=str(path),
            ext=path.suffix.lower() or "(no extension)",
        )
        return

    extract_kwargs: dict[str, Any] = {}
    if resume_from and getattr(extractor, "supports_resume", False):
        extract_kwargs["resume_from"] = resume_from

    produced = False
    for document in extractor.extract(path, **extract_kwargs):
        if document.is_empty:
            continue
        produced = True
        yield document

    if not produced:
        raise_error("ERR_NO_TEXT_LAYER", f"extract.{extractor.name}", path=str(path))
