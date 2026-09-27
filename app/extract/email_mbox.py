"""Mbox mailboxes: `.mbox` files via Python's stdlib mailbox module.

Layer: L2

mbox is a universal mail archive format supported by many mail clients:
- Google Takeout Gmail exports (as mbox files inside a zip)
- Mozilla Thunderbird
- Unix mail/Mail
- Legacy mail systems

The stdlib `mailbox.mbox` module handles the parsing with zero external
dependencies. Each message becomes a Document with the same structure as
`.eml` files — sender/recipients/subject/date — with identical quoting
removal and mail scope.

**Large file handling.** A 10GB Takeout Gmail mbox must be indexed without
materialising the entire file. `mailbox.mbox` is lazy by default — it iterates
messages without loading the whole file at once.

**Per-message checkpointing (work order 202626270509, item 1b).** A resumed
run passes `resume_from`, an mbox message index, and every message before it
is skipped without being parsed - no MIME decode, no quote-stripping, no
`Document` built - which is the expensive part on a multi-gigabyte archive.
`mailbox.mbox`'s own integer keys (`0..N-1`, assigned by a single linear
`From `-line scan - see `mailbox._generate_toc`) are stable across separate
opens of an unchanged file, so a persisted index is safe to reuse: this is
exactly the "message-index cursor" the order's own note names as the real
fix, as opposed to the file-level-only resume that was there before. The one
unavoidable cost is that scan itself, paid once per resumed run to rebuild
the table of contents - a byte/line scan, not a parse, and cheap next to what
it saves. `virtual_path` is set to `{path}/{key}` so each message gets a
stable identity independent of *this run's* position in the iteration -
without it, a resumed run's own `enumerate()` would start renumbering from
zero and collide with rows the previous run already wrote.

**mbox format detection.** Both `.mbox` extensions and extension-less files
with the `From ` line signature (RFC 4155) are handled.
"""

from __future__ import annotations

import email
import email.policy
import mailbox
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable, Optional

from app.core.errors import raise_error
from app.core.logging import logger
from app.extract import progress
from app.extract.base import (
    Document,
    SourceKind,
    register,
)
from app.extract.email_files import build_email_document, _addresses, _conversation_key

__all__ = ["MboxExtractor"]

_log = logger.bind(component="extract.mbox")


def _is_mbox_by_signature(path: Path) -> bool:
    """Detect mbox format by the RFC 4155 `From ` line signature.

    An mbox file without an extension should start with "From " (the literal
    sender line that separates messages). This distinguishes it from other
    text formats that might lack an extension.
    """
    try:
        with open(path, "rb") as f:
            first_line = f.readline()
            # RFC 4155: line starts with "From " followed by sender address
            if first_line.startswith(b"From "):
                return True
    except (OSError, IOError):
        pass
    return False


