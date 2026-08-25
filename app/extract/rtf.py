r"""Rich Text Format: .rtf, via `striprtf`. No external converter.

Layer: L2

`.rtf` went through LibreOffice until now, which meant a fifteen-year archive
full of them - and `.rtf` is everywhere, because it is what every application
writes when text is pasted between two things that disagree about formatting -
read nothing at all on a machine without LibreOffice installed. It is a
plain-text markup format. It never needed a subprocess.

**The file is decoded as latin-1, deliberately.** RTF's own body is 7-bit ASCII:
anything outside it is escaped as `\'xx` or `\uNNNN` and decoded by the parser,
not by us. latin-1 is the one codec that maps every byte to a character without
ever raising, so the bytes reach `striprtf` exactly as written and it applies
the codepage the document declares. Decoding as UTF-8 first would corrupt the
escapes in any file written by a non-English Word.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from app.core.errors import raise_error
from app.core.format_health import Requirement
from app.extract.base import (
    Document,
    DocumentBuilder,
    SourceKind,
    normalise_whitespace,
    register,
)

__all__ = ["RtfExtractor", "MAX_TEXT_CHARS"]

#: Matches the e-book cap, for the same reason: one file should not become ten
#: thousand chunks and crowd a corpus out of its own results.
MAX_TEXT_CHARS = 4 * 1024 * 1024


class RtfExtractor:
    """Rich Text Format, read in-process."""

    name = "rtf"
    extensions = frozenset({".rtf"})
    reads_externally = False
    requires = (Requirement("striprtf", "striprtf",
                            provides="Rich Text Format body text", hard=True),)

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        from striprtf.striprtf import rtf_to_text

        try:
            raw = path.read_bytes()
        except PermissionError as exc:
            raise_error("ERR_FILE_LOCKED", "extract.rtf", path=str(path), details=str(exc))
            return
        except OSError as exc:
            raise_error("ERR_FILE_CORRUPT", "extract.rtf", path=str(path), details=str(exc))
            return

        # An RTF document opens with `{\rtf`. Checking is worth one comparison:
        # without it a mislabelled binary is fed to the parser, which cheerfully
        # returns its stray ASCII as if it were the document's text.
        if not raw.lstrip()[:5].startswith(b"{\\rtf"):
            raise_error(
                "ERR_FILE_CORRUPT", "extract.rtf", path=str(path),
                details=r"does not begin with {\rtf, so it is not Rich Text "
                        "Format whatever the extension says",
            )
            return

        try:
            text = rtf_to_text(raw.decode("latin-1"), errors="ignore")
        except Exception as exc:                             # noqa: BLE001
            # striprtf raises assorted parse errors on malformed control words.
            # None of them should reach the user as a traceback.
            raise_error("ERR_FILE_CORRUPT", "extract.rtf", path=str(path),
                        details=f"{type(exc).__name__}: {exc}")
            return

        text = normalise_whitespace(text)
        if not text.strip():
            # base.extract turns an empty yield into ERR_NO_TEXT_LAYER, which is
            # the honest answer for an RTF holding nothing but an embedded image.
            return

        builder = DocumentBuilder(path, source_kind=SourceKind.FILE)
        builder.add(text[:MAX_TEXT_CHARS], label="Content")
        builder.meta["format"] = "rtf"
        yield builder.build()


register(RtfExtractor())
