"""Standalone email files: `.eml` via the stdlib, `.msg` via extract-msg.

Layer: L2

`.pst` archives are **not** handled here - they need `win32com` talking to a live
Outlook, and live in `email_pst.py`. `extract-msg` reads single `.msg` items only;
pointing it at a `.pst` fails in confusing ways, which is why the architecture
document calls it out by name.

**Threading.** `conversation` is derived from the `References` header, falling
back to `In-Reply-To`, then to the message's own `Message-ID`. The first entry in
`References` is the root of the thread, so every reply in a conversation lands on
the same key and the whole exchange groups in the results - which is the point of
indexing email at all: a decision is rarely in one message.

**Bodies.** `text/plain` is preferred. When a message is HTML-only the markup is
stripped with a small, dependency-free pass: good enough for search, where the
goal is words in the right order, not fidelity.
"""

from __future__ import annotations

import email
import email.policy
import html
import json
import re
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from app.core.errors import raise_error
from app.core.format_health import Requirement
from app.core.logging import logger
from app.extract.quoting import strip_quoted
from app.extract.base import (
    Document,
    DocumentBuilder,
    SourceKind,
    normalise_whitespace,
    register,
)

__all__ = [
    "EmlExtractor", "MsgExtractor", "html_to_text", "build_email_document",
    "document_from_message",
]

_log = logger.bind(component="extract.email")

_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_BLOCK_END = re.compile(r"</(p|div|tr|li|h[1-6]|table|blockquote)\s*>", re.IGNORECASE)
_LINE_BREAK = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")


def html_to_text(markup: str) -> str:
    """Strip HTML to readable text. Deliberately small, not a parser."""
    text = _SCRIPT_OR_STYLE.sub(" ", markup)
    text = _LINE_BREAK.sub("\n", text)
    text = _BLOCK_END.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)
    # The third character in the class is a literal no-break space (U+00A0), the
    # entity `&nbsp;` becomes after `unescape`. 2026-10-08 review: Python source
    # here is ASCII by convention; `\u00a0` would say the same thing visibly.
    text = re.sub(r"[ \t ]+", " ", text)
    return normalise_whitespace(text)


def _addresses(message: EmailMessage, *fields: str) -> list[str]:
    raw = [(message.get(field) or "") for field in fields]
    return [address for _name, address in getaddresses(raw) if address]


def _conversation_key(message: EmailMessage) -> Optional[str]:
    """The thread root, so replies group with what they reply to."""
    references = (message.get("References") or "").split()
    if references:
        return references[0]
    in_reply_to = (message.get("In-Reply-To") or "").strip()
    return in_reply_to or (message.get("Message-ID") or "").strip() or None


def _body_text(message: EmailMessage) -> str:
    """Best available body: plain text if offered, otherwise stripped HTML."""
    if message.is_multipart():
        plain: list[str] = []
        markup: list[str] = []
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_filename():                       # an attachment, not the body
                continue
            content_type = part.get_content_type()
            try:
                payload = part.get_content()
            except Exception:                             # noqa: BLE001 - one bad part
                continue
            if not isinstance(payload, str):
                continue
            if content_type == "text/plain":
                plain.append(payload)
            elif content_type == "text/html":
                markup.append(payload)
        if plain:
            return normalise_whitespace("\n".join(plain))
        return html_to_text("\n".join(markup)) if markup else ""

    try:
        payload = message.get_content()
    except Exception:                                     # noqa: BLE001
        return ""
    if not isinstance(payload, str):
        return ""
    if message.get_content_type() == "text/html":
        return html_to_text(payload)
    return normalise_whitespace(payload)


def _attachment_names(message: EmailMessage) -> list[str]:
    if not message.is_multipart():
        return []
    return [part.get_filename() for part in message.walk() if part.get_filename()]


