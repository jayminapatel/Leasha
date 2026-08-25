"""Everything the UI does that is not drawing — and therefore everything testable.

Layer: L5

Qt widgets cannot be instantiated without a display, so if the interesting logic
lives inside them it can only ever be verified by a person clicking. That is not
a standard this project holds anywhere else, and there is no reason for the UI
to be the exception.

So the layer is split. This module holds the decisions:

  * which search tier to run for a given keystroke,
  * how to cut a 1,600-character chunk down to a snippet centred on the match,
  * where the highlight ranges fall,
  * how to say "3 hours remaining" from a throughput measurement,
  * how to turn 4,000 skipped files into a handful of actionable rows.

The widgets in the sibling modules are thin: they draw what these functions
return and forward events back. What is left unverified is wiring, which a
person notices immediately; what is verified is the logic, which a person would
not notice being subtly wrong.

Nothing here imports Qt.
"""

from __future__ import annotations

import re
import time as _time
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

__all__ = [
    "Tier",
    "tier_for",
    "Snippet",
    "build_snippet",
    "shorten_path",
    "format_eta",
    "format_count",
    "SkipGroup",
    "group_skips",
    "ResultRow",
    "to_row",
    "to_rows",
    "ResultGroup",
    "group_results",
    "breadcrumb",
    "fetch_depth",
    "GROUP_FETCH_MULTIPLIER",
    "FileRow",
    "file_rows",
    "StatRow",
    "index_summary",
    "read_index_summary",
    "folder_size",
    "when_text",
    "doctor_report",
    "doctor_lines",
    "search_shape",
    "status_line",
    "results_message",
    "mail_details",
    "why",
    "kind_tag",
    "group_subtitle",
    "result_tooltip",
    "KIND_LABELS",
    "file_query",
    "interpret_message",
    "progress_for",
    "progress_text",
    "finished_text",
    "mail_rows",
    "mail_filters",
    "MailRow",
    "semantic_health",
    "format_size",
    "format_when",
    "SNIPPET_CHARS",
    "TYPING_DEBOUNCE_MS",
    "IDLE_DEBOUNCE_MS",
]

#: Milliseconds of stillness before the interim (keyword-only) tier runs.
TYPING_DEBOUNCE_MS = 150

#: Milliseconds of stillness before the full hybrid pipeline runs. Longer,
#: because it costs a model call and the person may not have finished thinking.
IDLE_DEBOUNCE_MS = 400

#: Characters shown per result. Enough to judge relevance, short enough that ten
#: results fit on a screen.
SNIPPET_CHARS = 240

#: Below this many characters, a query is too vague to spend the full pipeline
#: on - "co" matches half the corpus and the answer is not useful anyway.
MIN_FULL_SEARCH_CHARS = 3


class Tier:
    NONE = "none"
    INTERIM = "interim"
    FULL = "full"


def tier_for(query: str, *, still_for_ms: int, submitted: bool = False) -> str:
    """Which search to run, given how long the person has stopped typing.

    Pressing Enter always means the full pipeline, however short the query and
    however recently a key was pressed: it is an explicit statement of intent and
    second-guessing it is infuriating.
    """
    text = query.strip()
    if not text:
        return Tier.NONE
    if submitted:
        return Tier.FULL
    if len(text) < MIN_FULL_SEARCH_CHARS:
        # Still worth a keyword glance - prefix matching makes even two
        # characters useful - but not worth embedding.
        return Tier.INTERIM if still_for_ms >= TYPING_DEBOUNCE_MS else Tier.NONE
    if still_for_ms >= IDLE_DEBOUNCE_MS:
        return Tier.FULL
    if still_for_ms >= TYPING_DEBOUNCE_MS:
        return Tier.INTERIM
    return Tier.NONE


@dataclass(frozen=True)
class Snippet:
    """A window of text with the query terms located inside it."""

    text: str
    #: `(start, end)` pairs into `text`, ready for the view to paint.
    highlights: tuple[tuple[int, int], ...] = ()
    #: True when the window starts after the chunk's beginning.
    elided_start: bool = False
    elided_end: bool = False

    def marked(self, open_tag: str = "<b>", close_tag: str = "</b>") -> str:
        """The snippet with highlights wrapped, for a rich-text view.

        Built back-to-front so each insertion cannot shift the offsets of the
        ones still to come - the bug this ordering exists to prevent.
        """
        out = self.text
        for start, end in sorted(self.highlights, reverse=True):
            out = out[:start] + open_tag + out[start:end] + close_tag + out[end:]
        prefix = "…" if self.elided_start else ""
        suffix = "…" if self.elided_end else ""
        return prefix + out + suffix


def _term_pattern(terms: Sequence[str]) -> Optional[re.Pattern[str]]:
    """One case-insensitive pattern matching any term, longest first.

    Longest first matters: with "pump" and "pump station" both present, the
    shorter would otherwise win and highlight half the phrase.
    """
    cleaned = [re.escape(term.strip().rstrip("*")) for term in terms if term.strip()]
    if not cleaned:
        return None
    cleaned.sort(key=len, reverse=True)
    return re.compile(r"\b(" + "|".join(cleaned) + r")", re.IGNORECASE)


