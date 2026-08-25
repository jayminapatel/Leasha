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
from app.extract.source_types import ALL_SOURCE_EXTENSIONS, NAMED_FILES

__all__ = [
    "PlainTextExtractor", "decode_bytes", "looks_binary", "TEXT_EXTENSIONS",
    "NAMED_FILES",
]

#: Every extension read as plain text.
#:
#: **The list lives in `source_types.py`, grouped by ecosystem.** It was
#: thirty-four extensions here - the languages somebody happened to think of -
#: and a repository of PL/SQL packages, COBOL copybooks or SSIS packages was
#: silently three-quarters unindexed, with nothing to say so. Asked for
#: directly: *"add all types of code files from microsoft, oracle etc"*.
#:
#: A flat set is unreviewable, which is why the groups are next door: somebody
#: who knows Oracle can read twenty lines and say whether `.pkb` is there.
TEXT_EXTENSIONS = ALL_SOURCE_EXTENSIONS

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
    #: Whole filenames, for the build and configuration files that have no
    #: extension at all. `register` reads this into `base.NAME_REGISTRY`.
    names = NAMED_FILES

    def supports(self, path: Path) -> bool:
        """By extension, or by whole name for the files that have none.

        `Path("Makefile").suffix` is `""`, and so is `Path(".gitignore").suffix`
        - Python reads a leading dot as the start of the stem, not as a
        separator. So neither can be routed by extension at all, and a rule
        listing `".gitignore"` as one would never match anything while looking
        entirely correct. Both are matched on the whole name instead.
        """
        return (path.suffix.lower() in self.extensions
                or path.name.lower() in NAMED_FILES)

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
                # The registry's default suggestion talks about scans and OCR,
                # which would be nonsense here. An error that gives the wrong
                # advice is worse than one that gives none.
                suggestion=(
                    "The extension says text but the contents are binary - usually a database, "
                    "an archive or an image that has been renamed. Indexing its bytes as words "
                    "would fill the search index with matches for nothing. Rename it to its real "
                    "extension if you want it handled properly."
                ),
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
