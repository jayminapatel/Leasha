r"""Open a file that came out of an email: save a copy, open the copy.

2026-10-04, the owner: "build the open on attachment, save a copy and open
it", and "it must still have the option to open in outlook" - which it has,
beside this, unchanged. A program can only open a file it can find on disk,
and an attachment's bytes live inside the `.pst`, so "Open" writes a copy -
as Outlook itself does when an attachment is double-clicked.

**Where, and for how long.** `<CACHE_PATH>\opened\<key>\<name>`: Leasha's
own cache, never Downloads and never beside the archive. **Read-only**, so
an edit made in Excel cannot be taken for a change to the mail - Excel says
so and offers Save As. Copies from an earlier session are removed when the
next one opens anything (`_sweep`), and the window removes this session's as
it closes (`clear_opened`); a copy still open in its program is left and goes
next time. The archive is only ever read.

**Where the work runs.** This module holds the decisions and the disk
helpers. The worker body that reads the index is `tasks.save_attachment_copy`,
and the worker that runs it is `tasks.open_target`, behind every page's
Open (`workers.open_row_async`, 2026-10-04) - the places
`test_ui_never_blocks` allows a store read and a worker to be.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import stat
import time
from pathlib import Path
from typing import Any

from app.core.errors import AppErrorException, make_error
from app.core.logging import logger

__all__ = [
    "OPENED_FOLDER",
    "bytes_of",
    "clear_opened",
    "is_archive_attachment",
    "opens_from_a_copy",
    "read_zip_member",
    "shown_name",
    "write_copy",
    "zip_member_of",
]

_log = logger.bind(component="ui.attachment_open")

OPENED_FOLDER = "opened"
#: When this process started: a copy older than this is from an earlier session.
_SESSION_STARTED = time.time()


def is_archive_attachment(path: Any) -> bool:
    """A file read out of a mail archive - `pst://<store>/<message>/attachments/<name>`."""
    from app.ui.presenter.mail import attachment_of

    text = str(path or "")
    return text.startswith("pst://") and bool(attachment_of(text)[0])


#: A file inside a zip on disk: `D:\a\backup.zip/q3/report.docx`.
_ZIP_MEMBER = re.compile(r"^(?P<zip>(?![\w.+-]+://).+?\.(?:zip|jar|nupkg|whl))/(?P<inner>.+)$",
                         re.IGNORECASE)


def zip_member_of(path: Any) -> tuple[str, str]:
    """`(the zip on disk, the path inside it)`, or `("", "")`. No I/O.

    2026-10-04, the owner: "the same should be for zips". Not a catalogued
    drive's member (`leasha-volume://`): that drive may not be plugged in."""
    match = _ZIP_MEMBER.match(str(path or ""))
    return (match.group("zip"), match.group("inner")) if match else ("", "")


def opens_from_a_copy(path: Any) -> bool:
    """Whether "Open" has to write a copy first: an attachment, or a zip member."""
    return is_archive_attachment(path) or bool(zip_member_of(path)[0])


def shown_name(path: Any) -> str:
    """The name of the file itself: `report.pdf` for `D:\\a\\b.zip/q3/report.pdf`
    and for `pst://s/1/attachments/pack.zip/q3/report.pdf`. No I/O."""
    from app.ui.presenter.mail import attachment_of

    text = str(path or "")
    zip_path, inside = zip_member_of(text)
    if not zip_path:
        inside = attachment_of(text)[1]
    return inside.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def bytes_of(path: str, message: dict | None, *, reader: Any = None,
             search: bool = True, max_bytes: int | None = None) -> bytes:
    """The bytes of the attachment or zip member `path` names. **Worker body.**

    `message` is the parent message's `messages` row for an attachment (the
    caller reads it - this module does not touch the store), None for a zip
    member. `search` and `max_bytes` are `read_attachment`'s. Raises
    `AppErrorException` (`ERR_ATTACHMENT_OPEN`) with the way out.
    """
    from app.extract.pst_attachment import member_of, read_attachment
    from app.ui.presenter.mail import attachment_of

    zip_path, inside = zip_member_of(path)
    if zip_path:
        return read_zip_member(zip_path, inside, max_bytes=max_bytes)
    parent_path, rest = attachment_of(path)
    name, _, inner = rest.partition("/")
    if not message:
        raise AppErrorException(make_error(
            "ERR_ATTACHMENT_OPEN", "ui.attachment_open", path=parent_path, name=name,
            details="its message is not in the index"))
    data = (reader or read_attachment)(
        message.get("store_path") or "", str(message.get("entry_id") or ""), name,
        folder_path=message.get("folder_path"), folder_index=message.get("folder_index"),
        search=search, max_bytes=max_bytes)
    if inner:
        member = member_of(data, inner, max_bytes=max_bytes)
        if member is None:
            raise AppErrorException(make_error(
                "ERR_ATTACHMENT_OPEN", "ui.attachment_open", path=parent_path, name=inner,
                details=f"'{name}' no longer holds that file"))
        return member
    return data


def read_zip_member(zip_path: str, inside: str, *, max_bytes: int | None = None) -> bytes:
    """`inside` out of the zip on disk - read-only, nested zips too."""
    from app.extract.pst_attachment import member_of

    try:
        if not Path(zip_path).is_file():
            raise FileNotFoundError(zip_path)
        data = member_of(Path(zip_path), inside, max_bytes=max_bytes)
    except OSError as exc:
        raise AppErrorException(make_error(
            "ERR_ATTACHMENT_OPEN", "ui.attachment_open", path=zip_path, name=inside,
            details=f"the zip could not be read ({exc})")) from exc
    if data is None:
        raise AppErrorException(make_error(
            "ERR_ATTACHMENT_OPEN", "ui.attachment_open", path=zip_path, name=inside,
            details="the zip no longer holds that file"))
    return data


def write_copy(path: str, shown: str, data: bytes, cache_path: Path | str) -> Path:
    """`data` as a read-only copy named `shown`, in its own folder for `path`."""
    root = Path(cache_path) / OPENED_FOLDER
    _sweep(root)
    folder = root / hashlib.blake2b(path.encode("utf-8"), digest_size=6).hexdigest()
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / _safe_name(shown)
    if target.exists():
        target.chmod(stat.S_IWRITE | stat.S_IREAD)       # this session's copy, replaced
    target.write_bytes(data)
    target.chmod(stat.S_IREAD)
    return target


def clear_opened(cache_path: Path | str) -> None:
    """Remove the copies. A copy still open in its program stays until next time."""
    root = Path(cache_path) / OPENED_FOLDER
    if root.is_dir():
        shutil.rmtree(root, onerror=_writable_then_retry)


def _sweep(root: Path) -> None:
    """Copies from earlier sessions. Never raises - a leftover is not a failure."""
    if not root.is_dir():
        return
    for folder in root.iterdir():
        try:
            if folder.stat().st_mtime < _SESSION_STARTED:
                shutil.rmtree(folder, onerror=_writable_then_retry)
        except OSError as exc:
            _log.debug("left an old copy at {}: {}", folder, exc)


def _writable_then_retry(function: Any, path: str, _info: Any) -> None:
    """The copies are read-only, which `rmtree` cannot delete on Windows."""
    try:
        Path(path).chmod(stat.S_IWRITE | stat.S_IREAD)
        function(path)
    except OSError:
        pass                                     # open in its program; next time


def _safe_name(name: str) -> str:
    """The attachment's own name, less what Windows will not have in one."""
    cleaned = "".join("_" if c in '<>:"/\\|?*' or ord(c) < 32 else c for c in name).strip(" .")
    return cleaned or "attachment"
