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

**On the alternatives.** `extract-msg` reads single `.msg` items only and is never
a route into a `.pst`. `pypff` is different: the architecture doc rules it out for
having no Windows wheel, and that is true - but "no wheel" means it compiles at
install time, not that it cannot be used. Tested directly, `libpff-python` builds
and works, so it now lives in `pst_libpff.py` as the optional direct backend and
`choose_backend()` decides between the two. `.ost` still comes here regardless:
it is Outlook's own cache and libpff reads it poorly.

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
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Protocol, Sequence

from app.core.errors import AppError, AppErrorException, make_error, raise_error
from app.core.format_health import Requirement
from app.core.logging import logger
from app.core.row_facts import OUTLOOK_ARCHIVE_EXTS, suffixes
from app.extract import progress
from app.extract.archive import attachment_key
from app.extract.base import Document, SourceKind, looks_locked, register, with_closing_warning
from app.extract.email_files import build_email_document

__all__ = [
    "PstExtractor",
    "PstBackend",
    "choose_backend",
    "MailItem",
    "MapiSession",
    "walk_session",
    "iter_mailbox_documents",
    "OUTLOOK_EXTENSIONS",
    "archive_marker",
    "MARKER_PREFIX",
    "marker_difference",
    "DEFAULT_SKIP_FOLDERS",
    "MAX_ATTACHMENT_BYTES",
]

_log = logger.bind(component="extract.pst")

OUTLOOK_EXTENSIONS = suffixes(OUTLOOK_ARCHIVE_EXTS)    # 2026-10-04, code review: one list

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
    from app.extract import junk_images, reading
    from app.extract import mail_attachments as rules
    from app.extract.base import extract as extract_path

    policy = reading.current()
    for attachment in item.attachments:
        name = attachment.filename or "attachment"
        # The same rule as the libpff reader - see `mail_attachments` (2026-10-01).
        kind = rules.rule(name)
        if kind == rules.NAME_ONLY:
            yield rules.name_only_document(name, message_key, backend="outlook")
            continue
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
            if kind == rules.NAMES_INSIDE:
                if digest not in seen_hashes:
                    seen_hashes.add(digest)
                    yield rules.name_only_document(
                        name, message_key, inside=rules.names_inside(data),
                        digest=digest, backend="outlook")
                continue
            # Order 0z lane D: the junk-image filter, pictures only - D1, D3
            # and D4. **Not D2**: whether Outlook marks the attachment inline
            # or hidden is a MAPI property this backend does not read yet, so
            # "decorative" is never decided here. UNVERIFIED on Windows: this
            # path needs classic Outlook and has not run under the filter.
            book = policy.junk if _is_picture(name) else None
            if book is not None:
                book.saw(digest)
            if digest in seen_hashes:
                continue          # the same bytes are already indexed somewhere
            seen_hashes.add(digest)
            screened = None
            if book is not None:
                screened = junk_images.screen(book, digest, data, inline=False)
                if screened.reason:
                    policy.left_unread(screened.reason)
                    continue

            try:
                documents = extract_path(target)
                if book is not None:
                    documents, why = junk_images.settle(
                        book, digest, list(documents), screened)
                    if why:
                        policy.left_unread(why)
                        continue
                for document in documents:
                    # The member's own path kept after the attachment's (2026-09-30).
                    document.virtual_path = attachment_key(
                        message_key, name, document.virtual_path, target)
                    document.source_kind = SourceKind.PST_MESSAGE
                    document.meta.setdefault("attachment_of", message_key)
                    document.meta.setdefault("attachment_name", name)
                    # 2026-10-04: its own size, for the row. Without it the
                    # row carried the archive's (4.9 GB on a 400 KB sheet).
                    document.meta.setdefault("attachment_size", len(data))
                    document.meta.setdefault("content_hash", digest)
                    yield document
            except AppErrorException as exc:
                if book is not None and exc.error.code == "ERR_NO_TEXT_LAYER":
                    junk_images.settle(book, digest, [], screened)   # no words: D1
                # An unreadable attachment is a skip, never the end of the run.
                warnings.append(exc.error)
            except Exception as exc:                      # noqa: BLE001
                # Order 0z lane C: a reader failing with anything else - `xlrd`
                # raised `struct.error` on a damaged `.xls` - ended the archive
                # on the libpff path, measured. The same hole was here.
                warnings.append(make_error(
                    "ERR_FILE_CORRUPT", "extract.pst", path=f"{message_key}/{name}",
                    details=f"Could not be read: {type(exc).__name__}: {exc}",
                ))


