"""Reading `.pst` archives directly, without Outlook.

Layer: L2

Outlook was the only route for a long time, and it costs more than it looks.
Reading an archive through MAPI means **attaching it into the user's live Outlook
profile**, which mutates their mail setup; it holds a file lock, which is what
silently ended a whole index run; it needs COM, which is per-thread and made the
CLI work while the GUI did nothing; and it needs *classic* Outlook, which
Microsoft is steadily replacing with a web app that has no COM and cannot open a
`.pst` at all.

libpff reads the file. No Outlook, no COM, no lock, no profile changes - and,
unlike every other approach tried here, **it can be tested on any machine**,
which is why the largest untested surface in the project finally has a floor
under it.

**It is optional, and the import is guarded.** `libpff-python` has no Windows
wheel and compiles during install, which breaks the promise at the top of
`requirements.txt` that every pin ships one. So it is never a hard dependency:
absent, this module reports that clearly and the Outlook path still works.

**What it does not do.** `.ost` - the Cached Exchange Mode file that *is* the
live mailbox - is only partly readable by libpff, and the live mailbox is
Outlook's own business anyway. So the division is: **libpff for offline archives,
Outlook for live mail.** Each doing the job it is actually good at.
"""

from __future__ import annotations

import email.utils
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

from app.core.errors import AppError, AppErrorException, make_error, raise_error
from app.core.logging import logger
from app.extract.base import Document, SourceKind, looks_locked, with_closing_warning
from app.extract.email_files import build_email_document, html_to_text

__all__ = ["available", "read_archive", "export_to_eml", "LibpffUnavailable"]

_log = logger.bind(component="extract.pst_libpff")

