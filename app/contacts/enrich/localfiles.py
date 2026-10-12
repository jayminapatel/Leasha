"""The indexed files the local enrichment sources read: vCards (5a) and calendar invitations (5d).

Layer: L7

Order 1s section 5. The index knows *which* `.vcf` and `.ics` files exist (the `files` table, `ext`
without the dot); their structure - one card, one attendee - is not in `chunks` in any form a parser
can trust, because the chunker cuts text at passage boundaries, not at `END:VCARD`. So these sources
re-read the file itself from disk, **read-only** (non-negotiable 10: opened with `open(..., "rb")`,
never written, moved or touched).

**Which rows (order 1s, round 3).** A row whose text is indexed: `INDEXED`, and `PARTIAL` - text
indexed, vectors still to come in a text-first run, which `Pipeline._classify` and
`known_read_stamps` count as read (HANDOFF 2026-10-10). On the owner's index every `.vcf` and `.ics`
on disk was `PARTIAL`, so reading `INDEXED` alone found none of them.

**Attachments.** Most of a person's vCards and invitations arrive attached to a message (on the
owner's index 139 of 146 vCards and 921 of 966 invitations). An attachment has no path on disk, so
it is read from what the index kept of it, and failing that from its archive:

* **Its passages, stitched by their offsets** (`indexed_text`). The chunker's passages overlap
  (~64 tokens) and end at a word, not a line - checked 2026-10-13: joined in ordinal order they
  repeat a few hundred characters at every seam and break lines in the middle
  ("TEL;TYPE=CELL:+44 7700" then "N:Example..."). Each passage is `text[char_start:char_end]` of the
  extracted text, so stitched by those offsets they are that text exactly (whitespace-normalised:
  CRLF to LF, blank-line runs collapsed, trailing spaces gone - none of which a vCard or calendar
  line depends on). Trusted only when every offset is there, they start at 0 and leave no gap, and
  the text holds its BEGIN and a last END after it - a text cut at the extractor's size ceiling, or
  a passage missing, is not.
* **Else the archive** (`pst_attachment.read_attachment`, read-only through libpff, never the slow
  search), because under the default attachment rule a `.vcf` or `.ics` is indexed by its name only
  (`mail_attachments.rule`: not a content type), so its passages hold the file name and nothing else.
  A message with no archive to go back to (account mail, one read through Outlook) is skipped.

An attachment's receipt names its message (subject and date, as a signature's does); its date - the
receipt's, `Proposal.when` and the age window's - is the message's.

A file on an external drive is resolved through `offline_media.resolve_file_path`,
the one place that turns a volume row into today's drive letter; a drive that is not connected is
skipped. A file that has gone or cannot be read is skipped and logged, never an error: enrichment is
a convenience, and one stale row must not cost the person every other suggestion.

**Parsed once, not once a contact.** `enrich_selection` asks each source about one contact at a time;
re-reading 146 vCards for each of 1,445 contacts would be 200,000 file reads. `cached` keeps what
each file parsed to, keyed by its path, size and modified time - a file that changed is parsed again,
one that did not is never re-read. **Listed once a run, too** (`indexed_files`): finding the files is
two queries, one a join over every attachment row (0.47 s for the 966 invitations on the owner's index,
measured 2026-10-13), and asked once a contact it made the local sources cost 1.08 s a contact against
order 1s 9c's 0.15. A listing is kept for `LIST_SECONDS`, so a run asks once and a file indexed since is
seen by the next run; a file row added since (a higher `files.id`) lists again at once.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional, Sequence, TypeVar

from app.contacts.enrich.base import format_day
from app.core.logging import logger
from app.core.row_facts import ATTACHMENT_MARKER, MAIL_SOURCE_KINDS

__all__ = ["LocalFile", "indexed_files", "read_bytes", "cached", "MAX_FILE_BYTES", "clear_cache", "indexed_text",
           "READ_STATUSES", "LIST_SECONDS"]

_log = logger.bind(component="contacts.enrich.files")

#: A vCard or calendar file larger than this is not read. Not a setting: a real address book with
#: photos is a few megabytes, and a 100 MB ".vcf" is a backup of something else, which a person would
#: not expect enrichment to load into memory a contact at a time. Raise it if a real export is cut.
MAX_FILE_BYTES = 16 * 1024 * 1024

T = TypeVar("T")

#: How long a listing of the indexed files is reused (see the module docstring): a whole run, not a day.
LIST_SECONDS = 120.0

#: A row whose text the index holds (see the module docstring): PARTIAL only lacks its vectors.
READ_STATUSES = ("INDEXED", "PARTIAL")
#: What an attachment's stitched text must hold before it is trusted, by extension.
_ENVELOPE = {"vcf": ("BEGIN:VCARD", "END:VCARD"), "vcard": ("BEGIN:VCARD", "END:VCARD"),
             "ics": ("BEGIN:VCALENDAR", "END:VCALENDAR")}


@dataclass(frozen=True)
class LocalFile:
    path: Path                                           # where it is on disk now
    shown: str                                           # what the receipt names: the indexed path
    mtime: int                                           # Unix seconds, the receipt's date and `Proposal.when`
    size: int
    #: An attachment: its message's subject (the receipt names it), and how its bytes are read.
    subject: str = ""
    reader: Optional[Callable[[], Optional[bytes]]] = field(default=None, compare=False, repr=False)

    @property
    def receipt(self) -> str:
        """What a proposal from this file shows as proof: the file and its date, or for an attachment its
        message - subject and date, as a signature's receipt - and the attachment's name."""
        if self.reader is None:
            return f"{self.shown} - modified {format_day(self.mtime)}"
        name = self.shown.rsplit(ATTACHMENT_MARKER, 1)[-1]
        return f"{(self.subject or '(no subject)')[:80]} - {format_day(self.mtime)} (attached {name})"


_LISTED: dict[tuple[int, int, tuple[str, ...], Optional[int]], tuple[float, list[LocalFile]]] = {}


def indexed_files(store: Any, exts: Sequence[str], *, since: Optional[int] = None) -> list[LocalFile]:
    """The indexed files with one of `exts` ("vcf", no dot), newest first.

    `since` is the age window (order 1s D1) applied to the file's modified date: a file last changed
    before it is not read at all. The same listing is returned for `LIST_SECONDS` (module docstring)."""
    try:
        newest = int(store.conn.execute("SELECT COALESCE(MAX(id), 0) FROM files").fetchone()[0])
    except Exception:                                    # noqa: BLE001 - no files table: nothing to list
        newest = -1
    key = (id(store), newest, tuple(sorted(e.lower().lstrip(".") for e in exts)), since)
    hit = _LISTED.get(key)
    if hit is not None and time.monotonic() - hit[0] < LIST_SECONDS:
        return list(hit[1])
    out = _list_files(store, exts, since)
    _LISTED[key] = (time.monotonic(), out)
    return list(out)


def _list_files(store: Any, exts: Sequence[str], since: Optional[int]) -> list[LocalFile]:
    wanted = tuple(e.lower().lstrip(".") for e in exts)
    if not wanted:
        return []
    marks = ",".join("?" for _ in wanted)
    sql = (f"SELECT id, path, mtime_ns, size_bytes, volume_id, relative_path FROM files "
           f"WHERE ext IN ({marks}) AND source_kind = 'file' AND status IN ('INDEXED', 'PARTIAL') ")
    params: list[Any] = list(wanted)
    if since is not None:
        sql += "AND mtime_ns >= ? "
        params.append(int(since) * 1_000_000_000)
    sql += "ORDER BY mtime_ns DESC, id"
    out: list[LocalFile] = []
    connected: Optional[dict] = None
    for r in store.conn.execute(sql, params):
        if r["volume_id"] is None:
            path: Optional[Path] = Path(r["path"])
        else:
            try:                                         # only for a drive row: it enumerates volumes
                from app.index.offline_media import connected_volumes, resolve_file_path  # noqa: PLC0415
                if connected is None:
                    connected = connected_volumes(store)
                path = resolve_file_path(store, SimpleNamespace(path=r["path"], volume_id=r["volume_id"],
                                                                relative_path=r["relative_path"]), connected)
            except Exception as exc:                     # noqa: BLE001 - a drive we cannot see is skipped
                _log.info("enrichment skipped {}: its drive could not be resolved ({})", r["path"], exc)
                path = None
        if path is None:
            continue
        out.append(LocalFile(path, str(r["path"]), int(r["mtime_ns"] or 0) // 1_000_000_000,
                             int(r["size_bytes"] or 0)))
    out += _attachments(store, wanted, since)
    out.sort(key=lambda f: -f.mtime)                     # newest first, files and attachments together
    return out


def _attachments(store: Any, wanted: Sequence[str], since: Optional[int]) -> list[LocalFile]:
    """The attachments with one of `wanted` extensions, each with its message; the window is the message's."""
    marks = ",".join("?" for _ in wanted)
    kinds = ",".join("?" for _ in MAIL_SOURCE_KINDS)
    sql = (f"SELECT f.id, f.path, f.ext, f.size_bytes, m.subject, m.sent_at, m.store_path, m.entry_id, "
           f"m.folder_path, m.folder_index FROM files f "
           f"JOIN files p ON p.path = substr(f.path, 1, instr(f.path, ?) - 1) "
           f"JOIN messages m ON m.file_id = p.id "
           f"WHERE f.ext IN ({marks}) AND f.source_kind IN ({kinds}) AND f.status IN ('INDEXED', 'PARTIAL') "
           f"AND instr(f.path, ?) > 1 ")
    params: list[Any] = [ATTACHMENT_MARKER, *wanted, *MAIL_SOURCE_KINDS, ATTACHMENT_MARKER]
    if since is not None:
        sql += "AND m.sent_at >= ? "
        params.append(int(since))
    out: list[LocalFile] = []
    try:
        rows = store.conn.execute(sql, params).fetchall()
    except Exception as exc:                             # noqa: BLE001 - an index without the mail columns
        _log.info("enrichment could not list attachments: {}", exc)
        return out
    for r in rows:
        envelope = _ENVELOPE.get(str(r["ext"] or "").lower(), ("", ""))
        out.append(LocalFile(Path(str(r["path"])), str(r["path"]), int(r["sent_at"] or 0),
                             int(r["size_bytes"] or 0), str(r["subject"] or ""),
                             _attachment_reader(store, int(r["id"]), str(r["path"]), dict(r), envelope)))
    return out