def build_email_document(
    path: Path,
    *,
    subject: str,
    sender: str,
    recipients: list[str],
    sent_at: Optional[int],
    conversation: Optional[str],
    body: str,
    attachments: list[str],
    source_kind: str = SourceKind.EML,
    entry_id: Optional[str] = None,
    store_path: Optional[str] = None,
) -> Document:
    """Assemble one message into a Document.

    The headers are written into the indexed text as well as into `meta`. That is
    deliberate: 'the email from Priya about the survey' is how people search, and
    it only works if the sender and subject are in the text the retriever sees.
    `meta` mirrors the `messages` table so Layer 3 can store it without mapping.
    """
    builder = DocumentBuilder(path, source_kind=source_kind)

    header_lines = [f"Subject: {subject}" if subject else "", f"From: {sender}" if sender else ""]
    if recipients:
        header_lines.append(f"To: {', '.join(recipients)}")
    if attachments:
        header_lines.append(f"Attachments: {', '.join(attachments)}")
    builder.add("\n".join(line for line in header_lines if line), label="Headers")

    # Quoted chains and signatures are cut here, at the one point every mail
    # path passes through - EML, MSG, PST via Outlook, and PST via libpff all
    # call this function. Doing it in each extractor would be four
    # implementations that could disagree about what a thread looks like.
    #
    # A twelve-message thread otherwise puts its first message into the index
    # twelve times: once as itself and eleven more times quoted inside replies.
    # The visible symptom is ten results from one conversation all showing the
    # same paragraph, crowding out ten different documents - which a person
    # experiences as "search is bad" rather than as "the index is redundant".
    stripped = strip_quoted(body)
    if stripped.changed:
        _log.debug(
            "stripped {} chars of {} from {}",
            stripped.removed_chars, stripped.reason, path.name,
        )
    builder.add(stripped.text, label="Body")

    builder.meta.update(
        {
            # Plain empty string when absent, not None: every caller already
            # defaults its own argument to "" (mbox: `str(... or "").strip()`,
            # eml/msg the same), and no downstream code anywhere checks for
            # None specifically on either field (searched the whole tree) -
            # `or None` here just turned that "" back into None right before
            # storage, for no reason anyone was relying on. Confirmed safe:
            # the schema column is a nullable TEXT with no NOT NULL
            # constraint or COALESCE logic keyed on the distinction.
            "subject": subject,
            "sender": sender,
            "recipients": json.dumps(recipients),
            "sent_at": sent_at,
            "conversation": conversation,
            "entry_id": entry_id,
            "store_path": store_path,
            "has_attach": 1 if attachments else 0,
            "attachment_names": attachments,
            # **Measured here, shown in the preview.** The amount has been
            # computed since quoting was built and only ever logged - and the
            # mail preview shows the *indexed* text, so a reply arrives with
            # its thread gone. Without this the pane cannot say so, and a
            # message looks as though it was sent with no context.
            "quoted_removed": stripped.removed_chars,
        }
    )
    return builder.build()


def _sent_at(message: EmailMessage) -> Optional[int]:
    """The `Date:` header as Unix seconds, or None when absent or unreadable.

    A missing or garbled date is common in old mail and is not a reason to
    lose the message - it simply sorts and filters by nothing.
    """
    date_header = message.get("Date")
    if not date_header:
        return None
    try:
        return int(parsedate_to_datetime(date_header).timestamp())
    except (TypeError, ValueError):
        return None


def document_from_message(
    path: Path,
    message: EmailMessage,
    *,
    sent_at: Optional[int] = None,
) -> Document:
    """Turn one parsed RFC 822 message into the standard mail `Document`.

    **One function, so every reader of "a message in MIME form" agrees.** It
    is exactly what `EmlExtractor` did inline before it was lifted out; Apple
    Mail's `.emlx` (work order 0x, 8a) is the same MIME message with a byte
    count in front and a property list behind, so it calls this too, and the
    two cannot drift apart on what counts as the sender, the recipients or
    the body.

    `sent_at` is a fallback used only when the message has no readable
    `Date:` header - `.emlx` knows when Apple Mail *received* the message,
    which is better than nothing. `.eml` never passes it.
    """
    senders = _addresses(message, "From")
    header_date = _sent_at(message)
    return build_email_document(
        path,
        subject=str(message.get("Subject") or "").strip(),
        sender=senders[0] if senders else "",
        recipients=_addresses(message, "To", "Cc"),
        sent_at=header_date if header_date is not None else sent_at,
        conversation=_conversation_key(message),
        body=_body_text(message),
        attachments=_attachment_names(message),
    )


