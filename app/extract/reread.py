"""One message's whole body, read again from where it was indexed - read-only.

Layer: L2

Order 1s 4b. The index keeps a message cut at its signature or its quoted chain
(`quoting.strip_quoted`), and from schema 42 the cut part is kept beside it
(`message_tails`). Mail indexed before then has neither, so enrichment may ask for
the original - only when the person switched on "Read the original messages again
(slower)" (`AccountSettings.enrich_read_originals`), and only for a contact's newest
few messages; the caller decides how many.

**Read-only, every route** (non-negotiable 10). A message file is read as bytes; a
`.msg` through extract-msg, which opens the compound file for reading; a `.pst`
message through libpff (`pst_attachment.read_message_body`), which reads the file
without Outlook. Nothing is moved, flagged or marked read.

**Where it is not opened**, the answer is None and the caller keeps the indexed text:

* `account://` mail lives on the provider's server, and reading it again would be a
  network request this order does not allow;
* a message read through Outlook (an Outlook EntryID): the only way back in is to
  attach the archive to the person's Outlook profile, which changes their mail setup;
* a message inside an mbox, an `.olm` or a zip, and a file no longer on disk.

The same readers the indexer uses decode the body, so the original reads exactly as
the indexed text did before it was cut.
"""

from __future__ import annotations

import email
import email.policy
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from app.core.logging import logger
from app.extract.base import normalise_whitespace

__all__ = ["original_body", "REREAD_SUFFIXES"]

_log = logger.bind(component="extract.reread")


def _mime(raw: bytes) -> str:
    from app.extract.email_files import _body_text

    return _body_text(email.message_from_bytes(raw, policy=email.policy.default))


def _eml(path: Path) -> str:
    return _mime(path.read_bytes())


def _emlx(path: Path) -> str:
    from app.extract.email_emlx import split_emlx

    return _mime(split_emlx(path.read_bytes())[0])


def _msg(path: Path) -> str:
    import extract_msg

    item: Any = extract_msg.Message(str(path))
    try:
        return str(getattr(item, "body", "") or "")
    finally:
        close = getattr(item, "close", None)
        if callable(close):
            close()


_FILE_READERS: dict[str, Callable[[Path], str]] = {
    ".eml": _eml, ".mht": _eml, ".mhtml": _eml, ".emlx": _emlx, ".msg": _msg,
}

#: The message files read again, by suffix. A `.pst` message is read by its archive.
REREAD_SUFFIXES = frozenset(_FILE_READERS)


def original_body(row: Mapping[str, Any]) -> Optional[str]:
    """The whole body of one message - nothing cut - or None where it is not opened.

    `row` is the message's `files` and `messages` columns: `path`, `source_kind`,
    `store_path`, `entry_id`, `folder_path`, `folder_index`. Never raises: a message
    that cannot be read again costs the re-read, and the indexed text stands.
    """
    path = str(row.get("path") or "")
    try:
        if row.get("source_kind") == "pst_message" or path.startswith("pst://"):
            archive = str(row.get("store_path") or "")
            if not archive.lower().endswith(".pst"):
                return None
            from app.extract.pst_attachment import read_message_body

            index = row.get("folder_index")
            body = read_message_body(archive, str(row.get("entry_id") or ""),
                                     folder_path=row.get("folder_path"),
                                     folder_index=int(index) if index is not None else None)
        else:
            if "://" in path:
                return None
            reader = _FILE_READERS.get(Path(path).suffix.lower())
            if reader is None or not Path(path).is_file():
                return None
            body = reader(Path(path))
    except Exception as exc:                     # noqa: BLE001 - see the docstring
        _log.debug("could not read {} again: {}", path, exc)
        return None
    text = normalise_whitespace(body or "")
    return text or None
