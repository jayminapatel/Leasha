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
    Any, Callable, Iterable, Iterator, Mapping, Optional, Protocol, Sequence,
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
    "line_in_text",
    "reads_by_ocr",
    "looks_locked",
    "with_closing_warning",
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
    #: **The date this document is *from*, when the file itself knows better
    #: than its mtime does.** Work order 0f §3a: a photograph's EXIF
    #: `DateTimeOriginal`, set by `OcrExtractor` and `RawExtractor`. None for
    #: every other extractor, meaning "no opinion - use the file's mtime".
    #:
    #: This field is why the item was reopened. Both image extractors already
    #: did `builder.date = exif_date`, but `DocumentBuilder` declared no such
    #: attribute and `build()` never read one, so the assignment was a plain
    #: attribute set on an object nobody asked - a dead write that made the
    #: wiring look finished for months while every photo still filtered by its
    #: copy date (commit `c58dca9` diagnosed it). Declaring it here is what
    #: makes that assignment mean something.
    date: Optional[Any] = None

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


def line_in_text(text: str, char_start: int) -> int:
    """The line of `text` that offset `char_start` of `normalise_whitespace(text)`
    falls on, counted from 1; 0 when it is past the end. 2026-10-04.

    A code hit carries where its passage starts in the *indexed* text, which
    has lost blank-line runs and its leading blank lines - so its line in the
    file is found by replaying exactly what `normalise_whitespace` kept.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept: list[int] = []
    blank_run = False
    for number, line in enumerate(lines, 1):
        if line.rstrip():
            blank_run = False
            kept.append(number)
        elif not blank_run:
            blank_run = True
            kept.append(number)
    first = next((i for i, number in enumerate(kept) if lines[number - 1].strip()), len(kept))
    kept = kept[first:]                     # what the closing `.strip()` removed
    indexed = normalise_whitespace(text)
    if not 0 <= int(char_start) < len(indexed):
        return 0
    index = indexed.count("\n", 0, int(char_start))
    return kept[index] if index < len(kept) else 0


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
        #: See `Document.date`. Declared here so that setting it is a real
        #: assignment to a known attribute rather than an invented one that
        #: `build()` silently drops - which is exactly what it used to be.
        self.date: Optional[Any] = None
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
        """Freeze what was added into an immutable-shaped `Document`. The builder
        may be built more than once; each call copies, so later `add`s never
        reach a document already handed out."""
        return Document(
            path=self.path,
            text="".join(self._parts),
            segments=tuple(self._segments),
            source_kind=self.source_kind,
            meta=dict(self.meta),
            warnings=tuple(self.warnings),
            anchors=tuple(self._anchors),
            date=self.date,
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
    #:
    #: Work order `dates-live-log-and-interrupted-runs` 3b: `email_pst.
    #: PstExtractor` is the second, at folder granularity, and it also takes
    #: `resume_extra` - what a folder number alone cannot carry back (the
    #: attachments already read, and how many messages were) - see
    #: `pst_libpff.read_archive`.


#: True once `app.extract._readers` has been imported and every extractor has
#: registered. Module-level rather than an attribute of the dict, because both
#: registries below share one load.
_REGISTRY_LOADED = False
#: Held while one thread loads, so a second thread waits for a whole registry
#: rather than reading a half-filled one.
_REGISTRY_LOCK = __import__("threading").RLock()
#: Per-thread re-entry guard. `register()` reads the registry to refuse a
#: duplicate claim, and `register()` is exactly what the load runs - without
#: this, the load would call itself. Thread-local rather than a plain flag so
#: that *another* thread arriving mid-load waits (above) instead of seeing the
#: flag and reading an empty registry.
_REGISTRY_REENTRY = __import__("threading").local()


class _LazyRegistry(dict):
    """A registry dict that imports the extractor modules when first read.

    **Every read populates it; nothing else changed.** `REGISTRY[".pdf"]`,
    `".pdf" in REGISTRY`, iteration, `.items()`, `len()`, `dict(REGISTRY)` -
    each one loads first and then answers exactly as a plain dict would, so
    `supported_extensions()`, `extractor_for()`, `format_health`, `doctor`,
    `app.cli formats` and the walker all see the full set they always saw.
    A duplicate claim still raises from `register()`, which checks this dict
    with a raw `dict.get` - see the comment there for why it must not be the
    loading kind.

    Writes (`__setitem__`, `__delitem__`) deliberately do *not* load: they are
    how `register()` fills the dict during the load itself. `pop`, `clear`,
    `update` and `setdefault` do load, because a caller taking something out
    of the registry means the whole registry - that is what the tests that
    swap in a fake extractor and put the real set back are doing.

    `keys` and `__iter__` are both overridden on purpose: CPython's
    `dict(other)` takes a fast path that copies a dict subclass's storage
    directly *unless* the subclass overrides `tp_iter`, and then falls back to
    calling `keys()`. Overriding only one of the two would let `dict(REGISTRY)`
    return an empty dict.
    """

    def _ensure(self) -> None:
        global _REGISTRY_LOADED
        if _REGISTRY_LOADED or getattr(_REGISTRY_REENTRY, "busy", False):
            return
        with _REGISTRY_LOCK:
            if _REGISTRY_LOADED:
                return
            _REGISTRY_REENTRY.busy = True
            try:
                from app.extract import load_all_extractors

                load_all_extractors()
            finally:
                _REGISTRY_REENTRY.busy = False
            # Only on success: a reader that fails to import must fail again
            # on the next question, exactly as `import app.extract` used to,
            # rather than leaving a silently empty registry behind it.
            _REGISTRY_LOADED = True

    def __getitem__(self, key):                     # type: ignore[no-untyped-def]
        self._ensure()
        return dict.__getitem__(self, key)

    def __contains__(self, key):                    # type: ignore[no-untyped-def]
        self._ensure()
        return dict.__contains__(self, key)

    def __iter__(self):                             # type: ignore[no-untyped-def]
        self._ensure()
        return dict.__iter__(self)

    def __len__(self):                              # type: ignore[no-untyped-def]
        self._ensure()
        return dict.__len__(self)

    def __repr__(self):                             # type: ignore[no-untyped-def]
        self._ensure()
        return dict.__repr__(self)

    def get(self, key, default=None):               # type: ignore[no-untyped-def]
        self._ensure()
        return dict.get(self, key, default)

    def keys(self):                                 # type: ignore[no-untyped-def]
        self._ensure()
        return dict.keys(self)

    def values(self):                               # type: ignore[no-untyped-def]
        self._ensure()
        return dict.values(self)

    def items(self):                                # type: ignore[no-untyped-def]
        self._ensure()
        return dict.items(self)

    def copy(self):                                 # type: ignore[no-untyped-def]
        self._ensure()
        return dict(self)

    def pop(self, *args, **kwargs):                 # type: ignore[no-untyped-def]
        self._ensure()
        return dict.pop(self, *args, **kwargs)

    def popitem(self):                              # type: ignore[no-untyped-def]
        self._ensure()
        return dict.popitem(self)

    def setdefault(self, *args, **kwargs):          # type: ignore[no-untyped-def]
        self._ensure()
        return dict.setdefault(self, *args, **kwargs)

    def update(self, *args, **kwargs):              # type: ignore[no-untyped-def]
        self._ensure()
        return dict.update(self, *args, **kwargs)

    def clear(self):                                # type: ignore[no-untyped-def]
        self._ensure()
        return dict.clear(self)


#: extension (lowercase, with dot) -> extractor
#:
#: Lazily filled: see `_LazyRegistry`. The first read imports every extractor
#: module; before that it is genuinely empty, which is why nothing may read it
#: through `dict.__getitem__` and friends directly.
REGISTRY: dict[str, Extractor] = _LazyRegistry()


#: Whole filenames an extractor claims, lower-cased: `makefile`, `.gitignore`.
#:
#: **A second registry, and it has to be.** `Path("Makefile").suffix` is `""`
#: and so is `Path(".gitignore").suffix` - Python reads a leading dot as the
#: start of the stem, not as a separator. Neither can be a key in an
#: extension-keyed map, so a build file and every dotfile in a repository were
#: invisible to the walk. Filled by `register` from an extractor's optional
#: `names`, which keeps one source of truth per extractor.
NAME_REGISTRY: "dict[str, Extractor]" = _LazyRegistry()


def register(extractor: Extractor) -> Extractor:
    """Register an extractor for each of its extensions, and any whole names.

    Registering the same extension twice is a programming error and says so:
    silently overwriting would mean a file type is parsed by whichever module
    happened to import last, which is not something to discover in production.
    """
    for ext in extractor.extensions:
        key = ext.lower()
        # **Read raw, on purpose.** `REGISTRY.get` would load every other
        # extractor first, and `register()` runs at the top of every extractor
        # module - so importing one reader (`app.main` imports `app.extract.
        # ocr` to set its device) would drag all twenty-four in and undo the
        # whole deferral. The duplicate check loses nothing: there is one dict,
        # so whichever of the two claims arrives second sees the first and
        # raises, whether that is this call or the deferred load.
        existing = dict.get(REGISTRY, key)
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
    """Every extension some extractor claims, lower-cased with the dot. Loads the
    registry on first call (see `_LazyRegistry`); converter-only types are not here."""
    return frozenset(REGISTRY)


def supported_names() -> frozenset[str]:
    """Whole filenames that are indexed - `makefile`, `dockerfile`, dotfiles.

    Separate from `supported_extensions` because the walk filters on suffix and
    has to ask this second question explicitly. Folding them into one set would
    mean the walker comparing `""` against a set that contains `""`, which
    admits every extensionless file on the disk.
    """
    return frozenset(NAME_REGISTRY)


#: Work order `reader-process-isolation` (2026-10-09). True inside a reader
#: process (`app/index/read_process.py` sets it in the child before serving),
#: so a Layer 2 reader can know its role without importing Layer 3. The PDF
#: reader asks it: in a child it never OCRs, because the OCR models, their
#: memory and the GPU lock belong to the parent.
_READER_PROCESS = False


def in_reader_process() -> bool:
    """Is this code running inside a reader process rather than the indexer?"""
    return _READER_PROCESS


def set_reader_process(flag: bool) -> None:
    """Declare this process a reader process (or not). Called once by the child's
    `main`; tests use it to stand in a child without starting one."""
    global _READER_PROCESS
    _READER_PROCESS = bool(flag)


def reads_externally(path: Path) -> bool:
    """True if this file is read through another application, not by its bytes.

    Callers use it to skip hashing: the owning application holds the file open,
    so the read fails, and the bytes are not what gets parsed anyway.
    """
    extractor = extractor_for(path)
    return bool(getattr(extractor, "reads_externally", False))


def change_marker(path: Path) -> Optional[str]:
    """What a file that is not hashed offers instead, or None. **Never raises.**

    2026-10-02. A file read through another application has no content hash
    (`reads_externally`), which left its date and size as the whole change
    test - and Outlook moves the date of every archive it mounts. A reader
    that can tell cheaply whether such a file's *contents* moved says so
    through `change_marker(path)`; the pipeline keeps the answer where a hash
    would go and compares it next time (`Pipeline._classify`). None - no such
    reader, or it could not tell - means the date and size decide, as before.
    """
    try:
        marker = getattr(extractor_for(path), "change_marker", None)
        return marker(path) if marker is not None else None
    except Exception:                               # noqa: BLE001 - a check, not a read
        return None


#: What libpff says when another program holds the file exclusively. Measured
#: 2026-09-20 against pypff 20231205 on Windows 11: an exclusive hold gives
#: "...with error: The process cannot access the file because it is being used
#: by another process", while a damaged file gives "invalid file signature".
#: **"locked a portion of the file"** is Windows' own wording for error 33,
#: ERROR_LOCK_VIOLATION - and the one Outlook causes. Outlook does not refuse
#: sharing on an archive it has attached; it byte-range-locks it, so libpff's
#: open succeeds and its first read fails with this text. It was missing, so
#: every archive attached in Outlook was reported as damaged (2026-09-29, the
#: owner's laptop: 15 of 20 archives, every one of them readable through Outlook
#: and, once Outlook let go, through libpff).
_LOCK_PHRASES = ("used by another process", "sharing violation", "lock violation",
                 "locked a portion of the file")


def looks_locked(exc: BaseException) -> bool:
    """True if `exc` means another program has the file, not that it is damaged.

    The two need opposite handling. A lock is retried on the next pass
    (`pipeline._locked_candidates`); a damaged file is recorded as settled and
    never looked at again until it changes. Calling a lock "corrupt" therefore
    dropped an archive from the index for good.

    Python's own `open` raises `PermissionError` for a sharing violation, with
    no `winerror` set, so the type is checked as well as the Windows code and
    the text libpff carries in its message.
    """
    if isinstance(exc, PermissionError):
        return True
    if getattr(exc, "winerror", None) in (32, 33):
        return True
    text = str(exc).lower()
    return any(phrase in text for phrase in _LOCK_PHRASES)


def with_closing_warning(
    documents: Iterable[Document],
    closing: Callable[[], Optional[AppError]],
) -> Iterator[Document]:
    """Yield `documents`, then attach `closing()`'s warning to the last one.

    An extractor that skips part of a container has nowhere to say so: it only
    yields documents. The pipeline already counts every warning on a written
    document (`warned_by_code`), so the summary rides on the final message.
    Holding one document back is the whole cost.

    `closing` runs after the source is exhausted and may itself raise
    `AppErrorException` - the case where nothing at all could be read.

    **The one held back is never lost (2026-09-30).** It used to be, two ways.
    A read that was cut off - the no-progress limit, a Force skip - closed this
    generator with the newest message still inside it, and a generator being
    closed cannot yield: the last message read before every cut-off never
    reached the index. And a source that raised part-way took the held message
    with it. Now:

    * the document in hand is always named on the read's `reading.Reading`
      (`hold`), where the pipeline takes it when it cuts the read off
      (`Pipeline._kept_in_hand`) - from its own thread, or from the watchdog's
      when the reader is stuck in native code and its thread cannot be reached;
    * when the source raises an ordinary exception, the held document is
      handed on first and the exception follows on the next `next()`.
    """
    from app.extract import reading

    policy = reading.current()
    cell = policy.hold()
    held: Optional[Document] = None
    source = iter(documents)
    try:
        while True:
            try:
                document = next(source)
            except StopIteration:
                break
            except Exception:
                # Not `BaseException`: a thread told to let go of its file
                # (`file_watch.FileTimedOut`) must leave at once, and the
                # pipeline takes the document from `cell` instead.
                if held is not None:
                    last, held = held, None
                    cell[0] = None
                    yield last
                raise
            # Read, and not handed on, from here until it is itself yielded:
            # across the `yield` below and across the next read.
            cell[0] = document
            if held is not None:
                yield held
            held = document

        warning = closing()
        if held is None:
            return
        if warning is not None:
            held.warnings = (*held.warnings, warning)
        cell[0] = None
        yield held
    except BaseException as exc:
        # An exception raised *into* the thread to make it let go unwinds
        # this generator before the pipeline can ask what it was holding, so
        # for that one case the cell is left where it is - the `Reading` it
        # sits on lasts exactly as long as the file. Closing, finishing and
        # an ordinary failure all give the cell back.
        if not isinstance(exc, (Exception, GeneratorExit)):
            cell = None
        raise
    finally:
        if cell is not None:
            policy.release(cell)


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
    # 2026-10-08 note: every shipped converter has been enabled by default since
    # 2026-09-19 (config/extractors.toml explains the reversal); the paragraph
    # above describes the earlier default. The `rule.enabled` test below is
    # unchanged and still the point - a route switched off stays off.
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


def extract(
    path: Path, *, resume_from: int = 0,
    resume_extra: Optional[Mapping[str, Any]] = None,
) -> Iterator[Document]:
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

    `resume_extra` rides with it, under the same opt-in, and only when there
    is something in it - so `MboxExtractor`, which takes no such argument, is
    never handed one.
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
        if resume_extra:
            extract_kwargs["resume_extra"] = resume_extra
    elif (resume_extra and getattr(extractor, "supports_resume", False)
            and getattr(extractor, "takes_extra_from_the_top", False)):
        # 2026-10-07: an archive read from the top still has something to be
        # told - which of its messages are in the index already. Only for an
        # extractor that says it takes it; `MboxExtractor` is still never
        # handed one.
        extract_kwargs["resume_extra"] = resume_extra

    produced = False
    for document in extractor.extract(path, **extract_kwargs):
        if document.is_empty:
            continue
        produced = True
        yield document

    if not produced:
        raise_error("ERR_NO_TEXT_LAYER", f"extract.{extractor.name}", path=str(path))