def build_snippet(
    text: str,
    terms: Sequence[str],
    *,
    width: int = SNIPPET_CHARS,
) -> Snippet:
    """Cut `text` to a window around the densest cluster of `terms`.

    A chunk is ~1,600 characters and a result row has room for about 240. Which
    240 decides whether the person can tell, without opening the file, that this
    is the right result - so the window goes where the matches are, not at the
    start. A chunk whose only match is in its last sentence is exactly the case
    where showing the first 240 characters is useless.

    Falls back to the opening of the text when nothing matches, which is what a
    vector-only hit looks like: it matched on meaning, so there is no term to
    centre on.
    """
    if not text:
        return Snippet("")

    flat = " ".join(text.split())
    pattern = _term_pattern(terms)
    if pattern is None:
        return _head(flat, width)

    matches = list(pattern.finditer(flat))
    if not matches:
        return _head(flat, width)

    centre = _densest(matches, width)
    start = max(0, centre - width // 2)
    end = min(len(flat), start + width)
    start = max(0, end - width)

    start = _snap_back(flat, start)
    end = _snap_forward(flat, end)

    window = flat[start:end]
    highlights = tuple(
        (match.start() - start, match.end() - start)
        for match in matches
        if match.start() >= start and match.end() <= end
    )
    return Snippet(
        text=window,
        highlights=highlights,
        elided_start=start > 0,
        elided_end=end < len(flat),
    )


def _densest(matches: list[re.Match[str]], width: int) -> int:
    """The centre of the window containing the most matches.

    A result whose terms all appear together is more convincing than one where
    they are scattered, and showing the cluster is what makes that visible.
    """
    best_centre = matches[0].start()
    best_count = 0
    for match in matches:
        window_start = match.start()
        count = sum(1 for other in matches if window_start <= other.start() < window_start + width)
        if count > best_count:
            best_count = count
            best_centre = window_start + min(width, 80) // 2
    return best_centre


def _snap_back(text: str, index: int) -> int:
    """Move left to a word boundary, so a snippet never opens mid-word."""
    if index <= 0:
        return 0
    space = text.rfind(" ", max(0, index - 30), index)
    return space + 1 if space != -1 else index


def _snap_forward(text: str, index: int) -> int:
    if index >= len(text):
        return len(text)
    space = text.find(" ", index, min(len(text), index + 30))
    return space if space != -1 else index


def _head(text: str, width: int) -> Snippet:
    if len(text) <= width:
        return Snippet(text)
    end = _snap_forward(text, width)
    return Snippet(text[:end], elided_end=end < len(text))


def shorten_path(path: str, *, limit: int = 70) -> str:
    """Elide the middle of a long path, keeping the drive and the filename.

    The two ends carry the information: which drive it is on, and what it is
    called. The middle is usually a folder hierarchy the person already knows.
    """
    if len(path) <= limit:
        return path
    separator = "\\" if "\\" in path else "/"
    parts = path.split(separator)
    if len(parts) <= 2:
        return path[: limit - 1] + "…"

    head, tail = parts[0], parts[-1]
    if len(head) + len(tail) + 5 >= limit:
        return f"{head}{separator}…{separator}{tail[-(limit - len(head) - 3):]}"

    middle: list[str] = []
    budget = limit - len(head) - len(tail) - 5
    for part in reversed(parts[1:-1]):
        if len(part) + 1 > budget:
            break
        middle.insert(0, part)
        budget -= len(part) + 1
    return separator.join([head, "…", *middle, tail])


def format_count(value: int) -> str:
    return f"{value:,}"


def format_eta(remaining: int, *, files_per_minute: float) -> str:
    """A human ETA from a measured rate.

    Deliberately vague past an hour. A progress bar claiming "2 hours 14 minutes"
    on a rate measured over the last thirty seconds is precision the number does
    not have, and being visibly wrong about it costs more trust than saying
    "about 2 hours" and being right.
    """
    if remaining <= 0:
        return "done"
    if files_per_minute <= 0:
        return "estimating…"

    minutes = remaining / files_per_minute
    if minutes < 1:
        return "less than a minute"
    if minutes < 60:
        return f"about {round(minutes)} minute{'s' if round(minutes) != 1 else ''}"

    hours = minutes / 60
    if hours < 24:
        return f"about {round(hours)} hour{'s' if round(hours) != 1 else ''}"
    return f"about {round(hours / 24)} day{'s' if round(hours / 24) != 1 else ''}"


@dataclass
class SkipGroup:
    """One reason files were skipped, with its count and its fix."""

    code: str
    count: int
    message: str = ""
    suggestion: str = ""
    action_type: str = ""
    action_payload: Optional[str] = None
    examples: list[str] = field(default_factory=list)

    @property
    def retryable(self) -> bool:
        """Worth offering a retry button for.

        A locked file is worth retrying - the program holding it has probably
        closed. A scanned PDF is not: it will still have no text layer, and a
        retry button that cannot possibly help is worse than none.
        """
        return self.code in {"ERR_FILE_LOCKED", "ERR_OUTLOOK_BUSY", "ERR_CLOUD_ONLY"}


def group_skips(
    summary: dict[str, int],
    *,
    examples: Optional[dict[str, list[str]]] = None,
) -> list[SkipGroup]:
    """Turn a skip tally into rows for the "N files skipped — review" panel.

    Sorted by count, because on a 100GB run the panel's job is to answer "what
    is the biggest thing I am missing?" - and 4,000 scanned PDFs matters more
    than one locked spreadsheet however recently the spreadsheet failed.

    The message and fix come from the error registry, so the panel says exactly
    what every other surface says about the same code.
    """
    from app.core.errors import make_error

    groups: list[SkipGroup] = []
    for code, count in summary.items():
        if not count:
            continue
        error = make_error(code, "ui", path="these files", ext="?", folder="?")
        groups.append(SkipGroup(
            code=code,
            count=int(count),
            message=error.message,
            suggestion=error.suggestion,
            action_type=error.action_type.value,
            action_payload=error.action_payload,
            examples=list((examples or {}).get(code, []))[:5],
        ))

    groups.sort(key=lambda group: (-group.count, group.code))
    return groups


@dataclass
class ResultRow:
    """One search result, formatted for display."""

    rank: int
    chunk_id: int
    file_id: int
    path: str
    display_path: str
    snippet: Snippet
    explain: str
    location: str = ""
    score: float = 0.0
    #: Straight through from `SearchResult`, which now carries both. Kept on the
    #: row rather than only on the group so a flat list can show them too.
    ext: str = ""
    mtime_ns: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank, "chunk_id": self.chunk_id, "path": self.path,
            "display_path": self.display_path, "snippet": self.snippet.text,
            "highlights": [list(h) for h in self.snippet.highlights],
            "explain": self.explain, "location": self.location,
        }


def to_row(result: Any, terms: Sequence[str], *, path_limit: int = 70) -> ResultRow:
    """Turn a `SearchResult` into something a list widget can draw."""
    page = getattr(result, "page", None)
    location = f"page {page}" if page is not None else ""

    return ResultRow(
        rank=getattr(result, "rank", 0),
        chunk_id=getattr(result, "chunk_id", 0),
        file_id=getattr(result, "file_id", 0),
        path=getattr(result, "path", ""),
        display_path=shorten_path(getattr(result, "path", ""), limit=path_limit),
        snippet=build_snippet(getattr(result, "text", ""), terms),
        explain=result.explain() if hasattr(result, "explain") else "",
        location=location,
        score=float(getattr(result, "score", 0.0) or 0.0),
    )


