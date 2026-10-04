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
import hashlib
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from app.core.errors import AppError, AppErrorException, make_error, raise_error
from app.core.logging import logger
from app.extract import junk_images, progress, reading
from app.extract.archive import attachment_key
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

    def __init__(self, frame: Optional[progress.Frame] = None) -> None:
        self.read = 0
        self.messages = 0
        self.folders = 0
        #: Attachments that could not be opened or extracted. **Not** the
        #: reason `failed`/`reason()` fire the archive-level `ERR_PST_PARTIAL`
        #: notice below - one bad attachment is not a partial archive, the
        #: same way one bad message is not - but it is counted and logged so
        #: it is a number somewhere rather than only a log line nobody reads.
        self.attachments = 0
        self.examples: list[str] = []
        #: Order 0z lane C: the per-item status words live on the progress
        #: frame, so the Indexing page sees them while the archive is read.
        self.frame = frame if frame is not None else progress.Frame("pst", "", unit="message")
        #: `_readable_type`'s answers, one per extension, for this read only.
        self.types: dict[str, bool] = {}

    @property
    def failed(self) -> bool:
        return bool(self.messages or self.folders)

    def status(self, word: str) -> None:
        """One item (message or attachment) ended as `word`. See `progress.STATUS_WORDS`."""
        self.frame.count(word)

    def message_failed(self, folder_path: str, index: int, exc: BaseException) -> None:
        self.messages += 1
        self.status(progress.STATUS_FAILED)
        self._note(f"message {index} in {folder_path}", exc)

    def folder_failed(self, folder_path: str, exc: BaseException) -> None:
        self.folders += 1
        self._note(f"folder {folder_path}", exc)

    def attachment_failed(self, message_key: str, index: int, exc: BaseException) -> None:
        self.attachments += 1
        self.status(progress.STATUS_FAILED)
        self._note(f"attachment {index} on {message_key}", exc)

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


#: Guards against a damaged folder tree (order 0z lane C). **Constants, not
#: settings** (non-negotiable 11): they never fire on a healthy archive, and
#: nobody could choose a better number than "far past anything real".
#:
#: `MAX_FOLDER_DEPTH` - folders nested deeper than this are not entered. A
#: descriptor that points back at an ancestor makes the tree infinitely deep;
#: before this, the walk recursed until Python's recursion limit, and the
#: `RecursionError` ended the whole archive. Chosen, not measured: Outlook
#: users nest a handful deep; 64 is far past that.
MAX_FOLDER_DEPTH = 64
#: `MAX_CONSECUTIVE_FAILURES` - a folder whose items fail this many times in
#: a row is abandoned (and recorded as a folder that could not be read). A
#: damaged folder can claim billions of messages; each failed read takes
#: microseconds, so an honest loop over a lying count spins for hours with
#: `frame.n` rising and nothing read. Chosen, not measured: the fuzzed damage
#: on a real archive (`HANDOFF.md`) never produced more than a few failures
#: in a row in a readable folder.
MAX_CONSECUTIVE_FAILURES = 200


def _identifier(item: Any) -> Optional[int]:
    """libpff's node identifier, or None when it cannot be read (or a fake lacks it)."""
    try:
        return int(item.get_identifier())
    except Exception:                            # noqa: BLE001
        return None


