r"""Kindle e-books: `.mobi`, `.azw`, `.azw3`. No dependency.

Layer: L2

390 `.mobi` files on the measured disk, every one **unindexed** - no extractor and
no converter claimed the type, so they were not even skipped with a reason; they
were invisible. The owner's rule is a library where one exists and the standard
library where it does not, and this format is small enough for the second.

**The container.** A MOBI file is a PalmDB: a 78-byte header, a table of records,
then the records. Record 0 is a 16-byte PalmDOC header (compression, text length,
how many text records follow, encryption) and then the MOBI header (`MOBI`, text
encoding, offsets to the title and the extra-data flags). Records 1..N are the book
text, compressed with PalmDOC's LZ77 - a byte-oriented scheme small enough to be
twenty lines - and the text itself is HTML. Each record can carry *trailing
entries* after the compressed bytes (multibyte overlap, indexing data); the MOBI
header's extra-data flags say which, and getting this wrong corrupts the last few
bytes of every record, which is why it is done exactly as the flags say.

**What it does not read**, and says so rather than guessing: HUFF/CDIC compression
(compression 17480, used by some older Mobipocket files) and anything encrypted
(DRM). Both raise the ordinary corrupt-file error naming the reason - there is no
converter for `.mobi` to fall back to.

**Kindle Format 8** (`.azw3`) files carry a Mobipocket 6 copy of the book in front
of the KF8 section, with the same PalmDOC records, so the text is read from there.
The text-record count in the PalmDOC header stops the read before the KF8 half.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Iterable

from app.core.errors import raise_error
from app.core.logging import logger
from app.extract.base import Document, DocumentBuilder, register
from app.extract.ebook import MAX_TEXT_CHARS, text_from_xhtml
from app.extract.legacy_office import LegacyOfficeUnreadable, fall_back

__all__ = ["MobiExtractor", "palmdoc_decompress", "book_text"]

log = logger.bind(component="extract.mobi")

MAX_FILE_BYTES = 256 * 1024 * 1024

_COMPRESSION_NONE, _COMPRESSION_PALMDOC, _COMPRESSION_HUFF = 1, 2, 17480


def palmdoc_decompress(data: bytes) -> bytes:
    """PalmDOC LZ77. Bytes 0x01-0x08 are a literal run, 0x09-0x7F themselves,
    0x80-0xBF a back-reference (11-bit distance, 3-bit length), 0xC0-0xFF a space
    followed by the byte with its top bit cleared."""
    out = bytearray()
    i, n = 0, len(data)
    while i < n:
        c = data[i]
        i += 1
        if 1 <= c <= 8:
            out += data[i:i + c]
            i += c
        elif c < 0x80:
            out.append(c)
        elif c >= 0xC0:
            out.append(0x20)
            out.append(c ^ 0x80)
        else:
            if i >= n:
                break
            pair = (c << 8) | data[i]
            i += 1
            distance = (pair >> 3) & 0x7FF
            length = (pair & 7) + 3
            if distance == 0 or distance > len(out):
                raise ValueError("PalmDOC back-reference points before the text")
            start = len(out) - distance
            for k in range(length):
                out.append(out[start + k])
    return bytes(out)


def _trailing_size(record: bytes, flags: int) -> int:
    """Bytes of trailing entries at the end of one text record.

    Each set bit above bit 0 is a backward-varint-sized entry; bit 0 is the
    multibyte-overlap byte. Read from the end of the record, in bit order.
    """
    size = len(record)
    total = 0
    bits = flags >> 1
    while bits:
        if bits & 1:
            position = size - total
            value, shift = 0, 0
            while True:
                position -= 1
                if position < 0:
                    return total
                byte = record[position]
                value |= (byte & 0x7F) << shift
                shift += 7
                # A backward varint ends at the byte with its high bit set; four
                # bytes (28 bits) is the format's maximum, so stop there on damage.
                if byte & 0x80 or shift >= 28:
                    break
            total += value
        bits >>= 1
    if flags & 1 and size - total > 0:
        total += (record[size - total - 1] & 3) + 1
    return total


def book_text(data: bytes) -> tuple[str, dict[str, str]]:
    """`(text, metadata)` from a whole `.mobi`. Raises `LegacyOfficeUnreadable`."""
    if len(data) < 90 or data[60:68] not in (b"BOOKMOBI", b"TEXtREAd"):
        raise LegacyOfficeUnreadable("not a PalmDB Mobipocket file")
    (record_count,) = struct.unpack_from(">H", data, 76)
    if record_count < 2 or 78 + 8 * record_count > len(data):
        raise LegacyOfficeUnreadable("the record table is not valid")
    offsets = [struct.unpack_from(">I", data, 78 + 8 * i)[0] for i in range(record_count)]
    offsets.append(len(data))
    if any(b < a for a, b in zip(offsets, offsets[1:])) or offsets[-2] > len(data):
        raise LegacyOfficeUnreadable("record offsets go backwards")

    first = data[offsets[0]:offsets[1]]
    if len(first) < 16:
        raise LegacyOfficeUnreadable("record 0 is too short")
    compression, _unused, _text_length, text_records, _record_size, encryption = \
        struct.unpack_from(">HHIHHH", first, 0)
    if encryption:
        raise LegacyOfficeUnreadable("the book is encrypted (DRM)")
    if compression == _COMPRESSION_HUFF:
        raise LegacyOfficeUnreadable("HUFF/CDIC compression is not supported")
    if compression not in (_COMPRESSION_NONE, _COMPRESSION_PALMDOC):
        raise LegacyOfficeUnreadable(f"unknown compression {compression}")

    encoding, flags, title = "cp1252", 0, ""
    if first[16:20] == b"MOBI":
        (header_length,) = struct.unpack_from(">I", first, 20)
        (code_page,) = struct.unpack_from(">I", first, 28)
        encoding = "utf-8" if code_page == 65001 else "cp1252"
        # The extra-data flags live at MOBI header offset 0xE2 (0xF2 from the
        # record start, after the 16-byte PalmDOC header) and exist only when
        # the header is long enough to hold them (Mobipocket 6, length >= 0xE4).
        if header_length >= 0xE4 and len(first) >= 0xF4:
            (flags,) = struct.unpack_from(">H", first, 0xF2)
        # Full name offset and length: MOBI header offsets 0x54 and 0x58, i.e.
        # 84 and 88 from the record start.
        if len(first) >= 92:
            name_offset, name_length = struct.unpack_from(">II", first, 84)
            if 0 < name_length < 1024 and name_offset + name_length <= len(first):
                title = first[name_offset:name_offset + name_length].decode(encoding, "replace")

    text_records = min(text_records, record_count - 1)
    pieces: list[bytes] = []
    total = 0
    for index in range(1, text_records + 1):
        record = data[offsets[index]:offsets[index + 1]]
        if flags:
            record = record[:len(record) - _trailing_size(record, flags)]
        if compression == _COMPRESSION_PALMDOC:
            record = palmdoc_decompress(record)
        pieces.append(record)
        total += len(record)
        if total > MAX_TEXT_CHARS * 8:                       # markup is most of it
            break

    html = b"".join(pieces).decode(encoding, "replace")
    if "<" not in html[:4096] and len(html) > 200 and html.count("\x00") > len(html) // 4:
        raise LegacyOfficeUnreadable("the decompressed text is not text")
    text = text_from_xhtml(html)
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
    return text, ({"title": title.strip()} if title.strip() else {})


class MobiExtractor:
    """`.mobi`, `.azw` and `.azw3` Kindle books."""

    name = "mobi"
    extensions = frozenset({".mobi", ".azw", ".azw3"})
    reads_externally = False
    #: No converter exists for these; `fall_back` therefore raises the ordinary
    #: corrupt-file error. Set so the rule "in-process first" is one rule.
    falls_back_to_converter = True

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        """One document: the title and the book's prose, capped at `MAX_TEXT_CHARS`.

        DRM, HUFF/CDIC compression, a damaged record table or an oversized file
        go to `fall_back`, which has no converter for these and so raises
        `ERR_FILE_CORRUPT` naming the reason. A locked file is `ERR_FILE_LOCKED`.
        Reads only.
        """
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                raise LegacyOfficeUnreadable("larger than any Kindle book")
            text, meta = book_text(path.read_bytes())
        except LegacyOfficeUnreadable as exc:
            log.debug("mobi reader declined {}: {}", path.name, exc)
            yield from fall_back(path, "extract.mobi", str(exc))
            return
        except ValueError as exc:
            yield from fall_back(path, "extract.mobi", str(exc))
            return
        except PermissionError as exc:
            raise_error("ERR_FILE_LOCKED", "extract.mobi", path=str(path), details=str(exc))
            return
        except OSError as exc:
            raise_error("ERR_FILE_CORRUPT", "extract.mobi", path=str(path), details=str(exc))
            return

        builder = DocumentBuilder(path)
        builder.meta.update({"format": "mobipocket", "read_by": "in-process", **meta})
        if meta.get("title"):
            builder.add(meta["title"])
        builder.add(text)
        yield builder.build()


register(MobiExtractor())