def to_rows(results: Iterable[Any], terms: Sequence[str], **kwargs: Any) -> list[ResultRow]:
    return [to_row(result, terms, **kwargs) for result in results]


# ---------------------------------------------------------------------------
# One row per document, not one per chunk
#
# **The problem this solves.** Results are chunk-level, and nothing grouped
# them, so a long PDF matching in five places took five of the top ten rows.
# The person saw three documents where they should have seen ten.
#
# **Grouping lives here and never in the engine.** That is not a stylistic
# preference. The measured baseline in HANDOFF.md §3b - 75% at rank 1 after
# translation - was taken against chunk-level ranking. Grouping inside the
# engine would change what "rank 1" means and make every future measurement
# incomparable with that one, silently. `SearchEngine` keeps returning exactly
# what it returns today; this decides how to draw it.
# ---------------------------------------------------------------------------

#: How many chunks to fuse before grouping, as a multiple of the groups shown.
#:
#: **Grouping shrinks the list, so the fetch has to be deeper than the display.**
#: If fifty chunks come back and thirty belong to one PDF, grouping yields far
#: fewer documents than chunks - and a fetch sized for the display count would
#: leave the page half empty on exactly the corpora this feature exists for.
#: Four is enough for a document matching in a handful of places without
#: quadrupling rerank cost.
GROUP_FETCH_MULTIPLIER = 4


def fetch_depth(display_count: int, *, multiplier: int = GROUP_FETCH_MULTIPLIER) -> int:
    """How many chunks to ask for, to end up with `display_count` documents."""
    return max(1, int(display_count)) * max(1, int(multiplier))


@dataclass(frozen=True, slots=True)
class ResultGroup:
    """Every matching chunk of one document, as a single row."""

    file_id: int
    #: Filename for a file; the subject for a message.
    name: str
    #: A breadcrumb, not a raw path. The full path stays on `path` for the
    #: tooltip, for opening, and for "Copy path" - shortening is a display
    #: choice and must never be the only copy of the truth.
    folder: str
    kind: str
    when: str
    path: str
    #: Every matching chunk, best first.
    rows: list[ResultRow] = field(default_factory=list)

    @property
    def best(self) -> Optional[ResultRow]:
        """The chunk shown while collapsed."""
        return self.rows[0] if self.rows else None

    @property
    def score(self) -> float:
        """**The best chunk's score, never the mean.**

        Averaging punishes a long document that matches strongly in one place -
        which is the common case in an archive, and precisely the document
        somebody is looking for.
        """
        return self.rows[0].score if self.rows else 0.0

    @property
    def match_count(self) -> int:
        return len(self.rows)

    @property
    def match_label(self) -> str:
        """Shown only when there is more than one. "1 match" is noise on every
        row of a list where one is the normal case."""
        return f"{self.match_count} matches" if self.match_count > 1 else ""


def group_results(
    rows: Sequence[ResultRow],
    *,
    limit: int = 0,
    details: Optional[Mapping[int, Mapping[str, Any]]] = None,
    now: Optional[float] = None,
) -> list[ResultGroup]:
    """Chunk rows to document groups, ordered by each group's best chunk.

    `rows` must already be in rank order - the engine's order is preserved
    rather than recomputed, which is what keeps this display-only.

    `details` optionally maps `file_id` to mail metadata (see
    `store.messages_for`), so a message can use its subject rather than a
    synthetic path nobody would recognise. Absent, or missing an entry, falls
    back to the filename without raising.
    """
    order: list[int] = []
    collected: dict[int, list[ResultRow]] = {}
    for row in rows:
        if row.file_id not in collected:
            collected[row.file_id] = []
            # First appearance decides position, so the best-ranked chunk of a
            # document decides where the document sits. No re-sorting needed.
            order.append(row.file_id)
        collected[row.file_id].append(row)

    groups = [
        _build_group(file_id, collected[file_id], (details or {}).get(file_id), now=now)
        for file_id in order
    ]
    return groups[:limit] if limit else groups


def _build_group(
    file_id: int,
    rows: list[ResultRow],
    detail: Optional[Mapping[str, Any]],
    *,
    now: Optional[float] = None,
) -> ResultGroup:
    path = rows[0].path if rows else ""
    name = path.replace("\\", "/").rstrip("/").rpartition("/")[2] or path
    folder = breadcrumb(path[: len(path) - len(name)])
    kind = (rows[0].ext if rows else "") or _ext_of(name)
    when = format_when(rows[0].mtime_ns, now=now) if rows else ""

    if detail:
        # A message: its path is a synthetic key nobody typed and nobody would
        # recognise, so the subject is the only usable name.
        subject = str(detail.get("subject") or "").strip()
        sender = format_address(detail.get("sender"))
        attachments = "1 attachment" if detail.get("has_attach") else ""
        name = subject or "(no subject)"
        folder = "  ·  ".join(bit for bit in (f"from {sender}" if sender else "",
                                              attachments) if bit)
        kind = "email"
        sent = detail.get("sent_at")
        if sent:
            when = format_sent(sent, now=now)

    return ResultGroup(
        file_id=file_id, name=name, folder=folder, kind=kind,
        when=when, path=path, rows=rows,
    )


def _ext_of(name: str) -> str:
    return name.rpartition(".")[2].lower() if "." in name else ""


#: How many folders of a path to show in the breadcrumb. The last few are the
#: ones that distinguish; `D:\Archive` is the same for everything.
BREADCRUMB_PARTS = 3


def breadcrumb(path: str, *, parts: int = BREADCRUMB_PARTS) -> str:
    r"""A path as `Archive > 2019 > Leeds`, keeping the end rather than the start.

    **`shorten_path` elides the middle, which is where the distinguishing part
    of a long archive path lives.** A person recognises the last two or three
    folders; nobody scans `D:\Archive\2019\Projects\...`. Keeping the tail is
    the same instinct as showing a filename before its directory.
    """
    cleaned = (path or "").replace("\\", "/").strip("/")
    if not cleaned:
        return ""
    pieces = [piece for piece in cleaned.split("/") if piece]
    # A drive letter on its own is not a folder anybody thinks in.
    if pieces and pieces[0].endswith(":"):
        pieces = pieces[1:]
    if not pieces:
        return ""
    tail = pieces[-parts:]
    prefix = "… > " if len(pieces) > parts else ""
    return prefix + " > ".join(tail)