def _walk_folders(
    folder: Any, path: str = "", report: Optional[_Report] = None,
) -> Iterator[tuple[str, Any]]:
    """Every folder in the archive, depth first, with its path.

    **Iterative, with a cycle guard** (order 0z lane C). The order is exactly
    the recursive order it replaced - pre-order, children fetched one at a
    time - because `_messages` numbers folders by it and a saved resume cursor
    is that number. What changed is what a damaged tree can do: a folder seen
    before (by libpff identifier) is not entered again, nesting stops at
    `MAX_FOLDER_DEPTH`, and a folder whose children fail
    `MAX_CONSECUTIVE_FAILURES` times in a row stops being asked. Each is
    recorded as a folder that could not be read.
    """
    def failed(where: str, exc: BaseException) -> None:
        if report is not None:
            report.folder_failed(where, exc)
        else:
            _log.warning("folder {} unreadable: {}", where, exc)

    def children(here: str, node: Any) -> int:
        try:
            return int(node.get_number_of_sub_folders())
        except Exception as exc:                 # noqa: BLE001
            # A whole subtree vanishes here, so it is recorded rather than shrugged off.
            failed(here, exc)
            return 0

    seen: set[int] = set()
    root_id = _identifier(folder)
    if root_id is not None:
        seen.add(root_id)
    name = _safe(folder, "get_name") or "(unnamed)"
    here = f"{path}/{name}".strip("/")
    yield here, folder
    # Each entry: [path, folder, child count, next child index, failures in a row].
    stack: list[list[Any]] = [[here, folder, children(here, folder), 0, 0]]
    while stack:
        top = stack[-1]
        parent_path, parent, count, index, bad = top
        if index >= count:
            stack.pop()
            continue
        top[3] = index + 1
        if bad >= MAX_CONSECUTIVE_FAILURES:
            failed(f"{parent_path} (from #{index})", RuntimeError(
                f"gave up after {bad} unreadable sub-folders in a row "
                f"({count} claimed)"))
            stack.pop()
            continue
        try:
            child = parent.get_sub_folder(index)
        except Exception as exc:                 # noqa: BLE001 - one bad folder, not a bad archive
            top[4] = bad + 1
            failed(f"{parent_path}/#{index}", exc)
            continue
        top[4] = 0
        child_id = _identifier(child)
        name = _safe(child, "get_name") or "(unnamed)"
        child_path = f"{parent_path}/{name}".strip("/")
        if child_id is not None and child_id in seen:
            failed(child_path, RuntimeError(
                f"folder {child_id} appears twice in the folder tree (a loop)"))
            continue
        if len(stack) >= MAX_FOLDER_DEPTH:
            failed(child_path, RuntimeError(
                f"nested more than {MAX_FOLDER_DEPTH} folders deep"))
            continue
        if child_id is not None:
            seen.add(child_id)
        yield child_path, child
        stack.append([child_path, child, children(child_path, child), 0, 0])


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


#: `Document.meta` keys for the folder cursor. Work order
#: `dates-live-log-and-interrupted-runs` 3b; the pipeline reads them by these
#: names (`app/index/pipeline.py`), so they are spelt once, here.
#:
#: `pst_folder` is the folder's place in `_walk_folders`' depth-first order,
#: counting every folder including the skipped ones - the one numbering that is
#: the same on every read of the same bytes, whatever the settings say.
FOLDER_META_KEY = "pst_folder"
#: How many messages had been read before this document's folder began, so a
#: resumed read can still say "(412 were)" about the whole archive.
READ_BEFORE_META_KEY = "pst_read_before"


def read_archive(
    path: Path,
    *,
    skip_folders: Optional[frozenset[str]] = None,
    resume_from: int = 0,
    seen_attachments: Iterable[str] = (),
    read_before: int = 0,
) -> Iterator[Document]:
    """Every message in a `.pst`, as Documents. No Outlook involved.

    Deliberately mirrors `email_pst.walk_session`'s output so both backends are
    interchangeable: same `Document` shape, same `meta` keys, same conversation
    grouping. Layer 3 cannot tell which one produced a message, and should not.

    Attachments are extracted too, through the normal registry and deduplicated
    by content hash within this archive - see `_attachment_documents`. This
    used to be a known gap (names only, no content, "a job of its own" left to
    the Outlook backend); `pypff.attachment.read_buffer` turned out to make
    that job small enough to do here.

    **Resuming at a folder** (work order `dates-live-log-and-interrupted-runs`
    3b). `resume_from` skips every folder numbered below it without reading a
    message in it - the folder tree is still walked, because that walk is the
    numbering. The two things a folder number cannot carry come back with it:
    `seen_attachments`, the content hashes of attachments already read, so an
    attachment whose first copy was in a skipped folder is still deduplicated
    exactly as an uninterrupted read would; and `read_before`, so the partial-
    read summary still counts the whole archive.

    Every document carries `FOLDER_META_KEY` and `READ_BEFORE_META_KEY` for
    the pipeline to persist - **until anything fails to read.** From then on
    they are left off, so no cursor can move past the first failure: a resumed
    read always walks back over it, counts it again, and `ERR_PST_PARTIAL`
    stays true of the whole archive rather than only of the part read last.
    A damaged archive therefore resumes only from before its first damage,
    which costs a re-parse of what follows it and nothing else.
    """
    # Work order 0x section 3b. The frame is opened before the file is, so a
    # slow open of a 20GB archive already reads "opening Archive2019.pst"
    # rather than nothing. `_read_archive` holds what used to be this body.
    with progress.enter("pst", path.name, unit="message",
                        stage=progress.STAGE_OPENING) as frame:
        yield from _read_archive(
            path, frame, skip_folders=skip_folders, resume_from=resume_from,
            seen_attachments=seen_attachments, read_before=read_before)


