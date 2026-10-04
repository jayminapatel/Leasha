r"""The Life Timeline: everything from a stretch of time, across every source.

Layer: L4 (reports) - reads L1 (`SqliteStore`) only, never writes to it and
never touches a user file. Non-negotiable #10 applies in full, exactly as it
does for `app/reports/inheritance.py` and `app/reports/space.py`.

Order 202626270602 (0n) section 4. The Space Report says what the index holds
twice; this says *when*. "June 2015" is a place you can go: every photograph,
letter, video and message from that month, wherever it lives now - this
computer, a drive in a drawer, a share - in the order it happened.

**Which date an item is placed by (the truthful-date rules).** One file has
several dates and they disagree, so the choice is written down once, here:

1. A camera's own date (`files.taken_at_ns`, `taken_at_is_hint = 0`) - the
   photograph's EXIF shot date, or a video's own creation date. A fact the
   camera wrote once.
2. A date sidecar (a Google Takeout ``.json`` beside the photo). **Not built:**
   `app/extract/era_hints.py` checked and found no reader for one anywhere in
   this codebase, so there is nothing to rank. When one exists it belongs
   here, between 1 and 3, and only the SQL below changes.
3. A year guessed from a folder name (`taken_at_is_hint = 1`) - a scanned
   print's only clue. A guess, and labelled as one.
4. For mail, the date the message was sent (`messages.sent_at`), never the
   file's own time: every message in a ``.pst`` carries the *container's*
   modified time, so ten thousand letters would all be "the day the archive
   was last opened".
5. The file's modified time (`files.mtime_ns`) - which after years of
   drive-to-drive copying is often the day of the last copy. Used last, and
   labelled as what it is.

Rules 1 and 3 are one column (`taken_at_ns`) and the writer has already
chosen between them; the query does not re-decide, it only reads the flag to
word the label.

**Why three small queries and a merge, not one query with a computed date.**
The same reason `app/storage/filters.py::merge_by_date` states, measured
there: `ORDER BY COALESCE(taken_at_ns, mtime_ns)` cannot use an index and
sorts everything in range; each date column alone can. So each source of a
date is its own index-served query, kept in order by ``(date, id)``, and this
module merges them. A page is therefore ``O(limit)`` however wide the period
- a free range across twenty years costs what one month does. The plans are
pinned by `tests/unit/test_timeline_query_plans.py`.

**Paging is a keyset, not an OFFSET.** A cursor is the position of the last
row given out - ``(date, branch, file id)`` - and the next page starts
strictly after it. Scrolling a month of 4,000 photographs never re-reads the
first 3,800 to find the next 200, and a file arriving in the index while
someone scrolls cannot shift the page under them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, NamedTuple, Optional, Sequence

from app.core.logging import logger
from app.core.row_facts import message_name, own_size

__all__ = [
    "KINDS", "PAGE_SIZE", "PHOTO_EXTS", "VIDEO_EXTS",
    "BRANCH_DATED", "BRANCH_FILE", "BRANCH_MAIL",
    "Period", "Cursor", "TimelineEntry", "TimelinePage", "Overview", "branch_sql",
    "timeline_page", "timeline_overview", "refresh_overview", "date_of_file", "kind_of", "month_of_ns",
]

_log = logger.bind(component="reports.timeline")

#: The kinds a person can narrow the timeline to, in the order they are offered.
#: "everything" leaves out program code on purpose: a source checkout touched
#: in June 2015 is thousands of files nobody is looking for when they ask what
#: happened in June 2015. It is one choice away ("code").
KINDS: tuple[str, ...] = ("everything", "photos", "videos", "documents", "mail", "code")

#: Rows fetched per page. **Fixed, not a setting**: it only trades one worker
#: round-trip against the size of one result, and 200 is a screenful and a
#: half of thumbnails - big enough that scrolling rarely waits, small enough
#: that a page is a few milliseconds. Nobody would ever have a reason to
#: change it; if measurement on a slow disk ever shows a page over ~100 ms,
#: lower it here.
PAGE_SIZE = 200

#: Kept equal to `OcrExtractor.extensions` (minus ``.svg``, which is not a
#: photograph) plus `RawExtractor.extensions` - asserted by a test, so the
#: timeline's idea of "a photo" cannot drift from the indexer's. No leading dot,
#: lower case: the form `files.ext` stores.
PHOTO_EXTS: frozenset[str] = frozenset({
    "png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp", "gif", "heic", "heif",
    "cr2", "nef", "dng", "arw",
})
#: Kept equal to `app.extract.media.VIDEO_EXTENSIONS`, asserted by a test.
VIDEO_EXTS: frozenset[str] = frozenset({
    "mp4", "m4v", "mov", "mkv", "avi", "wmv", "webm", "mpg", "mpeg", "3gp",
    "flv", "m2ts",
})

#: The three places a date can come from, in the order ties are broken. Each
#: is an index-served query of its own - see the module docstring.
BRANCH_DATED = 0    # files.taken_at_ns is set: a camera date, or a guessed year
BRANCH_FILE = 1     # no such date: the file's own modified time
BRANCH_MAIL = 2     # messages.sent_at

_NS = 1_000_000_000
_FAR = 2 ** 63 - 1  # "no limit" on one side of a range: the largest SQLite integer
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------

def _local_ns(moment: datetime) -> int:
    r"""Local wall-clock `moment` as epoch nanoseconds - the same reading the
    ``after:``/``before:`` operators and the indexer's EXIF dates use.

    Falls back to UTC arithmetic where the platform cannot: Windows refuses
    ``datetime.timestamp()`` for anything before 1970 (``OSError 22``), and a
    scanned print from 1965 is exactly what this timeline exists to show.
    """
    try:
        return int(moment.timestamp()) * _NS + moment.microsecond * 1000
    except (OSError, OverflowError, ValueError):
        delta = moment.replace(tzinfo=timezone.utc) - _EPOCH
        return (delta.days * 86400 + delta.seconds) * _NS + delta.microseconds * 1000


def _day_start_ns(day: date) -> int:
    return _local_ns(datetime(day.year, day.month, day.day))


@dataclass(frozen=True)
class Period:
    """A stretch of time to browse. Half-open: ``start_ns <= when < end_ns``.

    `after`/`before` are the first and last *day* it covers, kept beside the
    nanoseconds because that is what a person types (`after:2015-06-01
    before:2015-06-30`) and what the timeline strip already speaks. Either may
    be None: an open end.
    """

    start_ns: int
    end_ns: int
    after: Optional[date] = None
    before: Optional[date] = None

    @classmethod
    def between(cls, after: Optional[date], before: Optional[date]) -> "Period":
        r"""From the first moment of `after` to the last moment of `before`
        (both whole days, both included). None leaves that side open.

        **A `datetime` is a moment, not a day** - a time typed in a range box
        (`2015-06-01T09:00`, order "dates" §1b). It arrives already resolved
        to the edge meant (`query._parse_moment`), so it is used as it is:
        rounded to its day it would quietly widen the range by up to a day
        on each side. The end is half-open, so one microsecond past the last
        moment included."""
        if isinstance(after, datetime):
            start = _local_ns(after)
        else:
            start = _day_start_ns(after) if after is not None else -_FAR
        if isinstance(before, datetime):
            end = _local_ns(before) + 1_000
        else:
            end = _day_start_ns(before + timedelta(days=1)) if before is not None else _FAR
        return cls(start, end, after, before)

    @classmethod
    def month(cls, year: int, month: int) -> "Period":
        first = date(year, month, 1)
        following = date(year + (month // 12), (month % 12) + 1, 1)
        return cls.between(first, following - timedelta(days=1))

    @classmethod
    def year(cls, year: int) -> "Period":
        return cls.between(date(year, 1, 1), date(year, 12, 31))

    @classmethod
    def from_words(cls, after: str = "", before: str = "",
                   *, today: Optional[date] = None) -> Optional["Period"]:
        r"""The period a person typed, by the search box's own date rules.

        Reuses the parser ``after:`` and ``before:`` already go through, so
        ``2015``, ``2015-06``, ``2015-06-01`` and ``3 months`` mean here what
        they mean there - and ``before:2015`` is the *whole* of 2015. Returns
        None when a side was given but is not a date: the caller says so, this
        never guesses a range from something it could not read.
        """
        from app.search.query import _instant, _parse_date

        first = _parse_date(after, today=today) if str(after or "").strip() else None
        last = _parse_date(before, today=today, end=True) if str(before or "").strip() else None
        if (str(after or "").strip() and first is None) or (str(before or "").strip() and last is None):
            return None
        # `_instant`, because a time typed on one side makes that side a
        # `datetime`, which Python refuses to compare with a plain date.
        if first is not None and last is not None and _instant(first) > _instant(last, end=True):
            # Typed backwards: swap the *words*, not the dates, so "2015-08 to
            # 2015-06" is June to August whole rather than 30 June to 1 August.
            return cls.from_words(before, after, today=today)
        return cls.between(first, last)

    @property
    def is_month(self) -> bool:
        return (self.after is not None and self.before is not None
                and self.after.day == 1
                and (self.before + timedelta(days=1)).day == 1
                and (self.after.year, self.after.month) == (self.before.year, self.before.month))


# ---------------------------------------------------------------------------
# What comes back
# ---------------------------------------------------------------------------

class Cursor(NamedTuple):
    """Where the last page ended: the next one starts strictly after this."""

    when_ns: int
    branch: int
    file_id: int

    def encode(self) -> str:
        return f"{self.when_ns}:{self.branch}:{self.file_id}"

    @classmethod
    def decode(cls, text: str) -> Optional["Cursor"]:
        try:
            when, branch, file_id = str(text).split(":")
            return cls(int(when), int(branch), int(file_id))
        except (TypeError, ValueError):
            return None


def kind_of(ext: str, source_kind: str = "file", repo_id: Any = None) -> str:
    """`photo`, `video`, `mail`, `code` or `document` - for the row's icon and words."""
    if source_kind in ("pst_message", "eml"):
        return "mail"
    if repo_id is not None:
        return "code"
    cleaned = str(ext or "").strip().lower().lstrip(".")
    if cleaned in PHOTO_EXTS:
        return "photo"
    if cleaned in VIDEO_EXTS:
        return "video"
    return "document"