# ---------------------------------------------------------------------------
# Finding a file by its name
#
# A different question from "which document says this", and it deserves a
# different answer: a name, where it lives, how big it is and when it changed.
# No snippet, because there is no match inside the text to show - the match is
# the name itself.
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class FileRow:
    file_id: int
    name: str
    folder: str
    kind: str
    size: str
    modified: str
    #: Set when the file is in the index but its contents are not searchable -
    #: a scanned PDF, something locked, something too big. Shown rather than
    #: hidden: "I can see the file but cannot search inside it" is a real and
    #: useful thing to know, and hiding it invites the same search twice.
    note: str = ""


def format_size(size_bytes: int) -> str:
    """Bytes as something a person reads. Never "1234567 bytes"."""
    value = float(max(0, size_bytes))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{value:,.1f} GB"


def format_when(mtime_ns: int, *, now: Optional[float] = None) -> str:
    """A modification time as an age, because that is what gets compared.

    "Yesterday" and "3 weeks ago" answer "is this the version I was working on"
    instantly; "2026-08-03 14:22:07" requires arithmetic. Beyond a year the
    date is more useful than the age, so it switches over.

    **Zero means "not known", and produces nothing.** It used to produce
    "01 Jan 1970", which is not a fallback but a claim - and a false one that
    looks entirely plausible in a fifteen-year archive. A row with no date is
    honest; a row dated 1970 sends somebody looking for a file that does not
    exist.
    """
    if not mtime_ns:
        return ""
    seconds = (now if now is not None else _time.time()) - (mtime_ns / 1_000_000_000)
    if seconds < 0:
        return "just now"          # a clock skew, or a file from the future
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86_400:
        hours = int(seconds // 3600)
        return f"{hours} hour ago" if hours == 1 else f"{hours} hours ago"
    days = int(seconds // 86_400)
    if days == 1:
        return "yesterday"
    if days < 30:
        return f"{days} days ago"
    if days < 365:
        weeks = days // 7
        return f"{weeks} week ago" if weeks == 1 else f"{weeks} weeks ago"
    return _time.strftime("%d %b %Y", _time.localtime(mtime_ns / 1_000_000_000))


#: What a status means to somebody looking at a list of files.
_STATUS_NOTES = {
    "SKIPPED": "indexed by name only - contents could not be read",
    "FAILED": "could not be read",
    "PENDING": "not indexed yet",
}


def file_rows(rows: Iterable[Mapping[str, Any]], *, now: Optional[float] = None) -> list[FileRow]:
    """Store rows to display rows for the Files list."""
    out: list[FileRow] = []
    for row in rows:
        path = str(row.get("path", ""))
        name = path.replace("\\", "/").rstrip("/").rpartition("/")[2] or path
        folder = path[: len(path) - len(name)].rstrip("/\\") or path
        status = str(row.get("status", ""))
        note = _STATUS_NOTES.get(status, "")
        if status == "SKIPPED" and row.get("skip_code"):
            note = f"{note} ({row['skip_code']})"
        out.append(FileRow(
            file_id=int(row.get("id", 0)),
            name=name,
            folder=shorten_path(folder, limit=60),
            kind=(str(row.get("ext", "")) or "?").upper(),
            size=format_size(int(row.get("size_bytes", 0))),
            modified=format_when(int(row.get("mtime_ns", 0)), now=now),
            note=note,
        ))
    return out


# ---------------------------------------------------------------------------
# The progress bar
#
# Its own function because the arithmetic was wrong and nothing could have
# caught it: a bar that moves too slowly still moves, and "the progress does not
# feel right" is the only symptom anybody can report.
# ---------------------------------------------------------------------------

def interpret_message(translation: Any) -> tuple[Optional[str], str]:
    """`(new box text or None, status line)` for a finished interpretation.

    `None` for the box means leave what the person typed alone. A translation
    that changed nothing must not overwrite the box with an identical string -
    it would move the cursor and clear the selection for no reason, which reads
    as the button having done something destructive.
    """
    if getattr(translation, "changed", False):
        return str(translation.query), str(getattr(translation, "note", "") or "")
    return None, str(getattr(translation, "note", "") or "")


def file_query(raw: str) -> tuple[str, list[str]]:
    """A Files-tab query as `(name text, extensions)`.

    **The `/` commands are parsed, not merely offered.** The dropdown arrived
    here as a copy of the search box's, so `/type pdf` was inserted as
    `type:pdf` and then handed to a trigram index as a literal string - matching
    nothing, with a dropdown cheerfully suggesting it. An offer the application
    does not honour is worse than no offer at all.

    Only the filters this tab can apply survive: `type:` becomes the `ext`
    argument the store already takes. Everything else in the parse is about
    document *contents*, which this tab never reads.
    """
    from app.search.commands import expand_slashes
    from app.search.query import parse_query

    parsed = parse_query(expand_slashes((raw or "").strip()))
    text = " ".join((*parsed.terms, *parsed.names)).strip() or (parsed.text or "").strip()
    return text, list(parsed.ext)


#: A short tag per kind, rather than an icon font or bundled SVGs.
#:
#: Text survives dark mode, high-DPI and a missing font file, all of which an
#: icon set has to be got right for - and none of which is worth spending on
#: before anybody has said the tags are insufficient.
KIND_LABELS = {
    "email": "MAIL",
    "pdf": "PDF",
    "docx": "DOC", "doc": "DOC", "odt": "DOC", "rtf": "DOC",
    "xlsx": "XLS", "xls": "XLS", "ods": "XLS", "csv": "CSV",
    "pptx": "PPT", "ppt": "PPT", "odp": "PPT",
    "txt": "TXT", "md": "TXT", "log": "TXT",
}


def kind_tag(kind: str) -> str:
    """Four characters at most, so an unknown type still gets a legible tag."""
    return KIND_LABELS.get(kind, (kind or "?").upper()[:4])


def why(row: Any) -> str:
    """Why this result is here, for a tooltip or the right-click menu.

    **Moved off the row, not deleted.** `keyword and meaning both matched ·
    score 0.83` is genuinely valuable - being able to ask is where trust comes
    from - but it was the second thing the eye landed on, on every row, for the
    life of the application.
    """
    bits = [
        getattr(row, "location", ""),
        getattr(row, "explain", ""),
        f"score {getattr(row, 'score', 0.0):.2f}",
    ]
    return "  ·  ".join(bit for bit in bits if bit)


def group_subtitle(group: Any, *, show_scores: bool = False,
                   expanded: bool = False) -> str:
    """The grey line under the name: where it is, and how many matches.

    `expanded` is passed in rather than read off the group, because a
    `ResultGroup` is frozen and describes the *data*. Whether its chunks are
    currently on screen is a fact about one view at one moment, and putting it
    on the dataclass would make two views of the same results fight over it.
    """
    bits = [getattr(group, "folder", "")]
    label = getattr(group, "match_label", "")
    if label:
        bits.append(f"{label} {'▾' if expanded else '▸'}")
    best = getattr(group, "best", None)
    if show_scores and best is not None:
        bits.append(f"{best.explain}  ·  score {group.score:.2f}")
    return "  ·  ".join(bit for bit in bits if bit)


def result_tooltip(payload: Any, *, missing: bool = False) -> str:
    """The full path and the explanation - both of which came off the row.

    The breadcrumb is a display choice; the tooltip is where the truth stays.
    """
    path = getattr(payload, "path", "")
    best = getattr(payload, "best", payload)
    lines = [path]
    if best is not None:
        lines.append(why(best))
    if missing:
        lines.append("This file is missing - the index is stale for it.")
    return "\n\n".join(line for line in lines if line)


def mail_details(store: Any, results: Any) -> dict:
    """Subjects and senders for the messages on one page of results.

    **One query for the page, never one per row.** At the fetch depth grouping
    needs, a per-row lookup is fifty queries per keystroke - the shape of
    slowness that gets blamed on the search itself.

    Never raises. A missing subtitle is a cosmetic loss; failing the search that
    produced it is not, and a store that has been closed underneath a worker is
    a normal condition during shutdown rather than an error.
    """
    if store is None or not hasattr(store, "messages_for"):
        return {}
    try:
        return store.messages_for([getattr(r, "file_id", 0) for r in results or ()])
    except Exception:                            # noqa: BLE001 - see docstring
        return {}


def results_message(response: Any) -> tuple[str, str]:
    """`(summary, status)` for a completed search.

    Three branches deciding two strings, which is logic - and logic inside a Qt
    widget can only be checked by a person searching for the right thing at the
    right moment.

    The precedence matters and is the reason this is one function rather than
    three scattered `setText` calls: an ignored operator is the most actionable
    thing that can be said, so it wins; a dead semantic half is next, because
    silent degradation is how "search feels worse than it should" goes
    unreported for weeks; the plain count is the fallback.
    """
    parsed = getattr(response, "parsed", None)
    summary = status_line(response)

    if not getattr(response, "results", None):
        hint = ""
        if parsed is not None and getattr(parsed, "has_filters", False):
            # The single most common cause of a surprising empty result, and
            # invisible otherwise: the filters are doing exactly what they were
            # told and excluding everything.
            hint = "  The filters may be excluding everything."
        return f"No results.{hint}", summary

    unknown = list(getattr(parsed, "unknown_operators", ()) or ()) if parsed else []
    if unknown:
        return summary, "Ignored: " + ", ".join(unknown)
    return summary, semantic_health(response) or ""


def progress_for(stats: Any, *, total_estimate: int = 0) -> tuple[int, int]:
    """`(value, maximum)` for the bar, given a progress tick.

    **The bug this replaces:** the numerator was `unchanged + skipped`, which
    leaves out `indexed` - the files the run is actually doing work on. A first
    index of a fresh corpus has nothing unchanged and little skipped, so the bar
    sat near zero for hours while the log showed thousands of files done. The
    one job of a progress bar is to say how far through it is, and it was
    reporting the opposite of the truth on the run where it matters most.

    Everything the walker has finished with counts, whichever way it finished.

    The denominator is what the walker has found *so far*, which grows as it
    goes. Honest, and it moves - unlike a fixed total nobody can know before the
    walk completes, or an indeterminate bar that spins forever and reads as
    stuck.
    """
    done = (
        int(getattr(stats, "indexed", 0) or 0)
        + int(getattr(stats, "unchanged", 0) or 0)
        + int(getattr(stats, "skipped", 0) or 0)
    )
    seen = int(getattr(stats, "seen", 0) or 0)
    total = max(int(total_estimate or 0), seen, done, 1)
    # Clamped: `seen` can lag `done` by a tick, and a bar drawn past its own
    # maximum is a Qt warning on the console and a full bar on screen while the
    # run is plainly still going.
    return min(done, total), total


def status_line(response: Any) -> str:
    """The line under the search box: how many, how fast, and with what caveats.

    **The caveats are the point.** "12 results" beside a list that is still
    being reranked is a different claim from "12 results" beside a finished one,
    and a cached result that looks identical to a fresh one is how somebody
    concludes the index is not picking up their new files. Each qualifier is
    there because leaving it out would let the line say something untrue.
    """
    bits = [
        f"{len(getattr(response, 'results', []) or [])} result(s)",
        f"{getattr(response, 'elapsed_ms', 0) or 0:.0f}ms",
    ]
    if getattr(response, "interim", False):
        bits.append("keyword only, still searching…")
    if getattr(response, "from_cache", False):
        bits.append("cached")
    if getattr(response, "reranked", False):
        bits.append("reranked")
    return "  ·  ".join(bits)


def progress_text(stats: Any, *, total_estimate: int = 0, stopping: bool = False) -> tuple[str, str]:
    """`(headline, detail)` for a progress tick.

    Here rather than in the view for the reason this module exists: it is string
    formatting with three branches, and a branch inside a Qt widget can only be
    checked by a person watching an index run at the right moment.
    """
    if stopping:
        # Stopping can take a while on a large file, and a dead button with no
        # explanation reads as a click that was ignored.
        return (
            "Stopping after the current file…",
            f"{format_count(getattr(stats, 'indexed', 0))} indexed so far. "
            "Everything indexed is kept.",
        )

    done, _total = progress_for(stats, total_estimate=total_estimate)
    remaining = max(0, total_estimate - done) if total_estimate else 0
    eta = format_eta(remaining, files_per_minute=getattr(stats, "files_per_minute", 0) or 0)

    headline = (
        f"{format_count(getattr(stats, 'indexed', 0))} documents  ·  "
        f"{format_count(getattr(stats, 'seen', 0))} files seen  ·  "
        f"{format_count(getattr(stats, 'skipped', 0))} skipped"
    )

    current = getattr(stats, "current", "") or ""
    reading = f"  ·  reading {current}" if current else ""
    if current and getattr(stats, "current_item", 0):
        reading += f" [{stats.current_item:,}]"

    detail = (
        f"{format_count(getattr(stats, 'chunks', 0))} chunks  ·  "
        f"{getattr(stats, 'files_per_minute', 0) or 0:,.0f} files/min  ·  {eta}{reading}"
    )
    return headline, detail


def finished_text(stats: Any) -> tuple[str, str]:
    """`(headline, detail)` for a completed run.

    A run stopped by the resource governor reports *its* message rather than a
    tally: "Finished: 400 indexed" after a run that gave up at 4% because the
    disk filled is technically true and completely misleading.
    """
    stopped = getattr(stats, "stopped_early", None)
    if stopped is not None:
        return str(getattr(stopped, "message", stopped)), str(getattr(stopped, "suggestion", ""))

    headline = (
        f"Finished: {format_count(getattr(stats, 'indexed', 0))} indexed, "
        f"{format_count(getattr(stats, 'skipped', 0))} skipped, "
        f"{format_count(getattr(stats, 'deleted', 0))} removed"
    )
    detail = (
        f"{format_count(getattr(stats, 'chunks', 0))} chunks in "
        f"{getattr(stats, 'elapsed_s', 0) or 0:,.0f}s  ·  "
        f"{getattr(stats, 'files_per_minute', 0) or 0:,.0f} files/min, "
        f"{getattr(stats, 'mb_per_minute', 0) or 0:,.1f} MB/min"
    )
    return headline, detail


# ---------------------------------------------------------------------------
# Mail, as a table
#
# A third question again. A message has no useful filename and no folder, and
# ranking a mailbox by relevance puts an eight-year-old thread above this
# morning's - so mail gets its own columns and its own order.
#
# The columns are the ones people scan for: who, to whom, when, what it was
# called, whether anything was attached, and how big. Everything below is string
# formatting, which is why it lives here and not in the view.
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class MailRow:
    file_id: int
    sender: str
    recipients: str
    sent: str
    subject: str
    attachment: str
    size: str
    #: The synthetic path for the message. Not shown - nobody typed it and
    #: nobody would recognise it - but the menu needs it to act on the row.
    path: str = ""
    #: Sortable originals, so the table can sort by real values rather than by
    #: the formatted strings. "3 KB" and "10 KB" sort the wrong way as text, and
    #: a date column sorted alphabetically is worse than no sorting at all.
    sent_at: int = 0
    size_bytes: int = 0
    has_attachment: bool = False


#: How many recipients to name before summarising. Long enough to recognise a
#: two-person thread at a glance, short enough that a message to a distribution
#: list does not push every other column off the screen.
RECIPIENTS_SHOWN = 2


def format_address(value: Any) -> str:
    """One address, as short as it can be without becoming ambiguous.

    `Dave Smith <dave@acme.com>` becomes `Dave Smith`, because in a column of
    thirty rows the name is what distinguishes them and the domain is usually
    the same for all thirty. A bare address is left alone - there is nothing to
    shorten to.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if "<" in text and text.endswith(">"):
        name = text.split("<", 1)[0].strip().strip('"').strip()
        if name:
            return name
    return text


def format_recipients(value: Any, *, shown: int = RECIPIENTS_SHOWN) -> str:
    """The recipients column, from the JSON array the store holds.

    **Never raises on bad input.** This runs over every row of a mailbox that
    may hold two hundred thousand messages written by a decade of different
    clients; one malformed field must cost that row its column, not the table.
    """
    import json

    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):
            try:
                value = json.loads(text)
            except ValueError:
                return text          # not JSON after all; show what is there
        else:
            return text

    if not isinstance(value, (list, tuple)):
        return str(value or "")

    names = [format_address(item) for item in value]
    names = [name for name in names if name]
    if not names:
        return ""
    if len(names) <= shown:
        return ", ".join(names)
    return f"{', '.join(names[:shown])} +{len(names) - shown}"


def format_sent(sent_at: Any, *, now: Optional[float] = None) -> str:
    """A date a person can scan a column of.

    Absolute, not "3 days ago". Relative time reads well for a single file but
    badly down a sorted column, where the eye is looking for a boundary between
    March and April and finds "5 weeks ago" instead.
    """
    try:
        seconds = int(sent_at)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""

    reference = now if now is not None else _time.time()
    stamp = _time.localtime(seconds)
    # Within the last year, the year is noise - the month and day carry it, and
    # the time of day is what separates messages sent the same afternoon.
    if 0 <= reference - seconds < 365 * 86_400:
        return _time.strftime("%d %b %H:%M", stamp)
    return _time.strftime("%d %b %Y", stamp)


def mail_rows(
    rows: Iterable[Mapping[str, Any]], *, now: Optional[float] = None
) -> list[MailRow]:
    """Store rows to display rows for the Mail table."""
    out: list[MailRow] = []
    for row in rows:
        sent_at = row.get("sent_at") or 0
        try:
            sent_at = int(sent_at)
        except (TypeError, ValueError):
            sent_at = 0
        size_bytes = int(row.get("size_bytes") or 0)
        attached = bool(row.get("has_attach"))
        out.append(MailRow(
            file_id=int(row.get("file_id") or 0),
            sender=format_address(row.get("sender")),
            recipients=format_recipients(row.get("recipients")),
            sent=format_sent(sent_at, now=now),
            # An empty subject is common and meaningful. Blank looks like a
            # rendering fault; saying so does not.
            subject=str(row.get("subject") or "").strip() or "(no subject)",
            attachment="Yes" if attached else "",
            size=format_size(size_bytes),
            path=str(row.get("path") or ""),
            sent_at=sent_at,
            size_bytes=size_bytes,
            has_attachment=attached,
        ))
    return out


def mail_filters(parsed: Any) -> dict[str, Any]:
    """A `ParsedQuery` as keyword arguments for `store.browse_messages`.

    The whole reason the Mail tab can reuse the `/` commands: the parser already
    produces `senders`, `recipients`, `subjects`, `has_attachment`, `after` and
    `before`, which is precisely the set of columns `messages` has. Nothing new
    had to be invented, and a filter that works in the search box works here
    with the same spelling.

    **Only the first value of each is used.** `from:dave from:priya` is a
    contradiction on a single column - no message has two senders - and taking
    the first is more honest than silently ANDing to zero results.

    Free text is deliberately ignored. `browse_messages` reads `messages` and
    never touches chunk text, so accepting words here would produce an empty
    table for a query that looks reasonable. The view says so instead.
    """
    def first(values: Any) -> Optional[str]:
        items = tuple(values or ())
        return str(items[0]) if items else None

    filters: dict[str, Any] = {
        "sender": first(getattr(parsed, "senders", ())),
        "recipient": first(getattr(parsed, "recipients", ())),
        "subject": first(getattr(parsed, "subjects", ())),
        "has_attachment": getattr(parsed, "has_attachment", None),
    }

    # Dates arrive as `date` objects and the column holds epoch seconds.
    # `before` is exclusive in the store, so a `before:2024-06-01` excludes the
    # whole of that day rather than including part of it - the reading of
    # "before June" that matches what people mean.
    for name in ("after", "before"):
        value = getattr(parsed, name, None)
        if value is not None:
            filters[name] = int(
                _time.mktime((value.year, value.month, value.day, 0, 0, 0, 0, 0, -1))
            )

    return {key: value for key, value in filters.items() if value is not None}


# ---------------------------------------------------------------------------
# doctor.py, run and rendered
#
# Here rather than in `settings_view.py` for the reason this module exists: a
# subprocess call and a text formatter are logic, and logic in a Qt widget can
# only be checked by a person clicking a button. The view is left with two
# lines - start a worker, put the result in a text box.
# ---------------------------------------------------------------------------

#: doctor.py probes Outlook over COM and opens LanceDB, so it is slow by nature
#: rather than by accident. Generous, because this now runs in a worker and a
#: timeout that fires early turns a slow answer into no answer.
DOCTOR_TIMEOUT_S = 180


def doctor_report(timeout_s: int = DOCTOR_TIMEOUT_S) -> dict:
    """Run `doctor.py --json --quick` and parse the result.

    **Never call this on the UI thread.** It was called there, and a check that
    can take two minutes with the event loop stopped is a window Windows paints
    "Not Responding" over - indistinguishable from a crash, and unkillable with
    Ctrl+C because Qt never lets the interpreter run to see the signal.

    `CREATE_NO_WINDOW` stops a console flashing up on Windows when the app was
    launched from a shortcut: `pythonw.exe` has no console, so the child would
    otherwise create one of its own.
    """
    import json
    import subprocess
    import sys

    from app.core.config import project_root
    from app.core.errors import AppErrorException, make_error

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    finished = subprocess.run(
        [sys.executable, str(project_root() / "doctor.py"), "--json", "--quick"],
        capture_output=True, text=True, timeout=timeout_s, check=False,
        creationflags=flags,
    )
    try:
        return json.loads(finished.stdout)
    except ValueError:
        # Doctor's diagnostics are worth most at exactly the moment it fails to
        # produce JSON, so stderr is surfaced rather than swallowed.
        detail = (finished.stderr or finished.stdout or "").strip()[:2000]
        raise AppErrorException(make_error(
            "ERR_UNEXPECTED", "ui.doctor",
            details=f"doctor.py exited {finished.returncode} without valid JSON:\n{detail}",
            suggestion=(
                "Run it yourself to see the whole output: "
                r"venv\Scripts\python.exe doctor.py"
            ),
        )) from None


def doctor_lines(report: Mapping[str, Any]) -> list[str]:
    """Render a doctor report as plain text for the Settings panel.

    Tolerant of a malformed report on purpose: this is the diagnostics view, and
    a formatter that raises on a missing key hides the very output somebody
    opened it to read.
    """
    lines = ["READY" if report.get("ready") else "NOT READY", ""]
    for check in report.get("checks") or ():
        ok = bool(check.get("ok"))
        mark = "PASS" if ok else ("WARN" if check.get("optional") else "FAIL")
        lines.append(f"[{mark}] {check.get('name', '?')}  {check.get('detail', '')}".rstrip())
        if not ok and check.get("fix"):
            lines.append(f"       FIX: {check['fix']}")
    return lines


def search_shape(response: Any, *, query_len: int, scope: str) -> dict[str, Any]:
    """One completed search, described by shape only - never by its text.

    For the debug recorder. `keyword_hits` and `vector_hits` are the two fields
    that justify the whole thing: `vector.search` returns `[]` for an empty
    vector store, a failed embedding or a LanceDB hiccup, and search carries on
    with keyword results because half a search beats none. That is the right
    behaviour and it makes the failure **invisible** - results look thin, and
    nothing distinguishes "the corpus is thin" from "the semantic half is dead".

    `vector_hits == 0` beside a healthy `keyword_hits` is the signal, and a
    session file carries it without anybody having to know to ask.
    """
    parsed = getattr(response, "parsed", None)
    return {
        "tier": "interim" if getattr(response, "interim", False) else "full",
        "query_len": query_len,
        "terms": len(parsed.terms) if parsed else 0,
        "phrases": len(parsed.phrases) if parsed else 0,
        "filters": bool(parsed and parsed.has_filters),
        "scope": scope,
        "results": len(getattr(response, "results", ())),
        "keyword_hits": getattr(response, "keyword_count", None),
        "vector_hits": getattr(response, "vector_count", None),
        "elapsed_ms": round(getattr(response, "elapsed_ms", 0.0), 1),
        "reranked": bool(getattr(response, "reranked", False)),
        "from_cache": bool(getattr(response, "from_cache", False)),
    }


def semantic_health(response: Any) -> Optional[str]:
    """A sentence for the status bar when meaning-based search is not working.

    None when it is fine. The condition - keyword results but no vector results -
    is not something a person can infer from a results list, and the fix is a
    single command, so saying it is far better than letting the results quietly
    be worse than they should be.
    """
    keyword = getattr(response, "keyword_count", 0) or 0
    vector = getattr(response, "vector_count", 0) or 0
    if keyword and not vector:
        return ("Keyword results only - meaning-based search returned nothing. "
                "Check it with: app.cli stats")
    return None


#: Entities loaded into the Graph panel's table. Above this the table itself
#: becomes the bottleneck rather than the query, and nobody scrolls 500 rows.
GRAPH_TABLE_LIMIT = 500




# ---------------------------------------------------------------------------
# What is in the index, as a panel
#
# The Indexing tab showed one line - documents and chunks - and showed nothing
# at all when the store read failed, because the handler was `except: return`.
# A blank page is the worst possible answer to "is my index working": it is
# indistinguishable from an empty index, a broken one, and a bug.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StatRow:
    """One line of the index summary."""

    label: str
    value: str
    #: A quiet explanation under the value, or "".
    note: str = ""
    #: True when this row is reporting a problem the person should act on.
    warn: bool = False


def index_summary(
    stats: Optional[Mapping[str, Any]],
    vectors: Optional[Mapping[str, Any]] = None,
    *,
    data_path: str = "",
    disk_bytes: Optional[int] = None,
    last_run: str = "",
    next_run: str = "",
    error: str = "",
) -> list[StatRow]:
    """Everything worth knowing about the index, in one list.

    Takes plain mappings rather than a store, so it is tested without a
    database and cannot itself do I/O on the UI thread.

    **It always returns rows.** When the store could not be read it says so, in
    the same shape as any other answer - because the alternative, which is what
    it did before, is an empty panel that looks identical to having no index.
    """
    if error:
        return [StatRow("Could not read the index", error, warn=True)]

    if not stats:
        return [StatRow(
            "Nothing indexed yet",
            "Add a folder in Settings, then press Start indexing.",
        )]

    documents = int(stats.get("files_total", 0) or 0)
    chunks = int(stats.get("chunks_total", 0) or 0)
    embedded = int(stats.get("chunks_embedded", 0) or 0)
    rows_in_vectors = int((vectors or {}).get("rows", 0) or 0)

    out = [
        StatRow("Documents", f"{documents:,}", _by_status(stats.get("files"))),
        StatRow("Searchable passages", f"{chunks:,}"),
    ]

    # **The coverage line.** Meaning-based search runs only on passages that
    # have a vector, and a partly-embedded corpus is findable by exact words
    # only - silently. This is the number that was invisible for weeks while
    # 154 of 3,355 passages were embedded and search quietly did half its job.
    if chunks:
        covered = rows_in_vectors / chunks
        out.append(StatRow(
            "Meaning-based search covers",
            f"{covered:.0%}",
            note=(f"{rows_in_vectors:,} of {chunks:,} passages have a vector. "
                  "The rest are findable by exact words only."
                  if covered < 0.95 else
                  f"{rows_in_vectors:,} vectors"),
            warn=covered < 0.95,
        ))
        if embedded and rows_in_vectors > embedded * 1.05:
            out.append(StatRow(
                "Orphaned vectors",
                f"{rows_in_vectors - embedded:,}",
                note="More vectors than passages. Rebuild to clear them.",
                warn=True,
            ))

    skipped = stats.get("skipped_by_code") or {}
    if skipped:
        worst = sorted(skipped.items(), key=lambda kv: -int(kv[1]))[:3]
        out.append(StatRow(
            "Skipped",
            f"{sum(int(v) for v in skipped.values()):,}",
            note=", ".join(f"{code} ({count})" for code, count in worst),
        ))

    if data_path:
        # Answers "is the index where I told it to be" at a glance, which is
        # otherwise a question requiring the CLI.
        size = f"  ·  {format_size(disk_bytes)}" if disk_bytes else ""
        out.append(StatRow("Index location", data_path, note=f"on disk{size}" if size else ""))

    if last_run:
        out.append(StatRow("Last run", last_run, note=next_run))
    elif next_run:
        out.append(StatRow("Next run", next_run))

    return out


def _by_status(files: Any) -> str:
    """`{'INDEXED': 355}` as words. Failures are named; successes are counted."""
    if not isinstance(files, Mapping) or not files:
        return ""
    parts = []
    for status, count in sorted(files.items()):
        label = str(status).lower()
        parts.append(f"{int(count):,} {label}")
    return ", ".join(parts)


def read_index_summary(store: Any, settings: Any = None) -> dict[str, Any]:
    """Gather everything `index_summary` needs. **Runs in a worker, never on the
    UI thread** - it opens the vector store and walks a folder.

    Returns a payload rather than rows so the failure is data too: a locked
    database produces `{"error": ...}`, which `index_summary` renders as a row
    like any other. The previous version of this returned early on any
    exception, leaving the page blank with nothing to explain it.
    """
    payload: dict[str, Any] = {"error": ""}
    try:
        payload["stats"] = store.stats()
        payload["last_run"] = store.get_state("index:last_run") or ""
    except Exception as exc:                     # noqa: BLE001 - reported, not swallowed
        payload["error"] = f"{type(exc).__name__}: {exc}"
        return payload

    if settings is None:
        return payload

    payload["data_path"] = str(getattr(settings, "data_path", ""))
    payload["disk_bytes"] = folder_size(getattr(settings, "data_path", None))
    try:
        from app.storage.vector_store import VectorStore

        with VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors:
            payload["vectors"] = vectors.stats() if vectors.exists else {}
    except Exception:                            # noqa: BLE001 - one missing number
        payload["vectors"] = {}
    return payload


def folder_size(path: Any) -> Optional[int]:
    """Bytes under a folder, or None. Never raises and never takes long.

    Capped at a few thousand files: the index is a handful of large files plus a
    model cache, so a full walk is unnecessary, and a diagnostic that stalls on
    a network drive is worse than one that says nothing.
    """
    if not path:
        return None
    from pathlib import Path as _Path

    total = 0
    seen = 0
    try:
        for item in _Path(path).rglob("*"):
            if seen > 5000:
                break
            try:
                if item.is_file():
                    total += item.stat().st_size
                    seen += 1
            except OSError:
                continue
    except OSError:
        return None
    return total or None


def when_text(iso: str) -> str:
    """An ISO timestamp as "2 hours ago", or "" if it is not one.

    A corrupt or absent timestamp must not stop the panel drawing - it is one
    line out of eight, and losing the other seven to it would be a poor trade.
    """
    if not iso:
        return ""
    from datetime import datetime

    try:
        moment = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    return format_when(int(moment.timestamp() * 1e9))