class EmlExtractor:
    """RFC 822 message files (`.eml`) and web archives saved as MIME (`.mht`)."""

    name = "eml"
    extensions = frozenset({".eml", ".mht", ".mhtml"})

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        """One mail `Document` (headers and quote-stripped body) per file.

        Unreadable bytes are `ERR_FILE_CORRUPT`, a locked file `ERR_FILE_LOCKED`,
        and a message with no subject, sender or body `ERR_NO_TEXT_LAYER`.
        Attachments are listed by name only, never unpacked. Reads only.
        """
        try:
            raw = path.read_bytes()
        except PermissionError as exc:
            raise_error("ERR_FILE_LOCKED", "extract.eml", path=str(path), details=str(exc))
            return
        except OSError as exc:
            raise_error("ERR_FILE_CORRUPT", "extract.eml", path=str(path), details=str(exc))
            return

        try:
            message = email.message_from_bytes(raw, policy=email.policy.default)
        except Exception as exc:                          # noqa: BLE001
            raise_error("ERR_FILE_CORRUPT", "extract.eml", path=str(path), details=str(exc))
            return

        document = document_from_message(path, message)
        if document.is_empty:
            raise_error(
                "ERR_NO_TEXT_LAYER",
                "extract.eml",
                path=str(path),
                details="The message has no subject, no sender and no readable body.",
            )
            return
        yield document


class MsgExtractor:
    """Single Outlook `.msg` items, via extract-msg.

    Never used for `.pst`: extract-msg reads one message per file and cannot open
    an archive. PST goes through Outlook MAPI in `email_pst.py`.
    """

    name = "msg"
    extensions = frozenset({".msg"})
    #: Hard: there is no fallback reader for the compound-file .msg layout, so
    #: without extract-msg every .msg in the corpus is name-only.
    requires = (
        Requirement("extract_msg", "extract-msg",
                    provides="the body and headers of Outlook .msg files",
                    hard=True),
    )

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        """One mail `Document` per `.msg`, through extract-msg.

        Without the package the file is `ERR_UNSUPPORTED_TYPE` with the install
        command; a file extract-msg rejects is `ERR_FILE_CORRUPT`; an empty
        message `ERR_NO_TEXT_LAYER`. The item is always closed. Reads only.
        """
        try:
            import extract_msg
        except ImportError as exc:
            raise_error(
                "ERR_UNSUPPORTED_TYPE",
                "extract.msg",
                path=str(path),
                ext=".msg",
                suggestion=(
                    "Reading .msg files needs the 'extract-msg' package. Install it with: "
                    "venv\\Scripts\\python.exe -m pip install extract-msg"
                ),
                details=str(exc),
            )
            return

        try:
            item: Any = extract_msg.Message(str(path))
        except Exception as exc:                          # noqa: BLE001
            raise_error("ERR_FILE_CORRUPT", "extract.msg", path=str(path), details=str(exc))
            return

        try:
            sent_at: Optional[int] = None
            if getattr(item, "date", None) is not None:
                try:
                    sent_at = int(item.date.timestamp())
                except (AttributeError, ValueError, OSError):
                    sent_at = None

            recipients = [
                str(recipient) for recipient in (getattr(item, "recipients", None) or [])
            ]
            attachments = [
                str(getattr(attachment, "longFilename", None)
                    or getattr(attachment, "shortFilename", None)
                    or "attachment")
                for attachment in (getattr(item, "attachments", None) or [])
            ]

            document = build_email_document(
                path,
                subject=str(getattr(item, "subject", "") or "").strip(),
                sender=str(getattr(item, "sender", "") or "").strip(),
                recipients=recipients,
                sent_at=sent_at,
                conversation=str(getattr(item, "messageId", "") or "") or None,
                body=normalise_whitespace(str(getattr(item, "body", "") or "")),
                attachments=attachments,
            )
            if document.is_empty:
                raise_error(
                    "ERR_NO_TEXT_LAYER",
                    "extract.msg",
                    path=str(path),
                    details="The message has no subject, no sender and no readable body.",
                )
                return
            yield document
        finally:
            close = getattr(item, "close", None)
            if callable(close):
                close()


register(EmlExtractor())
register(MsgExtractor())