def _read_archive(
    path: Path,
    frame: progress.Frame,
    *,
    skip_folders: Optional[frozenset[str]],
    resume_from: int,
    seen_attachments: Iterable[str],
    read_before: int,
) -> Iterator[Document]:
    """The body of `read_archive`, inside its progress frame. See there."""
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

    report = _Report(frame)
    report.read = max(0, int(read_before or 0))
    policy = reading.current()

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

    # **Dedup scope is one archive.** The same reservoir shape `email_pst.
    # walk_session` gives its own `seen_hashes`: a deck mailed round the team
    # eight times inside one `.pst` produces one extraction and eight
    # `content_hash`-identical references, not eight embeddings of the same
    # bytes.
    seen_hashes: set[str] = set(seen_attachments or ())
    scratch = _Scratch()

    finished = False
    try:
        yield from with_closing_warning(
            _messages(root, path, store_name, skip, report, seen_hashes,
                      resume_from=max(0, int(resume_from or 0)),
                      frame=frame, policy=policy, scratch=scratch), closing)
        finished = True
    finally:
        # However the read ended - finished, failed or abandoned - the pipeline
        # gets the counts for its end-of-archive log line (order 0z lane C),
        # and the scratch folder goes.
        #
        # **The counts are what the reader counted, with no allowance
        # (2026-09-30).** This used to take one off Indexed for an abandoned
        # read, because `with_closing_warning` holds the newest document back
        # and a read that was cut off never handed it on - it was counted and
        # not indexed. It is handed on now (`Reading.in_hand`, taken by
        # `Pipeline._kept_in_hand`), so everything counted here as Indexed
        # reached the index, and the old allowance would make the line one too
        # low. The one case left: a cut-off that lands while the pipeline is
        # cutting a message's text into passages loses that message, which
        # this reader had already counted - one too many, for a Force skip
        # pressed in those few milliseconds.
        policy.counts = dict(frame.counts)
        scratch.close()
        try:
            archive.close()
        except Exception:                        # noqa: BLE001
            pass


def display_folder(folder_path: str) -> str:
    """A folder path as a person would say it: `Inbox/Projects`.

    `_walk_folders` starts every path at the archive's root folder, which has
    no name a person has ever seen (libpff reports it empty, so it reads
    "(unnamed)"), and Outlook puts everything under a folder called "Top of
    Personal Folders" or "Top of Outlook data file". Neither is where anybody
    thinks their mail is, so both are left off for the progress line only -
    `folder_path` in `Document.meta` is unchanged.

    (UNCONFIRMED against real archives: the "Top of ..." names are the ones
    Outlook's own folder list shows; a localised Outlook may use other words,
    in which case they simply stay in the displayed path.)
    """
    parts = [part for part in str(folder_path or "").split("/") if part]
    parts = parts[1:]                            # the root folder
    if parts and parts[0].strip().lower().startswith("top of "):
        parts = parts[1:]
    return "/".join(parts)