def indexed_text(store: Any, file_id: int, begin: str, end: str) -> Optional[str]:
    """An attachment's extracted text rebuilt from its passages by their offsets, or None when it cannot
    be trusted (see the module docstring): an offset or a passage missing, no `begin`, or no `end` after
    the last `begin` (a text cut at the size ceiling)."""
    rows = store.conn.execute(
        "SELECT text, char_start, char_end FROM chunks WHERE file_id = ? AND COALESCE(label, '') <> 'Headers' "
        "ORDER BY ordinal", (int(file_id),)).fetchall()
    if not rows or any(r["char_start"] is None or r["char_end"] is None for r in rows):
        return None
    if int(rows[0]["char_start"]) != 0:
        return None
    text, reach = "", 0
    for r in rows:
        start, stop, piece = int(r["char_start"]), int(r["char_end"]), str(r["text"])
        if start > reach or len(piece) != stop - start:
            return None                                  # a gap between passages, or one not as it was cut
        if stop > reach:
            text += piece[reach - start:]
            reach = stop
    if begin and (begin not in text or text.rfind(end) < text.rfind(begin)):
        return None
    return text


def _attachment_reader(store: Any, file_id: int, path: str, message: dict,
                       envelope: tuple[str, str]) -> Callable[[], Optional[bytes]]:
    def read() -> Optional[bytes]:
        try:
            text = indexed_text(store, file_id, *envelope)
        except Exception as exc:                         # noqa: BLE001 - an index without offsets
            _log.info("enrichment could not read the passages of {}: {}", path, exc)
            text = None
        if text is not None:
            return text.encode("utf-8")
        return _from_archive(path, message)
    return read


