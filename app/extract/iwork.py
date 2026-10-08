r"""Apple iWork: `.pages`, `.numbers` and `.key`, read in-process. No dependency.

Layer: L2

Non-negotiable 12, and the owner's instruction of 2026-09-20 ("speed is
important ... for all types of files applicable"). These three used to go through
LibreOffice: 5-13 seconds a file, a process spawned each time, and **every one of
the 27 `.key` files on the owner's own disk failed** - the `.key` converter used
Writer's `txt:Text` export filter, which Impress does not have, so it exited 1
and wrote nothing (the same fault `.ppt` had; see `config/extractors.toml`).

**The format, as much of it as search needs.** A modern iWork file is a ZIP.
Inside, `Index/*.iwa` hold the document. An `.iwa` is a run of chunks, each a
zero byte, a three-byte little-endian length and a *raw Snappy block* (no stream
framing, no checksum). Decompressed, it is a run of records: a varint length, an
`ArchiveInfo` protobuf message, then the payload messages that message names, back
to back, each with its length and a numeric type.

**No schema, on purpose.** Apple's `.proto` files are undocumented and change
between releases, so nothing here depends on one. Three numeric message types
carry every word a person can search for, and they have been stable for a decade:

    2001  TSWP.StorageArchive    field 3, repeated: a run of text (body, slide,
                                 shape, footnote, comment)
    6005  TST.TableDataList      field 1 = list kind (1 = strings); field 3,
                                 repeated: an entry whose field 3 is a cell string
    6001  TST.TableModelArchive  field 2: a table's name
    2     TN.SheetArchive        field 1: a sheet's name (Numbers)

That is measured against real files (see `docs/EXTRACTION_SPEED.md` and the
work-order note), not recalled.

**It says no rather than guess.** Anything it cannot read with confidence - an
encrypted document, a corrupt block, an iWork '09 package (`index.xml`, no `.iwa`
at all) - raises `LegacyOfficeUnreadable`, and `fall_back` then hands the file to
the LibreOffice route if it is switched on. It never returns empty text for a
file it could not read.

**What it does not read**: cell *numbers* in `.numbers`. Numbers stores a number
inline in a cell's tile record rather than in a data list, and decoding tiles is
most of a spreadsheet engine. Every label, header, name and text cell is read;
the figures are not. The Settings file-types row says so.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Iterable, Iterator, Optional

from app.core.errors import raise_error
from app.core.logging import logger
from app.extract.base import Document, DocumentBuilder, register
from app.extract.legacy_office import LegacyOfficeUnreadable, fall_back

__all__ = [
    "IWorkExtractor",
    "snappy_decompress",
    "decompress_iwa",
    "iwa_records",
    "text_of_iwa",
]

log = logger.bind(component="extract.iwork")

#: A hostile or damaged archive must not be able to make this allocate a gigabyte.
MAX_IWA_BYTES = 256 * 1024 * 1024

#: Members that hold no words worth indexing. Master slides carry template
#: placeholders ("Slide Title", "Double-click to edit") that would appear in every
#: deck in the corpus, and the tile, style and view-state files are layout.
_SKIP_MEMBERS = (
    "MasterSlide", "DocumentStylesheet", "ViewState", "Metadata", "CalculationEngine",
    "AnnotationAuthorStorage", "/Tile", "HeaderStorageBucket", "DocumentMetadata",
)

_STORAGE, _DATA_LIST, _TABLE_MODEL, _SHEET = 2001, 6005, 6001, 2

#: U+FFFC is an inline object (a picture) and the rest are control characters.
#: U+2028 and U+2029 are Pages' line and paragraph separators.
_CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffc\ufeff]")


# -- Snappy, raw block format ------------------------------------------------

def snappy_decompress(buf: bytes) -> bytes:
    """Decompress one raw Snappy block. ~40 lines rather than a dependency.

    `python-snappy` builds a C extension; iWork blocks are a few kilobytes each,
    so the pure-Python cost is below the cost of finding out the wheel was
    missing. Raises `ValueError` for anything malformed, never returns short.
    """
    position, shift, expected = 0, 0, 0
    while True:
        byte = buf[position]
        position += 1
        expected |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            break
        # A Snappy length is at most 32 bits, so five 7-bit groups; a sixth means
        # the block is not Snappy and the loop must not run off the end.
        if shift > 35:
            raise ValueError("snappy length is not a varint")
    if expected > MAX_IWA_BYTES:
        raise ValueError(f"snappy block claims {expected} bytes")

    out = bytearray()
    limit = len(buf)
    while position < limit:
        tag = buf[position]
        position += 1
        kind = tag & 3
        if kind == 0:                                     # literal
            length = tag >> 2
            if length < 60:
                length += 1
            else:
                extra = length - 59
                length = int.from_bytes(buf[position:position + extra], "little") + 1
                position += extra
            if position + length > limit:
                raise ValueError("snappy literal runs past the block")
            out += buf[position:position + length]
            position += length
            continue
        if kind == 1:
            length = ((tag >> 2) & 7) + 4
            offset = ((tag >> 5) << 8) | buf[position]
            position += 1
        elif kind == 2:
            length = (tag >> 2) + 1
            offset = int.from_bytes(buf[position:position + 2], "little")
            position += 2
        else:
            length = (tag >> 2) + 1
            offset = int.from_bytes(buf[position:position + 4], "little")
            position += 4
        start = len(out) - offset
        if offset <= 0 or start < 0:
            raise ValueError("snappy copy points before the start")
        if offset >= length:
            out += out[start:start + length]
        else:                                             # overlapping: byte at a time
            for i in range(length):
                out.append(out[start + i])
    if len(out) != expected:
        raise ValueError(f"snappy produced {len(out)} bytes, header said {expected}")
    return bytes(out)


def decompress_iwa(data: bytes) -> bytes:
    """The concatenated blocks of one `.iwa` member."""
    position, parts, total = 0, [], 0
    end = len(data)
    while position < end:
        if data[position] != 0 or position + 4 > end:
            raise ValueError("not an IWA chunk header")
        length = int.from_bytes(data[position + 1:position + 4], "little")
        position += 4
        block = snappy_decompress(data[position:position + length])
        position += length
        total += len(block)
        if total > MAX_IWA_BYTES:
            raise ValueError("an IWA member expands beyond the safety limit")
        parts.append(block)
    return b"".join(parts)


# -- Protobuf wire format, just enough --------------------------------------

def _varint(buf: bytes, position: int) -> tuple[int, int]:
    result, shift = 0, 0
    while True:
        byte = buf[position]
        position += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, position
        shift += 7
        # Protobuf varints hold 64 bits in ten bytes; more is damage, and an
        # unbounded loop on hostile bytes is the thing this guards against.
        if shift > 70:
            raise ValueError("varint too long")


def _fields(buf: bytes) -> Iterator[tuple[int, int, object]]:
    """`(field number, wire type, value)`; bytes for length-delimited, int else."""
    position, end = 0, len(buf)
    while position < end:
        key, position = _varint(buf, position)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, position = _varint(buf, position)
        elif wire == 2:
            size, position = _varint(buf, position)
            if position + size > end:
                raise ValueError("length-delimited field runs past the message")
            value = buf[position:position + size]
            position += size
        elif wire == 1:
            value = buf[position:position + 8]
            position += 8
        elif wire == 5:
            value = buf[position:position + 4]
            position += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        yield number, wire, value


def iwa_records(data: bytes) -> Iterator[tuple[int, bytes]]:
    """`(message type, payload)` for every message in a decompressed `.iwa`."""
    position, end = 0, len(data)
    while position < end:
        size, position = _varint(data, position)
        header = data[position:position + size]
        position += size
        for number, wire, value in _fields(header):
            if number != 2 or wire != 2:
                continue
            kind = length = 0
            for inner, inner_wire, inner_value in _fields(value):          # type: ignore[arg-type]
                if inner == 1 and inner_wire == 0:
                    kind = inner_value                                     # type: ignore[assignment]
                elif inner == 3 and inner_wire == 0:
                    length = inner_value                                   # type: ignore[assignment]
            yield kind, data[position:position + length]
            position += length


def _clean(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")
    text = text.replace("\u2028", "\n").replace("\u2029", "\n")
    return _CONTROL.sub("", text)


def text_of_iwa(data: bytes) -> tuple[list[str], list[tuple[int, str]], int]:
    """`(text runs, [(key, cell string)], records seen)` from one decompressed `.iwa`.

    Names (sheets, tables) are returned among the runs. The count is how the
    caller knows a member parsed at all: a member with records but no text is a
    picture-only slide, a member that yields no records is not an IWA stream.
    """
    runs: list[str] = []
    cells: list[tuple[int, str]] = []
    seen = 0
    for kind, payload in iwa_records(data):
        seen += 1
        if kind == _STORAGE:
            for number, wire, value in _fields(payload):
                if number == 3 and wire == 2:
                    runs.append(_clean(value))                             # type: ignore[arg-type]
        elif kind == _DATA_LIST:
            list_kind, entries = 0, []
            for number, wire, value in _fields(payload):
                if number == 1 and wire == 0:
                    list_kind = value                                      # type: ignore[assignment]
                elif number == 3 and wire == 2:
                    entries.append(value)
            if list_kind != 1:                       # strings only; not formats or formulas
                continue
            for entry in entries:
                key, string = 0, None
                for number, wire, value in _fields(entry):                 # type: ignore[arg-type]
                    if number == 1 and wire == 0:
                        key = value                                        # type: ignore[assignment]
                    elif number == 3 and wire == 2:
                        string = value
                if string is not None:
                    cells.append((key, _clean(string)))                    # type: ignore[arg-type]
        elif kind == _TABLE_MODEL:
            for number, wire, value in _fields(payload):
                if number == 2 and wire == 2:
                    runs.append(_clean(value))                             # type: ignore[arg-type]
        elif kind == _SHEET:
            for number, wire, value in _fields(payload):
                if number == 1 and wire == 2:
                    runs.append(_clean(value))                             # type: ignore[arg-type]
    return runs, cells, seen


def _wanted(name: str) -> bool:
    if not name.endswith(".iwa") or not name.startswith("Index/"):
        return False
    return not any(marker in name for marker in _SKIP_MEMBERS)


def _sequence(name: str) -> tuple[int, str]:
    """Document first, then the rest by name; a slide's number is in its name."""
    return (0 if name == "Index/Document.iwa" else 1, name)