def _messages(
    root: Any, path: Path, store_name: str, skip: frozenset[str], report: _Report,
    seen_hashes: set[str], *, resume_from: int = 0,
    frame: Optional[progress.Frame] = None,
    policy: Optional[reading.Reading] = None,
    scratch: Optional["_Scratch"] = None,
) -> Iterator[Document]:
    """Every readable message, recording the ones that are not.

    **The conversion is inside the guard, not only the fetch.** A message that
    fetched fine but broke `_to_document` used to raise out of the generator and
    end the archive - every message after it never read. One bad message now
    costs one message.

    `resume_from` and the two cursor keys: see `read_archive`.

    **Progress (work order 0x section 3b).** `frame` is told the folder, and
    "message n of m" *within that folder*. The per-folder count is the one
    libpff already hands over (`get_number_of_sub_messages`, called here
    anyway to drive the loop), so it costs nothing. A whole-archive total
    would need every folder visited first - a second walk of a file that can
    be tens of gigabytes - so there is none, on purpose.

    **Order 0z lane C.** Every message ends as one status word on the frame
    (`progress.STATUS_WORDS`), and `frame.n` moves for every message -
    read, empty or failed - so a per-file time limit watching it never takes
    a slow archive for a stalled one. A folder whose messages fail
    `MAX_CONSECUTIVE_FAILURES` times in a row is abandoned and recorded,
    because a damaged folder can claim more messages than exist.

    `policy` is the pass's images rule (`app.extract.reading`). With
    `IMAGES_ONLY` - the pictures pass coming back for an archive whose
    pictures were held - messages are not converted or yielded (the text pass
    indexed them) and only picture attachments are read.
    """
    if frame is None:
        frame = report.frame
    if policy is None:
        policy = reading.current()
    if scratch is None:
        scratch = _Scratch()
    images_only = policy.images == reading.IMAGES_ONLY
    seen_messages: set[int] = set()
    for ordinal, (folder_path, folder) in enumerate(_walk_folders(root, report=report)):
        if ordinal < resume_from:
            continue
        leaf = folder_path.rsplit("/", 1)[-1].strip().lower()
        if leaf in skip:
            continue
        read_before = report.read

        # Once per folder, never per message: a string split, then plain stores.
        frame.stage = progress.STAGE_FOLDER
        frame.where = display_folder(folder_path)
        frame.n = 0
        frame.total = None
        try:
            count = int(folder.get_number_of_sub_messages())
        except Exception as exc:                 # noqa: BLE001
            report.folder_failed(folder_path, exc)
            continue
        frame.total = count
        frame.stage = progress.STAGE_MESSAGES

        bad_in_a_row = repeats_in_a_row = 0
        for index in range(count):
            if bad_in_a_row >= MAX_CONSECUTIVE_FAILURES:
                report.folder_failed(f"{folder_path} (from message {index})", RuntimeError(
                    f"gave up after {bad_in_a_row} unreadable messages in a row "
                    f"({count} claimed)"))
                break
            if repeats_in_a_row >= MAX_CONSECUTIVE_FAILURES:
                # Everything here was already read: not damage worth a partial
                # warning, only a loop worth leaving.
                _log.info("left {} after {} messages in a row that were already read",
                          folder_path, repeats_in_a_row)
                break
            frame.n = index + 1
            try:
                message = folder.get_sub_message(index)
                identifier = _identifier(message)
                if identifier is not None and identifier in seen_messages:
                    # The same message twice - a damaged index pointing back at
                    # itself, or a folder listing another's messages. Its
                    # `virtual_path` is its identifier, so a second read would
                    # only overwrite the first.
                    repeats_in_a_row += 1
                    report.status(progress.STATUS_DUPLICATE)
                    continue
                repeats_in_a_row = 0
                attachments = _attachments(message, report)
                if images_only:
                    document = None
                    message_key = f"pst://{store_name}/{identifier if identifier is not None else f'{folder_path}#{index}'}"
                else:
                    document = _to_document(message, path, store_name, folder_path,
                                            attachment_names=[name for name, _a in attachments],
                                            folder_index=index)
                    message_key = (document.virtual_path if document is not None
                                   else f"pst://{store_name}/{folder_path}/{index}")
            except Exception as exc:             # noqa: BLE001 - one bad message, not a bad run
                bad_in_a_row += 1
                report.message_failed(folder_path, index, exc)
                continue
            bad_in_a_row = 0
            if identifier is not None:
                seen_messages.add(identifier)

            if images_only:
                report.read += 1
            elif document is None:
                report.status(progress.STATUS_SKIPPED)
            else:
                report.read += 1
                report.status(progress.STATUS_INDEXED)
                _mark_folder(document, report, ordinal, read_before)
                yield document
            # **After the message, not instead of it.** One bad attachment
            # must never cost the message itself - `_to_document` already
            # returned successfully by the time this runs, so the worst an
            # attachment failure does now is one missing attachment, logged
            # and counted in `report.attachments`.
            if attachments:
                for attached in _attachment_documents(
                    message, message_key, seen_hashes, report, frame=frame,
                    attachments=attachments, policy=policy, scratch=scratch,
                ):
                    if not images_only:
                        _mark_folder(attached, report, ordinal, read_before)
                    yield attached


def _mark_folder(document: Document, report: _Report, ordinal: int, read_before: int) -> None:
    """Stamp the folder cursor on a document - unless something has failed.

    See `read_archive` for why a failure freezes the cursor where it is. An
    attachment that failed is not a failure of the archive (`report.failed`
    ignores it, as `ERR_PST_PARTIAL` always has), so it freezes nothing.
    """
    if report.failed:
        return
    document.meta[FOLDER_META_KEY] = ordinal
    document.meta[READ_BEFORE_META_KEY] = read_before


