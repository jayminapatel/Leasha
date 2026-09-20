r"""Legacy PowerPoint: .ppt, read in-process with `olefile`. No LibreOffice needed.

Layer: L2

**Why this exists (owner instruction, 2026-09-20).** `.ppt` used to be handed to
LibreOffice, which costs 5 to 10 seconds a file - almost all of it LibreOffice
starting up. A `.ppt` is an OLE2 container holding a stream of length-prefixed
records ([MS-PPT]), and the text in it is stored in two record types that are
easy to find. `olefile` was already a dependency, so reading it here is the
non-negotiable-12 answer: a library, in-process, milliseconds, and nothing to
install beside the application.

**What is read, and in what order.**

1. `Current User` names the newest `UserEditAtom`. A PowerPoint file is saved
   incrementally, so old versions of a slide can still be lying in the stream;
   reading the stream front to back would index text the author deleted. The
   `UserEditAtom` chain gives the *persist directory* - persist id -> offset of
   the current version - and only current versions are read.
2. The `DocumentContainer`'s `SlideListWithText` (instance 0) lists the slides
   in show order, and holds the text of each slide's placeholders (title, body).
3. Each slide's own container holds the text of everything else - text boxes,
   table cells, grouped shapes - as `TextCharsAtom` (UTF-16LE) and
   `TextBytesAtom` (one byte per character) records, wherever they nest.
4. Speaker notes are found the same way and labelled as notes, exactly as
   `PptxExtractor` labels them, so a 1998 deck and a 2024 deck read alike.

5. WordArt and shape text (the `gtextUNICODE` property of an Escher property
   table), the deck-wide Header & Footer (document container) and text boxes on
   the masters the slides use are read too - measured against LibreOffice, they
   were the whole of the gap (worst deck 0.49 -> 0.98 word recall).

**Fails closed.** Each `SlidePersistAtom` states how many text runs its slide
has; fewer found (in the list plus the slide's shapes) than declared, a listed
slide or a used master missing from the persist directory, all raise
`LegacyOfficeUnreadable`. `fall_back` counts them for the end-of-run summary.

**What it refuses, and what happens then.** Encrypted decks, files with no
`Current User` stream (PowerPoint 4 and 95), a record tree that does not add up,
and anything that is not an OLE2 container at all raise `LegacyOfficeUnreadable`.
The extractor does not guess and never returns empty text for those: it hands
the file to the LibreOffice converter route (`config/extractors.toml`
`[converters.".ppt"]`), which is kept for exactly this and made fast by the warm
session in `app/extract/converter.py`. Where LibreOffice is absent too, the
ordinary `ERR_FILE_CORRUPT` is raised and the file is indexed by name only.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Iterable, Iterator, Optional

from app.core.errors import raise_error
from app.core.format_health import Requirement
from app.core.logging import logger
from app.extract.base import Document, DocumentBuilder, normalise_whitespace, register
from app.extract.legacy_office import LegacyOfficeUnreadable, clean_text, fall_back

__all__ = ["PptExtractor", "read_ppt"]

log = logger.bind(component="extract.ppt")

# Record types, from [MS-PPT] section 2.13.24 (RecordType enumeration).
RT_DOCUMENT = 0x03E8
RT_SLIDE = 0x03EE
RT_NOTES = 0x03F0
RT_NOTES_ATOM = 0x03F1
RT_SLIDE_PERSIST_ATOM = 0x03F3
RT_HEADERS_FOOTERS = 0x0FD9
RT_CSTRING = 0x0FBA
RT_TEXT_HEADER_ATOM = 0x0F9F
RT_TEXT_CHARS_ATOM = 0x0FA0
RT_TEXT_BYTES_ATOM = 0x0FA8
RT_SLIDE_LIST_WITH_TEXT = 0x0FF0
RT_USER_EDIT_ATOM = 0x0FF5
RT_PERSIST_DIRECTORY_ATOM = 0x1772
RT_MAIN_MASTER = 0x03F8
RT_SLIDE_ATOM = 0x03EF
RT_CLIENT_TEXTBOX = 0xF00D

# Escher property tables (`OfficeArtFOPT` and its two variants), which carry the
# text of WordArt and of shapes whose text is drawn as geometry.
RT_ESCHER_OPT = (0xF00B, 0xF121, 0xF122)
_PROP_GTEXT_UNICODE = 0x00C0

#: `Current User` headerToken for an encrypted presentation.
_ENCRYPTED_TOKEN = 0xF3D1C4DF

#: Guard rails. A record tree is untrusted input: a crafted or damaged file
#: must end in `LegacyOfficeUnreadable`, not in a stack overflow or a loop.
_MAX_DEPTH = 24
_MAX_EDIT_CHAIN = 4096
_MAX_TEXT_CHARS = 20_000_000

_HEADER = struct.Struct("<HHI")                  # ver/instance, type, length


def _records(data: bytes, start: int, end: int) -> Iterator[tuple[int, int, int, int, int]]:
    """`(version, instance, type, body_start, body_end)` for each record in a span.

    Stops quietly at a header that does not fit, and raises when a record claims
    to run past the end of its parent: a length that lies means everything after
    it is misread, which is worse than not reading.
    """
    position = start
    while position + 8 <= end:
        ver_instance, record_type, length = _HEADER.unpack_from(data, position)
        body_start = position + 8
        body_end = body_start + length
        if body_end > end:
            raise LegacyOfficeUnreadable(
                f"a record at offset {position} runs past the end of its parent")
        yield ver_instance & 0xF, ver_instance >> 4, record_type, body_start, body_end
        position = body_end


def _text_of(data: bytes, record_type: int, start: int, end: int) -> str:
    body = data[start:end]
    if record_type == RT_TEXT_CHARS_ATOM:
        return body[: len(body) & ~1].decode("utf-16-le", "replace")
    # TextBytesAtom: each byte is the low half of a code point. Windows-1252
    # rather than Latin-1 would differ only in 0x80-0x9F, and the specification
    # says Latin-1; measured against LibreOffice on real decks, they agree.
    return body.decode("latin-1")


def _walk_text(data: bytes, start: int, end: int, depth: int = 0) -> list[str]:
    """Every text atom under a span, in file order, descending into containers.

    A container is a record whose version nibble is 0xF. Escher's own
    containers (`DgContainer`, `SpgrContainer`, `SpContainer`, `ClientTextbox`)
    follow the same rule, which is why group shapes and table cells need no
    special handling: their text is just nested deeper.
    """
    if depth > _MAX_DEPTH:
        raise LegacyOfficeUnreadable("record tree nested too deeply")
    found: list[str] = []
    for version, _instance, record_type, body_start, body_end in _records(data, start, end):
        if record_type in (RT_TEXT_CHARS_ATOM, RT_TEXT_BYTES_ATOM):
            found.append(_text_of(data, record_type, body_start, body_end))
        elif record_type in RT_ESCHER_OPT:
            found.extend(_shape_property_text(data, body_start, body_end, _instance))
        elif version == 0xF:
            found.extend(_walk_text(data, body_start, body_end, depth + 1))
    return found


def _shape_property_text(data: bytes, start: int, end: int, count: int) -> list[str]:
    """WordArt text: the `gtextUNICODE` property of a shape's property table.

    A table is `count` six-byte entries (id, value); an entry with the complex
    bit set has its value as a byte length into the data that follows the whole
    table, in order. Chemical-plant P&ID style decks, and anything titled with
    WordArt, keep their words here and nowhere else.
    """
    found: list[str] = []
    table_end = start + count * 6
    if table_end > end:
        return found
    cursor = table_end
    for index in range(count):
        ident, value = struct.unpack_from("<HI", data, start + index * 6)
        if not ident & 0x8000:
            continue
        size = value
        if cursor + size > end:
            break
        if ident & 0x3FFF == _PROP_GTEXT_UNICODE:
            found.append(data[cursor:cursor + (size & ~1)].decode("utf-16-le", "replace").rstrip("\x00"))
        cursor += size
    return found


def _footers(data: bytes, start: int, end: int) -> list[str]:
    """Header and footer text one slide carries (`CString` atoms, instances 1 and 2).

    "Internal KOM Meeting Summary v03, October 2005" or a conference name, typed
    once in Insert > Header & Footer and shown on every slide. It is not in any
    text box, so reading only text atoms misses it - measured against
    LibreOffice, that was the single largest thing missing from real decks.
    """
    found: list[str] = []
    for _v, _i, record_type, body_start, body_end in _records(data, start, end):
        if record_type != RT_HEADERS_FOOTERS:
            continue
        for _v2, instance, inner_type, inner_start, inner_end in _records(data, body_start, body_end):
            if inner_type == RT_CSTRING and instance in (1, 2):
                text = data[inner_start:inner_end]
                found.append(text[: len(text) & ~1].decode("utf-16-le", "replace"))
    return found


def _master_text(data: bytes, start: int, end: int, depth: int = 0) -> list[str]:
    """Words a master slide draws on every slide that uses it.

    A conference name typed into a text box on the master, or a footer typed in
    Header & Footer, is on every slide of the deck and in no slide container -
    LibreOffice shows it, so a search for it has to find the deck. Placeholder
    text ("Click to edit Master title style") is furniture and is not read: only
    text boxes of type Other, footer strings and WordArt are.
    """
    if depth > _MAX_DEPTH:
        raise LegacyOfficeUnreadable("record tree nested too deeply")
    found: list[str] = []
    for version, instance, record_type, body_start, body_end in _records(data, start, end):
        if record_type == RT_CSTRING and instance in (1, 2):
            text = data[body_start:body_end]
            found.append(text[: len(text) & ~1].decode("utf-16-le", "replace"))
        elif record_type == RT_CLIENT_TEXTBOX:
            kind = None
            runs: list[str] = []
            for _v, _i, inner_type, inner_start, inner_end in _records(data, body_start, body_end):
                if inner_type == RT_TEXT_HEADER_ATOM and inner_end - inner_start >= 4:
                    (kind,) = struct.unpack_from("<I", data, inner_start)
                elif inner_type in (RT_TEXT_CHARS_ATOM, RT_TEXT_BYTES_ATOM):
                    runs.append(_text_of(data, inner_type, inner_start, inner_end))
            if kind == 4:
                found.extend(run for run in runs if not run.startswith("Click to edit"))
        elif record_type in RT_ESCHER_OPT:
            found.extend(_shape_property_text(data, body_start, body_end, instance))
        elif version == 0xF:
            found.extend(_master_text(data, body_start, body_end, depth + 1))
    return found


def _persist_directory(data: bytes, current_edit: int) -> tuple[dict[int, int], int]:
    """`(persist id -> offset, document persist id)` from the newest edit back.

    Walked newest to oldest with `setdefault`, so the most recent save of any
    object wins and an older save is used only for objects never rewritten.
    """
    directory: dict[int, int] = {}
    document_ref = 0
    offset = current_edit
    seen: set[int] = set()
    for _ in range(_MAX_EDIT_CHAIN):
        if offset in seen or offset <= 0 or offset + 8 > len(data):
            break
        seen.add(offset)
        _v, _i, record_type, body_start, body_end = next(
            _records(data, offset, len(data)), (0, 0, 0, 0, 0))
        if record_type != RT_USER_EDIT_ATOM or body_end - body_start < 20:
            raise LegacyOfficeUnreadable("the edit chain does not lead to a UserEditAtom")
        # lastSlideIdRef(4) version(2) minor(1) major(1) offsetLastEdit(4)
        # offsetPersistDirectory(4) documentRef(4) maxPersistWritten(4) ...
        offset_last_edit, offset_directory, doc_ref = struct.unpack_from(
            "<III", data, body_start + 8)
        if not document_ref:
            document_ref = doc_ref

        directory_at = offset_directory
        if directory_at + 8 > len(data):
            raise LegacyOfficeUnreadable("the persist directory is outside the stream")
        _v, _i, dir_type, dir_start, dir_end = next(
            _records(data, directory_at, len(data)), (0, 0, 0, 0, 0))
        if dir_type != RT_PERSIST_DIRECTORY_ATOM:
            raise LegacyOfficeUnreadable("the persist directory is missing")
        cursor = dir_start
        while cursor + 4 <= dir_end:
            (head,) = struct.unpack_from("<I", data, cursor)
            cursor += 4
            first_id = head & 0xFFFFF
            count = head >> 20
            for index in range(count):
                if cursor + 4 > dir_end:
                    break
                (target,) = struct.unpack_from("<I", data, cursor)
                cursor += 4
                directory.setdefault(first_id + index, target)
        if offset_last_edit == offset:
            break
        offset = offset_last_edit
    return directory, document_ref


def _container_at(data: bytes, offset: int, expected: int) -> Optional[tuple[int, int]]:
    """`(body_start, body_end)` of the record of type `expected` at `offset`."""
    if offset < 0 or offset + 8 > len(data):
        return None
    _v, _i, record_type, body_start, body_end = next(
        _records(data, offset, len(data)), (0, 0, 0, 0, 0))
    if record_type != expected:
        return None
    return body_start, body_end


def _slide_list(data: bytes, start: int, end: int, instance_wanted: int
                ) -> list[tuple[int, int, list[str], int, int]]:
    """`(persistIdRef, slideIdentifier, placeholder text, declared, seen)` per slide.

    Text atoms belong to the `SlidePersistAtom` that most recently preceded
    them. A `TextHeaderAtom` starts a text run; `declared` is how many runs the
    `SlidePersistAtom` says the slide has and `seen` how many headers were here.
    """
    slides: list[tuple[int, int, list[str], int, int]] = []
    for version, instance, record_type, body_start, body_end in _records(data, start, end):
        if record_type != RT_SLIDE_LIST_WITH_TEXT or instance != instance_wanted:
            continue
        for _v, _i, inner_type, inner_start, inner_end in _records(
                data, body_start, body_end):
            if inner_type == RT_SLIDE_PERSIST_ATOM and inner_end - inner_start >= 16:
                persist_ref, _flags, boxes, identifier = struct.unpack_from(
                    "<IIiI", data, inner_start)
                slides.append((persist_ref, identifier, [], boxes, 0))
            elif inner_type == RT_TEXT_HEADER_ATOM and slides:
                last = slides[-1]
                slides[-1] = (*last[:4], last[4] + 1)
            elif inner_type in (RT_TEXT_CHARS_ATOM, RT_TEXT_BYTES_ATOM) and slides:
                slides[-1][2].append(_text_of(data, inner_type, inner_start, inner_end))
    return slides


def _count_text_runs(data: bytes, start: int, end: int, depth: int = 0) -> int:
    """`TextHeaderAtom` records anywhere under a span: the text runs of shapes."""
    if depth > _MAX_DEPTH:
        raise LegacyOfficeUnreadable("record tree nested too deeply")
    total = 0
    for version, _instance, record_type, body_start, body_end in _records(data, start, end):
        if record_type == RT_TEXT_HEADER_ATOM:
            total += 1
        elif version == 0xF:
            total += _count_text_runs(data, body_start, body_end, depth + 1)
    return total


def _master_id_of(data: bytes, start: int, end: int) -> int:
    """`masterIdRef` of a slide: which master's furniture it shows (0 if unknown)."""
    for _v, _i, record_type, body_start, body_end in _records(data, start, end):
        if record_type == RT_SLIDE_ATOM and body_end - body_start >= 16:
            return struct.unpack_from("<I", data, body_start + 12)[0]
        break
    return 0