class IWorkExtractor:
    """`.pages`, `.numbers` and `.key`. `.iwa` inside a ZIP; iWork '09 falls back."""

    name = "iwork"
    extensions = frozenset({".pages", ".numbers", ".key"})
    reads_externally = False
    #: The LibreOffice routes in `config/extractors.toml` stay behind this reader
    #: for what it cannot read (iWork '09 packages, encrypted files).
    falls_back_to_converter = True
    #: No `requires`: a ZIP and a page of Snappy are the standard library and this
    #: file, so there is nothing that can be missing.

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        """Text runs and table strings of a modern iWork file, read in-process.

        Anything the reader cannot vouch for (iWork '09, encrypted, damaged) is
        handed to `fall_back` - the LibreOffice route, or `ERR_FILE_CORRUPT`
        when none is on. A locked file is `ERR_FILE_LOCKED`. Reads the zip only.
        """
        try:
            body, segments = self._read(path)
        except LegacyOfficeUnreadable as exc:
            log.debug("iwork reader declined {}: {}", path.name, exc)
            yield from fall_back(path, "extract.iwork", str(exc))
            return
        except PermissionError as exc:
            raise_error("ERR_FILE_LOCKED", "extract.iwork", path=str(path), details=str(exc))
            return

        builder = DocumentBuilder(path)
        builder.meta["format"] = "iwork"
        builder.meta["read_by"] = "in-process"
        if body:
            builder.add(body)
        for label, text in segments:
            builder.add(text, label=label, prefix_label=bool(label))
        yield builder.build()

    def _read(self, path: Path) -> tuple[str, list[tuple[Optional[str], str]]]:
        try:
            archive = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as exc:
            if isinstance(exc, PermissionError):
                raise
            raise LegacyOfficeUnreadable(f"not a readable iWork archive: {exc}") from exc

        with archive:
            members = sorted((n for n in archive.namelist() if _wanted(n)), key=_sequence)
            if not members:
                raise LegacyOfficeUnreadable("no Index/*.iwa members - an iWork '09 package or not iWork")

            runs: list[str] = []
            cells: list[str] = []
            seen = 0
            try:
                for name in members:
                    member_runs, member_cells, count = text_of_iwa(
                        decompress_iwa(archive.read(name)))
                    seen += count
                    runs.extend(member_runs)
                    # **Keys are per table, not per file**: every table's list
                    # numbers its strings from 1, so a dictionary keyed on the key
                    # alone kept the first table's strings and dropped the rest -
                    # the recall check against LibreOffice caught it at 74%.
                    # Ascending key within one list is creation order, which is
                    # close to reading order.
                    cells.extend(t for _k, t in sorted(member_cells, key=lambda kv: kv[0]))
            except (ValueError, IndexError, KeyError, zipfile.BadZipFile, RuntimeError) as exc:
                # RuntimeError: a password-protected member. All of them mean
                # "cannot vouch for this", which is what the caller needs.
                raise LegacyOfficeUnreadable(f"IWA parse failed: {exc}") from exc

        if not seen:
            raise LegacyOfficeUnreadable("no protobuf records were found in any IWA member")

        # Adjacent duplicates only: an outline repeats a heading, but the same
        # word on two different slides is two different facts.
        lines: list[str] = []
        for run in runs:
            for line in run.split("\n"):
                line = line.strip()
                if line and (not lines or lines[-1] != line):
                    lines.append(line)
        body = "\n".join(lines)

        segments: list[tuple[Optional[str], str]] = []
        if cells:
            table = "\n".join(t.strip() for t in cells if t.strip())
            segments.append(("Cells", table))
        return body, segments


register(IWorkExtractor())