def _to_document(
    message: Any,
    archive_path: Path,
    store_name: str,
    folder_path: str,
    attachment_names: Optional[list[str]] = None,
    folder_index: Optional[int] = None,
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

    if attachment_names is None:
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
    # 2026-10-04: its position in that folder, so "Open" on one of its
    # attachments finds it without walking the archive (`pst_attachment`).
    if folder_index is not None:
        document.meta["folder_index"] = folder_index
    document.meta["store_name"] = store_name
    document.meta["store_cached_only"] = False
    document.meta["backend"] = "libpff"
    return document


def _attachments(message: Any, report: Optional[_Report] = None) -> list[tuple[str, Any]]:
    """Each attachment's name and libpff object, fetched **once** per message.

    Order 0z lane C, measured: `get_attachment` is the costliest call in a
    read without OCR (about 2ms each on a real archive), and it used to be
    made twice per attachment - once for the name on the message, once to read
    the bytes. An attachment that cannot be fetched keeps its place as
    `attachment-N` with `None` for the object, and is counted as failed when
    its bytes are wanted.
    """
    try:
        count = int(message.get_number_of_attachments())
    except Exception:                            # noqa: BLE001
        return []
    found: list[tuple[str, Any]] = []
    for index in range(max(0, count)):
        try:
            attachment = message.get_attachment(index)
        except Exception as exc:                 # noqa: BLE001
            found.append((f"attachment-{index}", exc))
            continue
        try:
            found.append((_attachment_name(attachment, index), attachment))
        except Exception:                        # noqa: BLE001
            found.append((f"attachment-{index}", attachment))
    return found


def _attachment_names(message: Any) -> list[str]:
    return [name for name, _attachment in _attachments(message)]


#: MAPI property tags carrying an attachment's file name, per [MS-OXPROPS].
#: Long filename is preferred - the real name, spaces and all - dropping to
#: the DOS 8.3 short name, then to a name synthesised from the extension
#: alone, only when nothing better is there.
_PROP_ATTACH_EXTENSION = 0x3703
_PROP_ATTACH_FILENAME = 0x3704
_PROP_ATTACH_LONG_FILENAME = 0x3707


def _attachment_name(attachment: Any, index: int) -> str:
    """The attachment's real file name, read from its own MAPI record set.

    **`get_name`/`get_long_filename`/`get_filename` do not exist on `pypff.
    attachment`** - `dir()` on a live instance has no such methods. They were
    a guess at an API libpff-python never had, so every call raised
    `AttributeError`, was swallowed by the `except Exception` this replaces,
    and every attachment silently got the synthetic `attachment-N` fallback -
    which meant `.zip`, `.pdf`, whatever it actually was, was never in the
    name Layer 3 saw, and content extraction (`_attachment_documents`) could
    never route an extensionless synthetic name to the right reader either.

    The real name is an ordinary MAPI property, reached by walking the
    attachment's record set for the entry whose type is one of the three
    tags above - exactly how `_addresses` and `_sent_at` already read other
    MAPI properties off a message elsewhere in this file, just one level
    deeper.
    """
    long_name = short_name = extension = ""
    try:
        for set_index in range(attachment.get_number_of_record_sets()):
            record_set = attachment.get_record_set(set_index)
            for entry_index in range(record_set.get_number_of_entries()):
                entry = record_set.get_entry(entry_index)
                try:
                    tag = entry.get_entry_type()
                except Exception:                # noqa: BLE001
                    continue
                if tag not in (_PROP_ATTACH_LONG_FILENAME, _PROP_ATTACH_FILENAME,
                               _PROP_ATTACH_EXTENSION):
                    continue
                try:
                    value = entry.get_data_as_string()
                except Exception:                # noqa: BLE001 - wrong value type, or absent
                    continue
                if not value:
                    continue
                if tag == _PROP_ATTACH_LONG_FILENAME:
                    long_name = value
                elif tag == _PROP_ATTACH_FILENAME:
                    short_name = value
                elif tag == _PROP_ATTACH_EXTENSION:
                    extension = value
    except Exception:                            # noqa: BLE001 - one bad attachment
        pass

    if long_name:
        return long_name
    if short_name:
        return short_name
    if extension:
        dotted = extension if extension.startswith(".") else f".{extension}"
        return f"attachment-{index}{dotted}"
    return f"attachment-{index}"


def _hash_bytes(data: bytes) -> str:
    return hashlib.blake2b(data, digest_size=16).hexdigest()


class _Scratch:
    """One temporary folder per archive, for handing attachment bytes to a reader.

    Order 0z lane C. Every reader takes a path, so an attachment's bytes still
    go to disk - but through one folder made on first use and removed when the
    archive closes, instead of a `TemporaryDirectory` created and deleted per
    attachment. Measured on Linux (tmpfs): 0.34ms per attachment for the
    folder per attachment against 0.10ms for the write alone. On Windows, where
    each create and delete also passes the virus scanner, the saving should be
    larger - UNCONFIRMED, not measured there.

    Each file is deleted as soon as its reader has finished with it, so the
    folder never holds more than one attachment.
    """

    __slots__ = ("_dir",)

    def __init__(self) -> None:
        self._dir: Optional[Path] = None

    def write(self, name: str, data: bytes) -> Path:
        if self._dir is None:
            self._dir = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=_scratch_parent()))
        target = self._dir / _attachment_filename(name)
        target.write_bytes(data)
        return target

    def close(self) -> None:
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None


