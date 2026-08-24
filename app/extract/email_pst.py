"""Outlook archives and the live mailbox, via MAPI.

Layer: L2

**Why this module has a seam in it.** Everything here ultimately talks to a
running Outlook through COM, which exists only on Windows and cannot be faked at
the COM layer in any way worth trusting. But almost none of the *logic* is about
COM: walking a folder tree, grouping a conversation, deciding a message is empty,
deduplicating an attachment, turning a `com_error` into the right `AppError` and
carrying on. All of that is ordinary code, and all of it is where the bugs live.

So the COM calls are confined to `Win32ComSession`, and everything above it works
against the small `MapiSession` / `MapiFolder` / `MapiItem` duck types. Tests
drive a `FakeSession`, so the walk, the threading, the dedup and every error path
are verified on any machine. What stays unverified until it runs on Windows is
one thin adapter - which is the smallest honest surface this can have.

**Never `pypff`** (no reliable Windows wheels) and **never `extract-msg` for a
`.pst`** - it reads single `.msg` items only. Both are named in
LOCAL_KNOWLEDGE_GRAPH_V2.md because both have been tried and both failed.

**Attachments** are extracted through the normal registry and deduplicated by
content hash: a 30GB archive set contains the same deck mailed round the team
eight times, and indexing all eight produces eight identical search results and
eight times the embedding work. The hash set is supplied by the caller so Layer 3
can persist it in `files.content_hash` and dedup across runs, not just within one.

**Cached Exchange Mode** means the live mailbox is only partly on disk. Nothing
here reaches past the local cache: coverage is whatever Outlook already holds,
and `store_cached_only` records that on every document so the gap is visible
rather than mysterious.
"""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Optional, Protocol, Sequence

from app.core.errors import AppError, AppErrorException, make_error, raise_error
from app.extract.base import Document, SourceKind, register
from app.extract.email_files import build_email_document

__all__ = [
    "PstExtractor",
    "MailItem",
    "MapiSession",
    "walk_session",
    "iter_mailbox_documents",
    "OUTLOOK_EXTENSIONS",
    "DEFAULT_SKIP_FOLDERS",
    "MAX_ATTACHMENT_BYTES",
]

OUTLOOK_EXTENSIONS = frozenset({".pst", ".ost"})

#: Folders whose contents are noise by default. Deleted Items is the big one:
#: on a 15-year archive it is often a third of the messages, all of them things
#: the owner decided they did not want.
DEFAULT_SKIP_FOLDERS = frozenset({
    "deleted items", "junk email", "junk e-mail", "spam",
    "sync issues", "conflicts", "local failures", "server failures",
    "rss feeds", "outbox",
})

#: Attachments above this are not opened. A 200MB video has no text and would
#: cost a temp-file write and a parse attempt to discover that.
MAX_ATTACHMENT_BYTES = 64 * 1024 * 1024


# ---------------------------------------------------------------------------
# The seam: what this module needs from Outlook, expressed without Outlook
# ---------------------------------------------------------------------------

@dataclass
class MailItem:
    """One message, normalised and free of COM."""

    entry_id: str
    subject: str = ""
    sender: str = ""
    recipients: list[str] = field(default_factory=list)
    sent_at: Optional[int] = None
    conversation: Optional[str] = None
    body: str = ""
    attachments: list["MapiAttachment"] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.subject.strip() or self.body.strip() or self.sender.strip())


class MapiAttachment(Protocol):
    filename: str
    size_bytes: int

    def save_to(self, path: Path) -> None: ...


class MapiFolder(Protocol):
    name: str
    path: str

    def folders(self) -> Iterable["MapiFolder"]: ...
    def items(self) -> Iterable[MailItem]: ...


class MapiStore(Protocol):
    display_name: str
    file_path: Optional[str]
    is_live: bool
    cached_only: bool

    def root(self) -> MapiFolder: ...


class MapiSession(Protocol):
    def stores(self) -> Iterable[MapiStore]: ...


# ---------------------------------------------------------------------------
# The logic, testable without Windows
# ---------------------------------------------------------------------------