def _from_archive(path: str, message: dict) -> Optional[bytes]:
    """The attachment's bytes out of its Outlook archive, read-only, or None (logged)."""
    archive = str(message.get("store_path") or "")
    name = path.split(ATTACHMENT_MARKER, 1)[-1]
    if not archive.lower().endswith(".pst") or "/" in name:
        return None                                      # no archive to go back to, or inside a zip
    try:
        from app.extract import pst_attachment  # noqa: PLC0415 - only a re-read needs the reader

        index = message.get("folder_index")
        return pst_attachment.read_attachment(
            archive, str(message.get("entry_id") or ""), name, folder_path=message.get("folder_path"),
            folder_index=int(index) if index is not None else None, search=False, max_bytes=MAX_FILE_BYTES)
    except Exception as exc:                             # noqa: BLE001 - one unreadable attachment is skipped
        _log.info("enrichment could not read {} from its archive: {}", path, exc)
        return None


def read_bytes(f: LocalFile) -> Optional[bytes]:
    """The file's bytes, read-only, or None (logged) when it is gone, unreadable or too big. An attachment's
    come from its passages or its archive (`_attachment_reader`)."""
    if f.reader is not None:
        return f.reader()
    try:
        size = f.path.stat().st_size
        if size > MAX_FILE_BYTES:
            _log.info("enrichment skipped {}: {:,} bytes is more than {:,}", f.shown, size, MAX_FILE_BYTES)
            return None
        with open(f.path, "rb") as fh:
            return fh.read(MAX_FILE_BYTES + 1)
    except OSError as exc:
        _log.info("enrichment skipped {}: {}", f.shown, exc)
        return None


_CACHE: dict[tuple[str, str, int, int], Any] = {}


def cached(kind: str, f: LocalFile, parse: Callable[[bytes], T]) -> Optional[T]:
    """`parse(read_bytes(f))`, remembered until the file changes. None when the file cannot be read or parsed."""
    key = (kind, str(f.path), f.size, f.mtime)
    if key in _CACHE:
        return _CACHE[key]
    data = read_bytes(f)
    value: Optional[T] = None
    if data is not None:
        try:
            value = parse(data)
        except Exception as exc:                         # noqa: BLE001 - one odd file, not a failed run
            _log.info("enrichment could not read {}: {}", f.shown, exc)
            value = None
    _CACHE[key] = value
    return value


def clear_cache() -> None:
    _CACHE.clear()
    _LISTED.clear()
