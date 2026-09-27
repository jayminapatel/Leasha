"""Apple Mail messages: `.emlx` and `.partial.emlx`, via the stdlib only.

Layer: L2

Work order 0x, section 8a ("mail files from a Mac"). Apple Mail keeps every
message as its own small file, deep inside the user's Library folder:

    ~/Library/Mail/V10/<account>/<mailbox>.mbox/<uuid>/Data/<n>/<n>/Messages/12345.emlx

(the `V10` number goes up with macOS releases; the reader does not care which
one it is in, because it reads the file, not the folder).

**The format, in three parts** - and all three are plain, documented pieces:

1. The first line is a decimal number, the byte count of the message that
   follows, then a newline. For example `2417\\n`.
2. Exactly that many bytes of an ordinary RFC 822 / MIME message - the very
   same thing a `.eml` file holds.
3. An Apple XML property list ("plist") of bookkeeping: flags, the date the
   message was received, the server's id for it, and so on.

So the reader is a thin wrapper: split off part 2, hand it to Python's
`email` package, and build the document with
`email_files.document_from_message` - the exact function `.eml` uses. That is
what makes an Apple Mail message identical in shape to an `.eml`: same
sender, recipients, subject and date, same quote-stripping (it happens inside
`build_email_document`), same `source_kind`, so it lands in mail scope. Part 3
is read with `plistlib` for one thing only - the received date, used when a
message has no readable `Date:` header. Anything else in it is ignored.

**`.partial.emlx`.** Apple Mail writes this name when it kept the message's
text but did **not** download its attachments (the "download attachments:
recent / none" account setting, or a large message). The MIME parts for the
attachments are still listed - so their *names* are known and indexed - but
their contents are not in the file. The message is indexed as normal and
carries a warning, `ERR_MAIL_ATTACHMENTS_NOT_DOWNLOADED`, plus
`meta["attachments_downloaded"] = False`, so the skip ledger and the preview
can say the attachments are missing rather than leaving somebody to wonder
why a search for words inside one finds nothing.

**Failure is a value (non-negotiables #2 and #3).** A file whose first line
is not a number, or that cannot be read at all, raises a structured
`AppErrorException` (`ERR_FILE_CORRUPT` / `ERR_FILE_LOCKED`), which the
pipeline records as a skip and moves on from. A file that is shorter than its
own byte count says - usually a copy that was cut off - is indexed as far as
it goes and warned with `ERR_MAIL_PARTIAL`. A damaged plist costs nothing but
the fallback date.

**Read-only (non-negotiable #10).** The file is opened for reading, once, and
nothing is ever written back - including the "read" flag Apple Mail keeps in
the plist.

**(UNCONFIRMED on macOS)** Built and tested against files made in the test
suite from the published description of the format; checking it against a
real `~/Library/Mail/V10` folder is on `docs/MAC_VERIFICATION.md`. Apple Mail
also writes `.emlxpart` files (one downloaded attachment each) in some
versions; they are not read here.
"""

from __future__ import annotations

import email
import email.policy
import plistlib
from pathlib import Path
from typing import Any, Iterable, Optional

from app.core.errors import make_error, raise_error
from app.core.logging import logger
from app.extract.base import Document, register
from app.extract.email_files import document_from_message

__all__ = ["EmlxExtractor", "split_emlx", "PARTIAL_SUFFIX"]

_log = logger.bind(component="extract.emlx")

#: The tail of a partially-downloaded message's file name. Checked on the
#: whole name, because `Path("1.partial.emlx").suffix` is only `.emlx` - the
#: same trap `email_mbox.MboxExtractor.supports` documents for `.mbox.bak`.
PARTIAL_SUFFIX = ".partial.emlx"

#: The longest first line accepted as a byte count. Real files have a handful
#: of digits (a 1GB message is ten); the cap stops a file that is not an
#: `.emlx` at all - a binary blob with no newline for megabytes - from being
#: scanned end to end looking for one.
_MAX_COUNT_LINE = 32


def split_emlx(raw: bytes) -> tuple[bytes, bytes, int]:
    """Split an `.emlx` file's bytes into (message, plist, declared length).

    Raises `ValueError` when the first line is not a byte count - the one
    thing that makes a file unmistakably *not* an `.emlx`, and the caller
    turns that into `ERR_FILE_CORRUPT`.

    The declared length is returned too, so the caller can tell a complete
    message (`len(message) == declared`) from one that was cut short.
    """
    # The newline ends the count line. Searched only in the first few bytes -
    # see `_MAX_COUNT_LINE`.
    newline = raw.find(b"\n", 0, _MAX_COUNT_LINE + 1)
    if newline < 0:
        raise ValueError("the first line is not a byte count (no newline near the start)")
    count_text = raw[:newline].strip()
    # `isdigit()` before `int()`, because `int()` happily accepts "+5", " 5"
    # and "5_000", none of which Apple Mail writes.
    if not count_text.isdigit():
        raise ValueError(f"the first line is not a byte count: {count_text[:20]!r}")
    declared = int(count_text)
    start = newline + 1
    message = raw[start:start + declared]
    plist = raw[start + declared:]
    return message, plist, declared