class MboxExtractor:
    """Extract messages from mbox mailboxes.

    Handles `.mbox` extension, `.mbox.bak` backups, and extension-less mbox
    files detected by RFC 4155 signature. Streaming approach allows large
    archives to be indexed without materializing them in memory.
    """

    name = "mbox"
    extensions = frozenset({".mbox", ".mbox.bak"})

    #: See `app/extract/base.py`'s `Extractor` Protocol docstring. Lets the
    #: generic `extract()` dispatcher pass a `resume_from` message index into
    #: `extract()` below.
    supports_resume = True

    def supports(self, path: Path) -> bool:
        """Check if this path is an mbox file.

        Matches:
        - Files ending in .mbox or .mbox.bak
        - Extension-less files starting with "From " (RFC 4155)
        """
        # `Path.suffix` only ever returns the LAST extension component -
        # "archive.mbox.bak".suffix is ".bak", never ".mbox.bak" - so the
        # membership check against `self.extensions` (which includes the
        # compound ".mbox.bak") could never match a real .mbox.bak file.
        # Checking the filename's tail directly handles both the simple and
        # compound cases.
        name_lower = path.name.lower()
        if any(name_lower.endswith(ext) for ext in self.extensions):
            return True
        # Check for extension-less mbox by signature, but only if there's
        # no extension or the extension is not a common non-mbox type.
        if path.suffix == "" or path.suffix.lower() in {".txt", ".mail", ".mailbox"}:
            return _is_mbox_by_signature(path)
        return False

    def extract(self, path: Path, *, resume_from: int = 0) -> Iterable[Document]:
        """Iterate messages from the mbox file, yielding one Document per message.

        Lazily opens the file with mailbox.mbox and yields documents without
        materializing the entire archive. Handles encoding issues gracefully.

        `resume_from` skips every message whose mbox key is below it, without
        parsing it - see the module docstring. `0` (the default) reads the
        file from the top, exactly as before this parameter existed.
        """
        try:
            # mailbox.mbox opens the file in text mode by default and handles
            # the mbox format (message separation by "From " lines)
            mbox = mailbox.mbox(str(path), create=False)
        except (FileNotFoundError, mailbox.NoSuchMailboxError):
            # `mailbox.mbox(..., create=False)` on a missing file raises its
            # own `NoSuchMailboxError`, not `FileNotFoundError` - it isn't a
            # subclass of it, so the original except-clause here never
            # caught it, and the raw stdlib exception escaped straight past
            # this extractor's error contract (every other path through this
            # method goes through `raise_error`/`AppErrorException`).
            raise_error(
                "ERR_FILE_NOT_FOUND",
                "extract.mbox",
                path=str(path),
            )
            return
        except (OSError, IOError) as exc:
            raise_error(
                "ERR_FILE_CORRUPT",
                "extract.mbox",
                path=str(path),
                details=str(exc),
            )
            return

        try:
            # Iterated by key, not `for message in mbox:` (equivalent to
            # `itervalues()`, which fetches every key from 0). Getting the
            # keys first and skipping everything below `resume_from` means a
            # resumed run never pays to parse a message it already committed
            # in an earlier run - see the module docstring. Building the key
            # table (`mbox.iterkeys()` -> `_generate_toc()` the first time
            # it's needed) is still a full scan of the file, but it is a
            # `From `-line scan, not a MIME parse.
            #
            # Work order 0x section 3b: **the table of contents is the total,
            # for free.** `keys()` builds exactly the same `From `-line table
            # `iterkeys()` would have built on its first step - it is one scan
            # either way, never two - and once it exists its length is the
            # message count. So "message 812 of 2,000" costs no extra pass.
            # Measured 2026-09-27 on a generated 20,000-message mbox (Linux
            # sandbox, median of 5): `keys()` then iterating the list took
            # 87 ms against 86 ms for `iterkeys()` - the scan is the whole of
            # it - and the list is one small int per message.
            keys = mbox.keys()
            with progress.enter("mbox", path.name, unit="message",
                                total=len(keys),
                                stage=progress.STAGE_MESSAGES) as frame:
                yield from self._messages(mbox, keys, path, resume_from, frame)
        finally:
            # Close the mbox file properly
            try:
                mbox.close()
            except Exception as exc:  # noqa: BLE001
                _log.debug("Error closing mbox {}: {}", path.name, exc)

    def _messages(self, mbox: mailbox.mbox, keys: list, path: Path,
                  resume_from: int, frame: progress.Frame) -> Iterable[Document]:
        """The message loop, split out so the progress frame wraps all of it.

        `frame.n` is the mbox key plus one - a position counting from 1, the
        way a person counts - and is set before the message is parsed, so a
        message that takes a long time to decode is the one on the screen.
        """
        for key in keys:
            if key < resume_from:
                continue
            frame.n = key + 1
            try:
                message = mbox.get_message(key)
            except (KeyError, OSError) as exc:  # noqa: BLE001 - one bad slot
                _log.debug("Failed to read message {} in {}: {}", key, path.name, exc)
                continue
            # Convert mailbox.Message to email.message.EmailMessage
            # for consistent handling with the EML extractor
            try:
                # Get the raw bytes and re-parse as EmailMessage
                # This ensures consistent policy and header parsing
                raw = bytes(message)
                parsed = email.message_from_bytes(raw, policy=email.policy.default)
            except Exception as exc:  # noqa: BLE001
                _log.debug("Failed to parse message in {}: {}", path.name, exc)
                continue

            # Extract metadata exactly like email_files.py does
            sent_at: Optional[int] = None
            date_header = parsed.get("Date")
            if date_header:
                try:
                    sent_at = int(parsedate_to_datetime(date_header).timestamp())
                except (TypeError, ValueError):
                    sent_at = None

            senders = _addresses(parsed, "From")

            # Build document using the same function as EML and MSG extractors
            # This ensures identical treatment across all email formats
            try:
                document = build_email_document(
                    path,
                    subject=str(parsed.get("Subject") or "").strip(),
                    sender=senders[0] if senders else "",
                    recipients=_addresses(parsed, "To", "Cc"),
                    sent_at=sent_at,
                    conversation=_conversation_key(parsed),
                    body=self._get_body_text(parsed),
                    attachments=self._get_attachment_names(parsed),
                    source_kind=SourceKind.EML,
                )
                # A stable identity independent of this run's own
                # position in the loop - see the module docstring. Every
                # message in a file otherwise shares `str(path)`, which
                # made every message after the first collide in
                # `_extract_stream`'s duplicate-key guard and get a key
                # built from the *current run's* enumerate() index - fine
                # until a resumed run started renumbering from zero and
                # collided with rows the previous run had already
                # written under the same numbers.
                document.virtual_path = f"{path}/{key}"
                document.meta["mbox_index"] = key
                if not document.is_empty:
                    yield document
            except Exception as exc:  # noqa: BLE001
                _log.debug("Failed to build document for message in {}: {}", path.name, exc)
                continue

    @staticmethod
    def _get_body_text(message: email.message.EmailMessage) -> str:
        """Extract body text from message, preferring plain text.

        Reuses logic from email_files. Handles multipart and HTML-only messages.
        """
        from app.extract.email_files import html_to_text
        from app.extract.base import normalise_whitespace

        if message.is_multipart():
            plain: list[str] = []
            markup: list[str] = []
            for part in message.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                if part.get_filename():  # an attachment
                    continue
                content_type = part.get_content_type()
                try:
                    payload = part.get_content()
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(payload, str):
                    continue
                if content_type == "text/plain":
                    plain.append(payload)
                elif content_type == "text/html":
                    markup.append(payload)
            if plain:
                return normalise_whitespace("\n".join(plain))
            if markup:
                return html_to_text("\n".join(markup))
            return ""

        try:
            payload = message.get_content()
        except Exception:  # noqa: BLE001
            return ""
        if not isinstance(payload, str):
            return ""
        if message.get_content_type() == "text/html":
            return html_to_text(payload)
        from app.extract.base import normalise_whitespace
        return normalise_whitespace(payload)

    @staticmethod
    def _get_attachment_names(message: email.message.EmailMessage) -> list[str]:
        """Get attachment filenames from message."""
        if not message.is_multipart():
            return []
        return [part.get_filename() for part in message.walk() if part.get_filename()]


register(MboxExtractor())
