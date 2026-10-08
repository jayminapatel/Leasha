r"""One attachment, read back out of an Outlook archive by the direct reader.

2026-10-04, the owner: "build the open on attachment, save a copy and open
it". The index holds an attachment's words, not its bytes; to open it in its
own program the bytes are read again from the `.pst` - read-only, through
libpff, with Outlook never started.

Layer: L2

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
from app.core.row_facts import ZIP_FAMILY_EXTS, ext_alternation

__all__ = ["member_of", "read_attachment", "attachment_bytes", "archive_unavailable"]


def read_attachment(archive_path: Path | str, entry_id: str, name: str, *,
                    folder_path: str | None = None,
                    folder_index: int | None = None,
                    search: bool = True,
                    max_bytes: int | None = None) -> bytes:
    """The bytes of attachment `name` on message `entry_id` of the archive.

    Raises `AppErrorException` (`ERR_ATTACHMENT_OPEN`) with what went wrong
    and the way out - which is always "Open in Outlook", beside the button.

    `search=False` takes the message only where the index says it is, never
    the slow search: the preview pane asks for every attachment somebody
    arrows past, and 38 seconds of searching behind a down-arrow would hold a
    worker for nothing. `max_bytes` refuses an attachment larger than that
    before reading it (2026-10-04, the preview pane).
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
        message = _find_message(archive, wanted, folder_path, folder_index, search=search)
        if message is None:
            raise _error(path, name, "the message is no longer in the archive" if search
                         else "the index does not say where the message is")
        for found, attachment in pst_libpff._attachments(message):
            if found == name and not isinstance(attachment, BaseException):
                size = int(attachment.get_size() or 0)
                if max_bytes is not None and size > max_bytes:
                    raise _error(path, name, "it is too large to read into memory")
                return attachment.read_buffer(size) if size > 0 else b""
        raise _error(path, name, "the message no longer has that attachment")
    finally:
        with contextlib.suppress(Exception):     # closing a read
            archive.close()


#: Where an archive inside an archive ends in a key: `pack/inner.zip/x.docx`.
#: 2026-10-04, code review: built from `row_facts.ZIP_FAMILY_EXTS`.
_NESTED = re.compile(r"^(.+?\.(?:" + ext_alternation(ZIP_FAMILY_EXTS) + r"))/(.+)$",
                     re.IGNORECASE)


def member_of(source: bytes | Path | str, inner: str, *,
              max_bytes: int | None = None) -> bytes | None:
    """`inner` out of a zip, or None when it is not a zip that holds it.

    `source` is the zip's bytes, or its path on disk - read from the disk
    as it is needed, never whole into memory (a 5 GB backup zip on disk was
    read entire for one member, until 2026-10-04). `max_bytes` refuses a
    member larger than that, as None, before reading it.

    The key of a document read from inside a zip is `<container>/<path inside>`,
    and a zip inside a zip adds another - `pack/inner.zip/x.docx` - so a path
    the zip does not hold is tried as an archive and the rest."""
    inner = inner.replace("\\", "/")
    opened = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else str(source)
    try:
        with zipfile.ZipFile(opened) as zipped:
            try:
                info = zipped.getinfo(inner)
            except KeyError:
                nested = _NESTED.match(inner)
                if nested is None:
                    return None
                # 2026-10-04, code review: the inner zip is read whole into
                # memory, so `max_bytes` is asked of it first - a 5 GB zip
                # inside a zip was read entire to open one small member.
                outer = zipped.getinfo(nested.group(1))
                if max_bytes is not None and outer.file_size > max_bytes:
                    return None
                return member_of(zipped.read(outer), nested.group(2),
                                 max_bytes=max_bytes)
            if max_bytes is not None and info.file_size > max_bytes:
                return None
            return zipped.read(info)
    except (zipfile.BadZipFile, KeyError, RuntimeError):
        return None


def _find_message(archive: Any, wanted: int, folder_path: str | None,
                  folder_index: int | None, *, search: bool = True) -> Any:
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
    if not search:
        return None
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


#: What `read_attachment` says when the archive itself would not open - held
#: by Outlook, on a drive that is not there. The one failure worth trying again.
_NOT_OPENED = "the archive could not be opened"


def archive_unavailable(exc: BaseException) -> bool:
    """Whether a failed read was the archive not opening, not the attachment
    being gone. No I/O. The first is retried on a later run; the second is an
    answer."""
    error = getattr(exc, "error", None)
    text = " ".join(str(part) for part in (exc, getattr(error, "details", ""),
                                            getattr(error, "detail", "")))
    return _NOT_OPENED in text


def attachment_bytes(message: dict, rest: str, *, max_bytes: int | None = None) -> bytes:
    """The bytes an attachment key names, given its message's `messages` row.

    2026-10-07. `rest` is what follows `/attachments/` in the key: the
    attachment's name, and after a `/` the file inside it when it is an
    archive. Never the slow search - this is asked for a thousand pictures in
    a row. Raises `AppErrorException` (`ERR_ATTACHMENT_OPEN`) as
    `read_attachment` does."""
    name, _, inner = str(rest).partition("/")
    archive = message.get("store_path") or ""
    data = read_attachment(
        archive, str(message.get("entry_id") or ""), name,
        folder_path=message.get("folder_path"), folder_index=message.get("folder_index"),
        search=False, max_bytes=max_bytes)
    if not inner:
        return data
    member = member_of(data, inner, max_bytes=max_bytes)
    if member is None:
        raise _error(Path(archive), inner, f"'{name}' no longer holds that file")
    return member
