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

5. WordArt - text a shape *draws* rather than holds - is a `gtextUNICODE`
   property of the Escher drawing in the table stream (`fcDggInfo`), in no text
   story at all, and is read too: on real files it is the "DRAFT" watermark and
   the banner somebody titled the front page with.
6. Embedded Word documents and PowerPoint decks are read by the reader that
   owns that format. An embedded object whose words this reader cannot reach -
   a spreadsheet, a Visio drawing, a packaged file - is **counted and reported,
   not declined**: LibreOffice's own text export does not hold that text either
   (measured), so the document is indexed at full speed with its own words
   whole, `embedded_unread` in its metadata says what was left, and the run
   summary names how many files and of what kind.

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
from app.extract.legacy_office import (
    ESCHER_OPT, LegacyOfficeUnreadable, clean_text, fall_back, note_unread_embedded,
    records, shape_property_text)

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
_MAX_DEPTH = 24

#: `fcDggInfo`/`lcbDggInfo`, the Escher drawing, is pair 50 of `FibRgFcLcb97`
#: ([MS-DOC] 2.5.10). Measured on 388 readable real `.doc`: 311 have an
#: `OfficeArtDggContainer` (0xF000) at pair 50 in the table stream and no other
#: pair ever points at one.
_FC_DGG_INFO = 50

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

    # FibBase is a fixed 32 bytes ([MS-DOC] 2.5.2); the variable-length arrays
    # that follow each start with their own count, which is why the walk below
    # reads a count and skips rather than using fixed offsets.
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
    # fcClx/lcbClx are pair 33 of FibRgFcLcb97 ([MS-DOC] 2.5.5); each pair is
    # two 4-byte fields, hence `* 8`. `lw[3]` below is ccpText, the body's
    # character count, and `lw[4..10]` the other stories in FibRgLw97 order.
    fc_clx, lcb_clx = struct.unpack_from("<II", word, pairs_start + 33 * 8)
    fc_dgg, lcb_dgg = (0, 0)
    if cb_fc_lcb > _FC_DGG_INFO:
        fc_dgg, lcb_dgg = struct.unpack_from("<II", word, pairs_start + _FC_DGG_INFO * 8)
    return {
        "table1": 1 if flags & _FLAG_TABLE_1 else 0,
        "ccpText": lw[3], "ccpFtn": lw[4], "ccpHdd": lw[5],
        "ccpAtn": lw[7], "ccpEdn": lw[8], "ccpTxbx": lw[9], "ccpHdrTxbx": lw[10],
        "fcClx": fc_clx, "lcbClx": lcb_clx,
        "fcDggInfo": fc_dgg, "lcbDggInfo": lcb_dgg,
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


def _shape_text(table: bytes, start: int, end: int, depth: int = 0) -> list[str]:
    """WordArt text under one Escher container, however deeply grouped."""
    if depth > _MAX_DEPTH:
        raise LegacyOfficeUnreadable("the drawing is nested too deeply")
    found: list[str] = []
    for version, instance, record_type, body_start, body_end in records(table, start, end):
        if record_type in ESCHER_OPT:
            found.extend(shape_property_text(table, body_start, body_end, instance))
        elif version == 0xF:
            found.extend(_shape_text(table, body_start, body_end, depth + 1))
    return found


def _drawing_text(table: bytes, fc: int, lcb: int) -> list[str]:
    r"""The drawing layer's own words: WordArt, in the table stream.

    `OfficeArtContent` ([MS-DOC] 2.9.172) is an `OfficeArtDggContainer` followed
    by one or more Word drawings, and each of *those* is a single `dgglbl` byte
    (main document or header) in front of an `OfficeArtDgContainer` - the stray
    byte is why this walks the span itself instead of handing it to `records`.

    Text a shape *types* is in the textbox story and already read. Text a shape
    *draws* - a Fontwork or WordArt banner, which is how a 2003 front page was
    usually titled - is a property of the shape and is in no story at all. A
    span that does not parse as Escher raises rather than returning what it got:
    a drawing this reader cannot walk may hold words, and guessing it does not
    is the one unacceptable outcome.
    """
    if lcb <= 0:
        return []
    end = fc + lcb
    if fc < 0 or end > len(table):
        raise LegacyOfficeUnreadable("the drawing is outside the table stream")
    found: list[str] = []
    cursor, first = fc, True
    while cursor + 8 <= end:
        if not first:
            cursor += 1                                   # dgglbl
            if cursor + 8 > end:
                break
        ver_instance, _record_type, length = struct.unpack_from("<HHI", table, cursor)
        body_start = cursor + 8
        body_end = body_start + length
        if ver_instance & 0xF != 0xF or body_end > end:
            raise LegacyOfficeUnreadable("the drawing's records do not add up")
        found.extend(_shape_text(table, body_start, body_end))
        cursor, first = body_end, False
    return found


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

    drawing: list[str] = []
    for raw in _drawing_text(table, fib["fcDggInfo"], fib["lcbDggInfo"]):
        text = clean_text(raw).strip()
        # A grouped shape's property table can be written twice; the same banner
        # twice in the index is noise, not evidence.
        if text and text not in drawing:
            drawing.append(text)
    if drawing:
        stories["drawings"] = "\n".join(drawing)
    return stories


#: What an embedded object's own streams say it is. The order matters: a
#: storage holding a `WordDocument` is a Word document whatever else is in it.
_EMBEDDED_KINDS = (
    ("word", ("WordDocument",)),
    ("powerpoint", ("PowerPoint Document",)),
    ("excel", ("Workbook", "Book")),
    ("visio", ("VisioDocument",)),
    ("package", ("\x01Ole10Native",)),
)
#: Objects with no words for a person to search for. An equation is MathType's
#: own layout bytes (LibreOffice does not put them in its text either), and an
#: ActiveX control is a widget's state. Neither is silently lost text.
_EMBEDDED_WORDLESS = ("Equation Native", "\x03OCXDATA", "\x03OCXNAME")
#: Streams every embedded object has; they say nothing about what it holds.
#: Every Word document carries an `ObjectPool` storage whether or not anything
#: is in it, so its name alone says nothing either.
_EMBEDDED_WRAPPER = ("\x01CompObj", "\x01Ole", "\x03ObjInfo", "\x05SummaryInformation",
                     "\x05DocumentSummaryInformation", "\x02OlePres000", "\x02OlePres001",
                     "\x03PRINT", "\x03EPRINT", "\x03META", "\x03PIC", "\x01CompObjStream",
                     "ObjectPool")


def _embedded_kind(streams: set[str]) -> Optional[str]:
    """What kind of thing an `ObjectPool` storage holds, or `None` for no words."""
    for kind, markers in _EMBEDDED_KINDS:
        if any(marker in streams for marker in markers):
            return kind
    if any(marker in streams for marker in _EMBEDDED_WORDLESS):
        return None
    if all(name in _EMBEDDED_WRAPPER for name in streams):
        return None
    return "unknown"


#: The streams each readable kind of embedded object needs.
_EMBEDDED_WANTED = {
    "word": ("WordDocument", "0Table", "1Table"),
    "powerpoint": ("PowerPoint Document", "Current User"),
}


def _embedded_streams(ole) -> tuple[list[tuple[str, dict[str, bytes]]], list[str]]:
    """`([(kind, {stream: bytes})], [kind of each object left unread])`.

    An embedded Word document or PowerPoint deck is read by the reader that
    owns that format. An object this reader cannot reach into - a spreadsheet,
    a Visio drawing, a packaged file, an unrecognised object, an embedded
    document with embedded objects of its own - is **named and counted, not
    declined**: measured on real files, LibreOffice's own text export does not
    hold that text either, so handing the document over would cost 5 to 10
    seconds and return the same words. See `legacy_office.note_unread_embedded`
    for the measurement the decision rests on.
    """
    objects: dict[str, set[str]] = {}
    nested: set[str] = set()
    for entry in ole.listdir(streams=True, storages=True):
        if len(entry) >= 3 and entry[0] == "ObjectPool":
            objects.setdefault(entry[1], set()).add(entry[2])
            if len(entry) >= 5 and entry[2] == "ObjectPool":
                # An embedded document with embedded objects of its own. This
                # reader goes one level down, so say so rather than read the
                # outer one and call the document complete.
                nested.add(entry[1])
    found: list[tuple[str, dict[str, bytes]]] = []
    unread: list[str] = []
    for name, streams in sorted(objects.items()):
        kind = "unknown" if name in nested else _embedded_kind(streams)
        if kind is None:
            continue
        if kind not in _EMBEDDED_WANTED:
            unread.append(kind)
            continue
        found.append((kind, {
            stream: ole.openstream(["ObjectPool", name, stream]).read()
            for stream in _EMBEDDED_WANTED[kind] if stream in streams}))
    return found, unread


def _embedded_text(embedded: list[tuple[str, dict[str, bytes]]]) -> tuple[str, list[str]]:
    """`(the words inside embedded Word and PowerPoint objects, kinds left unread)`.

    An embedded document is the same format as its host, so the same reader
    reads it. One that declines is counted like any other embedded object this
    reader cannot reach: the host's own text is still whole, and LibreOffice
    has no more of the object's text than this does.
    """
    parts: list[str] = []
    unread: list[str] = []
    for kind, streams in embedded:
        try:
            if kind == "word":
                tables = {number: streams[f"{number}Table"]
                          for number in (0, 1) if f"{number}Table" in streams}
                inner = read_doc(streams.get("WordDocument", b""), tables)
                parts.extend(text for text in inner.values() if text.strip())
            else:
                from app.extract.ppt import read_ppt

                texts, notes, footers = read_ppt(streams.get("PowerPoint Document", b""),
                                                 streams.get("Current User", b""))
                parts.extend(text for text in texts if text.strip())
                parts.extend(text for _slide, text in notes if text.strip())
                parts.extend(footers)
        except (LegacyOfficeUnreadable, struct.error, IndexError,
                ValueError, OverflowError, RecursionError):
            unread.append(kind)
    return "\n".join(parts).strip(), unread


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
        """Body, footnotes, comments, text boxes and WordArt of a Word 97-2003 file.

        A file this reader will not vouch for (encrypted, Word 6/95, damaged,
        not OLE2) goes to the LibreOffice route via `fall_back`, which raises
        `ERR_FILE_CORRUPT` if no converter is on. A locked file is
        `ERR_FILE_LOCKED`. Reads the streams only; never writes beside the file.
        """
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
        # Out of band, not a story: the *absence* is metadata, not text to index.
        unread = stories.pop("_unread embedded", "")
        if unread:
            builder.meta["embedded_unread"] = unread
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
        embedded: list[tuple[str, dict[str, bytes]]] = []
        unread_kinds: list[str] = []
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
                embedded, unread_kinds = _embedded_streams(ole)
        except LegacyOfficeUnreadable:
            raise
        except PermissionError:
            raise
        except Exception as exc:                          # noqa: BLE001 - damaged file
            raise LegacyOfficeUnreadable(f"{type(exc).__name__}: {exc}") from exc

        try:
            stories = read_doc(word, table_for)
            inner, failed = _embedded_text(embedded)
            if inner:
                stories["embedded objects"] = inner
            unread = [*unread_kinds, *failed]
            if unread:
                # Counted, not declined: the file is indexed with its own words
                # whole, and the run says what was left inside the objects.
                note_unread_embedded(path, unread)
                stories["_unread embedded"] = ", ".join(sorted(set(unread)))
            return stories
        except LegacyOfficeUnreadable:
            raise
        except (struct.error, IndexError, ValueError, OverflowError) as exc:
            raise LegacyOfficeUnreadable(f"{type(exc).__name__}: {exc}") from exc


register(DocExtractor())