#: 2026-10-04, code review: the scratch folders' name, and where they go.
#: `close` removes each - but not when the process ends first (a run stopped
#: by ending its process, a reader left stuck in native code), and 33 were
#: found in the owner's %TEMP%. They now go under the settings' `CACHE_PATH`
#: (`configure_scratch`, called by `Pipeline.run`), and each run sweeps the
#: ones left behind (`sweep_scratch`).
SCRATCH_PREFIX = "lkg_attach_"
#: The sub-folder of `CACHE_PATH` they go in.
SCRATCH_FOLDER = "pst-attachments"
#: A scratch folder untouched this long is a leftover: each attachment written
#: or removed touches it, and a reader's time limit is minutes.
SCRATCH_STALE_S = 6 * 3600

_scratch_root: Optional[Path] = None


def _scratch_parent() -> Optional[str]:
    """The folder scratch folders are made in: the configured one if it can be
    made, else None - the system's temporary folder, as before."""
    root = _scratch_root
    if root is None:
        return None
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _log.debug("attachment scratch folder unusable, using the temp folder: {}", exc)
        return None
    return str(root)


def configure_scratch(cache_path: Any) -> Optional[Path]:
    """Put attachment scratch folders under `cache_path` (None: the system's
    temporary folder). Returns the folder used. No I/O until one is needed."""
    global _scratch_root
    _scratch_root = (Path(cache_path) / SCRATCH_FOLDER) if cache_path else None
    return _scratch_root


def sweep_scratch(cache_path: Any = None, *, older_than_s: float = SCRATCH_STALE_S,
                  now: Optional[float] = None) -> int:
    """Remove scratch folders left behind by a reader that never closed - under
    `cache_path`'s scratch folder and in the system's temporary folder (where
    they went before). Only folders named `SCRATCH_PREFIX*` untouched for
    `older_than_s`, so a reader still at work keeps its own. Returns how many
    went. **Never raises**: tidying."""
    import time

    current = time.time() if now is None else now
    places = [Path(tempfile.gettempdir())]
    if cache_path:
        places.insert(0, Path(cache_path) / SCRATCH_FOLDER)
    removed = 0
    for place in places:
        try:
            entries = list(place.glob(f"{SCRATCH_PREFIX}*"))
        except OSError:
            continue
        for entry in entries:
            try:
                if not entry.is_dir() or current - entry.stat().st_mtime < older_than_s:
                    continue
                shutil.rmtree(entry, ignore_errors=True)
                removed += not entry.exists()
            except OSError:
                continue
    if removed:
        _log.info("removed {} attachment scratch folder(s) left by earlier runs", removed)
    return removed


def _attachment_filename(name: str) -> str:
    """A name safe to write on any file system, keeping the extension readers route by."""
    base = Path(str(name).replace("\\", "/")).name or "attachment"
    stem, dot, ext = base.rpartition(".")
    if not dot:
        return _safe_filename(base, limit=120)
    return f"{_safe_filename(stem, limit=120)}.{_UNSAFE.sub('_', ext)[:16]}"


def _readable_type(name: str, known: dict[str, bool]) -> bool:
    """Would the registry read a file with this name? Cached per extension, per archive.

    Order 0z lane C, measured: an attachment of a type nothing reads (`.url`,
    `.dat`, ...) was written to disk, handed to `extract`, and turned down -
    and on the way `extract` parsed `extractors.toml` again to look for a
    converter, about 4ms each. One answer per extension per archive now,
    with no write.
    """
    ext = Path(name).suffix.lower()
    answer = known.get(ext)
    if answer is None:
        from app.extract.base import extractor_for

        answer = extractor_for(Path(name)) is not None
        if not answer:
            try:
                from app.core.formats import load_rules

                rule = load_rules().converter_for(ext)
                answer = bool(rule is not None and rule.enabled)
            except Exception:                    # noqa: BLE001 - let extract decide
                answer = True
        known[ext] = answer
    return answer