def _hash_bytes(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=16).hexdigest()


def _message_key(store: MapiStore, item: MailItem) -> str:
    """A stable identity for a message.

    Keyed on `EntryID`, not on a folder path: moving a message between folders
    must not make it look like a new message and re-index it, and on a mailbox
    people reorganise constantly that is the difference between an index that
    settles and one that grows forever.
    """
    return f"pst://{store.display_name}/{item.entry_id}"


def _should_skip_folder(folder: MapiFolder, skip: frozenset[str]) -> bool:
    return folder.name.strip().lower() in skip


def _attachment_documents(
    item: MailItem,
    message_key: str,
    seen_hashes: set[str],
    *,
    warnings: list[AppError],
) -> Iterator[Document]:
    """Extract each attachment through the normal registry, deduplicated.

    The dedup is by content, not by name: `proposal.pptx` and
    `proposal_v2_FINAL.pptx` may be identical bytes, and `report.pdf` from two
    senders usually is. Hashing is cheap next to parsing and embedding.
    """
    from app.extract.base import extract as extract_path

    for attachment in item.attachments:
        name = attachment.filename or "attachment"
        if attachment.size_bytes > MAX_ATTACHMENT_BYTES:
            warnings.append(make_error(
                "ERR_FILE_CORRUPT", "extract.pst",
                path=f"{message_key}/{name}",
                suggestion=(
                    f"Attachment '{name}' is larger than "
                    f"{MAX_ATTACHMENT_BYTES // 1_048_576}MB and was not opened. Files that big "
                    "are almost always media, which hold no text."
                ),
                details=f"{attachment.size_bytes:,} bytes.",
            ))
            continue

        with tempfile.TemporaryDirectory(prefix="lkg_attach_") as scratch:
            target = Path(scratch) / Path(name).name
            try:
                attachment.save_to(target)
                data = target.read_bytes()
            except Exception as exc:                      # noqa: BLE001 - one bad attachment
                warnings.append(make_error(
                    "ERR_FILE_CORRUPT", "extract.pst",
                    path=f"{message_key}/{name}",
                    details=f"Could not be saved out of the message: {exc}",
                ))
                continue

            digest = _hash_bytes(data)
            if digest in seen_hashes:
                continue          # the same bytes are already indexed somewhere
            seen_hashes.add(digest)

            try:
                for document in extract_path(target):
                    document.virtual_path = f"{message_key}/attachments/{name}"
                    document.source_kind = SourceKind.PST_MESSAGE
                    document.meta.setdefault("attachment_of", message_key)
                    document.meta.setdefault("attachment_name", name)
                    document.meta.setdefault("content_hash", digest)
                    yield document
            except AppErrorException as exc:
                # An unreadable attachment is a skip, never the end of the run.
                warnings.append(exc.error)


def walk_session(
    session: MapiSession,
    *,
    include_live: bool = True,
    only_paths: Optional[Sequence[str]] = None,
    skip_folders: frozenset[str] = DEFAULT_SKIP_FOLDERS,
    with_attachments: bool = True,
    seen_hashes: Optional[set[str]] = None,
    on_folder: Optional[Callable[[str, int], None]] = None,
) -> Iterator[Document]:
    """Walk stores, folders and messages, yielding one Document per message.

    `on_folder(folder_path, messages_yielded)` fires as each folder completes.
    That is the checkpoint boundary: a 4GB archive is resumable at folder
    granularity, so an interrupted run restarts at the folder it was in rather
    than at the beginning.

    A `com_error` inside one folder is converted to `ERR_OUTLOOK_BUSY` and the
    walk moves to the next folder. Outlook being closed mid-run must cost the
    remaining folders, never the folders already done.
    """
    seen_hashes = seen_hashes if seen_hashes is not None else set()
    wanted = {str(p).lower() for p in only_paths} if only_paths else None

    for store in session.stores():
        if store.is_live and not include_live:
            continue
        if wanted is not None:
            file_path = (store.file_path or "").lower()
            if not file_path or file_path not in wanted:
                continue

        try:
            root = store.root()
        except Exception as exc:                          # noqa: BLE001
            raise AppErrorException(make_error(
                "ERR_OUTLOOK_BUSY", "extract.pst",
                folder=store.display_name, details=str(exc),
            )) from exc

        yield from _walk_folder(
            store, root,
            skip_folders=skip_folders,
            with_attachments=with_attachments,
            seen_hashes=seen_hashes,
            on_folder=on_folder,
        )