def read_ppt(stream_data: bytes, current_user: bytes
             ) -> tuple[list[str], list[tuple[int, str]], list[str]]:
    """`(slide texts in show order, [(slide number, notes text), ...], footers)`.

    `footers` are the distinct header/footer strings of the deck, each once.

    Raises `LegacyOfficeUnreadable` for anything it cannot read with confidence.
    """
    if len(current_user) < 20:
        raise LegacyOfficeUnreadable("no usable 'Current User' stream")
    token, current_edit = struct.unpack_from("<II", current_user, 12)
    if token == _ENCRYPTED_TOKEN:
        raise LegacyOfficeUnreadable("the presentation is encrypted")

    directory, document_ref = _persist_directory(stream_data, current_edit)
    document_offset = directory.get(document_ref)
    if document_offset is None:
        raise LegacyOfficeUnreadable("the document container is not in the persist directory")
    span = _container_at(stream_data, document_offset, RT_DOCUMENT)
    if span is None:
        raise LegacyOfficeUnreadable("the document container is not where the directory says")

    slide_entries = _slide_list(stream_data, span[0], span[1], 0)
    master_entries = _slide_list(stream_data, span[0], span[1], 1)
    notes_entries = _slide_list(stream_data, span[0], span[1], 2)
    master_of = {identifier: persist_ref for persist_ref, identifier, *_rest in master_entries}
    masters_used: list[int] = []

    texts: list[str] = []
    identifiers: list[int] = []
    footers: list[str] = []
    # The deck-wide Header & Footer (the conference name typed once, shown on every
    # slide) lives in the document container's own HeadersFooters record.
    for footer in _footers(stream_data, span[0], span[1]):
        footer = clean_text(footer).strip()
        if footer and footer not in footers:
            footers.append(footer)
    total = 0
    for persist_ref, identifier, placeholder_text, declared, seen in slide_entries:
        parts = list(placeholder_text)
        offset = directory.get(persist_ref)
        container = _container_at(stream_data, offset, RT_SLIDE) if offset is not None else None
        if container is None:
            # A slide the deck lists and the directory cannot find is a slide
            # whose text is not in what was read: say so rather than index a hole.
            raise LegacyOfficeUnreadable(
                f"slide {len(texts) + 1} is listed but its container is not where the directory says")
        if container is not None:
            master_id = _master_id_of(stream_data, container[0], container[1])
            if master_id in master_of and master_of[master_id] not in masters_used:
                masters_used.append(master_of[master_id])
            # Completeness: the slide states how many text runs it has. The list
            # and the slide's own shapes between them must account for that many.
            # Fewer found than declared means a run of text is somewhere this
            # reader did not look.
            found_runs = seen + _count_text_runs(stream_data, container[0], container[1])
            if found_runs < declared:
                raise LegacyOfficeUnreadable(
                    f"slide {len(texts) + 1} declares {declared} text runs and {found_runs} were found")
            parts.extend(_walk_text(stream_data, container[0], container[1]))
            for footer in _footers(stream_data, container[0], container[1]):
                footer = clean_text(footer).strip()
                if footer and footer not in footers:
                    footers.append(footer)
        body = clean_text("\r".join(parts))
        total += len(body)
        if total > _MAX_TEXT_CHARS:
            raise LegacyOfficeUnreadable("the presentation holds an implausible amount of text")
        texts.append(body)
        identifiers.append(identifier)

    for persist_ref in masters_used:
        offset = directory.get(persist_ref)
        # A title master is stored as a slide container, the others as a main master.
        master = None
        if offset is not None:
            master = (_container_at(stream_data, offset, RT_MAIN_MASTER)
                      or _container_at(stream_data, offset, RT_SLIDE))
        if master is None:
            raise LegacyOfficeUnreadable("a master slide the slides use is not where the directory says")
        for footer in _master_text(stream_data, master[0], master[1]):
            footer = clean_text(footer).strip()
            if footer and footer not in footers:
                footers.append(footer)

    by_identifier = {ident: number for number, ident in enumerate(identifiers, start=1)}
    notes: list[tuple[int, str]] = []
    for persist_ref, _identifier, placeholder_text, _declared, _seen in notes_entries:
        offset = directory.get(persist_ref)
        container = _container_at(stream_data, offset, RT_NOTES) if offset is not None else None
        slide_number = 0
        parts = list(placeholder_text)
        if container is not None:
            for _v, _i, rtype, bstart, bend in _records(stream_data, container[0], container[1]):
                if rtype == RT_NOTES_ATOM and bend - bstart >= 4:
                    (slide_ref,) = struct.unpack_from("<I", stream_data, bstart)
                    slide_number = by_identifier.get(slide_ref, 0)
                    break
            parts.extend(_walk_text(stream_data, container[0], container[1]))
        body = clean_text("\r".join(parts))
        if slide_number and body:
            notes.append((slide_number, body))
    return texts, notes, footers