def _is_picture(name: str) -> bool:
    """Would this attachment be read by OCR? Never raises."""
    from app.extract.base import reads_by_ocr

    try:
        return reads_by_ocr(Path(name))
    except Exception:                            # noqa: BLE001
        return False


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

        # 2026-10-02: the frame the libpff route has always opened (work order
        # 0x section 3b), which this route never did. An archive read through
        # Outlook therefore had no position at all on the Indexing page - the
        # file's name and a clock - and `2009.pst`, which was handing over
        # 200 to 2,200 documents every hour, was Force-skipped after 16 h 58
        # min as stuck. `total` stays None: MAPI gives no count worth trusting
        # without a second walk, and the frame's own rule is never to guess.
        # 2026-10-05: `name_of` - Outlook hands a Windows path whatever reads it.
        from app.core.osbridge.pathnames import name_of

        name = name_of(store.file_path) if store.file_path else store.display_name
        with progress.enter("pst", name, unit="message",
                            stage=progress.STAGE_OPENING) as frame:
            try:
                root = store.root()
            except Exception as exc:                      # noqa: BLE001
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
                frame=frame,
            )


def _folder_shown(folder_path: str) -> str:
    """A folder path for the progress line: without the store's root folder,
    whose name is the archive's own and is already on the line."""
    return "/".join(part for part in str(folder_path or "").split("/")[1:] if part)


def _walk_folder(
    store: MapiStore,
    folder: MapiFolder,
    *,
    skip_folders: frozenset[str],
    with_attachments: bool,
    seen_hashes: set[str],
    on_folder: Optional[Callable[[str, int], None]],
    frame: Optional[progress.Frame] = None,
) -> Iterator[Document]:
    if _should_skip_folder(folder, skip_folders):
        return

    if frame is None:
        # A caller with no frame of its own still gets a working one; nobody
        # reads it, and that is fine (`progress.frames`).
        frame = progress.Frame("pst", "", unit="message")
    # Once per folder, never per message: plain attribute stores.
    frame.stage = progress.STAGE_MESSAGES
    frame.where = _folder_shown(folder.path)
    frame.n = 0

    produced = 0
    # **Iterated lazily, not `list(folder.items())`.**
    #
    # That materialised every message in the folder at once - including every
    # body - and a 100,000-message Inbox is the corpus this exists for. It is
    # the memory profile that makes a 30GB PST run swap: the peak was one whole
    # folder, when the working set only ever needs to be one message.
    #
    # The `try` still has to wrap the *iteration* rather than a single call,
    # because a COM enumerator can fail part-way through - Outlook closing
    # mid-walk is the ordinary case - and a generator that raises from inside a
    # `for` gives no chance to record it. `_iter_folder_items` keeps the guard
    # and the laziness together.
    for item in _iter_folder_items(store, folder):
        # Every message moves the position, empty ones too, and `beat` rises
        # for every message and every attachment read - so the no-progress
        # limit (`file_watch`) sees a slow archive move, as it does for libpff.
        frame.n += 1
        frame.beat += 1
        if item.is_empty:
            continue
        key = _message_key(store, item)
        warnings: list[AppError] = []

        attachment_documents: list[Document] = []
        if with_attachments and item.attachments:
            frame.stage = progress.STAGE_ATTACHMENTS
            try:
                for attached in _attachment_documents(
                        item, key, seen_hashes, warnings=warnings):
                    attachment_documents.append(attached)
                    frame.beat += 1
            finally:
                frame.stage = progress.STAGE_MESSAGES

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
            frame=frame,
        )