def _walk_folder(
    store: MapiStore,
    folder: MapiFolder,
    *,
    skip_folders: frozenset[str],
    with_attachments: bool,
    seen_hashes: set[str],
    on_folder: Optional[Callable[[str, int], None]],
) -> Iterator[Document]:
    if _should_skip_folder(folder, skip_folders):
        return

    produced = 0
    try:
        items = list(folder.items())
    except Exception as exc:                              # noqa: BLE001 - Outlook closed or busy
        # One unreachable folder, not a failed run. The remaining folders are
        # still walked, and the next incremental pass retries this one.
        yield from ()
        _record_busy(store, folder, exc)
        items = []

    for item in items:
        if item.is_empty:
            continue
        key = _message_key(store, item)
        warnings: list[AppError] = []

        attachment_documents: list[Document] = []
        if with_attachments and item.attachments:
            attachment_documents = list(
                _attachment_documents(item, key, seen_hashes, warnings=warnings)
            )

        document = build_email_document(
            Path(store.file_path or store.display_name),
            subject=item.subject,
            sender=item.sender,
            recipients=list(item.recipients),
            sent_at=item.sent_at,
            conversation=item.conversation,
            body=item.body,
            attachments=[a.filename for a in item.attachments],
            source_kind=SourceKind.PST_MESSAGE,
            entry_id=item.entry_id,
            store_path=store.file_path,
        )
        document.virtual_path = key
        document.meta["folder_path"] = folder.path
        document.meta["store_name"] = store.display_name
        document.meta["store_cached_only"] = store.cached_only
        document.warnings = (*document.warnings, *warnings)

        yield document
        produced += 1

        for attached in attachment_documents:
            attached.meta.setdefault("folder_path", folder.path)
            yield attached
            produced += 1

    if on_folder is not None:
        on_folder(folder.path, produced)

    try:
        children = list(folder.folders())
    except Exception as exc:                              # noqa: BLE001
        _record_busy(store, folder, exc)
        children = []

    for child in children:
        yield from _walk_folder(
            store, child,
            skip_folders=skip_folders,
            with_attachments=with_attachments,
            seen_hashes=seen_hashes,
            on_folder=on_folder,
        )


#: Folders that could not be read, as AppErrors. Collected rather than raised so
#: one closed Outlook does not end a walk that has already produced thousands of
#: messages; the caller reports them at the end.
_BUSY_FOLDERS: list[AppError] = []


def _record_busy(store: MapiStore, folder: MapiFolder, exc: BaseException) -> None:
    _BUSY_FOLDERS.append(make_error(
        "ERR_OUTLOOK_BUSY", "extract.pst",
        folder=f"{store.display_name}/{folder.path}",
        details=f"{type(exc).__name__}: {exc}",
    ))


def drain_busy_folders() -> list[AppError]:
    """Take and clear the folders that could not be read during the last walk."""
    global _BUSY_FOLDERS
    busy, _BUSY_FOLDERS = _BUSY_FOLDERS, []
    return busy


# ---------------------------------------------------------------------------
# The COM adapter - the only part that needs Windows
# ---------------------------------------------------------------------------

def _to_epoch(value: Any) -> Optional[int]:
    """pywintypes datetimes are datetime-alike; anything else is discarded."""
    if value is None:
        return None
    try:
        if isinstance(value, datetime):
            return int(value.timestamp())
        return int(datetime.fromtimestamp(float(value)).timestamp())
    except (ValueError, OSError, OverflowError, TypeError):
        return None


class _ComAttachment:
    def __init__(self, com_attachment: Any) -> None:
        self._attachment = com_attachment
        self.filename = str(getattr(com_attachment, "FileName", "") or "attachment")
        try:
            self.size_bytes = int(getattr(com_attachment, "Size", 0) or 0)
        except (TypeError, ValueError):
            self.size_bytes = 0

    def save_to(self, path: Path) -> None:
        self._attachment.SaveAsFile(str(path))


