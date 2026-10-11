"""Raw RFC 822 bytes from an account in, the fields the index stores out.

Layer: L7

Order 1k 5d/5f. Pure functions, no database and no network: the sync engine fetches
a message through a `MailProvider`, hands the bytes to `parse_raw`, and writes the
result to the index itself. Nothing here touches a mailbox or `app/storage`.

**One parser for mail.** The body, the address lists and the date are read by the
same helpers `.eml` files use (`app/extract/email_files.py`), so a message fetched
from Gmail and the same message saved as an `.eml` agree on who sent it, who it went
to and what it says (D13 matches them by `Message-ID`, and must not find them
different). That module is read, not changed.

**Paths.** A live message has no file, so its `files.path` is
`account://<account id>/<folder>/<remote id>`. Folder and remote id are percent-
encoded segment by segment: an IMAP folder is "[Gmail]/All Mail", a Graph id is
"<folder>/<message>" and both hold slashes that would otherwise make the path
ambiguous. `split_account_path` is the exact inverse.
"""

from __future__ import annotations

import email
import email.policy
import re
from dataclasses import dataclass, field
from email.message import Message
from typing import Optional
from urllib.parse import quote, unquote

# Private helpers on purpose: they are the one definition of "the body" and "the
# addresses" for every kind of mail in the index; a copy here could drift.
from app.extract.email_files import _addresses, _body_text, _sent_at  # noqa: PLC2701
from app.core.logging import logger

__all__ = ["ParsedMessage", "parse_raw", "account_path", "split_account_path",
           "normalise_message_id", "ACCOUNT_SCHEME"]

_log = logger.bind(component="accounts.mail_ingest")

ACCOUNT_SCHEME = "account://"
_ID_TOKEN = re.compile(r"<[^<>\s]+>")


@dataclass
class ParsedMessage:
    message_id: str                       # as in the header, whitespace trimmed; "" if absent
    subject: str
    sender: str                           # the first From address
    recipients: list[str]                 # To and Cc addresses, as `.eml` indexing does
    sent_at: Optional[int]                # Unix seconds from Date:, None when absent or unreadable
    body_text: str                        # plain text, or stripped HTML; quoted replies left in
    has_attachments: bool
    attachments: list[tuple[str, bytes, str]] = field(default_factory=list)   # (name, bytes, content type)
    references: list[str] = field(default_factory=list)                        # thread ids, oldest first


def normalise_message_id(message_id: str) -> str:
    """The comparison form of a Message-ID for D13's dedupe: no brackets, lower case."""
    return message_id.strip().strip("<>").strip().lower()


def _reference_ids(message: Message) -> list[str]:
    # References is the whole thread, oldest first; In-Reply-To alone is the next
    # best thing for a client that does not write References.
    for header in ("References", "In-Reply-To"):
        value = str(message.get(header) or "")
        found = _ID_TOKEN.findall(value) or value.split()
        if found:
            return found
    return []


def _attachment_bytes(part: Message) -> Optional[bytes]:
    payload = part.get_payload(decode=True)
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    # message/rfc822 (a forwarded mail) has sub-messages instead of a payload.
    inner = part.get_payload()
    if isinstance(inner, list) and inner:
        return inner[0].as_bytes()
    return None


def _attachments(message: Message) -> list[tuple[str, bytes, str]]:
    found: list[tuple[str, bytes, str]] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        try:
            name = part.get_filename() or ""
            disposition = str(part.get_content_disposition() or "")
            if not name and disposition != "attachment":
                continue                      # the body, or an unnamed inline part
            data = _attachment_bytes(part)
            if data is None:
                continue
            found.append((name or f"attachment-{len(found) + 1}", data, part.get_content_type()))
        except Exception as exc:              # noqa: BLE001 - one bad part must not lose the message (rule 3)
            _log.warning("skipped an unreadable attachment: {}", exc)
    return found


def parse_raw(raw: bytes) -> ParsedMessage:
    """Parse one message. Never raises for odd mail: a broken part is left out and
    a header that cannot be read is empty, because a half-readable message is
    still worth finding (rule 3)."""
    message = email.message_from_bytes(raw, policy=email.policy.default)

    def header(name: str) -> str:
        try:
            return str(message.get(name) or "").strip()
        except Exception:                      # noqa: BLE001 - a malformed header
            return ""

    try:
        senders = _addresses(message, "From")
        recipients = _addresses(message, "To", "Cc")
    except Exception:                          # noqa: BLE001
        senders, recipients = [], []
    try:
        body = _body_text(message)
    except Exception as exc:                   # noqa: BLE001
        _log.warning("unreadable body: {}", exc)
        body = ""
    attachments = _attachments(message)
    return ParsedMessage(
        message_id=header("Message-ID"),
        subject=header("Subject"),
        sender=senders[0] if senders else "",
        recipients=recipients,
        sent_at=_sent_at(message),
        body_text=body,
        has_attachments=bool(attachments),
        attachments=attachments,
        references=_reference_ids(message),
    )


def account_path(account_id: object, folder: str, remote_id: str) -> str:
    """`account://<id>/<folder>/<remote id>`, each part percent-encoded."""
    return f"{ACCOUNT_SCHEME}{quote(str(account_id), safe='')}/{quote(folder, safe='')}/{quote(remote_id, safe='')}"


def split_account_path(path: str) -> tuple[str, str, str]:
    """(account id, folder, remote id) from an `account://` path; ValueError if it is not one.

    The account id comes back as text; the caller turns it into whatever type its
    table uses.
    """
    if not path.startswith(ACCOUNT_SCHEME):
        raise ValueError(f"not an account path: {path!r}")
    parts = path[len(ACCOUNT_SCHEME):].split("/")
    if len(parts) != 3 or not parts[0] or not parts[2]:
        raise ValueError(f"not an account path: {path!r}")
    return unquote(parts[0]), unquote(parts[1]), unquote(parts[2])
