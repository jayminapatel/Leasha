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

import codecs
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

#: Bytes read at a time once the encoding is settled.
#:
#: 1MiB: large enough that a 2GB log is two thousand reads rather than two
#: million, small enough that it is not itself the memory problem.
BLOCK_BYTES = 1 << 20

#: Characters of text one plain file may produce.
#:
#: **This constant is the fix, and the streaming below only makes it possible.**
#: Measured on a 22MB log: `read_bytes()` then decode peaks at 43.2MB, and
#: streaming the read peaks at 43.2MB too - identical, because `str.join` holds
#: the pieces and the joined result at the same moment, which is exactly the
#: two-copies-of-the-text shape that reading the bytes first had. Streaming on
#: its own saves *nothing*, and shipping it with a comment claiming otherwise
#: was one measurement away from happening.
#:
#: What the cap does, on the same fixture with the cap lowered to bind: 9.4MB,
#: **4.6x less**. On the file this is actually about - the walker admits up to
#: 2GB - it is the difference between roughly 4GB for one file, times however
#: many extract workers are running, and about 128MB. Reading in blocks is what
#: lets the cap bind before the memory has already been spent; `read_bytes()`
#: would allocate the whole 2GB before there was anything to truncate.
#:
#: 64Mi characters is roughly ten million words: thirty times the longest book
#: anybody has written, and far past the point where a log file's tail is
#: telling you something its first sixty megabytes did not. Reaching it is
#: reported on the file's own row, never silently.
MAX_TEXT_CHARS = 64 << 20

#: The decoding ladder, in order. See the module docstring.
_ENCODINGS = ("utf-8-sig", "cp1252", "latin-1")


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


def read_text(path: Path, sniff: bytes = b"") -> tuple[str, bool, bool]:
    r"""Decode `path` in blocks, stopping at `MAX_TEXT_CHARS`.

    Returns `(text, degraded, truncated)`.

    `decode_bytes` still exists and is still correct; what it cannot be is the
    thing the extractor calls, because it takes the whole file as a `bytes` -
    which on a 2GB file means allocating all 2GB before anything can decide the
    file is too long to index whole.

    **Reading in blocks is not itself the saving** - see `MAX_TEXT_CHARS`, and
    the measurement recorded there. It is what lets the ceiling bind early
    enough to matter.

    **The ladder costs a re-read, and that is the right trade.** An incremental
    decoder cannot un-decide: if cp1252 fails four hundred megabytes in, the
    characters already produced are wrong and the only honest response is to
    start again on the next rung. That happens on a vanishing fraction of
    files, and the alternative - buffering everything so a restart is free - is
    the memory this function exists to avoid.
    """
    if sniff.startswith((b"\xff\xfe", b"\xfe\xff")):
        ladder: tuple[str, ...] = ("utf-16",) + _ENCODINGS
    else:
        ladder = _ENCODINGS

    for encoding in ladder:
        decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
        pieces: list[str] = []
        length = 0
        truncated = False
        try:
            with path.open("rb") as handle:
                while True:
                    block = handle.read(BLOCK_BYTES)
                    if not block:
                        pieces.append(decoder.decode(b"", True))
                        break
                    piece = decoder.decode(block)
                    if length + len(piece) >= MAX_TEXT_CHARS:
                        pieces.append(piece[:MAX_TEXT_CHARS - length])
                        truncated = True
                        break
                    pieces.append(piece)
                    length += len(piece)
        except UnicodeDecodeError:
            continue                     # next rung; see the docstring
        # latin-1 is the last rung and cannot fail, so reaching it means the
        # text is readable but may be wrong - which is what `degraded` says.
        return "".join(pieces), encoding == "latin-1", truncated

    # Unreachable while latin-1 is in the ladder, and asserted rather than
    # assumed: a future edit that reorders it would otherwise return None here.
    raise AssertionError("the decoding ladder must end in one that cannot fail")


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
            with path.open("rb") as handle:
                sniff = handle.read(SNIFF_BYTES)
        except PermissionError as exc:
            raise_error("ERR_FILE_LOCKED", "extract.plaintext", path=str(path), details=str(exc))
            return
        except OSError as exc:
            raise_error("ERR_FILE_CORRUPT", "extract.plaintext", path=str(path), details=str(exc))
            return

        if looks_binary(sniff):
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

        try:
            text, degraded, truncated = read_text(path, sniff)
        except PermissionError as exc:
            raise_error("ERR_FILE_LOCKED", "extract.plaintext", path=str(path), details=str(exc))
            return
        except OSError as exc:
            raise_error("ERR_FILE_CORRUPT", "extract.plaintext", path=str(path), details=str(exc))
            return

        builder = DocumentBuilder(path)
        builder.add(normalise_whitespace(text))
        if truncated:
            builder.warn(
                make_error(
                    "ERR_ENCODING",
                    "extract.plaintext",
                    path=str(path),
                    details=(
                        f"Longer than {MAX_TEXT_CHARS // (1 << 20)}M characters; "
                        f"indexed up to there. Everything before that point is "
                        f"searchable and the rest is not."
                    ),
                )
            )
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