def _attachment_documents(
    message: Any, message_key: str, seen_hashes: set[str], report: _Report,
    *, frame: Optional[progress.Frame] = None,
    attachments: Optional[list[tuple[str, Any]]] = None,
    policy: Optional[reading.Reading] = None,
    scratch: Optional[_Scratch] = None,
) -> Iterator[Document]:
    """Extract each attachment through the normal registry, deduplicated.

    **This used to be the one thing this backend could not do.** `read_
    archive`'s own docstring called it out as a known gap: attachment
    *names* were captured (`_attachment_names`, above), never their content,
    because reading bytes through libpff means walking a MAPI record set
    rather than the one-line `SaveAsFile` Outlook COM offers. `pypff.
    attachment` turns out to expose exactly what is needed for that -
    `get_size()` and `read_buffer(size)` - so the gap closes without Outlook.

    Mirrors `email_pst._attachment_documents` deliberately: same size cap
    (`MAX_ATTACHMENT_BYTES`), same content-hash dedup, same `virtual_path`
    and `meta` shape - so Layer 3 cannot tell, and should not have to,
    which backend produced an attachment.
    """
    if attachments is None:
        attachments = _attachments(message, report)
    if not attachments:
        return
    own_scratch = scratch is None
    if scratch is None:
        scratch = _Scratch()

    # Work order 0x section 3b: while attachments are read the page says so,
    # and names the one being read - a 60MB attachment is exactly the kind of
    # thing that makes one message take minutes. Put back afterwards however
    # this ends, so the next message is not shown as "extracting attachments".
    if frame is not None:
        frame.stage = progress.STAGE_ATTACHMENTS
    try:
        yield from _each_attachment(message, message_key, seen_hashes, report,
                                    attachments, frame,
                                    policy if policy is not None else reading.current(),
                                    scratch)
    finally:
        if own_scratch:
            scratch.close()
        if frame is not None:
            frame.stage = progress.STAGE_MESSAGES
            frame.detail = ""