class _ComFolder:
    def __init__(self, com_folder: Any, parent_path: str = "") -> None:
        self._folder = com_folder
        self.name = str(getattr(com_folder, "Name", "") or "")
        self.path = f"{parent_path}/{self.name}".strip("/")

    def folders(self) -> Iterator["_ComFolder"]:
        for child in self._folder.Folders:
            yield _ComFolder(child, self.path)

    def items(self) -> Iterator[MailItem]:
        collection = self._folder.Items
        # Sorting by received time makes the walk order stable, which is what
        # makes a folder-level checkpoint mean anything on a resumed run.
        try:
            collection.Sort("[ReceivedTime]", True)
        except Exception:                                 # noqa: BLE001 - not every folder supports it
            pass

        for entry in collection:
            item = _com_item(entry)
            if item is not None:
                yield item


def _com_item(entry: Any) -> Optional[MailItem]:
    """Normalise one COM item. Returns None for things with no text at all."""
    try:
        entry_id = str(getattr(entry, "EntryID", "") or "")
        subject = str(getattr(entry, "Subject", "") or "")
        body = str(getattr(entry, "Body", "") or "")
        if not body:
            html = str(getattr(entry, "HTMLBody", "") or "")
            if html:
                from app.extract.email_files import html_to_text

                body = html_to_text(html)

        sender = _com_sender(entry)
        recipients = []
        for recipient in getattr(entry, "Recipients", ()) or ():
            address = str(getattr(recipient, "Address", "") or "")
            name = str(getattr(recipient, "Name", "") or "")
            recipients.append(address or name)

        attachments = [
            _ComAttachment(a) for a in (getattr(entry, "Attachments", ()) or ())
        ]

        conversation = (
            str(getattr(entry, "ConversationID", "") or "")
            or str(getattr(entry, "ConversationTopic", "") or "")
            or None
        )

        return MailItem(
            entry_id=entry_id,
            subject=subject,
            sender=sender,
            recipients=[r for r in recipients if r],
            sent_at=_to_epoch(getattr(entry, "SentOn", None))
            or _to_epoch(getattr(entry, "ReceivedTime", None)),
            conversation=conversation,
            body=body,
            attachments=attachments,
        )
    except Exception:                                     # noqa: BLE001 - one odd item
        return None


def _com_sender(entry: Any) -> str:
    """SMTP address if it can be had, otherwise the display name.

    An Exchange sender's `SenderEmailAddress` is an X.500 string beginning
    `/O=EXCHANGELABS/...`, which nobody would ever type into a search box. The
    Exchange user lookup turns it into a real address; it fails on old archives
    whose sender no longer resolves, so the display name is the fallback.
    """
    address = str(getattr(entry, "SenderEmailAddress", "") or "")
    if address.startswith("/") or not address:
        try:
            exchange_user = entry.Sender.GetExchangeUser()
            if exchange_user is not None:
                resolved = str(exchange_user.PrimarySmtpAddress or "")
                if resolved:
                    return resolved
        except Exception:                                 # noqa: BLE001
            pass
        return str(getattr(entry, "SenderName", "") or address)
    return address


class _ComStore:
    def __init__(self, com_store: Any) -> None:
        self._store = com_store
        self.display_name = str(getattr(com_store, "DisplayName", "") or "")
        self.file_path = str(getattr(com_store, "FilePath", "") or "") or None
        extension = Path(self.file_path).suffix.lower() if self.file_path else ""
        self.is_live = extension not in OUTLOOK_EXTENSIONS
        # An .ost IS the Cached Exchange Mode cache, so what it holds is exactly
        # what was cached - which may be a fraction of the mailbox.
        self.cached_only = extension == ".ost"

    def root(self) -> _ComFolder:
        return _ComFolder(self._store.GetRootFolder())


