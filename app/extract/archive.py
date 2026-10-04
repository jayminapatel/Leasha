r"""Reading inside `.zip`, one member at a time, with every guard the cost needs.

Layer: L2

From `docs/WORKORDER-zip-archives.md` §3. Asked as *"does the program search
zipped files and its contents"* - and until §1a it did not even record that the
file existed. §1a made every `.zip` findable by name; this makes what is inside
one findable by its contents.

**The shape already existed, so none of it is invented here.** `.pst` solved the
same problem - one file on disk producing thousands of documents - and the
machinery is reused rather than paralleled: `source_kind="archive"`,
`virtual_path` as `container/member`, one `Document` per member, a per-document
digest so re-reading a changed archive rewrites only what moved. The pipeline,
the skip ledger, the UI and the change detector all handle that today.

**`zipfile` is in the standard library, so `.zip` costs no dependency.** `.7z`
and `.rar` do - `py7zr`, and `rarfile` which shells out to `unrar` - and they
are deliberately out of scope until the corpus is known to hold enough of them
to be worth it. `app.cli scan` counts them for exactly that decision.

## Why the guards are not optional

Every one of them is a way an archive ends a five-day index run, and none is
hypothetical:

* **A zip bomb** is a 40KB file that expands to 5GB. The uncompressed size and
  the compression ratio are both in the central directory, so the gigabyte is
  refused **without being allocated** - `odf.py` already reads sizes this way.
* **A thousand members of 50MB each** passes every per-member check and still
  costs 50GB, so there is a budget for the archive as a whole and it is shared
  with anything nested inside it.
* **Nesting** without a limit is a run that never finishes; zip quines exist.
  The depth limit is absolute rather than "keep going while it looks
  reasonable", which is what makes a self-referencing archive terminate.
* **An encrypted member** is detected from the flag bits *before* anything is
  attempted, because trying and failing is slower and says less.
* **A member named `..\..\Windows\System32\evil.dll`** must never be written
  outside the temp directory. `zipfile.extract` sanitises, but this code writes
  its own temp files and the check belongs where the write is.

And the cost control that is not a guard but a decision: members are extracted
**one at a time and deleted immediately**. Never the whole archive at once -
that is how a 20GB zip becomes 20GB of temp files on the index drive.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any, Iterator, Optional

from app.core.errors import AppError, AppErrorException, make_error
from app.core.logging import logger
from app.extract import progress
from app.extract.base import Document, SourceKind, register

__all__ = [
    "ArchiveExtractor", "MAX_DEPTH", "MAX_ARCHIVE_BYTES", "MAX_MEMBER_BYTES",
    "BOMB_RATIO", "BOMB_MIN_BYTES", "MAX_MEMBERS", "safe_member_name",
]

log = logger.bind(component="extract.archive")

#: Zip-format containers. **Not `.docx` and friends**, which are also zips and
#: have their own extractors: they are documents stored as zips, not archives,
#: and reading them here would produce one "document" per part of a Word file.
ARCHIVE_EXTENSIONS = (".zip", ".jar", ".nupkg", ".whl")

#: How far down archives are read, counted from the file on disk. Depth 1 is the
#: archive itself; depth 2 is an archive inside it.
#:
#: **Absolute, not adaptive.** A zip whose member is itself exists, and quines
#: exist; a limit that keeps going "while it looks reasonable" does not
#: terminate on either. Deeper members are recorded by name.
MAX_DEPTH = 2

#: Total uncompressed bytes one archive may yield, **shared with everything
#: nested inside it**. A thousand members of 50MB each passes every per-member
#: check and still costs 50GB.
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024

#: The largest single member that will be read.
MAX_MEMBER_BYTES = 64 * 1024 * 1024

#: Declared expansion above which a member is refused on that basis alone.
#: Ordinary text compresses about 5:1; 200:1 is a bomb and nothing else.
BOMB_RATIO = 200

#: ...and only above this size. A 5KB log of one repeated line compresses a
#: thousand to one and is harmless; a bomb is dangerous because of what it
#: expands *to*. The same floor `index/scan.py` applies for the same reason.
BOMB_MIN_BYTES = 1024 * 1024

#: Members listed from one archive. A central directory declaring millions of
#: entries costs memory to *parse*, before anything is read.
MAX_MEMBERS = 20_000


def safe_member_name(name: str) -> Optional[str]:
    r"""A member name reduced to something safe to write. `None` to refuse.

    **Refused, not sanitised into silence.** A member called
    `..\..\Windows\System32\evil.dll` is not a mistake to be quietly corrected;
    it is the one thing in an archive that indicates intent, and it is recorded
    as a skip so it appears in the ledger.

    Absolute paths, drive letters and any `..` component are refused. Both
    separators are checked, because a zip written on Windows may use either and
    `Path` on Linux does not split on a backslash - which this project has now
    been bitten by seven times.
    """
    raw = str(name or "").strip()
    if not raw or raw.endswith("/") or raw.endswith("\\"):
        return None                              # a directory entry, not a file
    parts = [piece for piece in raw.replace("\\", "/").split("/") if piece]
    if not parts:
        return None
    if any(piece == ".." for piece in parts):
        return None
    first = parts[0]
    if raw.startswith("/") or raw.startswith("\\") or (len(first) > 1 and first[1] == ":"):
        return None
    return "/".join(parts)


class _Budget:
    """Uncompressed bytes left for one archive and everything inside it.

    A class rather than a number passed around, because the budget is *shared*
    with nested archives: a counter each would let three levels of nesting cost
    three times the ceiling, which is the whole thing the ceiling exists to
    stop.
    """

    __slots__ = ("left", "members")

    def __init__(self, total: int = MAX_ARCHIVE_BYTES) -> None:
        self.left = int(total)
        self.members = 0

    def take(self, size: int) -> bool:
        if size > self.left:
            return False
        self.left -= int(size)
        self.members += 1
        return True


def _refusal(entry: Any, archive: Path, size: int, packed: int) -> Optional[AppError]:
    r"""Why this member will not be read, decided **from the header alone**.

    Nothing is decompressed to answer this - the uncompressed size and the
    compressed size are both in the central directory, so a claimed gigabyte is
    refused without ever being allocated. That is the work order's *"check the
    header, not the read"*, and it is what makes a bomb cheap to decline.
    """
    member = str(getattr(entry, "filename", ""))
    if getattr(entry, "flag_bits", 0) & 0x1:
        return make_error("ERR_ARCHIVE_ENCRYPTED", "extract.archive",
                          member=member, path=str(archive))
    if size > MAX_MEMBER_BYTES:
        return make_error(
            "ERR_ARCHIVE_TOO_LARGE", "extract.archive", member=member,
            path=str(archive),
            reason=f"it is {size / 1_048_576:,.0f}MB, over the "
                   f"{MAX_MEMBER_BYTES // 1_048_576}MB per-file limit")
    if size >= BOMB_MIN_BYTES and packed > 0 and size / packed > BOMB_RATIO:
        return make_error(
            "ERR_ARCHIVE_TOO_LARGE", "extract.archive", member=member,
            path=str(archive),
            reason=f"it claims to expand {size // max(packed, 1):,}:1, which is "
                   f"not a ratio real data reaches")
    return None


class ArchiveExtractor:
    """Every readable member of a zip, as its own document."""

    extensions = ARCHIVE_EXTENSIONS
    name = "archive"

    def extract(self, path: Path) -> Iterator[Document]:
        settings = _settings()
        if settings is not None and not getattr(settings, "archive_read_inside", True):
            return                               # switched off in Settings
        ceiling = int(getattr(settings, "archive_max_mb", 0) or 0) * 1_048_576
        if ceiling:
            try:
                if path.stat().st_size > ceiling:
                    # **Recorded by name with a reason, never silently.** A 40GB
                    # backup zip is a decision about time, and somebody who
                    # cannot find what is in it deserves to know why and where
                    # the number lives.
                    log.info("{} is over the {}MB archive ceiling",
                             path.name, ceiling // 1_048_576)
                    yield _by_name(path, str(path), make_error(
                        "ERR_ARCHIVE_TOO_LARGE", "extract.archive",
                        member=path.name, path=str(path),
                        reason=f"the archive is over the "
                               f"{ceiling // 1_048_576}MB limit in Settings"))
                    return
            except OSError:
                pass
        yield from read_archive(path)


_SETTINGS: Any = None
_SETTINGS_READ = False


def _settings() -> Any:
    r"""The loaded settings, or `None`. **Never raises, and reads `.env` once.**

    Cached at module level because this is asked per archive on a worker
    thread, and re-reading a file for a preference is exactly the shape of cost
    that turns a five-minute run into an hour on a network share. A missing or
    unreadable configuration means the defaults, which is what this did before
    it was configurable.

    `create_dirs=False` and `check_writable=False`: reading a preference must
    never create a folder or fail because somewhere is read-only.
    """
    global _SETTINGS, _SETTINGS_READ
    if _SETTINGS_READ:
        return _SETTINGS
    _SETTINGS_READ = True
    try:
        from app.core.config import load_settings

        _SETTINGS = load_settings(create_dirs=False, check_writable=False)
    except Exception:                            # noqa: BLE001 - a preference
        _SETTINGS = None
    return _SETTINGS


def read_archive(
    path: Path,
    *,
    depth: int = 1,
    budget: Optional[_Budget] = None,
    container_key: str = "",
) -> Iterator[Document]:
    r"""Members of `path`, one document each. **Never raises.**

    A corrupt archive, a member that will not open, a nested archive too deep:
    each is one skip recorded on the archive's own row, and the run continues.
    One bad archive must not end a five-day index.

    `container_key` is the `files.path` this archive is known by, which for a
    nested one is already `outer.zip/inner.zip` - so a member three levels in
    reads as the path a person would write to describe it.
    """
    budget = budget or _Budget()
    key = container_key or str(path)

    declared = _member_count(path)
    if declared is not None and declared > MAX_MEMBERS:
        log.warning("{} declares {:,} members - not read", path.name, declared)
        return

    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            # Work order 0x section 3b: "member 12 of 40, q3/report.docx".
            # The total is the central directory's own length, already in
            # memory, so it costs nothing. Directory entries are counted too:
            # they are in the list, skipping them would need a second pass,
            # and "of 40" being two folders generous is harmless. A nested
            # archive is read on this same thread, so its frame lands on top
            # of this one and the page shows `outer.zip › inner.zip › ...`.
            with progress.enter("zip", path.name, unit="member",
                                total=len(entries),
                                stage=progress.STAGE_ZIP) as frame:
                for position, entry in enumerate(entries, start=1):
                    if budget.left <= 0:
                        log.info("{} reached its {}MB budget", path.name,
                                 MAX_ARCHIVE_BYTES // 1_048_576)
                        return
                    frame.n = position
                    frame.where = entry.filename
                    yield from _member(archive, entry, path, key, depth, budget)
    except (zipfile.BadZipFile, OSError) as exc:
        # Corrupt, truncated, or gone between the walk and here.
        #
        # **Raised rather than returned empty.** Yielding nothing let the
        # pipeline reach its "extracted successfully but produced no text"
        # branch, so a damaged archive was reported as `ERR_NO_TEXT_LAYER` -
        # which is a statement about a document that opened fine, and is the
        # queue `--only-ocr` reads back. `SKIP_CONTINUE`, so this still costs
        # one line in a report and never the run.
        log.info("could not read {}: {}", path, exc)
        raise AppErrorException(make_error(
            "ERR_ARCHIVE_UNREADABLE", "extract.archive",
            path=str(path), reason=str(exc) or exc.__class__.__name__,
        )) from exc


def _member(archive: Any, entry: Any, path: Path, key: str,
            depth: int, budget: _Budget) -> Iterator[Document]:
    """One member: refuse it, recurse into it, or read it."""
    from app.extract.base import extractor_for

    name = safe_member_name(getattr(entry, "filename", ""))
    if name is None:
        raw = str(getattr(entry, "filename", ""))
        if raw and not raw.endswith(("/", "\\")):
            log.warning("refused a member named {!r} in {}", raw, path.name)
        return

    size = int(getattr(entry, "file_size", 0) or 0)
    packed = int(getattr(entry, "compress_size", 0) or 0)
    member_key = f"{key}/{name}"

    refused = _refusal(entry, path, size, packed)
    if refused is not None:
        yield _by_name(path, member_key, refused)
        return

    inner = Path(name)
    nested = inner.suffix.lower() in ARCHIVE_EXTENSIONS

    # **A nested archive is not charged for itself.** Its bytes are its
    # members' bytes, and every one of those is taken from this same budget a
    # moment later - so charging the container too made a 40MB inner zip cost
    # 80MB of a 100MB ceiling, and an archive of archives stopped at roughly
    # half of what the setting promised. Charge the leaves; the container is
    # only a wrapper around them.
    if not nested and not budget.take(size):
        yield _by_name(path, member_key, make_error(
            "ERR_ARCHIVE_TOO_LARGE", "extract.archive", member=name,
            path=str(path), reason="the archive's total size budget is spent"))
        return

    if nested:
        if depth >= MAX_DEPTH:
            yield _by_name(path, member_key, make_error(
                "ERR_ARCHIVE_TOO_DEEP", "extract.archive", member=name,
                path=str(path), depth=MAX_DEPTH))
            return
        # **Recursed with the shared budget**, which is what stops three levels
        # of nesting costing three times the ceiling.
        with _extracted(archive, entry, inner.name) as temp:
            if temp is not None:
                yield from read_archive(temp, depth=depth + 1, budget=budget,
                                        container_key=member_key)
        return

    if extractor_for(inner) is None:
        # **Recorded by name, not dropped.** An archive of `.dwg` files should
        # say what is in it; silence is the failure §1a exists to remove, and
        # reproducing it one level down would be the same mistake twice.
        yield _by_name(path, member_key, None)
        return

    with _extracted(archive, entry, inner.name) as temp:
        if temp is None:
            return
        yield from _read_one(temp, path, member_key)


def _read_one(temp: Path, archive_path: Path, member_key: str) -> Iterator[Document]:
    """Run the ordinary extractor over one member. Never raises.

    **Reuses the registry rather than reimplementing anything.** An archive
    reader with its own idea of how to read a `.docx` is a second, worse copy of
    Layer 2 that drifts from the first the moment either is fixed.
    """
    from app.extract.base import extract

    try:
        for document in extract(temp):
            yield Document(
                path=archive_path,
                text=document.text,
                segments=document.segments,
                source_kind=SourceKind.ARCHIVE,
                meta={**document.meta, "inside_archive": str(archive_path),
                      "member": member_key.split("/", 1)[-1],
                      # 2026-10-04: its own size, for its row - without it the
                      # row carried the archive's, as mail attachments did.
                      "member_size": _size_of(temp)},
                warnings=document.warnings,
                virtual_path=member_key,
            )
    except AppErrorException as exc:
        yield _by_name(archive_path, member_key, exc.error)
    except Exception as exc:                     # noqa: BLE001 - one member
        log.warning("member {} failed: {}", member_key, exc)


def attachment_key(message_key: str, name: str, virtual_path: Optional[str],
                   saved_as: Optional[Path]) -> str:
    r"""The key of one document read from a mail attachment.

    `{message}/attachments/{name}`, and - when the attachment was itself an
    archive - the member's path inside it after that. **2026-09-30:** both PST
    readers set every document of an attachment to the first part only, so the
    files inside a zipped attachment all shared one key; the pipeline made them
    unique with `#10`, `#11`... and warned "the extractor is not setting
    virtual_path" (seen on the owner's `2009.pst`). `saved_as` is where the
    attachment was written out to be read, which is what an archive member's
    key starts with.
    """
    base = f"{message_key}/attachments/{name}"
    if not virtual_path or saved_as is None:
        return base
    inner = _inside(virtual_path, saved_as)
    return f"{base}/{inner}" if inner and inner != str(virtual_path) else base


def _inside(member_key: str, archive_path: Path) -> str:
    """The member's path *within* the archive.

    `member_key` is the whole `files.path` - `D:\\a\\backup.zip/q3/report.docx` -
    because that is what the index is keyed on. For display and for the
    name-only text, only the part after the container is wanted.
    """
    prefix = str(archive_path)
    key = str(member_key)
    return key[len(prefix):].lstrip("/\\") if key.startswith(prefix) else key


def _size_of(path: Path) -> Optional[int]:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _by_name(archive_path: Path, member_key: str,
             warning: Optional[AppError]) -> Document:
    r"""A member recorded by name, with why its contents were not read.

    The text is the member's own path, so it is findable by the words in its
    name - which is the whole of what a name-only row is for, and is exactly
    what `NAME_ONLY` does one level up for files nothing can read.
    """
    name = member_key.split("/")[-1]
    return Document(
        path=archive_path,
        # **The member's path inside the archive, not the container's path on
        # disk.** Including the latter puts `D:`, `SearchData` and every folder
        # above it into the searchable text of every unreadable member, so a
        # search for a drive letter would return thousands of them. The archive
        # keeps its own name, which is what somebody would type.
        text=" ".join(filter(None, [
            Path(str(archive_path)).name,
            _inside(member_key, archive_path).replace("/", " "),
        ])) or name,
        source_kind=SourceKind.ARCHIVE,
        meta={"inside_archive": str(archive_path), "member": name,
              "contents_read": False},
        warnings=(warning,) if warning is not None else (),
        virtual_path=member_key,
    )


class _extracted:
    r"""One member on disk, deleted the moment it has been read.

    **Never the whole archive at once**, which is how a 20GB zip becomes 20GB of
    temp files on the index drive. A context manager rather than a helper so the
    delete cannot be skipped by an early return or an exception - and there are
    several of both above.

    The write goes to the system temp directory and the name is taken from the
    member's *basename only*, so a traversal that somehow survived
    `safe_member_name` still cannot land outside it.
    """

    __slots__ = ("_archive", "_entry", "_name", "_dir", "path")

    def __init__(self, archive: Any, entry: Any, name: str) -> None:
        self._archive = archive
        self._entry = entry
        self._name = Path(str(name)).name or "member"
        self._dir: Any = None
        self.path: Optional[Path] = None

    def __enter__(self) -> Optional[Path]:
        import tempfile

        try:
            self._dir = tempfile.TemporaryDirectory(prefix="leasha-zip-")
            target = Path(self._dir.name) / self._name
            resolved = target.resolve()
            if Path(self._dir.name).resolve() not in resolved.parents:
                # Belt and braces over `safe_member_name`. The check belongs
                # where the write is, because this is the line that could put
                # bytes somewhere they should not be.
                log.warning("refused to write {} outside the temp directory",
                            self._name)
                return None
            with self._archive.open(self._entry) as source:
                target.write_bytes(source.read())
            self.path = target
            return target
        except Exception as exc:                 # noqa: BLE001 - one member
            log.info("could not extract {}: {}", self._name, exc)
            return None

    def __exit__(self, *_exc: Any) -> None:
        if self._dir is not None:
            try:
                self._dir.cleanup()
            except OSError:
                pass


def _member_count(path: Path) -> Optional[int]:
    """How many members the archive declares, without parsing them all.

    Shares the reasoning - and the failure modes - with `index/scan.py`'s
    `_declared_members`: `infolist()` builds an object per entry eagerly, so an
    archive claiming millions costs memory to *list*. Read from the end record
    before `zipfile` is handed the file.
    """
    from app.index.scan import _declared_members

    try:
        return _declared_members(path)
    except Exception:                            # noqa: BLE001 - a guard
        return None


# **An instance, not the class.** `register` stores what it is given and
# `extract()` is called on it directly, so registering the class would leave
# every call an unbound `extract(path)` missing its `self` - which is a
# `TypeError` at read time, recorded as ERR_UNEXPECTED against the archive, and
# invisible until something actually opens a zip. Every other extractor in this
# package is registered the same way; this one was not, and the pipeline told me
# so within a minute of the first end-to-end run.
register(ArchiveExtractor())
