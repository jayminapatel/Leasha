r"""Legacy Word: .doc, read in-process with `olefile`. No LibreOffice needed.

Layer: L2

**Why this exists (owner instruction, 2026-09-20).** A `.doc` used to cost a
LibreOffice start-up - 5 to 10 seconds - for what is, structurally, a short walk:
an OLE2 container with a `WordDocument` stream (the File Information Block and
the text) and a `0Table` or `1Table` stream holding the *piece table*
([MS-DOC] 2.4.1, 2.9.177) that says where each run of characters lives. `olefile`
was already a dependency; nothing new is installed.

**The walk.**

1. The FIB (`WordDocument` offset 0) gives `nFib`, the encryption flag, which
   table stream is current, the character counts of each story (`ccpText`
   for the body, then footnotes, headers, comments, endnotes, text boxes) and
   the position of the `Clx` in the table stream.
2. The `Clx` holds a `PlcPcd`: n+1 character positions and n piece descriptors.
   Each piece is either **compressed** (one byte per character, Windows-1252)
   or UTF-16LE, decided by bit 30 of its file offset. A document that was
   fast-saved has its text in *many* pieces in editing order, which is why
   reading the stream straight through - what a strings-style reader does -
   returns text the author deleted and out of order.
3. Field codes (`HYPERLINK "..."`, `PAGE`, `TOC \o`) sit between `0x13` and
   `0x14`; the visible result sits between `0x14` and `0x15`. Only results are
   kept, so a link is indexed by its words rather than its URL syntax.
4. Table cell and row marks (`0x07`) become tabs and line ends, the same shape
   `DocxExtractor` gives a table, so a cell stays beside its column header.

**What it refuses.** Encrypted or obfuscated documents, Word 6 and 95 files
(`nFib` below 0xC1: a different structure with no piece table), a `Clx` that
does not add up, and anything that is not an OLE2 container - a Word 2007 file,
an RTF or an HTML page renamed `.doc`. Each raises `LegacyOfficeUnreadable` and
the extractor hands the file to the LibreOffice route instead; see
`legacy_office.fall_back`. Headers and footers are not indexed: they are the
same boilerplate on every page.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import Iterable, Optional

from app.core.errors import raise_error
from app.core.format_health import Requirement
from app.core.logging import logger
from app.extract.base import Document, DocumentBuilder, normalise_whitespace, register
from app.extract.legacy_office import LegacyOfficeUnreadable, clean_text, fall_back

__all__ = ["DocExtractor", "read_doc"]

log = logger.bind(component="extract.doc")

_WORD_MAGIC = 0xA5EC
#: Word 97 is nFib 0x00C1. Earlier versions have no piece table to read.
_MIN_NFIB = 0x00C1
_FLAG_ENCRYPTED = 0x0100
_FLAG_OBFUSCATED = 0x8000
_FLAG_TABLE_1 = 0x0200

#: Guard rails on untrusted counts.
_MAX_PIECES = 5_000_000
_MAX_CHARS = 200_000_000

#: A field's opening, separator and closing marks.
_FIELD_BEGIN, _FIELD_SEP, _FIELD_END = "\x13", "\x14", "\x15"

_ROW_END = re.compile("\x07\x07")


def _fib(word: bytes) -> dict[str, int]:
    """The handful of FIB fields this reader needs, validated."""
    if len(word) < 0x60:
        raise LegacyOfficeUnreadable("the WordDocument stream is too short")
    magic, n_fib = struct.unpack_from("<HH", word, 0)
    if magic != _WORD_MAGIC:
        raise LegacyOfficeUnreadable("not a Word binary (bad FIB signature)")
    if n_fib < _MIN_NFIB:
        raise LegacyOfficeUnreadable(f"Word 6/95 layout (nFib 0x{n_fib:04X})")
    (flags,) = struct.unpack_from("<H", word, 0x0A)
    if flags & (_FLAG_ENCRYPTED | _FLAG_OBFUSCATED):
        raise LegacyOfficeUnreadable("the document is encrypted")

    position = 32
    (csw,) = struct.unpack_from("<H", word, position)
    position += 2 + csw * 2
    (cslw,) = struct.unpack_from("<H", word, position)
    lw_start = position + 2
    if cslw < 11:
        raise LegacyOfficeUnreadable("FibRgLw97 is too short")
    lw = struct.unpack_from("<11I", word, lw_start)
    position = lw_start + cslw * 4
    (cb_fc_lcb,) = struct.unpack_from("<H", word, position)
    pairs_start = position + 2
    if cb_fc_lcb < 34:
        raise LegacyOfficeUnreadable("FibRgFcLcb is too short to hold a Clx")
    fc_clx, lcb_clx = struct.unpack_from("<II", word, pairs_start + 33 * 8)
    return {
        "table1": 1 if flags & _FLAG_TABLE_1 else 0,
        "ccpText": lw[3], "ccpFtn": lw[4], "ccpHdd": lw[5],
        "ccpAtn": lw[7], "ccpEdn": lw[8], "ccpTxbx": lw[9], "ccpHdrTxbx": lw[10],
        "fcClx": fc_clx, "lcbClx": lcb_clx,
    }


def _pieces(table: bytes, fc_clx: int, lcb_clx: int) -> list[tuple[int, int, int, bool]]:
    """`(cp_start, cp_end, byte_offset, compressed)` per piece, in text order."""
    end = fc_clx + lcb_clx
    if lcb_clx < 5 or end > len(table):
        raise LegacyOfficeUnreadable("the Clx is outside the table stream")
    cursor = fc_clx
    while cursor < end:
        marker = table[cursor]
        if marker == 0x01:                                # Prc: skip its property list
            (size,) = struct.unpack_from("<H", table, cursor + 1)
            cursor += 3 + size
        elif marker == 0x02:                              # Pcdt: the piece table
            (plc_size,) = struct.unpack_from("<I", table, cursor + 1)
            plc = cursor + 5
            if plc + plc_size > end or plc_size < 4 or (plc_size - 4) % 12:
                raise LegacyOfficeUnreadable("the piece table has an impossible size")
            count = (plc_size - 4) // 12
            if count > _MAX_PIECES:
                raise LegacyOfficeUnreadable("the piece table is implausibly large")
            cps = struct.unpack_from(f"<{count + 1}I", table, plc)
            descriptors = plc + (count + 1) * 4
            found: list[tuple[int, int, int, bool]] = []
            for index in range(count):
                _flags, fc, _prm = struct.unpack_from("<HIH", table, descriptors + index * 8)
                compressed = bool(fc & 0x40000000)
                offset = (fc & 0x3FFFFFFF) // 2 if compressed else fc & 0x3FFFFFFF
                if cps[index + 1] < cps[index]:
                    raise LegacyOfficeUnreadable("piece boundaries run backwards")
                found.append((cps[index], cps[index + 1], offset, compressed))
            return found
        else:
            raise LegacyOfficeUnreadable(f"unknown Clx entry 0x{marker:02X}")
    raise LegacyOfficeUnreadable("the Clx holds no piece table")


def _decode(word: bytes, pieces: list[tuple[int, int, int, bool]]) -> str:
    parts: list[str] = []
    total = 0
    for cp_start, cp_end, offset, compressed in pieces:
        length = cp_end - cp_start
        total += length
        if total > _MAX_CHARS:
            raise LegacyOfficeUnreadable("the text is implausibly long")
        width = 1 if compressed else 2
        if offset + length * width > len(word):
            # The piece table promises characters the stream does not hold: a
            # file cut short. Returning what is there would be silent loss.
            raise LegacyOfficeUnreadable("a piece runs past the end of the WordDocument stream")
        if compressed:
            chunk = word[offset:offset + length]
            parts.append(chunk.decode("cp1252", "replace"))
        else:
            chunk = word[offset:offset + length * 2]
            parts.append(chunk.decode("utf-16-le", "replace"))
    return "".join(parts)


def _strip_fields(text: str) -> str:
    """Keep field results, drop field codes. Nested fields nest.

    A field with no separator is all code (a `PAGE` in a footer, a `SEQ`
    counter) and is dropped whole. Unbalanced marks - a document damaged or
    cut mid-field - are tolerated rather than raised, since the text around them
    is still what somebody will search for.
    """
    if _FIELD_BEGIN not in text:
        return text
    out: list[str] = []
    #: One entry per open field: True while inside its code, False in its result.
    stack: list[bool] = []
    for char in text:
        if char == _FIELD_BEGIN:
            stack.append(True)
        elif char == _FIELD_SEP:
            if stack:
                stack[-1] = False
        elif char == _FIELD_END:
            if stack:
                stack.pop()
        elif not any(stack):
            out.append(char)
    return "".join(out)


def _story_text(raw: str) -> str:
    """One story (body, footnotes...) as plain lines."""
    text = _strip_fields(raw)
    # Word ends a table row with a cell mark *and* a row mark.
    text = _ROW_END.sub("\n", text)
    text = text.replace("\x07", "\t")
    text = clean_text(text)
    # Anchors for pictures, footnote and comment references.
    text = text.replace("\x01", "").replace("\x02", "").replace("\x05", "").replace("\x08", "")
    return text


def read_doc(word: bytes, table_for: dict[int, bytes]) -> dict[str, str]:
    """`{story name: text}` for the body and the stories worth indexing."""
    fib = _fib(word)
    table = table_for.get(fib["table1"])
    if table is None:
        table = table_for.get(1 - fib["table1"])
    if table is None:
        raise LegacyOfficeUnreadable("no table stream")

    pieces = _pieces(table, fib["fcClx"], fib["lcbClx"])
    # Completeness: the FIB states how many characters each story holds, and the
    # piece table how many exist. The stories are contiguous and Word writes one
    # closing mark after them, so the two totals agree to within one. Fewer means
    # text is missing from what would be read; more means text sits beyond every
    # story this reader knows, which it would not read. Either way the file goes
    # to the converter (measured: 287 of 287 real files agree).
    declared = sum(fib[name] for name in ("ccpText", "ccpFtn", "ccpHdd", "ccpAtn",
                                          "ccpEdn", "ccpTxbx", "ccpHdrTxbx"))
    available = pieces[-1][1] if pieces else 0
    if not 0 <= available - declared <= 1:
        raise LegacyOfficeUnreadable(
            f"the FIB declares {declared} characters and the piece table holds {available}")
    everything = _decode(word, pieces)

    # The stories run back to back in character-position space, in this order.
    # Headers (`ccpHdd`) are counted but not kept.
    order = (("body", fib["ccpText"]), ("footnotes", fib["ccpFtn"]),
             ("headers", fib["ccpHdd"]), ("comments", fib["ccpAtn"]),
             ("endnotes", fib["ccpEdn"]), ("textboxes", fib["ccpTxbx"]),
             ("header textboxes", fib["ccpHdrTxbx"]))
    stories: dict[str, str] = {}
    position = 0
    for name, count in order:
        segment = everything[position:position + count]
        position += count
        if name in ("headers", "header textboxes"):
            continue
        cleaned = _story_text(segment)
        if cleaned.strip():
            stories[name] = cleaned
    if "body" not in stories and not stories:
        # A file whose counts add up to no text at all is not evidence of an
        # empty document - it is evidence the counts were misread.
        if fib["ccpText"] == 0 and len(everything) > 2:
            raise LegacyOfficeUnreadable("the story lengths do not describe the text")
    return stories


class DocExtractor:
    """Word 97-2003 documents. `.docx` belongs to `DocxExtractor`."""

    name = "doc"
    extensions = frozenset({".doc", ".dot"})
    reads_externally = False
    #: See `PptExtractor.falls_back_to_converter`.
    falls_back_to_converter = True
    requires = (Requirement("olefile", "olefile",
                            provides="legacy Word document text", hard=True),)

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        try:
            stories = self._read(path)
        except LegacyOfficeUnreadable as exc:
            log.debug("doc reader declined {}: {}", path.name, exc)
            yield from fall_back(path, "extract.doc", str(exc))
            return
        except PermissionError as exc:
            raise_error("ERR_FILE_LOCKED", "extract.doc", path=str(path), details=str(exc))
            return

        builder = DocumentBuilder(path)
        builder.meta["format"] = "word-97"
        builder.meta["read_by"] = "olefile"
        for name, text in stories.items():
            body = normalise_whitespace(text)
            if name == "body":
                builder.add(body)
            else:
                builder.add(body, label=name.capitalize(), prefix_label=True)
        yield builder.build()

    @staticmethod
    def _read(path: Path) -> dict[str, str]:
        import olefile

        table_for: dict[int, bytes] = {}
        try:
            if not olefile.isOleFile(str(path)):
                raise LegacyOfficeUnreadable("not an OLE2 container")
            with olefile.OleFileIO(str(path)) as ole:
                if not ole.exists("WordDocument"):
                    raise LegacyOfficeUnreadable("no 'WordDocument' stream")
                word = ole.openstream("WordDocument").read()
                for number in (0, 1):
                    name = f"{number}Table"
                    if ole.exists(name):
                        table_for[number] = ole.openstream(name).read()
        except LegacyOfficeUnreadable:
            raise
        except PermissionError:
            raise
        except Exception as exc:                          # noqa: BLE001 - damaged file
            raise LegacyOfficeUnreadable(f"{type(exc).__name__}: {exc}") from exc

        try:
            return read_doc(word, table_for)
        except LegacyOfficeUnreadable:
            raise
        except (struct.error, IndexError, ValueError, OverflowError) as exc:
            raise LegacyOfficeUnreadable(f"{type(exc).__name__}: {exc}") from exc


register(DocExtractor())