class Win32ComSession:
    """The live Outlook, through MAPI. Windows only.

    `attach()` opens a `.pst` that Outlook does not already have loaded, and
    `close()` detaches only the stores this session attached - never one the
    user already had open, which would silently remove an archive from their
    Outlook and look exactly like data loss.
    """

    def __init__(self) -> None:
        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:
            raise_error("ERR_OUTLOOK_MISSING", "extract.pst", details=str(exc))
            raise  # unreachable; raise_error always raises

        # COM is per-thread, and the indexing pipeline extracts on worker
        # threads. Without this, `Dispatch` fails with "CoInitialize has not
        # been called" anywhere except the main thread - which is exactly why
        # `extract --mailbox` worked from the command line while indexing the
        # same archive from the GUI did nothing.
        self._com_initialised = False
        try:
            pythoncom.CoInitialize()
            self._com_initialised = True
        except Exception:                                 # noqa: BLE001 - already initialised is fine
            pass
        self._pythoncom = pythoncom

        try:
            self._outlook = win32com.client.Dispatch("Outlook.Application")
            self._namespace = self._outlook.GetNamespace("MAPI")
        except Exception as exc:                          # noqa: BLE001
            raise AppErrorException(make_error(
                "ERR_OUTLOOK_MISSING", "extract.pst",
                details=f"Outlook did not start: {exc}",
            )) from exc

        self._attached: list[str] = []

    def stores(self) -> Iterator[_ComStore]:
        for store in self._namespace.Stores:
            yield _ComStore(store)

    def attach(self, path: Path) -> None:
        """Load a .pst into Outlook if it is not already there."""
        target = str(path).lower()
        for store in self.stores():
            if (store.file_path or "").lower() == target:
                return
        try:
            self._namespace.AddStore(str(path))
            self._attached.append(target)
        except Exception as exc:                          # noqa: BLE001
            raise AppErrorException(make_error(
                "ERR_FILE_CORRUPT", "extract.pst",
                path=str(path),
                details=f"Outlook refused to open the archive: {exc}",
            )) from exc

    def close(self) -> None:
        try:
            self._detach()
        finally:
            if getattr(self, "_com_initialised", False):
                try:
                    self._pythoncom.CoUninitialize()
                except Exception:                         # noqa: BLE001
                    pass
                self._com_initialised = False

    def _detach(self) -> None:
        for store in self.stores():
            if (store.file_path or "").lower() in self._attached:
                try:
                    self._namespace.RemoveStore(store._store.GetRootFolder())
                except Exception:                         # noqa: BLE001 - leaving it attached is harmless
                    pass
        self._attached.clear()

    def __enter__(self) -> "Win32ComSession":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def iter_mailbox_documents(
    *,
    include_live: bool = True,
    seen_hashes: Optional[set[str]] = None,
    session: Optional[MapiSession] = None,
    **options: Any,
) -> Iterator[Document]:
    """Every message Outlook can reach: attached archives and the live mailbox.

    `session` is injectable so this is testable; left None it builds a real
    `Win32ComSession` and will raise `ERR_OUTLOOK_MISSING` off Windows.
    """
    owned = session is None
    active = session or Win32ComSession()
    try:
        yield from walk_session(
            active, include_live=include_live, seen_hashes=seen_hashes, **options
        )
    finally:
        if owned and hasattr(active, "close"):
            active.close()


class PstExtractor:
    """Registered for `.pst` and `.ost`, so an archive is a first-class input."""

    name = "pst"
    extensions = OUTLOOK_EXTENSIONS

    #: Read through Outlook, which holds the archive open - so it must never be
    #: byte-hashed. On the first real run that hash raised a permission error in
    #: the walker and took the entire index run down with it.
    reads_externally = True

    #: Injectable for tests; None means build a real Win32ComSession.
    session_factory: Optional[Callable[[], MapiSession]] = None

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        if self.session_factory is not None:
            session = self.session_factory()
            yield from walk_session(session, include_live=False, only_paths=[str(path)])
            return

        session = Win32ComSession()
        try:
            session.attach(path)
            yield from walk_session(session, include_live=False, only_paths=[str(path)])
        finally:
            session.close()


register(PstExtractor())