def _each_attachment(
    message: Any, message_key: str, seen_hashes: set[str], report: _Report,
    attachments: list[tuple[str, Any]], frame: Optional[progress.Frame],
    policy: reading.Reading, scratch: _Scratch,
) -> Iterator[Document]:
    """The attachment loop of `_attachment_documents`. See there.

    **Every attachment ends as exactly one status word** (order 0z lane C),
    and nothing a reader raises reaches the archive. Before, only
    `AppErrorException` was caught around the reader: a damaged `.xls` inside
    a damaged archive raised `struct.error` from `xlrd`, and the archive
    ended there - reproduced on 17 of 150 damaged copies of a real archive,
    each stopping after 7 to 62 of its 71 messages.

    **The cheap refusals come before the bytes are read**: a type nothing
    reads, a picture held for the pictures pass, an attachment over the size
    ceiling. Then the bytes are hashed *before* they are written, so a
    duplicate is never written at all.
    """
    from app.extract import mail_attachments as rules
    from app.extract.base import extract as extract_path
    from app.extract.base import reads_by_ocr
    from app.extract.email_pst import MAX_ATTACHMENT_BYTES

    hold = policy.images == reading.IMAGES_HOLD
    images_only = policy.images == reading.IMAGES_ONLY
    known = report.types

    for index, (name, attachment) in enumerate(attachments):
        if frame is not None:
            frame.detail = name
        if isinstance(attachment, BaseException):
            report.attachment_failed(message_key, index, attachment)
            continue
        try:
            is_picture = reads_by_ocr(Path(name))
        except Exception:                        # noqa: BLE001
            is_picture = False
        # **Owner, 1 October 2026: only Office documents and PDFs are read;
        # a zip gives its name and its members' names; everything else -
        # pictures included, never OCR'd - is recorded by name.** See
        # `mail_attachments`. An inline picture (a signature logo) gets no row.
        kind = rules.rule(name)
        if images_only and not (is_picture and kind == rules.CONTENTS):
            continue          # the text pass dealt with it; mail pictures are not read
        if kind == rules.NAME_ONLY:
            if is_picture and junk_images.is_inline(attachment):
                report.status(progress.STATUS_SKIPPED)
                continue
            report.status(progress.STATUS_INDEXED)
            yield rules.name_only_document(name, message_key, backend="libpff")
            continue
        if hold and is_picture:
            # Order 0z lane C: never opened on the text pass. Its name is on
            # the message already; the pictures pass comes back for it.
            policy.held += 1
            report.status(progress.STATUS_HELD)
            continue
        if not _readable_type(name, known):
            # Never opened, and - since 1 October 2026 - still listed by name,
            # like every attachment, whatever `MAIL_ATTACHMENTS` says.
            report.status(progress.STATUS_SKIPPED)
            _log.debug("attachment '{}' on {} is a type nothing reads", name, message_key)
            yield rules.name_only_document(name, message_key, backend="libpff")
            continue

        try:
            size = int(attachment.get_size())
        except Exception as exc:                 # noqa: BLE001
            report.attachment_failed(message_key, index, exc)
            continue

        if size > MAX_ATTACHMENT_BYTES:
            report.status(progress.STATUS_SKIPPED)
            _log.warning(
                "attachment '{}' on {} is {:,} bytes, over the {}MB ceiling, "
                "and was not opened - large attachments are almost always "
                "media, which hold no text",
                name, message_key, size, MAX_ATTACHMENT_BYTES // 1_048_576,
            )
            continue

        try:
            data = attachment.read_buffer(size) if size > 0 else b""
        except Exception as exc:                 # noqa: BLE001 - one bad attachment
            report.attachment_failed(message_key, index, exc)
            continue

        digest = _hash_bytes(data)
        # Order 0z lane D: the junk-image filter, pictures only. Every sighting
        # counts towards "seen five times", a duplicate within this archive too.
        book = policy.junk if is_picture else None
        if book is not None:
            book.saw(digest)
        if digest in seen_hashes:
            report.status(progress.STATUS_DUPLICATE)
            continue          # the same bytes are already indexed somewhere
        seen_hashes.add(digest)

        if kind == rules.NAMES_INSIDE:
            report.status(progress.STATUS_INDEXED)
            yield rules.name_only_document(name, message_key, inside=rules.names_inside(data),
                                           digest=digest, backend="libpff")
            continue

        screened: Optional[junk_images.Screened] = None
        if book is not None:
            screened = junk_images.screen(
                book, digest, data, inline=junk_images.is_inline(attachment))
            if screened.reason:
                _not_read(report, policy, name, message_key, screened.reason)
                continue

        target: Optional[Path] = None
        produced = False
        try:
            target = scratch.write(name, data)
            size = len(data)                     # for the row, after `data` is let go
            del data
            documents: Iterable[Document] = extract_path(target)
            if book is not None:
                # D3 needs every word before any is indexed; a picture is one
                # document, so holding it costs nothing.
                documents, why = junk_images.settle(book, digest, list(documents), screened)
                if why:
                    _not_read(report, policy, name, message_key, why)
                    continue
            for document in documents:
                # The member's own path kept after the attachment's (2026-09-30).
                document.virtual_path = attachment_key(
                    message_key, name, document.virtual_path, target)
                document.source_kind = SourceKind.PST_MESSAGE
                document.meta.setdefault("attachment_of", message_key)
                document.meta.setdefault("attachment_name", name)
                # Its own size for its row - left out of this reader when the
                # Outlook route got it (bda8813), so every attachment read
                # directly would have shown a blank size (2026-10-04).
                document.meta.setdefault("attachment_size", size)
                document.meta.setdefault("content_hash", digest)
                document.meta.setdefault("backend", "libpff")
                produced = True
                yield document
        except AppErrorException as exc:
            # An unreadable attachment is a skip, never the end of the run.
            # No text, or a type turned down, is not damage: Skipped, at debug.
            if exc.error.code in ("ERR_NO_TEXT_LAYER", "ERR_UNSUPPORTED_TYPE"):
                if book is not None and exc.error.code == "ERR_NO_TEXT_LAYER":
                    junk_images.settle(book, digest, [], screened)   # no words: D1
                report.status(progress.STATUS_SKIPPED)
                _log.debug("attachment '{}' on {}: {}", name, message_key, exc.error.code)
                continue
            report.attachment_failed(message_key, index, exc)
            continue
        except Exception as exc:                 # noqa: BLE001 - see the docstring
            report.attachment_failed(message_key, index, exc)
            continue
        finally:
            if target is not None:
                try:
                    target.unlink()
                except OSError:
                    pass
        report.status(progress.STATUS_INDEXED if produced else progress.STATUS_SKIPPED)


def _not_read(report: _Report, policy: reading.Reading, name: str, message_key: str,
              reason: str) -> None:
    """A picture the junk-image filter left out: `Skipped`, counted by reason.

    Its name stays on the message (`attachment_names`), so it is still found by
    name - which is all a signature logo was ever going to be found by.
    """
    report.status(progress.STATUS_SKIPPED)
    policy.left_unread(reason)
    _log.debug("attachment '{}' on {}: {}", name, message_key,
               junk_images.REASON_TEXT.get(reason, reason))


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
            bad_in_a_row = 0
            for index in range(count):
                if bad_in_a_row >= MAX_CONSECUTIVE_FAILURES:
                    # The same guard as `_messages`: a damaged folder can claim
                    # more messages than it holds.
                    _log.warning("gave up on {} after {} unreadable messages in a row",
                                 folder_path, bad_in_a_row)
                    break
                try:
                    message = folder.get_sub_message(index)
                    body = _eml_bytes(message)
                except Exception as exc:         # noqa: BLE001
                    bad_in_a_row += 1
                    _log.warning("message {} in {} not exported: {}", index, folder_path, exc)
                    continue
                bad_in_a_row = 0

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