def _received_at(plist_bytes: bytes, path: Path) -> Optional[int]:
    """The `date-received` from the trailing plist, as Unix seconds, or None.

    **Only a fallback**, used when the message itself has no readable `Date:`
    header. Every failure here - no plist, a damaged plist, a plist without
    the key - returns None, because the plist is Apple Mail's bookkeeping and
    losing it costs one date, not the message.
    """
    body = plist_bytes.strip()
    if not body:
        return None
    try:
        data: Any = plistlib.loads(body)
    except Exception as exc:                            # noqa: BLE001 - metadata only
        _log.debug("unreadable plist in {}: {}", path.name, exc)
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("date-received")
    # Apple Mail writes seconds since 1970 as an integer or a real. A
    # `datetime` would also be valid plist, so it is accepted too.
    if isinstance(value, bool):                         # bool is an int subclass
        return None
    if isinstance(value, (int, float)):
        return int(value)
    timestamp = getattr(value, "timestamp", None)
    if callable(timestamp):
        try:
            return int(timestamp())
        except (OverflowError, OSError, ValueError):
            return None
    return None


class EmlxExtractor:
    """One Apple Mail message file, as one mail `Document`.

    Claims `.emlx`, which covers `.partial.emlx` too (its last suffix is
    `.emlx`); the partial case is told apart by name inside `extract`.
    """

    name = "emlx"
    extensions = frozenset({".emlx"})
    # Read from the file's own bytes - never through Mail.app - so a content
    # hash is meaningful and nothing holds the file open. See `Extractor`.
    reads_externally = False

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        """Yield the one message in `path`, or raise a structured error."""
        try:
            raw = path.read_bytes()
        except PermissionError as exc:
            # On a Mac this is usually Full Disk Access: `~/Library/Mail` is
            # protected and a process without that permission is refused.
            # (UNCONFIRMED on macOS) - listed in docs/MAC_VERIFICATION.md.
            raise_error("ERR_FILE_LOCKED", "extract.emlx", path=str(path), details=str(exc))
            return
        except OSError as exc:
            raise_error("ERR_FILE_CORRUPT", "extract.emlx", path=str(path), details=str(exc))
            return

        try:
            message_bytes, plist_bytes, declared = split_emlx(raw)
        except ValueError as exc:
            raise_error("ERR_FILE_CORRUPT", "extract.emlx", path=str(path), details=str(exc))
            return

        try:
            message = email.message_from_bytes(message_bytes, policy=email.policy.default)
        except Exception as exc:                        # noqa: BLE001
            raise_error("ERR_FILE_CORRUPT", "extract.emlx", path=str(path), details=str(exc))
            return

        # The same builder `.eml` goes through, so the two are identical in
        # shape - see the module docstring. Any surprise inside the email
        # package (it is lenient, but a hostile header can still trip it)
        # becomes a structured skip rather than an exception that escapes.
        try:
            document = document_from_message(
                path, message, sent_at=_received_at(plist_bytes, path))
        except Exception as exc:                        # noqa: BLE001 - one bad file
            raise_error("ERR_FILE_CORRUPT", "extract.emlx", path=str(path), details=str(exc))
            return

        if document.is_empty:
            raise_error(
                "ERR_NO_TEXT_LAYER",
                "extract.emlx",
                path=str(path),
                details="The message has no subject, no sender and no readable body.",
            )
            return

        partial = path.name.lower().endswith(PARTIAL_SUFFIX)
        # Written for every message, not only partial ones, so a reader of the
        # metadata never has to know that "missing" means "downloaded".
        document.meta["attachments_downloaded"] = not partial
        warnings = list(document.warnings)
        if partial:
            names = document.meta.get("attachment_names") or []
            warnings.append(make_error(
                "ERR_MAIL_ATTACHMENTS_NOT_DOWNLOADED", "extract.emlx",
                path=str(path),
                reason=(
                    f"Apple Mail did not download {len(names)} attachment"
                    f"{'s' if len(names) != 1 else ''} ({', '.join(names[:3])}"
                    f"{', ...' if len(names) > 3 else ''})"
                    if names else
                    "Apple Mail kept only part of this message (a .partial.emlx file)"
                ),
            ))
        if len(message_bytes) < declared:
            # Cut short - most often a copy interrupted part way. What is
            # there is still worth indexing; the warning says the rest is not.
            warnings.append(make_error(
                "ERR_MAIL_PARTIAL", "extract.emlx", path=str(path),
                reason=(f"the file holds {len(message_bytes):,} of the "
                        f"{declared:,} bytes its first line promises"),
            ))
        document.warnings = tuple(warnings)
        yield document


register(EmlxExtractor())
