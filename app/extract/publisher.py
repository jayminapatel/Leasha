r"""Microsoft Publisher `.pub`, read in-process with `olefile`.

Layer: L2

Non-negotiable 12 and the owner's instruction of 2026-09-20. `.pub` went through
LibreOffice, and **every `.pub` in the measured sample failed there** - the
converter asked for Writer's `txt:Text` export, Publisher opens in Draw, and Draw
has no text filter - so nothing was ever indexed from these files and each attempt
cost a LibreOffice start-up first.

**Where the words are.** A Publisher 2000-2010 file is an OLE2 compound file.
The stream `Quill/QuillSub/CONTENTS` is the "Quill" text engine's store: a
`CHNKINK ` header, a directory of four-letter chunks (`TEXT`, `STSH`, `FDPP`,
`FDPC` ...), and then the chunks. The `TEXT` chunk is *every story in the
publication, end to end, as UTF-16LE*, with `\r` between paragraphs. It is by far
the longest run of valid UTF-16 text in the stream - the other runs are font and
style names of a few dozen characters - so the reader finds that run rather than
parsing the directory, whose layout differs between Publisher versions and whose
only use here would be to point at the same bytes.

Measured against LibreOffice's own rendering of the same files (PDF, read back
with the PDF extractor), on eight real publications: 100% recall on all eight,
98-100% precision. Taking every run over 8 characters instead added the style
names and dropped precision to 56-84%, which is why it does not.

**It says no rather than guess.** No `Quill` stream, an 8-bit (Publisher 98) text
chunk that decodes to nothing sensible, or a longest run of fewer than 4
characters raises `LegacyOfficeUnreadable`, and `fall_back` gives the file to the
converter route if it is on. A publication that genuinely holds no text (all
pictures) is not a failure and yields nothing, which the caller records as
`ERR_NO_TEXT_LAYER`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from app.core.errors import raise_error
from app.core.format_health import Requirement
from app.core.logging import logger
from app.extract.base import Document, DocumentBuilder, normalise_whitespace, register
from app.extract.legacy_office import LegacyOfficeUnreadable, fall_back

__all__ = ["PublisherExtractor", "story_text"]

log = logger.bind(component="extract.publisher")

#: A Quill store is a few hundred kilobytes at most for a text-heavy brochure; a
#: stream past this is not one, and decoding it would be wasted memory.
MAX_QUILL_BYTES = 64 * 1024 * 1024

#: Printable UTF-16 plus the separators Quill writes: `\r` paragraph, `\x0b` line
#: break, `\x07` cell end, `\x0c` page or column break, tab. Private-use and
#: non-characters end a run, which is what keeps binary tables out of it.
_RUN = re.compile("[\x20-\x7e\xa0-\ud7ff\ue000-\ufdcf\ufdf0-\ufffd\r\n\x0b\x07\x0c\t]{4,}")


def story_text(quill: bytes) -> str:
    """The text of every story in a `Quill/QuillSub/CONTENTS` stream.

    Pure, so it is tested on bytes. Raises `LegacyOfficeUnreadable` when the
    stream does not look like a Quill store with a UTF-16 text chunk.
    """
    if not quill.startswith(b"CHNKINK"):
        raise LegacyOfficeUnreadable("the Quill stream has no CHNKINK header")
    text = quill[: len(quill) - (len(quill) % 2)].decode("utf-16le", "replace")
    longest = ""
    for match in _RUN.finditer(text):
        if len(match.group()) > len(longest):
            longest = match.group()
    if len(longest) < 4:
        raise LegacyOfficeUnreadable("no UTF-16 text chunk was found (an 8-bit Publisher 98 file?)")
    cleaned = (longest.replace("\r", "\n").replace("\x0b", "\n").replace("\x0c", "\n\n")
               .replace("\x07", "\t"))
    return normalise_whitespace(cleaned)


class PublisherExtractor:
    """Publisher 98-2010 publications. `.pubx` does not exist; `.pub` is always OLE."""

    name = "publisher"
    extensions = frozenset({".pub"})
    reads_externally = False
    #: The LibreOffice route in `config/extractors.toml` stays behind this reader.
    falls_back_to_converter = True
    requires = (Requirement("olefile", "olefile",
                            provides="Publisher story text", hard=True),)

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        """One document: every story's text from the Quill stream.

        No Quill stream, an 8-bit Publisher 98 chunk or an oversized stream
        goes to `fall_back` (LibreOffice to PDF, or `ERR_FILE_CORRUPT` when it
        is off). A locked file is `ERR_FILE_LOCKED`. Reads only.
        """
        try:
            text = self._read(path)
        except LegacyOfficeUnreadable as exc:
            log.debug("publisher reader declined {}: {}", path.name, exc)
            yield from fall_back(path, "extract.publisher", str(exc))
            return
        except PermissionError as exc:
            raise_error("ERR_FILE_LOCKED", "extract.publisher", path=str(path), details=str(exc))
            return

        builder = DocumentBuilder(path)
        builder.meta["format"] = "publisher"
        builder.meta["read_by"] = "olefile"
        builder.add(text)
        yield builder.build()

    def _read(self, path: Path) -> str:
        try:
            import olefile
        except ImportError as exc:
            raise LegacyOfficeUnreadable("olefile is not installed") from exc

        try:
            if not olefile.isOleFile(str(path)):
                raise LegacyOfficeUnreadable("not an OLE2 compound file")
            with olefile.OleFileIO(str(path)) as ole:
                stream = ["Quill", "QuillSub", "CONTENTS"]
                if not ole.exists(stream):
                    raise LegacyOfficeUnreadable("no Quill/QuillSub/CONTENTS stream")
                if ole.get_size(stream) > MAX_QUILL_BYTES:
                    raise LegacyOfficeUnreadable("the Quill stream is implausibly large")
                data = ole.openstream(stream).read()
        except PermissionError:
            raise
        except LegacyOfficeUnreadable:
            raise
        except Exception as exc:                          # noqa: BLE001 - olefile raises many kinds
            raise LegacyOfficeUnreadable(f"olefile could not open it: {exc}") from exc
        return story_text(data)


register(PublisherExtractor())