#: Folders that could not be read, as AppErrors, **keyed by the archive they were
#: in**. Collected rather than raised so one closed Outlook does not end a walk
#: that has already produced thousands of messages; the caller reports them at
#: the end.
#:
#: Work order `pst-resilience` 5a. This was one process-wide list, drained whole
#: by whoever finished first - so with two extraction workers reading archives
#: through Outlook at once, each could report the other's folders as its own
#: ("Only part of 'a.pst' could be read" over an archive that was read in full)
#: and, worse, the archive that really was short could lose its record. Keyed by
#: the store's path (lower-cased: Windows paths are case-insensitive, and
#: `walk_session` already compares them that way), each archive drains its own.
_BUSY_FOLDERS: dict[str, list[AppError]] = {}
_BUSY_LOCK = threading.Lock()


def _busy_key(path: Any) -> str:
    return str(path or "").lower()


def _iter_folder_items(store: MapiStore, folder: MapiFolder) -> Iterator[Any]:
    """One message at a time, with the same failure handling as before.

    A COM enumerator can fail at any point - Outlook being closed mid-walk is
    the ordinary case, not an edge one - and it may do so after yielding
    thousands of messages. Everything read up to that point is kept; the folder
    is recorded as busy and the next incremental pass retries it.

    Guarding the whole loop rather than one call is the point: the previous
    version's `list(...)` made the failure atomic by making the memory cost the
    whole folder.
    """
    try:
        iterator = iter(folder.items())
    except Exception as exc:                              # noqa: BLE001 - Outlook closed or busy
        _record_busy(store, folder, exc)
        return

    while True:
        try:
            item = next(iterator)
        except StopIteration:
            return
        except Exception as exc:                          # noqa: BLE001 - mid-enumeration
            _record_busy(store, folder, exc)
            return
        yield item


def _record_busy(store: MapiStore, folder: MapiFolder, exc: BaseException) -> None:
    error = make_error(
        "ERR_OUTLOOK_BUSY", "extract.pst",
        folder=f"{store.display_name}/{folder.path}",
        details=f"{type(exc).__name__}: {exc}",
    )
    key = _busy_key(getattr(store, "file_path", None) or store.display_name)
    with _BUSY_LOCK:
        _BUSY_FOLDERS.setdefault(key, []).append(error)


def drain_busy_folders(path: Any = None) -> list[AppError]:
    """Take and clear the folders that could not be read.

    With `path`, only that archive's - what a pipeline worker must ask for, so
    it never takes another worker's. With none, everything (the CLI's
    `extract --mailbox`, which walks every store in one process).
    """
    with _BUSY_LOCK:
        if path is None:
            busy = [e for errors in _BUSY_FOLDERS.values() for e in errors]
            _BUSY_FOLDERS.clear()
            return busy
        return _BUSY_FOLDERS.pop(_busy_key(path), [])


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


#: What Outlook answers a call with while it is starting, showing a dialog or
#: serving another caller: `RPC_E_CALL_REJECTED` and
#: `RPC_E_SERVERCALL_RETRYLATER`. Neither is about the archive, and both pass.
OUTLOOK_BUSY_HRESULTS = (-2147418111, -2147417846)

#: How long to wait before asking Outlook again, in turn: fifteen seconds in
#: all. Four readers start together at the top of a run, each wakes Outlook,
#: and it refuses whichever calls arrive while it is still coming up.
OUTLOOK_BUSY_WAITS_S = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)


def is_outlook_busy(exc: BaseException) -> bool:
    """Did Outlook refuse the call because it was busy, not because of the file?"""
    code = getattr(exc, "hresult", None)
    if code is None and getattr(exc, "args", None) and isinstance(exc.args[0], int):
        code = exc.args[0]
    return code in OUTLOOK_BUSY_HRESULTS