@dataclass(frozen=True)
class TimelineEntry:
    """One thing on the timeline, dated by the truthful-date rules."""

    file_id: int
    path: str
    name: str
    ext: str
    kind: str
    when_ns: int
    #: How `when_ns` was arrived at: `taken` | `guessed` | `sent` | `saved`.
    #: Carried as a code and worded by `timeline_words.basis_words`.
    basis: str
    size_bytes: int = 0
    #: "" for a file on this computer; else the catalogued source's own name.
    source_name: str = ""
    source_kind: str = ""
    #: The source's last known status, lower-case: online | offline | locked | archived.
    source_status: str = ""
    volume_id: Optional[int] = None
    relative_path: str = ""
    #: A real, openable path when the source is reachable *right now*; None
    #: when it is not, which is the whole meaning of "offline" here.
    real_path: Optional[str] = None
    content_hash: Optional[str] = None
    phash: Optional[str] = None
    place: str = ""
    subject: str = ""
    sender: str = ""

    # `app.search.folding.fold` reads these names off any row it is handed.
    @property
    def taken_at_ns(self) -> int:
        return self.when_ns

    @property
    def mtime_ns(self) -> int:
        return self.when_ns

    @property
    def on_a_source(self) -> bool:
        return self.volume_id is not None

    @property
    def reachable(self) -> bool:
        """Can the file itself be opened now? Always for a local file (the
        index cannot know a local file was since deleted without looking, and
        looking is the UI's job on a worker); for a catalogued one, only while
        its source is connected."""
        return (not self.on_a_source) or self.real_path is not None

    @property
    def when(self) -> datetime:
        try:
            return datetime.fromtimestamp(self.when_ns / _NS)
        except (OverflowError, OSError, ValueError):
            return _EPOCH + timedelta(microseconds=self.when_ns // 1000)


@dataclass(frozen=True)
class TimelinePage:
    """One page of the timeline, oldest first, already folded."""

    #: `app.search.folding.Fold` objects - the head is shown, `older` is what
    #: is behind it (near-identical photos, identical copies).
    items: tuple = ()
    #: Where the next page starts; None when this was the last one.
    cursor: Optional[Cursor] = None
    #: How many entries this page covered before folding.
    covered: int = 0
    #: Which catalogued drives were plugged in when this page was read, so the
    #: next page of the same scroll can reuse the answer instead of asking
    #: Windows again. None when the page had nothing on a drive.
    connected: Optional[dict] = None

    @property
    def done(self) -> bool:
        return self.cursor is None

    @property
    def entries(self) -> list:
        """Every entry, folded ones included, in page order."""
        out: list = []
        for fold in self.items:
            out.append(fold.head)
            out.extend(fold.older)
        return out


@dataclass(frozen=True)
class Overview:
    """What the timeline holds, by month - the year -> month picker's data.

    `generated_at` is `report_generated_at`, the same "data as of" moment every
    report shows, so the view can skip a recount when nothing was indexed.
    """

    #: ``((year, month, count), ...)`` oldest first, months with nothing left out.
    months: tuple = ()
    generated_at: Optional[int] = None
    kind: str = "everything"
    #: How many items each rule placed - the honest breakdown behind "N items".
    by_camera: int = 0
    by_folder_guess: int = 0
    by_file_date: int = 0
    by_sent_date: int = 0
    #: Of the photographs: dated by a camera vs only by their file's own time.
    photos_with_camera_date: int = 0
    photos_by_file_date_only: int = 0
    #: Items with no date at all - not on the timeline, and said so.
    undated: int = 0
    extra: dict = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(count for _y, _m, count in self.months)

    @property
    def years(self) -> tuple:
        """``((year, count), ...)`` oldest first."""
        totals: dict[int, int] = {}
        for year, _month, count in self.months:
            totals[year] = totals.get(year, 0) + count
        return tuple(sorted(totals.items()))

    def months_of(self, year: int) -> dict:
        """``{month: count}`` for one year - twelve keys, zero where nothing is."""
        found = {month: 0 for month in range(1, 13)}
        for y, month, count in self.months:
            if y == year:
                found[month] = count
        return found

    @property
    def first(self) -> Optional[tuple]:
        return (self.months[0][0], self.months[0][1]) if self.months else None

    @property
    def last(self) -> Optional[tuple]:
        return (self.months[-1][0], self.months[-1][1]) if self.months else None


# ---------------------------------------------------------------------------
# The queries
# ---------------------------------------------------------------------------

def _clean_kind(kind: str) -> str:
    return kind if kind in KINDS else "everything"


def _file_kind_clause(kind: str) -> tuple[str, list]:
    """The WHERE fragment (and its parameters) that narrows a *file* branch
    to one kind. Empty string: no narrowing. Mail is its own branch."""
    photo = sorted(PHOTO_EXTS)
    video = sorted(VIDEO_EXTS)
    marks = lambda values: ", ".join("?" for _ in values)          # noqa: E731
    if kind == "code":
        return "+f.repo_id IS NOT NULL", []
    base = "+f.repo_id IS NULL"
    if kind == "photos":
        return f"{base} AND +f.ext IN ({marks(photo)})", list(photo)
    if kind == "videos":
        return f"{base} AND +f.ext IN ({marks(video)})", list(video)
    if kind == "documents":
        both = photo + video
        return f"{base} AND +f.ext NOT IN ({marks(both)})", list(both)
    return base, []


def _ceil_seconds(ns: int) -> int:
    return -((-ns) // _NS)


def _mail_bounds(period: Period, cursor: Optional[Cursor]) -> tuple[list, list]:
    """WHERE fragments for the mail branch. `sent_at` is whole seconds, the
    period and the cursor are nanoseconds, so each bound is converted the
    exact way (a ceiling for a lower bound, a floor-plus-one for a strict one)
    rather than by a float division that could put a message in the wrong day."""
    where = ["m.sent_at > 0"]
    params: list = []
    # **The lower bound is the later of the period's start and the cursor.** The
    # exact "strictly after the cursor" tests below are row-value comparisons the
    # index cannot seek on, so without this a deep page walks the index from the
    # period's start to find where the cursor is: measured 2,095,300 SQLite steps
    # for one page 13 years in, against 22,000 for the first page.
    start = max(period.start_ns, cursor.when_ns) if cursor is not None else period.start_ns
    if start > -_FAR:
        where.append("m.sent_at >= ?")
        params.append(_ceil_seconds(start))
    if period.end_ns < _FAR:
        where.append("m.sent_at < ?")
        params.append(_ceil_seconds(period.end_ns))
    if cursor is not None:
        seconds = cursor.when_ns // _NS
        if cursor.branch == BRANCH_MAIL:
            where.append("(m.sent_at > ? OR (m.sent_at = ? AND m.file_id > ?))")
            params.extend([seconds, seconds, cursor.file_id])
        elif cursor.branch < BRANCH_MAIL:          # mail sorts after the file branches on a tie
            where.append("m.sent_at >= ?")
            params.append(_ceil_seconds(cursor.when_ns))
        else:                                       # pragma: no cover - mail is the last branch
            where.append("m.sent_at > ?")
            params.append(seconds)
    return where, params


def _file_bounds(branch: int, period: Period, cursor: Optional[Cursor]) -> tuple[list, list]:
    column = "f.taken_at_ns" if branch == BRANCH_DATED else "f.mtime_ns"
    where = [f"{column} >= ?", f"{column} < ?"]
    # See `_mail_bounds`: the cursor's own date is a lower bound too, so the index
    # can *seek* to it instead of scanning up to it.
    start = max(period.start_ns, cursor.when_ns) if cursor is not None else period.start_ns
    params: list = [max(start, -_FAR), min(period.end_ns, _FAR)]
    if branch == BRANCH_DATED:
        where.append("f.taken_at_ns IS NOT NULL")
    else:
        where.extend(["f.taken_at_ns IS NULL", "f.mtime_ns > 0"])
    if cursor is not None:
        if cursor.branch == branch:
            where.append(f"({column}, f.id) > (?, ?)")
            params.extend([cursor.when_ns, cursor.file_id])
        elif cursor.branch < branch:
            where.append(f"{column} >= ?")
            params.append(cursor.when_ns)
        else:
            where.append(f"{column} > ?")
            params.append(cursor.when_ns)
    return where, params


_FILE_COLUMNS = (
    "f.id AS file_id, f.path, f.ext, f.size_bytes, f.content_hash, f.phash, "
    "f.volume_id, f.relative_path, f.place, f.taken_at_is_hint, "
    "v.name AS source_name, v.kind AS source_kind, v.status AS source_status")


def branch_sql(branch: int, period: Period, cursor: Optional[Cursor], kind: str,
               limit: int) -> Optional[tuple[str, list]]:
    """The SQL and parameters for one branch's next `limit` rows, or None when
    this branch has nothing to say for `kind` (mail is not a photograph).

    Public because the query plans are part of the contract: see
    `tests/unit/test_timeline_query_plans.py`, which runs `EXPLAIN QUERY PLAN`
    over exactly this text.
    """
    if branch == BRANCH_MAIL:
        if kind not in ("everything", "mail"):
            return None
        where, params = _mail_bounds(period, cursor)
        sql = (f"SELECT {_FILE_COLUMNS}, m.sent_at * {_NS} AS when_ns, m.subject, m.sender, "
               f"f.source_kind AS file_source_kind "
               "FROM messages m JOIN files f ON f.id = m.file_id "
               "LEFT JOIN volumes v ON v.id = f.volume_id "
               f"WHERE {' AND '.join(where)} ORDER BY m.sent_at, m.file_id LIMIT ?")
        return sql, [*params, int(limit)]
    if kind == "mail":
        return None
    where, params = _file_bounds(branch, period, cursor)
    clause, kind_params = _file_kind_clause(kind)
    where.extend(["+f.source_kind = 'file'", clause])
    params.extend(kind_params)
    column = "f.taken_at_ns" if branch == BRANCH_DATED else "f.mtime_ns"
    sql = (f"SELECT {_FILE_COLUMNS}, {column} AS when_ns, NULL AS subject, NULL AS sender, "
           "f.source_kind AS file_source_kind, f.repo_id AS repo_id "
           "FROM files f LEFT JOIN volumes v ON v.id = f.volume_id "
           f"WHERE {' AND '.join(where)} ORDER BY {column}, f.id LIMIT ?")
    return sql, [*params, int(limit)]


def _fetch_branch(store: Any, branch: int, period: Period, cursor: Optional[Cursor],
                  kind: str, limit: int) -> list:
    """Up to `limit` rows of one branch after `cursor`, in ``(date, id)`` order."""
    built = branch_sql(branch, period, cursor, kind, limit)
    if built is None:
        return []
    try:
        return store.conn.execute(built[0], built[1]).fetchall()
    except Exception as exc:                          # noqa: BLE001 - one branch, not the page
        _log.debug("timeline branch {} failed: {}", branch, exc)
        return []


def _basis_of(branch: int, hint: Any) -> str:
    if branch == BRANCH_MAIL:
        return "sent"
    if branch == BRANCH_DATED:
        return "guessed" if hint else "taken"
    return "saved"


def _name_of(path: str, relative_path: str) -> str:
    text = (relative_path or path or "").replace("\\", "/")
    return text.rsplit("/", 1)[-1]


def _entry(row: Any, branch: int, connected: Optional[dict]) -> TimelineEntry:
    volume_id = row["volume_id"]
    real: Optional[str] = None
    if volume_id is not None and connected is not None and int(volume_id) in connected:
        root = connected[int(volume_id)]
        real = str(root / (row["relative_path"] or "")) if row["relative_path"] else None
    subject = str(row["subject"] or "")
    kind = "mail" if branch == BRANCH_MAIL else kind_of(
        row["ext"], row["file_source_kind"], row["repo_id"])
    path = str(row["path"])
    # 2026-10-04, the owner ("the same code should run"): a message is named
    # the way every list names it - its subject, or "(no subject)" - never by
    # the last piece of its key (an EntryID); and one read out of an archive
    # has no size of its own (`row_facts`), not the archive's.
    return TimelineEntry(
        file_id=int(row["file_id"]), path=path,
        name=(message_name(subject) if branch == BRANCH_MAIL
              else subject or _name_of(path, row["relative_path"] or "")),
        ext=str(row["ext"] or ""), kind=kind, when_ns=int(row["when_ns"]),
        basis=_basis_of(branch, row["taken_at_is_hint"]),
        size_bytes=own_size(row["size_bytes"], path, row["file_source_kind"]),
        source_name=str(row["source_name"] or ""), source_kind=str(row["source_kind"] or ""),
        source_status=str(row["source_status"] or "").lower(),
        volume_id=int(volume_id) if volume_id is not None else None,
        relative_path=str(row["relative_path"] or ""), real_path=real,
        content_hash=row["content_hash"], phash=row["phash"],
        place=str(row["place"] or ""), subject=subject, sender=str(row["sender"] or ""),
    )


def _connected(store: Any, entries_have_volumes: bool) -> Optional[dict]:
    """Which catalogued drives are plugged in right now - asked once per page,
    and only when the page has an item on one. A live check (it can touch a
    sleeping drive), so this is worker-only, like `resolve_open_path`."""
    if not entries_have_volumes:
        return None
    try:
        from app.index.offline_media import connected_volumes

        return connected_volumes(store)
    except Exception as exc:                          # noqa: BLE001 - "cannot tell" reads as offline
        _log.debug("could not check which drives are connected: {}", exc)
        return {}


def timeline_page(store: Any, period: Period, cursor: Optional[Cursor] = None,
                  limit: int = PAGE_SIZE, kind: str = "everything", fold: bool = True,
                  connected: Optional[dict] = None) -> TimelinePage:
    r"""The next `limit` items of `period`, oldest first, after `cursor`.

    **Worker only** - it reads the store, and (for an item on a catalogued
    drive) asks Windows which drives are plugged in. Never raises: a branch
    that fails is logged and contributes nothing, so a page is short rather
    than absent.

    `fold` groups near-identical photographs and identical copies behind one
    row (`app.search.folding`) - a burst of forty frames of the same moment is
    one row that says "39 similar photos", not forty. Folding happens within a
    page, so a burst straddling a page edge is two rows; cheaper and simpler
    than holding a page open until the burst ends, and a burst is seconds
    long, so it is rare.
    """
    limit = max(1, int(limit))
    kind = _clean_kind(kind)
    fetched: list = []
    for branch in (BRANCH_DATED, BRANCH_FILE, BRANCH_MAIL):
        # limit + 1 from each branch, so "more exist" is a fact rather than a guess.
        for row in _fetch_branch(store, branch, period, cursor, kind, limit + 1):
            fetched.append((int(row["when_ns"]), branch, int(row["file_id"]), row))
    fetched.sort(key=lambda item: item[:3])
    more = len(fetched) > limit
    taken = fetched[:limit]

    have_volumes = any(row["volume_id"] is not None for *_k, row in taken)
    online = connected if connected is not None else _connected(store, have_volumes)
    entries = [_entry(row, branch, online) for _when, branch, _id, row in taken]

    if fold and len(entries) > 1:
        from app.search.folding import fold as fold_rows

        items = tuple(fold_rows(entries, enabled=True))
    else:
        from app.search.folding import Fold

        items = tuple(Fold(head=entry) for entry in entries)
    last = taken[-1][:3] if taken else None
    return TimelinePage(
        items=items, covered=len(entries), connected=online,
        cursor=Cursor(*last) if (more and last is not None) else None)


# ---------------------------------------------------------------------------
# The overview, and one file's date
# ---------------------------------------------------------------------------

#: `YYYYMM` in local time, from nanoseconds. Local, because that is what
#: the period bounds and the `after:`/`before:` operators use; the UTC form is
#: only the fallback for a platform that cannot do local time that far back.
_MONTH_OF_NS = ("CAST(COALESCE(strftime('%Y%m', {col} / 1000000000, 'unixepoch', 'localtime'), "
                "strftime('%Y%m', {col} / 1000000000, 'unixepoch')) AS INTEGER)")


def _rows(store: Any, sql: str, params: Sequence = ()) -> list:
    try:
        return store.conn.execute(sql, list(params)).fetchall()
    except Exception as exc:                          # noqa: BLE001 - a count, not the timeline
        _log.debug("timeline overview query failed: {}", exc)
        return []


def timeline_overview(store: Any, kind: str = "everything") -> Overview:
    r"""Counts by month, and how each item's date was arrived at. **Worker only.**

    **One pass over `files`, deliberately a table scan.** The files that count
    are found by `taken_at_ns`, `mtime_ns`, `source_kind`, `repo_id` and
    `ext` together, so no index covers the question, and the planner's
    alternative - walk `idx_files_mtime` and fetch each row - reads the table
    in date order, which is random order on disk. Measured on 200,000 rows
    (`test_timeline_scale.py`): that plan 3.1 s, a plain scan 0.25 s. So the
    scan is asked for (`NOT INDEXED`) rather than left to a plan that changes
    with the statistics. It is a count for a picker, run on a worker and cached
    until the index changes; it is not on any scrolling path.

    Months with nothing in them are absent; `Overview.months_of` fills the gaps
    for a picker. Never raises.
    """
    from app.reports.inheritance import report_generated_at

    kind = _clean_kind(kind)
    months: dict[int, int] = {}
    camera = guess = filed = sent = photo_camera = photo_filed = undated = 0

    def add(ym: Any, count: int) -> None:
        if ym and 1 <= int(ym) % 100 <= 12:
            months[int(ym)] = months.get(int(ym), 0) + count

    if kind != "mail":
        clause, kind_params = _file_kind_clause(kind)
        photos = sorted(PHOTO_EXTS)
        marks = ", ".join("?" for _ in photos)
        when = _MONTH_OF_NS.format(col="d")
        # `basis`: 0 a camera's date, 2 a year guessed from a folder, 1 the
        # file's own time; `d` is NULL for a file with no usable date at all.
        sql = (
            f"SELECT {when} AS ym, basis, photo, COUNT(*) AS n FROM ("
            "SELECT CASE WHEN f.taken_at_ns IS NOT NULL THEN f.taken_at_ns "
            "            WHEN f.mtime_ns > 0 THEN f.mtime_ns END AS d, "
            "       CASE WHEN f.taken_at_ns IS NULL THEN 1 "
            "            WHEN f.taken_at_is_hint THEN 2 ELSE 0 END AS basis, "
            f"       CASE WHEN f.ext IN ({marks}) THEN 1 ELSE 0 END AS photo "
            "FROM files AS f NOT INDEXED "
            f"WHERE f.source_kind = 'file' AND {clause.replace('+f.', 'f.')}) "
            "GROUP BY ym, basis, photo")
        for row in _rows(store, sql, [*photos, *kind_params]):
            n = int(row["n"])
            if not row["ym"]:
                if kind == "everything":
                    undated += n
                continue
            add(row["ym"], n)
            if row["basis"] == 0:
                camera += n
                photo_camera += n if row["photo"] else 0
            elif row["basis"] == 2:
                guess += n
            else:
                filed += n
                photo_filed += n if row["photo"] else 0
    if kind in ("everything", "mail"):
        mail_ym = _MONTH_OF_NS.format(col="(m.sent_at * 1000000000)")
        for row in _rows(store, f"SELECT {mail_ym} AS ym, COUNT(*) AS n FROM messages m "
                                "WHERE m.sent_at > 0 GROUP BY ym"):
            add(row["ym"], int(row["n"]))
            sent += int(row["n"]) if row["ym"] else 0
        row = _rows(store, "SELECT COUNT(*) AS n FROM messages m "
                           "WHERE m.sent_at IS NULL OR m.sent_at <= 0")
        undated += int(row[0]["n"]) if row else 0

    ordered = tuple(sorted((ym // 100, ym % 100, count) for ym, count in months.items() if count))
    return Overview(
        months=ordered, generated_at=report_generated_at(store), kind=kind,
        by_camera=camera, by_folder_guess=guess, by_file_date=filed, by_sent_date=sent,
        photos_with_camera_date=photo_camera, photos_by_file_date_only=photo_filed,
        undated=undated)


def refresh_overview(store: Any, kind: str = "everything",
                     known_generated_at: Optional[int] = None) -> Optional[Overview]:
    r"""`timeline_overview`, unless nothing has been indexed since the caller's
    copy - then None, and the counts already on screen are still right.

    "Cached until the next index run", read the way the Space Report's cache
    reads it (`report_generated_at` is `MAX(files.indexed_at)`): the recount
    is a scan of the whole table, and it should not run on every tab switch
    that changes nothing. **Worker only.**
    """
    from app.reports.inheritance import report_generated_at

    now = report_generated_at(store)
    if known_generated_at is not None and now == known_generated_at:
        return None
    return timeline_overview(store, kind)


def date_of_file(store: Any, file_id: int) -> Optional[int]:
    r"""The truthful date of one file, in epoch nanoseconds, or None.

    What "see everything from this month" asks: a result row carries a
    modified time, which for a photograph is the copy date and for a message
    is its container's. **Worker only.**
    """
    rows = _rows(
        store,
        "SELECT f.taken_at_ns, f.mtime_ns, f.source_kind, m.sent_at FROM files f "
        "LEFT JOIN messages m ON m.file_id = f.id WHERE f.id = ?", [int(file_id)])
    if not rows:
        return None
    row = rows[0]
    if row["sent_at"] and int(row["sent_at"]) > 0:
        return int(row["sent_at"]) * _NS
    if row["source_kind"] in ("pst_message", "eml"):
        return None                     # a message with no sent date has no honest one
    if row["taken_at_ns"] is not None:
        return int(row["taken_at_ns"])
    mtime = int(row["mtime_ns"] or 0)
    return mtime if mtime > 0 else None


def month_of_ns(when_ns: int) -> tuple[int, int]:
    """`(year, month)` a nanosecond timestamp falls in, in local time."""
    try:
        moment = datetime.fromtimestamp(when_ns / _NS)
    except (OverflowError, OSError, ValueError):
        moment = _EPOCH + timedelta(microseconds=when_ns // 1000)
    return moment.year, moment.month


