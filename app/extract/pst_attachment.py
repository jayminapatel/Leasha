r"""One attachment, read back out of an Outlook archive by the direct reader.

2026-10-04, the owner: "build the open on attachment, save a copy and open
it". The index holds an attachment's words, not its bytes; to open it in its
own program the bytes are read again from the `.pst` - read-only, through
libpff, with Outlook never started.

**Found by where it sits, not by searching.** pypff has no lookup by message
number, and walking a 4.9 GB archive's 6,278 messages for one of them took
38 s (measured on the owner's `2024.pst`, 2026-10-04) - his largest is 18 GB.
So the direct reader records each message's folder and its position in it
(`messages.folder_path`, `messages.folder_index`, schema 32), and this goes
straight there: the folder tree is walked (milliseconds), the message is
taken by position, and its number is checked against the one indexed. Only
when that fails - an index written before schema 32, or an archive that has
changed since - does it search, the folder first and then the archive.
"""

from __future__ import annotations

import contextlib
import io
import re
import zipfile
from pathlib import Path
from typing import Any

from app.core.errors import AppErrorException, make_error

__all__ = ["member_of", "read_attachment"]


def read_attachment(archive_path: Path | str, entry_id: str, name: str, *,
                    folder_path: str | None = None,
                    folder_index: int | None = None) -> bytes:
    """The bytes of attachment `name` on message `entry_id` of the archive.

    Raises `AppErrorException` (`ERR_ATTACHMENT_OPEN`) with what went wrong
    and the way out - which is always "Open in Outlook", beside the button.
    """
    from app.extract import pst_libpff

    path = Path(archive_path)
    try:
        wanted = int(str(entry_id).strip())
    except ValueError:
        # An Outlook EntryID (hexadecimal): the message was read through
        # Outlook, whose numbers libpff does not know.
        raise _error(path, name, "this message was indexed through Outlook, so Leasha "
                                 "cannot find it in the archive by itself") from None
    if not pst_libpff.available():
        raise _error(path, name, "the direct archive reader (libpff) is not installed")

    import pypff

    archive = pypff.file()
    try:
        archive.open(str(path))
    except Exception as exc:
        raise _error(path, name, f"the archive could not be opened ({exc})") from exc
    try:
        message = _find_message(archive, wanted, folder_path, folder_index)
        if message is None:
            raise _error(path, name, "the message is no longer in the archive")
        for found, attachment in pst_libpff._attachments(message):
            if found == name and not isinstance(attachment, BaseException):
                size = int(attachment.get_size() or 0)
                return attachment.read_buffer(size) if size > 0 else b""
        raise _error(path, name, "the message no longer has that attachment")
    finally:
        with contextlib.suppress(Exception):     # closing a read
            archive.close()


#: Where an archive inside an archive ends in a key: `pack/inner.zip/x.docx`.
_NESTED = re.compile(r"^(.+?\.(?:zip|jar|nupkg|whl))/(.+)$", re.IGNORECASE)


def member_of(data: bytes, inner: str) -> bytes | None:
    """`inner` out of zip bytes, or None when they are not a zip that holds it.

    The key of a document read from inside a zip is `<container>/<path inside>`,
    and a zip inside a zip adds another - `pack/inner.zip/x.docx` - so a path
    the zip does not hold is tried as an archive and the rest."""
    inner = inner.replace("\\", "/")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zipped:
            try:
                return zipped.read(inner)
            except KeyError:
                nested = _NESTED.match(inner)
                if nested is None:
                    return None
                return member_of(zipped.read(nested.group(1)), nested.group(2))
    except (zipfile.BadZipFile, KeyError, RuntimeError):
        return None


def _find_message(archive: Any, wanted: int, folder_path: str | None,
                  folder_index: int | None) -> Any:
    from app.extract.pst_libpff import _identifier, _walk_folders

    folders = list(_walk_folders(archive.get_root_folder()))
    where = dict(folders)
    # 1. Where the index says it is.
    folder = where.get(folder_path) if folder_path else None
    if folder is not None and folder_index is not None:
        try:
            message = folder.get_sub_message(int(folder_index))
            if _identifier(message) == wanted:
                return message
        except Exception:
            pass
    # 2. Its folder, then 3. every folder.
    order = ([folder] if folder is not None else []) + [f for _p, f in folders if f is not folder]
    for candidate in order:
        try:
            count = int(candidate.get_number_of_sub_messages())
        except Exception:
            continue
        for index in range(count):
            try:
                message = candidate.get_sub_message(index)
            except Exception:
                continue
            if _identifier(message) == wanted:
                return message
    return None


def _error(path: Path, name: str, why: str) -> AppErrorException:
    return AppErrorException(make_error(
        "ERR_ATTACHMENT_OPEN", "extract.pst_attachment", path=str(path), name=name,
        details=why))