class PptExtractor:
    """PowerPoint 97-2003 decks. `.pptx` belongs to `PptxExtractor`."""

    name = "ppt"
    extensions = frozenset({".ppt", ".pps", ".pot"})
    reads_externally = False
    #: The LibreOffice route in `config/extractors.toml` stays behind this reader
    #: for what it cannot read. `test_libraries_before_converters` knows.
    falls_back_to_converter = True
    requires = (Requirement("olefile", "olefile",
                            provides="legacy PowerPoint slide and notes text", hard=True),)

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        try:
            texts, notes, footers = self._read(path)
        except LegacyOfficeUnreadable as exc:
            log.debug("ppt reader declined {}: {}", path.name, exc)
            yield from fall_back(path, "extract.ppt", str(exc))
            return
        except PermissionError as exc:
            raise_error("ERR_FILE_LOCKED", "extract.ppt", path=str(path), details=str(exc))
            return

        builder = DocumentBuilder(path)
        builder.meta["slide_count"] = len(texts)
        builder.meta["format"] = "powerpoint-97"
        builder.meta["read_by"] = "olefile"
        notes_by_slide: dict[int, list[str]] = {}
        for number, body in notes:
            notes_by_slide.setdefault(number, []).append(body)

        for number, body in enumerate(texts, start=1):
            builder.add(normalise_whitespace(body), page=number,
                        label=f"Slide {number}", prefix_label=True)
            for note in notes_by_slide.get(number, ()):
                builder.add(normalise_whitespace(note), page=number,
                            label=f"Slide {number} speaker notes", prefix_label=True)
        if footers:
            # Once for the deck, not once per slide: it is the same words.
            builder.add("\n".join(footers), label="Header and footer", prefix_label=True)
        yield builder.build()

    @staticmethod
    def _read(path: Path) -> tuple[list[str], list[tuple[int, str]], list[str]]:
        import olefile

        try:
            if not olefile.isOleFile(str(path)):
                raise LegacyOfficeUnreadable("not an OLE2 container")
            with olefile.OleFileIO(str(path)) as ole:
                if not ole.exists("PowerPoint Document"):
                    raise LegacyOfficeUnreadable("no 'PowerPoint Document' stream")
                stream = ole.openstream("PowerPoint Document").read()
                current_user = (ole.openstream("Current User").read()
                                if ole.exists("Current User") else b"")
        except LegacyOfficeUnreadable:
            raise
        except PermissionError:
            raise
        except Exception as exc:                          # noqa: BLE001 - damaged file
            raise LegacyOfficeUnreadable(f"{type(exc).__name__}: {exc}") from exc

        try:
            return read_ppt(stream, current_user)
        except LegacyOfficeUnreadable:
            raise
        except (struct.error, IndexError, ValueError, OverflowError, RecursionError) as exc:
            raise LegacyOfficeUnreadable(f"{type(exc).__name__}: {exc}") from exc


register(PptExtractor())
