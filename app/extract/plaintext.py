"""Plain text, Markdown, CSV and source code, with encoding detection.

Layer: L2

There is no reliable way to know a text file's encoding; there is only a ladder
of decreasingly likely guesses. The order matters:

1. **UTF-8 strict.** Correct for anything modern. A leading BOM is stripped, so
   the first word of a Notepad-saved file is not `\\ufeffDear`, which no query
   would ever match.
2. **cp1252.** The Windows default before UTF-8 won. Decodes most legacy files
   on this platform cleanly, and gets curly quotes, en dashes and `£` right.
3. **latin-1.** Cannot fail - every byte maps to something. That is precisely
   why it is last: it always "works", and is therefore only ever a fallback.
   Reaching it raises `ERR_ENCODING` as an `AUTO_FIX` warning: the file is
   indexed, possibly with mojibake, and the user is told rather than not.

Binary files with a text extension - a `.log` that is really a database, a `.txt`
that is a renamed image - are detected by their NUL bytes and skipped as
`ERR_NO_TEXT_LAYER`, because indexing their bytes as words would pollute the FTS
index with garbage that matches nothing and slows everything.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from app.core.errors import make_error, raise_error
from app.extract.base import Document, DocumentBuilder, normalise_whitespace, register

__all__ = ["PlainTextExtractor", "decode_bytes", "looks_binary", "TEXT_EXTENSIONS"]

TEXT_EXTENSIONS = frozenset(
    {
        ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv",
        ".json", ".yaml", ".yml", ".xml", ".ini", ".cfg", ".toml",
        ".py", ".js", ".ts", ".sql", ".ps1", ".bat", ".cmd", ".sh",
        ".c", ".h", ".cpp", ".cs", ".java", ".go", ".rs", ".rb", ".php",
        ".html", ".htm", ".css",
    }
)

#: Read for the binary sniff. Enough to catch a header without reading a 2GB log.
SNIFF_BYTES = 8192

#: Above this share of NULs in the sniff, treat the file as binary.
_NUL_THRESHOLD = 0.01


def looks_binary(sample: bytes) -> bool:
    """True if `sample` looks like binary rather than text.

    NUL bytes are the signal: they are illegal in UTF-8 text and vanishingly rare
    in legacy encodings, but structural in almost every binary format. UTF-16 is
    excluded from the check by its BOM, which is handled before this is reached.
    """
    if not sample:
        return False
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):     # UTF-16 LE / BE
        return False
    return sample.count(0) / len(sample) > _NUL_THRESHOLD


def decode_bytes(raw: bytes) -> tuple[str, bool]:
    """Decode `raw`, returning `(text, degraded)`.

    `degraded` is True when the ladder fell through to latin-1, meaning the text
    is readable but may be wrong. See the module docstring for the order.
    """
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        encoding = "utf-16"
        try:
            return raw.decode(encoding), False
        except UnicodeDecodeError:
            pass

    try:
        return raw.decode("utf-8-sig"), False
    except UnicodeDecodeError:
        pass

    try:
        return raw.decode("cp1252"), False
    except UnicodeDecodeError:
        pass

    return raw.decode("latin-1"), True


class PlainTextExtractor:
    """Text-shaped files of every flavour."""

    name = "plaintext"
    extensions = TEXT_EXTENSIONS

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        try:
            raw = path.read_bytes()
        except PermissionError as exc:
            raise_error("ERR_FILE_LOCKED", "extract.plaintext", path=str(path), details=str(exc))
            return
        except OSError as exc:
            raise_error("ERR_FILE_CORRUPT", "extract.plaintext", path=str(path), details=str(exc))
            return

        if looks_binary(raw[:SNIFF_BYTES]):
            raise_error(
                "ERR_NO_TEXT_LAYER",
                "extract.plaintext",
                path=str(path),
                details="Contains NUL bytes, so it is a binary file with a text extension.",
            )
            return

        text, degraded = decode_bytes(raw)

        builder = DocumentBuilder(path)
        builder.add(normalise_whitespace(text))
        if degraded:
            builder.warn(
                make_error(
                    "ERR_ENCODING",
                    "extract.plaintext",
                    path=str(path),
                    details="Not valid UTF-8 or cp1252; decoded as latin-1, which never fails "
                            "but can misread accented characters.",
                )
            )
        yield builder.build()


register(PlainTextExtractor())