def when_outlook_answers(call: Callable[[], Any], *,
                         waits: Sequence[float] = OUTLOOK_BUSY_WAITS_S,
                         sleep: Optional[Callable[[float], None]] = None) -> Any:
    """Make a call to Outlook, asking again while it says it is busy.

    2026-10-05, the owner's run of twenty archives through Outlook: three were
    skipped as "An unexpected error occurred ... This is a bug" within eight
    seconds of the run starting, each on `pywintypes.com_error: (-2147418111,
    'Call was rejected by callee.')` from the very first thing asked of Outlook
    (its list of stores). The other seventeen, asked a moment later, were read.
    A refusal that passes in a second is waited out; anything else is raised at
    once, and so is a refusal that outlasts every wait.
    """
    import time

    pause = sleep or time.sleep
    for wait in (*waits, None):
        try:
            return call()
        except Exception as exc:                          # noqa: BLE001 - re-raised below
            if wait is None or not is_outlook_busy(exc):
                raise
            pause(wait)
    return None                                           # unreachable


def outlook_stayed_busy(path: Any, exc: BaseException) -> AppErrorException:
    """Outlook refused every try. **A lock, not a fault and not a damaged file:**
    `ERR_FILE_LOCKED` is the one code the next run reads again by itself."""
    return AppErrorException(make_error(
        "ERR_FILE_LOCKED", "extract.pst", path=str(path),
        suggestion=("Outlook was busy and did not answer. Leave Outlook open and idle; "
                    "the archive is read again automatically on the next run."),
        details=f"Outlook refused the call after {len(OUTLOOK_BUSY_WAITS_S) + 1} tries: {exc}",
    ))


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
        # Read whole, and asked again while Outlook is busy (2026-10-05): this
        # is the first call every reader makes, and the one it refused.
        yield from when_outlook_answers(
            lambda: [_ComStore(store) for store in self._namespace.Stores])

    def attach(self, path: Path) -> None:
        """Load a .pst into Outlook if it is not already there."""
        target = str(path).lower()
        try:
            already = list(self.stores())
        except Exception as exc:                          # noqa: BLE001 - re-raised unless busy
            if is_outlook_busy(exc):
                raise outlook_stayed_busy(path, exc) from exc
            raise
        for store in already:
            if (store.file_path or "").lower() == target:
                return
        try:
            when_outlook_answers(lambda: self._namespace.AddStore(str(path)))
            self._attached.append(target)
        except Exception as exc:                          # noqa: BLE001
            # 2026-10-05: busy is neither of the two below. Called "corrupt" it
            # would have been settled and never read again; left as it was, it
            # reached the owner as a bug in Leasha.
            if is_outlook_busy(exc):
                raise outlook_stayed_busy(path, exc) from exc
            # A file another program holds is retried next pass; a damaged one
            # is settled. Reporting a lock as corruption dropped it for good.
            code = "ERR_FILE_LOCKED" if looks_locked(exc) else "ERR_FILE_CORRUPT"
            raise AppErrorException(make_error(
                code, "extract.pst",
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
        # **Never raises** (2026-10-05). It runs in `extract`'s `finally`, so
        # an Outlook too busy to list its stores here replaced the error the
        # read had actually ended on with a second one from the tidying-up.
        try:
            stores = list(self.stores()) if self._attached else []
        except Exception:                                 # noqa: BLE001 - leaving it attached is harmless
            stores = []
        for store in stores:
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


#: How to read a `.pst`. Settings exposes this; `auto` is what almost everyone
#: should use.
class PstBackend:
    #: libpff if it is installed, otherwise Outlook. Prefers libpff because it
    #: needs no Outlook, takes no file lock, does not touch the user's mail
    #: profile, and works from any thread.
    AUTO = "auto"
    #: Force Outlook/MAPI. The only route for `.ost` and the live mailbox.
    OUTLOOK = "outlook"
    #: Force direct file reading. Fails clearly if libpff is absent.
    LIBPFF = "libpff"

    ALL = (AUTO, OUTLOOK, LIBPFF)


def choose_backend(path: Path, preference: str = PstBackend.AUTO) -> str:
    """Which backend will actually be used for this file, and why.

    `.ost` always goes to Outlook: it is the Cached Exchange Mode file, libpff
    reads it poorly, and it belongs to a running Outlook anyway.
    """
    from app.extract import pst_libpff

    if path.suffix.lower() == ".ost":
        return PstBackend.OUTLOOK
    if preference == PstBackend.LIBPFF:
        return PstBackend.LIBPFF
    if preference == PstBackend.OUTLOOK:
        return PstBackend.OUTLOOK
    return PstBackend.LIBPFF if pst_libpff.available() else PstBackend.OUTLOOK


class PstExtractor:
    """Registered for `.pst` and `.ost`, so an archive is a first-class input."""

    name = "pst"
    extensions = OUTLOOK_EXTENSIONS

    #: 2026-10-07: `extract` is handed `resume_extra` even for a read from the
    #: top, because it now carries the messages not to read again
    #: (`pst_libpff.read_archive`'s `known_stamps`) as well as a resume's state.
    takes_extra_from_the_top = True

    #: Read through Outlook, which holds the archive open - so it must never be
    #: byte-hashed. On the first real run that hash raised a permission error in
    #: the walker and took the entire index run down with it.
    reads_externally = True

    #: Hard: PST is read through Outlook's MAPI interface, and pywin32 is the
    #: only way to reach it. Outlook itself being installed is a separate
    #: question, checked at extraction time - `find_spec` cannot answer it.
    requires = (
        Requirement("win32com", "pywin32",
                    provides="Outlook MAPI access to .pst archives", hard=True),
    )

    #: Injectable for tests; None means build a real Win32ComSession.
    session_factory: Optional[Callable[[], MapiSession]] = None

    #: Which route to take. Settings writes this; `auto` prefers libpff.
    backend: str = PstBackend.AUTO

    #: 2026-10-07, the owner: "need a way for each pst file it can be
    #: configured how to index outlook or direct". `{archive key: backend}`,
    #: keyed by `app.index.archives.normalise`, overriding `backend` for that
    #: one file only. Set at the start of every run from what Settings saved
    #: (`run_setup.apply_saved_pst_backend`); `auto` is never stored here.
    backends: dict[str, str] = {}

    def backend_for(self, path: Any) -> str:
        """The route chosen for this archive: its own choice if it has one,
        otherwise the global `backend`. What `choose_backend` is then asked
        about, so an `.ost` still goes to Outlook whatever was chosen."""
        from app.index.archives import normalise

        return (self.backends or {}).get(normalise(path)) or self.backend

    #: Work order `dates-live-log-and-interrupted-runs` 3b. An interrupted
    #: archive carries on at the folder it was in - **through libpff only.**
    #: See `app/extract/base.py`'s `Extractor` for the protocol and
    #: `pst_libpff.read_archive` for what the cursor means.
    #:
    #: 2026-09-27, the Outlook path, and why it takes no cursor: its folder
    #: numbering is not provably the same between two reads. `walk_session`
    #: walks MAPI's `Folders` collections, whose order Outlook does not promise
    #: and which is a live view that Outlook itself changes (a folder added,
    #: a search folder built); and attaching an archive to Outlook writes to
    #: it and moves its modified time (seen on this machine, `WORKORDER-pst-
    #: resilience.md` §0), so a cursor checked against size and time would be
    #: thrown away by the very read that wanted it. An Outlook read therefore
    #: starts from the top, as before, and skips what is already indexed by
    #: its text (`Pipeline._already_current`).
    supports_resume = True

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def change_marker(self, path: Path) -> Optional[str]:
        """What stands in for a content hash of an archive: see `archive_marker`."""
        return archive_marker(path)

    def extract(
        self, path: Path, *, resume_from: int = 0,
        resume_extra: Optional[Mapping[str, Any]] = None,
    ) -> Iterable[Document]:
        if self.session_factory is not None:
            session = self.session_factory()
            yield from walk_session(session, include_live=False, only_paths=[str(path)])
            return

        held_open: Optional[AppErrorException] = None
        # This archive's own choice, or the global one (`backends`).
        preference = self.backend_for(path)
        if choose_backend(path, preference) == PstBackend.LIBPFF:
            from app.extract import pst_libpff

            yielded = False
            extra = dict(resume_extra or {})
            try:
                for document in pst_libpff.read_archive(
                        path, resume_from=resume_from,
                        seen_attachments=extra.get("seen") or (),
                        read_before=int(extra.get("read", 0) or 0),
                        # 2026-10-07: the messages already in the index as they
                        # are here, which the direct reader passes over.
                        known_stamps=extra.get("stamps") or None):
                    yielded = True
                    yield document
                return
            except AppErrorException as exc:
                # An archive Outlook holds open cannot be read directly, but it
                # can be read *through* Outlook - that is what having it open
                # means. Only before the first message: a fallback part-way
                # through would index the archive twice. Only in `auto`: a
                # forced backend was a choice, and quietly overriding it hides
                # that libpff is not doing the job.
                if (yielded or exc.error.code != "ERR_FILE_LOCKED"
                        or preference != PstBackend.AUTO):
                    raise
                held_open = exc
                _log.warning("{} is held open; trying Outlook instead", path.name)
            except pst_libpff.LibpffUnavailable as exc:
                if preference == PstBackend.LIBPFF:
                    raise_error(
                        "ERR_OUTLOOK_MISSING", "extract.pst", path=str(path),
                        suggestion=(
                            "The PST backend is set to 'libpff' but libpff is not installed. "
                            "Either install it (needs Visual Studio Build Tools on Windows: "
                            "pip install libpff-python) or set the backend to 'auto' in "
                            "Settings to fall back to Outlook."
                        ),
                        details=str(exc),
                    )
                    return
                # auto: libpff vanished between the check and the read. Fall
                # through to Outlook rather than failing the file.
                _log.warning("libpff unavailable, falling back to Outlook: {}", exc)

        try:
            session = Win32ComSession()
        except AppErrorException:
            # No Outlook to fall back on: the lock is the honest reason.
            if held_open is not None:
                raise held_open
            raise
        try:
            try:
                session.attach(path)
            except AppErrorException:
                if held_open is not None:
                    raise held_open
                raise
            yield from with_closing_warning(
                walk_session(session, include_live=False, only_paths=[str(path)]),
                lambda: _busy_warning(path),
            )
        finally:
            session.close()


#: What `archive_marker` writes in front of its answer, so the value can never
#: be taken for a hash of bytes (those are bare hex).
MARKER_PREFIX = "pst-header:"

#: How much of the file `archive_marker` reads. The header of a Unicode `.pst`
#: is 564 bytes ([MS-PST] 2.2.2.6); the last field read ends at 524.
_HEADER_BYTES = 564


def archive_marker(path: Path) -> Optional[str]:
    r"""A few numbers from the archive's header that move when its mail does
    and stay put when only its date does. None when they cannot be read.

    **Why** (2026-10-02). Outlook rewrites the header of every archive it has
    mounted, so the file's date moves while nothing in it has: all eight
    archives in the owner's index had the size they were indexed at and a date
    15-17 hours later. Date and size were the whole change test for an archive
    (its bytes are not hashed - tens of gigabytes, and held open), so every run
    read every message of every archive again.

    **What is read.** One read of 564 bytes. From it: the next page and block
    numbers the file will hand out (`bidNextP`, `bidNextB`), where the file
    ends (`ibFileEof`), and the number and place of the two root pages every
    folder and message is reached through (`BREFNBT`, `BREFBBT`). A `.pst`
    never changes a block or a page where it lies: it writes a new one,
    numbered from those counters, and then points the header at the new roots
    ([MS-PST] 2.6.1). So mail added, changed or removed moves these numbers.

    **What is left out, on purpose:** `dwUnique`, which counts header writes,
    and the two checksums over the header. Measured on the owner's `2010.pst`
    and `2011.pst` across one afternoon mounted in Outlook and its closing:
    `dwUnique` rose by five, both checksums changed, the date moved - and
    every number used here was the same.

    **It can only err towards reading.** A header that cannot be read (Outlook
    holds a byte-range lock on an archive it has mounted - Windows error 33),
    that is not a Unicode `.pst` (`wVer` 23; an ANSI one lays its header out
    differently, and none was to hand to measure), or that is torn mid-write
    gives None or a different answer, and the archive is read as it always
    was. UNCONFIRMED by measurement here: that Outlook never changes a
    message without moving these numbers - that rests on the published format.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(_HEADER_BYTES)
    except OSError:
        return None
    if len(raw) < _HEADER_BYTES or raw[:4] != b"!BDN" or raw[8:10] != b"SM":
        return None
    if int.from_bytes(raw[10:12], "little") != 23:
        return None

    def number(offset: int) -> int:
        return int.from_bytes(raw[offset:offset + 8], "little")

    return MARKER_PREFIX + ":".join(f"{number(offset):x}" for offset in (
        32,          # bidNextP
        516,         # bidNextB
        184,         # ibFileEof
        216, 224,    # BREFNBT: bid, ib
        232, 240,    # BREFBBT: bid, ib
    ))


#: The numbers `archive_marker` joins, in its order, by their [MS-PST] names.
_MARKER_FIELDS = ("bidNextP", "bidNextB", "ibFileEof",
                  "BREFNBT.bid", "BREFNBT.ib", "BREFBBT.bid", "BREFBBT.ib")


def marker_difference(old: Optional[str], new: Optional[str]) -> str:
    """Which header numbers differ between two markers, in words for the log.

    2026-10-07. On the day three archives were read again with no mail known
    to have changed, the old markers had already been overwritten, so nobody
    could say whether Outlook had really written to them or the marker is too
    easily moved. This is what the log now keeps. No I/O; never raises."""
    try:
        before = str(old or "")[len(MARKER_PREFIX):].split(":")
        after = str(new or "")[len(MARKER_PREFIX):].split(":")
        if (not str(old or "").startswith(MARKER_PREFIX)
                or not str(new or "").startswith(MARKER_PREFIX)
                or len(before) != len(_MARKER_FIELDS) or len(after) != len(_MARKER_FIELDS)):
            return f"was {old!r}, now {new!r}"
        moved = [f"{name} {int(a, 16):,} -> {int(b, 16):,} ({int(b, 16) - int(a, 16):+,})"
                 for name, a, b in zip(_MARKER_FIELDS, before, after) if a != b]
        return "; ".join(moved) if moved else "no number differs"
    except Exception:                               # noqa: BLE001 - a log line, not a read
        return f"was {old!r}, now {new!r}"


def _busy_warning(path: Path) -> Optional[AppError]:
    """Fold the folders Outlook could not read into one warning for the archive.

    `_BUSY_FOLDERS` was only ever drained by the CLI, so in the app the record
    of a skipped folder was written and never read.
    """
    busy = drain_busy_folders(path)
    if not busy:
        return None
    return make_error(
        "ERR_PST_PARTIAL", "extract.pst", path=str(path),
        # 4a: Outlook being busy passes, so the next run should try again; the
        # pipeline reads this to withhold the archive's `unchanged` marker.
        transient=True,
        reason=f"{len(busy)} folder{'s' if len(busy) != 1 else ''} could not be read "
               "because Outlook was busy or closed",
        details="; ".join(str(e.details) for e in busy[:3]),
    )


register(PstExtractor())