_HEADER_ADDRESSES = re.compile(r"^(To|Cc):\s*(.+)$", re.IGNORECASE | re.MULTILINE)
_HEADER_FIELD = re.compile(r"^{}:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


class LibpffUnavailable(RuntimeError):
    """libpff is not installed. Never fatal - the Outlook path still exists."""


def available() -> bool:
    """True if archives can be read without Outlook on this machine."""
    try:
        import pypff  # noqa: F401
    except ImportError:
        return False
    return True


def _require() -> Any:
    try:
        import pypff
    except ImportError as exc:
        raise LibpffUnavailable(str(exc)) from exc
    return pypff


def _header(headers: str, field: str) -> str:
    match = re.search(rf"^{field}:\s*(.+)$", headers or "", re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else ""


def _addresses(headers: str) -> list[str]:
    """Recipients from the transport headers.

    libpff exposes recipients as MAPI record sets, which are awkward and often
    hold Exchange X.500 addresses nobody would ever type. The RFC822 headers the
    message was delivered with carry real addresses, so they are the better
    source when present.
    """
    # Joined with a comma, not a space. `getaddresses` was hardened against
    # malformed input (CVE-2023-27043) and now returns *nothing at all* rather
    # than doing its best - so running "To" and "Cc" together with a space
    # silently emptied the recipient list for every message in every archive.
    parts = [match.group(2).strip().rstrip(",") for match in _HEADER_ADDRESSES.finditer(headers or "")]
    raw = ", ".join(part for part in parts if part)
    return [address for _name, address in email.utils.getaddresses([raw]) if address]


def _sent_at(message: Any) -> Optional[int]:
    for accessor in ("get_client_submit_time", "get_delivery_time", "get_creation_time"):
        try:
            value = getattr(message, accessor)()
        except Exception:                        # noqa: BLE001 - libpff raises on absent fields
            continue
        if isinstance(value, datetime):
            try:
                return int(value.timestamp())
            except (OSError, OverflowError, ValueError):
                continue
    return None


def _text(message: Any) -> str:
    """Plain body if there is one, else the HTML stripped, else RTF's text."""
    for accessor, transform in (
        ("get_plain_text_body", lambda v: v),
        ("get_html_body", html_to_text),
        ("get_rtf_body", html_to_text),
    ):
        try:
            raw = getattr(message, accessor)()
        except Exception:                        # noqa: BLE001
            continue
        if not raw:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        body = transform(raw)
        if body.strip():
            return body
    return ""


def _safe(message: Any, accessor: str) -> str:
    try:
        value = getattr(message, accessor)()
    except Exception:                            # noqa: BLE001
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value or "")


class _Report:
    """What a read could not reach, so the run can say so instead of only logging it.

    Before this, an unreadable message or folder went to the log file and
    nowhere else: an archive that was 60% indexed looked identical to one that
    was complete.
    """

    def __init__(self) -> None:
        self.read = 0
        self.messages = 0
        self.folders = 0
        self.examples: list[str] = []

    @property
    def failed(self) -> bool:
        return bool(self.messages or self.folders)

    def message_failed(self, folder_path: str, index: int, exc: BaseException) -> None:
        self.messages += 1
        self._note(f"message {index} in {folder_path}", exc)

    def folder_failed(self, folder_path: str, exc: BaseException) -> None:
        self.folders += 1
        self._note(f"folder {folder_path}", exc)

    def _note(self, where: str, exc: BaseException) -> None:
        _log.warning("{} unreadable: {}: {}", where, type(exc).__name__, exc)
        if len(self.examples) < 3:
            self.examples.append(f"{where}: {type(exc).__name__}")

    def reason(self) -> str:
        parts = []
        if self.messages:
            parts.append(f"{self.messages} message{'s' if self.messages != 1 else ''}")
        if self.folders:
            parts.append(f"{self.folders} folder{'s' if self.folders != 1 else ''}")
        return " and ".join(parts) + f" could not be read ({self.read} were)"


def _walk_folders(
    folder: Any, path: str = "", report: Optional[_Report] = None,
) -> Iterator[tuple[str, Any]]:
    """Every folder in the archive, depth first, with its path."""
    name = _safe(folder, "get_name") or "(unnamed)"
    here = f"{path}/{name}".strip("/")
    yield here, folder

    try:
        count = folder.get_number_of_sub_folders()
    except Exception as exc:                     # noqa: BLE001
        # A whole subtree vanishes here, so it is recorded rather than shrugged off.
        if report is not None:
            report.folder_failed(here, exc)
        count = 0
    for index in range(count):
        try:
            child = folder.get_sub_folder(index)
        except Exception as exc:                 # noqa: BLE001 - one bad folder, not a bad archive
            if report is not None:
                report.folder_failed(f"{here}/#{index}", exc)
            else:
                _log.warning("folder {} of {} unreadable: {}", index, here, exc)
            continue
        yield from _walk_folders(child, here, report)


def _open_error(path: Path, exc: BaseException) -> AppErrorException:
    """The right skip code for a failed open.

    A lock and a damaged file need opposite handling - see `looks_locked` - and
    the second is the only one `scanpst` is any use for.
    """
    if looks_locked(exc):
        return AppErrorException(make_error(
            "ERR_FILE_LOCKED", "extract.pst", path=str(path),
            details=f"libpff could not open the archive: {exc}",
            suggestion="Another program has this archive open, most likely Outlook. Close it, "
                       "or leave the backend on 'auto' so the archive is read through Outlook. "
                       "It is retried automatically on the next pass.",
        ))
    return AppErrorException(make_error(
        "ERR_FILE_CORRUPT", "extract.pst",
        path=str(path),
        details=f"libpff could not open the archive: {exc}",
        suggestion="The archive may be damaged or password-protected. Run scanpst.exe "
                   "against it, or index it through Outlook instead.",
    ))


def read_archive(
    path: Path,
    *,
    skip_folders: Optional[frozenset[str]] = None,
) -> Iterator[Document]:
    """Every message in a `.pst`, as Documents. No Outlook involved.

    Deliberately mirrors `email_pst.walk_session`'s output so both backends are
    interchangeable: same `Document` shape, same `meta` keys, same conversation
    grouping. Layer 3 cannot tell which one produced a message, and should not.

    **Known gap:** attachment *names* are captured and searchable, but their
    contents are not yet extracted on this path - the Outlook backend does that.
    Reading attachment bytes through libpff means walking MAPI record sets, which
    is a job of its own; it is recorded in HANDOFF.md rather than half-done here.
    """
    from app.extract.email_pst import DEFAULT_SKIP_FOLDERS

    skip = skip_folders if skip_folders is not None else DEFAULT_SKIP_FOLDERS
    pypff = _require()

    archive = pypff.file()
    try:
        archive.open(str(path))
    except Exception as exc:                     # noqa: BLE001
        raise _open_error(path, exc) from exc

    store_name = path.stem
    try:
        root = archive.get_root_folder()
    except Exception as exc:                     # noqa: BLE001
        archive.close()
        raise AppErrorException(make_error(
            "ERR_FILE_CORRUPT", "extract.pst", path=str(path),
            details=f"the archive has no readable root folder: {exc}",
        )) from exc

    report = _Report()

    def closing() -> Optional[AppError]:
        if not report.failed:
            return None
        if report.read == 0:
            # Nothing came out at all. Returning quietly would let the pipeline
            # call this "no text", which sends the owner looking in the wrong place.
            raise AppErrorException(make_error(
                "ERR_FILE_CORRUPT", "extract.pst", path=str(path),
                details=f"{report.reason()}; first failures: {'; '.join(report.examples)}",
            ))
        return make_error(
            "ERR_PST_PARTIAL", "extract.pst", path=str(path), reason=report.reason(),
            details="; ".join(report.examples),
        )

    try:
        yield from with_closing_warning(
            _messages(root, path, store_name, skip, report), closing)
    finally:
        try:
            archive.close()
        except Exception:                        # noqa: BLE001
            pass


def _messages(
    root: Any, path: Path, store_name: str, skip: frozenset[str], report: _Report,
) -> Iterator[Document]:
    """Every readable message, recording the ones that are not.

    **The conversion is inside the guard, not only the fetch.** A message that
    fetched fine but broke `_to_document` used to raise out of the generator and
    end the archive - every message after it never read. One bad message now
    costs one message.
    """
    for folder_path, folder in _walk_folders(root, report=report):
        leaf = folder_path.rsplit("/", 1)[-1].strip().lower()
        if leaf in skip:
            continue

        try:
            count = folder.get_number_of_sub_messages()
        except Exception as exc:                 # noqa: BLE001
            report.folder_failed(folder_path, exc)
            continue

        for index in range(count):
            try:
                message = folder.get_sub_message(index)
                document = _to_document(message, path, store_name, folder_path)
            except Exception as exc:             # noqa: BLE001 - one bad message, not a bad run
                report.message_failed(folder_path, index, exc)
                continue

            if document is not None:
                report.read += 1
                yield document


def _to_document(
    message: Any,
    archive_path: Path,
    store_name: str,
    folder_path: str,
) -> Optional[Document]:
    headers = _safe(message, "get_transport_headers")
    subject = _safe(message, "get_subject")
    sender = _header(headers, "From") or _safe(message, "get_sender_name")
    if "<" in sender:
        _name, _, address = sender.partition("<")
        sender = address.rstrip(">").strip() or sender

    body = _text(message)
    if not (subject.strip() or body.strip() or sender.strip()):
        return None

    try:
        identifier = str(message.get_identifier())
    except Exception:                            # noqa: BLE001
        identifier = f"{folder_path}#{id(message)}"

    # Same threading rule as the .eml path: the root of References is the thread,
    # so a reply groups with what it replies to. Falling back to the MAPI
    # conversation topic keeps archives without headers grouped by subject.
    references = _header(headers, "References").split()
    conversation = (
        references[0] if references
        else _header(headers, "In-Reply-To")
        or _header(headers, "Message-ID")
        or _safe(message, "get_conversation_topic")
        or None
    )

    attachment_names = _attachment_names(message)
    document = build_email_document(
        archive_path,
        subject=subject,
        sender=sender,
        recipients=_addresses(headers),
        sent_at=_sent_at(message),
        conversation=conversation,
        body=body,
        attachments=attachment_names,
        source_kind=SourceKind.PST_MESSAGE,
        entry_id=identifier,
        store_path=str(archive_path),
    )
    document.virtual_path = f"pst://{store_name}/{identifier}"
    document.meta["folder_path"] = folder_path
    document.meta["store_name"] = store_name
    document.meta["store_cached_only"] = False
    document.meta["backend"] = "libpff"
    return document


def _attachment_names(message: Any) -> list[str]:
    try:
        count = message.get_number_of_attachments()
    except Exception:                            # noqa: BLE001
        return []

    names: list[str] = []
    for index in range(count):
        try:
            attachment = message.get_attachment(index)
            names.append(_attachment_name(attachment, index))
        except Exception:                        # noqa: BLE001
            names.append(f"attachment-{index}")
    return names


def _attachment_name(attachment: Any, index: int) -> str:
    for accessor in ("get_name", "get_long_filename", "get_filename"):
        try:
            value = getattr(attachment, accessor)()
        except Exception:                        # noqa: BLE001
            continue
        if value:
            return str(value)
    return f"attachment-{index}"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_to_eml(
    path: Path,
    destination: Path,
    *,
    skip_folders: Optional[frozenset[str]] = None,
    on_progress: Optional[Any] = None,
) -> int:
    """Write every message out as an `.eml` file. Returns how many were written.

    The permanent escape hatch. Once an archive is EML on disk it needs neither
    Outlook nor libpff ever again - it is just a folder of files the ordinary
    `.eml` extractor already handles, which can be backed up, inspected in any
    mail client, and re-indexed by anything.

    Folder structure is preserved, so `Inbox/2019/subject.eml` still says where
    the message lived.
    """
    from app.extract.email_pst import DEFAULT_SKIP_FOLDERS

    skip = skip_folders if skip_folders is not None else DEFAULT_SKIP_FOLDERS
    pypff = _require()
    destination.mkdir(parents=True, exist_ok=True)

    archive = pypff.file()
    try:
        archive.open(str(path))
    except Exception as exc:                     # noqa: BLE001
        raise _open_error(path, exc) from exc

    written = 0
    try:
        for folder_path, folder in _walk_folders(archive.get_root_folder()):
            if folder_path.rsplit("/", 1)[-1].strip().lower() in skip:
                continue
            try:
                count = folder.get_number_of_sub_messages()
            except Exception:                    # noqa: BLE001
                continue

            target_dir = destination / _safe_relative(folder_path)
            for index in range(count):
                try:
                    message = folder.get_sub_message(index)
                    body = _eml_bytes(message)
                except Exception as exc:         # noqa: BLE001
                    _log.warning("message {} in {} not exported: {}", index, folder_path, exc)
                    continue

                target_dir.mkdir(parents=True, exist_ok=True)
                name = _safe_filename(_safe(message, "get_subject") or f"message-{index}")
                target = _unique(target_dir / f"{name}.eml")
                target.write_bytes(body)
                written += 1

                if on_progress is not None and written % 100 == 0:
                    on_progress(written)
    finally:
        try:
            archive.close()
        except Exception:                        # noqa: BLE001
            pass

    return written


def _eml_bytes(message: Any) -> bytes:
    """Reassemble an RFC822 message from what libpff exposes.

    The stored transport headers are used verbatim when present - they are the
    real ones - and a minimal set is synthesised when they are not, which is the
    case for messages this mailbox sent rather than received.
    """
    headers = _safe(message, "get_transport_headers").strip()
    body = _text(message)

    if not headers:
        subject = _safe(message, "get_subject")
        sender = _safe(message, "get_sender_name")
        sent = _sent_at(message)
        lines = [f"Subject: {subject}", f"From: {sender}"]
        if sent:
            lines.append(f"Date: {email.utils.formatdate(sent)}")
        headers = "\n".join(lines)

    return (headers.rstrip() + "\n\n" + body).encode("utf-8", errors="replace")


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(name: str, *, limit: int = 80) -> str:
    cleaned = _UNSAFE.sub("_", name).strip(". ") or "message"
    return cleaned[:limit]


def _safe_relative(folder_path: str) -> Path:
    return Path(*[_safe_filename(part) for part in folder_path.split("/") if part])


def _unique(target: Path) -> Path:
    """Never overwrite: two messages can share a subject, and often do."""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for counter in range(1, 10_000):
        candidate = target.with_name(f"{stem}_{counter}{suffix}")
        if not candidate.exists():
            return candidate
    return target.with_name(f"{stem}_{id(target)}{suffix}")
