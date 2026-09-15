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

import os
import re
import time as _time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, NamedTuple, Optional, Sequence

from app.core.logging import logger

#: Only ever used to record something being swallowed. Nothing in this module
#: logs on a success path: it runs per result row and per keystroke, and a log
#: line there is a log nobody can read.
_log = logger.bind(component="ui.presenter")

__all__ = [
    "cell_location",
    "GIT_ONLY",
    "CodeRoute",
    "code_route",
    "code_type_filter",
    "GitScope",
    "git_rows_matching",
    "code_rows_for",
    "code_summary",
    "git_result_row",
    "git_summary",
    "repo_root_for",
    "notice_line",
    "Tier",
    "tier_for",
    "Snippet",
    "build_snippet",
    "shorten_path",
    "elide_path_left",
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
    "RepoRow",
    "repo_rows",
    "repo_summary",
    "repo_empty_state",
    "RepoFilter",
    "repo_filter",
    "repo_filter_summary",
    "REPO_KINDS",
    "StatRow",
    "index_summary",
    "read_index_summary",
    "folder_size",
    "when_text",
    "archive_summary",
    "mail_summary",
    "doctor_report",
    "doctor_lines",
    "install_package",
    "search_shape",
    "status_line",
    "results_message",
    "mail_details",
    "missing_paths",
    "file_summary",
    "search_options",
    "decorate_results",
    "record_open",
    "why",
    "row_identity",
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
    "results_terminator",
    "Terminator",
    "CODE_EXTENSIONS",
    "is_code_kind",
    "notice_register_for",
    "SNIPPET_CHARS",
    "TYPING_DEBOUNCE_MS",
    "IDLE_DEBOUNCE_MS",
    "value_suggestions",
    "enabled_extensions",
    "clear_format_catalogue",
    "VALUE_LIMIT",
    "VALUE_LIMITS",
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
    """Move to a nearby word/sentence boundary, preferring sentence over word.

    Never opens mid-word. Prefer starting at a sentence boundary when one
    begins within a few words of the ideal window start - in EITHER
    direction, not only before it. The common case this exists for is
    exactly the one where the search-only-backward version of this function
    failed its own test: the density-derived ideal start lands mid-word,
    partway through an earlier sentence, with the *next* sentence beginning
    only a few words later. Searching backward alone never finds that -
    there is no sentence boundary behind a mid-first-sentence index - so a
    forward check within the same small distance is needed too. Backward is
    tried first because it loses the least content when both exist.
    """
    if index <= 0:
        return 0

    # A sentence boundary shortly behind the ideal start.
    sent_boundary = _find_sentence_start(text, max(0, index - 60), index)
    if sent_boundary is not None:
        return sent_boundary

    # None behind - the same check just ahead, closest terminator first.
    limit = min(len(text), index + 60)
    for i in range(index, limit):
        if text[i] in ".!?":
            pos = i + 1
            while pos < len(text) and text[pos] == " ":
                pos += 1
            if pos <= limit:
                return pos
            break

    # Fall back to word boundary
    space = text.rfind(" ", max(0, index - 30), index)
    return space + 1 if space != -1 else index


def _snap_forward(text: str, index: int) -> int:
    """Move right to a word/sentence boundary, preferring sentence over word."""
    if index >= len(text):
        return len(text)

    # Look for a sentence boundary within a reasonable distance
    sent_boundary = _find_sentence_end(text, index, min(len(text), index + 60))
    if sent_boundary is not None:
        return sent_boundary

    # Fall back to word boundary
    space = text.find(" ", index, min(len(text), index + 30))
    return space if space != -1 else index


def _find_sentence_start(text: str, start_idx: int, end_idx: int) -> Optional[int]:
    """Find the start of a sentence (after . ! ?) between start_idx and end_idx.

    Returns the position after the sentence terminator (ready to be the start
    of the window), or None if no sentence boundary found.
    """
    for i in range(end_idx - 1, start_idx - 1, -1):
        if i < 0:
            break
        if text[i] in ".!?":
            # Found a sentence terminator; skip it and any following spaces
            pos = i + 1
            while pos < len(text) and text[pos] == " ":
                pos += 1
            if start_idx <= pos <= end_idx:
                return pos
    return None


def _find_sentence_end(text: str, start_idx: int, end_idx: int) -> Optional[int]:
    """Find the end of a sentence (. ! ?) between start_idx and end_idx.

    Returns the position after the sentence terminator, or None if no
    sentence boundary found.
    """
    for i in range(start_idx, end_idx):
        if i >= len(text):
            break
        if text[i] in ".!?":
            # Found a sentence terminator; return position after it
            pos = i + 1
            if start_idx <= pos <= end_idx:
                return pos
    return None


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


def elide_path_left(path: str, *, limit: int = 70) -> str:
    r"""Elide the left (beginning) of a long path, keeping the tail.

    The leaf folder and filename distinguish the path; the parent hierarchy
    is usually known. `…\Projects\Foo\Final` reads better than
    `D:\Archive\2019\Projects\...` for identifying a result.

    Item 4a of work order 0q.
    """
    if len(path) <= limit:
        return path

    separator = "\\" if "\\" in path else "/"
    parts = path.split(separator)

    # A single path component (e.g., a filename) cannot be shortened
    if len(parts) <= 1:
        return path[:limit] + "…" if len(path) > limit else path

    # Build the tail: keep at least the last two components (parent dir + name)
    # unless that's already too long
    tail_parts = parts[-2:] if len(parts) >= 2 else parts
    tail = separator.join(tail_parts)

    if len(tail) >= limit:
        # Tail alone is too long; show just the filename elided
        return "…" + separator + parts[-1][:limit - 3]

    # Try to fit more parent directories from right to left
    budget = limit - len(tail) - 1  # -1 for the "…"
    extra_parts = []
    for part in reversed(parts[:-2]):
        needed = len(part) + 1  # +1 for separator
        if needed > budget:
            break
        extra_parts.insert(0, part)
        budget -= needed

    if extra_parts:
        return "…" + separator + separator.join(extra_parts + tail_parts)
    return "…" + separator + tail


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
    #:
    #: **And `to_row` did not actually copy them, for the life of the
    #: feature.** The comment above said "straight through" and the
    #: constructor omitted both, so `ResultGroup.when` - built from
    #: `rows[0].mtime_ns` - was `format_when(0)`, which is the empty string.
    #: **Every document result showed no date at all.** Mail was unaffected
    #: and hid it: a message takes its date from `sent_at` in the details map,
    #: so the Mail tab looked right while the other three quietly did not.
    ext: str = ""
    mtime_ns: int = 0
    #: Which retrievers found this, straight from `SearchResult.sources`.
    #:
    #: **Carried as data, not parsed back out of `explain`.** The rule this
    #: codebase set for notices - the UI never reads a message string to
    #: decide anything - applies just as well to a row deciding whether to
    #: show a "meaning match" marker.
    sources: tuple = ()
    #: The raw locator - `Q3!A14` - kept beside the sentence in `location`.
    #: Adoptions §6a. The grid preview will need the machine-readable form to
    #: scroll to the region, and re-parsing the sentence to get it back would
    #: be the mistake `cell_location` exists to prevent.
    label: str = ""
    #: Offline Media §3a/3b. `None` for an ordinary file. Straight through
    #: from `SearchResult` - see that field's own comment for why `path` on
    #: such a row is never a real filesystem path and must be resolved
    #: before it is opened.
    volume_id: Optional[int] = None
    relative_path: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank, "chunk_id": self.chunk_id, "path": self.path,
            "display_path": self.display_path, "snippet": self.snippet.text,
            "highlights": [list(h) for h in self.snippet.highlights],
            "explain": self.explain, "location": self.location,
        }


def cell_location(locator: Any) -> str:
    r"""`Q3!D14` as a sentence: *Sheet 'Q3' · near D14*. Adoptions §6a.

    **The store keeps the code and the words are written here**, the same rule
    the notices follow - nothing anywhere has to parse a sentence back apart,
    and the wording can be changed without a migration.

    *near*, not *at*. The locator is the row the chunk **starts** on, and a
    passage is several rows long; saying "at" would be a precision the value
    does not have, on the one screen where a person is deciding whether to
    open a forty-thousand-row workbook.

    `""` for anything that is not a locator, which is every document that is
    not a spreadsheet.
    """
    from app.extract.cells import parse

    found = parse(locator)
    if found is None:
        return ""
    sheet, column, row = found
    return f"Sheet '{sheet}' · near {column}{row}"


def to_row(result: Any, terms: Sequence[str], *, path_limit: int = 70) -> ResultRow:
    """Turn a `SearchResult` into something a list widget can draw."""
    page = getattr(result, "page", None)
    # **The cell wins over the sheet number.** For a spreadsheet `page` is the
    # sheet *index*, so "page 3" is both true and useless - it names a thing
    # nobody's spreadsheet calls a page and gives no way to find the row.
    location = cell_location(getattr(result, "label", ""))
    if not location:
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
        ext=str(getattr(result, "ext", "") or ""),
        # **The shot date before the copy date - work order 0f §3a's third
        # clause.** `ResultRow.mtime_ns` is what every date this row shows
        # (`_build_group`'s `when`/`when_exact`, the tooltip in
        # `result_tooltip`) is formatted from, so it has to already be the
        # right date rather than something a later step corrects - the field
        # keeps its name because renaming it would ripple through every one
        # of those, and it already means "the date to show" nowhere else.
        mtime_ns=int(getattr(result, "taken_at_ns", 0) or 0)
                 or int(getattr(result, "mtime_ns", 0) or 0),
        sources=tuple(getattr(result, "sources", ()) or ()),
        label=str(getattr(result, "label", "") or ""),
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
    #: The precise date `when` may have blurred into "yesterday" or "3 weeks
    #: ago" - item 4b. Always computed, whatever register `when` itself is
    #: reading in, so the tooltip can show it regardless.
    when_exact: str = ""
    #: `(start, end)` into `folder` - item 4c. `(0, 0)` means nothing to
    #: emphasise: the common case, where no other result in the set shares
    #: this group's name. Set only by `group_results`, over the whole result
    #: set already in hand - never a second query.
    folder_emphasis: tuple[int, int] = (0, 0)
    #: §5b: this row's file is an email attachment, not the message itself.
    #: **The attachment is the object, its message is the context** - `name`
    #: stays the attachment's own filename (unlike a message, whose path is a
    #: synthetic key nobody would recognise) and `folder` carries who sent it
    #: and what it was about instead of a breadcrumb through a `pst://` key
    #: nobody typed. `kind` is untouched too, deliberately: it is the
    #: attachment's own real extension, so the icon painted is the document's
    #: actual type rather than a generic "attachment" glyph.
    is_attachment: bool = False

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
    register: str = "plain",
) -> list[ResultGroup]:
    """Chunk rows to document groups, ordered by each group's best chunk.

    `rows` must already be in rank order - the engine's order is preserved
    rather than recomputed, which is what keeps this display-only.

    `details` optionally maps `file_id` to mail metadata (see
    `store.messages_for`), so a message can use its subject rather than a
    synthetic path nobody would recognise. Absent, or missing an entry, falls
    back to the filename without raising.

    `register` gates `when` between "yesterday"/"3 weeks ago" (`"plain"`, the
    default) and an exact date (`"technical"`) - item 4b. Whichever it picks,
    `ResultGroup.when_exact` always carries the exact date, for the tooltip.
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
        _build_group(file_id, collected[file_id], (details or {}).get(file_id),
                    now=now, register=register)
        for file_id in order
    ]
    groups = _distinguish_twins(groups)
    return groups[:limit] if limit else groups


def _path_pieces(path: str) -> list[str]:
    """A path's folder segments, root to leaf, drive letter dropped."""
    cleaned = (path or "").replace("\\", "/").strip("/")
    pieces = [piece for piece in cleaned.split("/") if piece]
    if pieces and pieces[0].endswith(":"):
        pieces = pieces[1:]
    return pieces[:-1] if len(pieces) > 1 else []


def _distinguish_twins(groups: list[ResultGroup]) -> list[ResultGroup]:
    """Item 4c: when two groups in one set share a display name, extend the
    shorter of their breadcrumbs until they read differently, and mark where
    the newly-added, distinguishing segment starts.

    Computed once over the whole set already in hand - zero extra queries,
    the same discipline `group_results` already keeps. A name that appears
    once is untouched: this is the rare case, not the common one.
    """
    by_name: dict[str, list[int]] = {}
    for index, group in enumerate(groups):
        by_name.setdefault(group.name.strip().lower(), []).append(index)

    twins = {index: [j for j in indices if j != index]
            for indices in by_name.values() if len(indices) > 1
            for index in indices}
    if not twins:
        return groups

    out = list(groups)
    for index, rivals in twins.items():
        group = out[index]
        pieces = _path_pieces(group.path)
        rival_pieces = [_path_pieces(out[j].path) for j in rivals]
        folder, emphasis = _twin_breadcrumb(pieces, rival_pieces)
        if folder:
            out[index] = replace(group, folder=folder, folder_emphasis=emphasis)
    return out


def _twin_breadcrumb(pieces: list[str],
                     rival_pieces: list[list[str]]) -> tuple[str, tuple[int, int]]:
    """The breadcrumb for one twin, extended only as far as it needs to read
    differently from every rival's own folder segments - item 4c.

    Returns `(text, (start, end))`; `(0, 0)` means the ordinary
    `BREADCRUMB_PARTS`-deep breadcrumb already told the two apart, so there is
    nothing new to draw attention to.
    """
    if not pieces:
        return "", (0, 0)
    for depth in range(min(BREADCRUMB_PARTS, len(pieces)), len(pieces) + 1):
        tail = pieces[-depth:]
        # A rival shallower than this depth cannot have the same tail at this
        # depth at all, so it can never collide - only a rival at least this
        # deep needs comparing.
        if all(rival[-depth:] != tail for rival in rival_pieces if len(rival) >= depth):
            prefix = "… > " if depth < len(pieces) else ""
            text = prefix + " > ".join(tail)
            if depth == min(BREADCRUMB_PARTS, len(pieces)):
                return text, (0, 0)          # already distinct at the usual depth
            return text, (len(prefix), len(prefix) + len(tail[0]))
    # Identical all the way to the root - genuinely the same folder. Show the
    # full path's worth of breadcrumb; there is no single segment to point at.
    text = " > ".join(pieces)
    return text, (0, 0)


def _build_group(
    file_id: int,
    rows: list[ResultRow],
    detail: Optional[Mapping[str, Any]],
    *,
    now: Optional[float] = None,
    register: str = "plain",
) -> ResultGroup:
    path = rows[0].path if rows else ""
    name = path.replace("\\", "/").rstrip("/").rpartition("/")[2] or path
    folder = breadcrumb(path[: len(path) - len(name)])
    kind = (rows[0].ext if rows else "") or _ext_of(name)
    friendly = str(register or "plain").lower() != "technical"
    when_exact = _exact_date(rows[0].mtime_ns) if rows else ""
    when = format_when(rows[0].mtime_ns, now=now) if (rows and friendly) else when_exact

    is_attachment = bool(detail and detail.get("attachment_of"))

    if detail and is_attachment:
        # §5b: **the attachment is the object, its message is the context.**
        # `name` and `kind` are left exactly as computed above - the
        # attachment's own filename and real extension, the same as any
        # other file result - and only `folder` changes, from a breadcrumb
        # through a `pst://…/attachments/…` key nobody typed to who sent it
        # and what it was about. This is the *parent* message's detail
        # (`mail_details` resolved it that way), never this row's own.
        subject = str(detail.get("subject") or "").strip()
        sender = format_address(detail.get("sender"))
        context = f"from {sender}" if sender else ""
        if subject:
            context = f"{context} · {subject}" if context else subject
        folder = context or "from a message"
        sent = detail.get("sent_at")
        if sent:
            when_exact = _exact_date_from_epoch(sent)
            # **The message's own sent date, not the attachment file's own
            # `mtime_ns`.** The latter is when the attachment was written out
            # during indexing - today, for every attachment, on every run -
            # which tells nobody anything; when the message went out is the
            # date that actually distinguishes one attachment from another.
            when = format_when(int(sent) * 1_000_000_000, now=now) if friendly else when_exact
    elif detail:
        # A message: its path is a synthetic key nobody typed and nobody would
        # recognise, so the subject is the only usable name.
        subject = str(detail.get("subject") or "").strip()
        sender = format_address(detail.get("sender"))
        attachments = "1 attachment" if detail.get("has_attach") else ""
        # **Item 3b: sender-first.** "Mum — Re: holiday photos" is how people
        # remember mail, not "Re: holiday photos" with the sender relegated to
        # the grey line underneath. Display order only - `folder` no longer
        # repeats the sender it now leads the name with, but grouping, the
        # payload and every action stay exactly what they were.
        name = f"{sender} — {subject or '(no subject)'}" if sender else (subject or "(no subject)")
        folder = attachments
        kind = "email"
        sent = detail.get("sent_at")
        if sent:
            when_exact = _exact_date_from_epoch(sent)
            # A message's own sent date, in ns, so the same friendly ageing
            # rules apply to mail as to a file - item 4b.
            when = format_when(int(sent) * 1_000_000_000, now=now) if friendly else when_exact

    return ResultGroup(
        file_id=file_id, name=name, folder=folder, kind=kind,
        when=when, path=path, rows=rows, when_exact=when_exact,
        is_attachment=is_attachment,
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
    #: The values those two are *formatted from*, so the columns can be sorted
    #: by what they mean rather than by how they read.
    #:
    #: **The lesson `SortableItem` was written for, applied here.** "10 KB"
    #: sorts before "3 KB" and "3 weeks ago" sorts before "yesterday"; the
    #: numbers were thrown away at formatting time, so the Files list could
    #: not have sorted correctly even if it had been allowed to.
    size_bytes: int = 0
    mtime_ns: int = 0
    #: The full path. `folder` is shortened for the column and cannot be
    #: rejoined to it, and the preview pane needs the real thing.
    path: str = ""
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


def _exact_date(mtime_ns: int) -> str:
    """The precise moment `format_when` blurs into an age - item 4b.

    Always available regardless of register, because the tooltip promises it
    whichever way the visible row is reading.
    """
    if not mtime_ns:
        return ""
    return _time.strftime("%d %b %Y, %H:%M", _time.localtime(mtime_ns / 1_000_000_000))


def _exact_date_from_epoch(epoch_seconds: Any) -> str:
    """The same exact format as `_exact_date`, from a mail `sent_at` (seconds,
    not nanoseconds)."""
    try:
        seconds = int(epoch_seconds)
    except (TypeError, ValueError):
        return ""
    if seconds <= 0:
        return ""
    return _time.strftime("%d %b %Y, %H:%M", _time.localtime(seconds))


def notice_register_for(surface: str, preferences: Any = None) -> str:
    """`"plain"` or `"technical"` for this surface, right now - item 4b.

    The same seam `search_options` resolves a `SearchPolicy` through
    (`notice_register`), so a person who has switched off "Explain in plain
    words" gets exact dates on the Search tab too, not just on the power
    surfaces whose default already is technical.
    """
    from app.search.policy import from_settings

    return from_settings(surface, preferences).notice_register


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
            size_bytes=int(row.get("size_bytes", 0)),
            mtime_ns=int(row.get("mtime_ns", 0)),
            path=path,
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


def file_query(raw: str) -> Any:
    r"""One typed line, parsed - **the whole of it, not the part a tab liked.**

    This used to return `(text, extensions)`, which is a fair summary of what
    the Files tab could do at the time and the reason it could do no more.
    `/path`, `/after`, `/size`, `/repo` and every mail field were parsed
    correctly and then thrown away here, one function above the store call - so
    the dropdown offered filters that did nothing, and the same query typed in
    Files and in Search returned two different sets.

    Now the parse travels intact to `SqliteStore.browse_files`, which applies
    every switch through the one definition in `storage/filters.py`. The tab
    decides which rows it is about; it no longer decides what a switch means.

    Note what is *not* here any more: the old special-casing that folded
    `parsed.names` into the free text. `/name` is an ordinary filter on the
    basename, and leaving it as one is what makes it narrow a content search to
    filenames without a mode flag.
    """
    from app.search.commands import expand_slashes
    from app.search.query import parse_query

    return parse_query(expand_slashes((raw or "").strip()))


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


def row_identity(payload: Any) -> Any:
    """A stable identity for a row or group - item 5d's stable-update rule.

    "The row under the pointer must not visibly jump" needs to find *the same
    payload* again after a rebuild adds rows or re-ranks them, which a screen
    position or a model row index cannot do - both change on every rebuild by
    definition. A `ResultGroup`'s `file_id` survives a re-rank; a chunk row
    (expanded inside a group) is identified by its own `chunk_id`, since two
    chunks of the same document share a `file_id`.
    """
    if isinstance(payload, ResultGroup):
        return ("group", payload.file_id)
    return ("chunk", getattr(payload, "chunk_id", None))


def kind_tag(kind: str) -> str:
    """Four characters at most, so an unknown type still gets a legible tag."""
    return KIND_LABELS.get(kind, (kind or "?").upper()[:4])


#: Extensions painted in monospace - item 3c. Deliberately narrower than
#: `preview_loader`'s text-file list: that one decides what can be *read* at
#: all, this one decides what reads like *code* to the form coders already
#: use everywhere else. `.txt`/`.md`/`.csv` are prose and data, not source,
#: and stay in the ordinary body font.
CODE_EXTENSIONS = frozenset({
    "py", "js", "jsx", "ts", "tsx", "java", "c", "h", "cpp", "hpp", "cc",
    "cs", "go", "rs", "rb", "php", "sql", "ps1", "sh", "bash", "bat", "cmd",
    "kt", "swift", "scala", "lua", "pl", "r", "css", "scss", "html", "htm",
})


def is_code_kind(kind: str) -> bool:
    """Whether `kind` (a `ResultGroup.kind` or `ResultRow.ext`) is source code.

    Item 3c: **only the font changes here.** A line number was asked for
    alongside the monospace font and is deliberately not shown - see the
    dated note on item 3c in the work order. Nothing from extraction through
    to a `SearchResult` carries a line number today, chunk-relative or
    absolute, and painting a guessed one would cost this list more trust than
    it earns, on the one order whose acceptance sentence is about trust.
    """
    return (kind or "").lower() in CODE_EXTENSIONS


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
                   expanded: bool = False, policy: Any = None) -> str:
    """The grey line under the name: where it is, and how many matches.

    `expanded` is passed in rather than read off the group, because a
    `ResultGroup` is frozen and describes the *data*. Whether its chunks are
    currently on screen is a fact about one view at one moment, and putting it
    on the dataclass would make two views of the same results fight over it.
    """
    bits = [getattr(group, "folder", "")]
    # §2 of the adoptions order. **On the group's best row, and only there.**
    # A badge on every row would make the one that matters invisible; this
    # appears exactly where a person would otherwise think the search had
    # made a mistake - a result with none of their words in it.
    marker = match_marker(getattr(group, "best", None), policy)
    if marker:
        bits.append(marker)
    label = getattr(group, "match_label", "")
    if label:
        bits.append(f"{label} {'▾' if expanded else '▸'}")
    best = getattr(group, "best", None)
    if show_scores and best is not None:
        bits.append(f"{best.explain}  ·  score {group.score:.2f}")
    return "  ·  ".join(bit for bit in bits if bit)


def result_tooltip(payload: Any, *, missing: bool = False, volume_note: str = "") -> str:
    """The full path and the explanation - both of which came off the row.

    The breadcrumb is a display choice; the tooltip is where the truth stays.
    So is the date - item 4b promises the exact one here regardless of
    whatever register the visible row is reading in. Item 3a/7a: the kind
    word (`kind_tag`) is here too, now that the painted row shows an icon
    instead of the `[PDF]` text it used to carry the word in directly.

    `volume_note` is Offline Media §3a's own sentence - "on **<name>**
    (offline, scanned <date>) - plug it in to open" - from `app.ui.
    presenter.offline_volume_note`, and it replaces the generic `missing`
    line rather than joining it: a person does not need to be told "this
    file is missing" and then, separately, exactly where it actually is.
    """
    best = getattr(payload, "best", payload)
    relative_path = str(getattr(best, "relative_path", "") or "")
    # **A catalogued-volume row's `path` is never a real filesystem path** -
    # it is the letter-free key `volume_synthetic_path` builds - so showing
    # it verbatim here would read as a broken tooltip rather than a helpful
    # one. `relative_path` is where on the drive the file actually is.
    path = relative_path if relative_path else getattr(payload, "path", "")
    lines = [path]
    kind = str(getattr(payload, "kind", "") or "")
    if kind:
        lines.append(kind_tag(kind))
    if best is not None:
        lines.append(why(best))
    exact = getattr(payload, "when_exact", "") or _exact_date(getattr(best, "mtime_ns", 0))
    if exact:
        lines.append(f"Date: {exact}")
    if volume_note:
        lines.append(volume_note)
    elif missing:
        lines.append("This file is missing - the index is stale for it.")
    return "\n\n".join(line for line in lines if line)


def settings_labels(store: Any) -> tuple:
    r"""`(searches, direct_pst)` for the two slow Settings labels. **Worker.**

    Both used to be computed inside `SettingsView.__init__`, which is inside
    `MainWindow.__init__`: a `COUNT(*)` against the store and an import probe
    for `pst_libpff`, on the UI thread, before the first frame was drawn. The
    count is cheap on an idle database and not on one an index run is writing
    to, and an import is never free the first time.

    `-1` means the count could not be read, which `history_label_text` renders
    as a sentence rather than as a number nobody should trust.
    """
    from app.extract import pst_libpff

    searches = -1
    if store is not None:
        try:
            searches = int(store.count_searches())
        except Exception:                        # noqa: BLE001 - a label, not a search
            searches = -1
    try:
        direct = bool(pst_libpff.available())
    except Exception:                            # noqa: BLE001
        direct = False
    return searches, direct


def history_label_text(searches: int) -> str:
    """How many searches are recorded, or that the number is unavailable."""
    if searches < 0:
        return "The search history could not be read."
    return f"{searches:,} searches recorded."


def pst_status_text(available: bool) -> str:
    """Which route Outlook archives take, and what the other one costs.

    A greyed-out option with no explanation is a dead end, so the unavailable
    case names the command that changes it.
    """
    if available:
        return "Direct reading is available - archives can be indexed without Outlook."
    return (
        "Direct reading is not installed, so archives go through Outlook. "
        "To read them without it: pip install libpff-python "
        "(needs Build Tools for Visual Studio on Windows)."
    )


def accessible_text(payload: Any, *, expanded: bool = False) -> str:
    r"""One line naming a result, for anything that cannot see it drawn.

    **The delegate migration left the model empty.** Painting moved into
    `result_delegate`, which reads `ROLE_PAYLOAD` and draws from it - so the
    item itself no longer carried any text at all. Everything sighted still
    worked, and a screen reader was handed fifty items that each said nothing.
    `QAccessible` reads `AccessibleTextRole`, falling back to `DisplayRole`;
    with neither set there is nothing to fall back to.

    Deliberately not the tooltip: that is two or three lines with the full path
    and an explanation, which is right to *hover* and wrong to have read aloud
    for every row while somebody arrows down a list. This is the name, the
    folder and the date - what a sighted reader takes from the row at a glance.

    Item 7a's three additions, each because a sighted reader gets the
    equivalent for free and a screen reader user must not be the one person
    who does not: **the kind word** (`kind_tag`) now that a picture has
    replaced the `[PDF]` text that used to carry it; **whether a multi-match
    group is expanded**, since the chevron is otherwise a purely visual cue;
    and **the exact date**, never the register's friendly "yesterday" -
    sighted or not, this is the one line meant to be trusted outright.
    """
    name = str(getattr(payload, "name", "") or getattr(payload, "title", "") or "")
    if not name:
        path = str(getattr(payload, "path", "") or "")
        name = path.replace("\\", "/").rsplit("/", 1)[-1] or path
    parts = [name]
    kind = str(getattr(payload, "kind", "") or "")
    if kind:
        parts.append(kind_tag(kind))
    folder = str(getattr(payload, "folder", "") or "")
    if folder:
        parts.append(f"in {folder}")
    label = str(getattr(payload, "match_label", "") or "")
    if label:
        parts.append(f"{label}, {'expanded' if expanded else 'collapsed'}")
    when = str(getattr(payload, "when_exact", "") or getattr(payload, "when", "")
              or getattr(payload, "modified", "") or "")
    if when:
        parts.append(when)
    return ", ".join(part for part in parts if part)


def search_options(tier: str, *, scope: str, rerank: bool,
                   surface: str = "search", preferences: Any = None) -> dict:
    r"""What to pass the engine for one tier.

    `rerank` is only meaningful on the full tier - the interim one is BM25 with
    no model at all, and passing it there would look like a setting that does
    nothing. Here rather than in the view because "which options apply to which
    tier" is a rule, and a rule inside a widget is a rule nobody can test.

    `surface` chooses the **policy** - what this tab may do on the person's
    behalf. It is resolved here, in the one function every search already goes
    through, precisely so that no view ever grows `if self.is_search_tab:`.
    That was the alternative, and it would have written each rule twice: once
    where it was decided and once where it was almost decided.
    """
    from app.search.policy import from_settings

    options: dict[str, Any] = {
        "scope": scope,
        "policy": from_settings(surface, preferences),
    }
    if tier == Tier.FULL:
        options["rerank"] = bool(rerank)
    return options


def decorate_results(store: Any, results: Any) -> dict:
    """Mail subtitles, missing-file marks and Offline Media status for one
    page. **Worker only.**

    Three halves were running, or would have run, on the UI thread - a
    SQLite query, one filesystem stat per row, and (§3a) a live Windows
    volume check. Together here so there is one worker rather than three,
    and one place that says which of this work is off-thread.
    """
    return {
        "details": mail_details(store, results),
        "missing": missing_paths(
            getattr(row, "path", "") for row in results or ()
            if getattr(row, "volume_id", None) is None
        ),
        "volumes": offline_volume_marks(store, results),
    }


def offline_volume_marks(store: Any, results: Any) -> dict[int, dict]:
    r"""Which of this page's rows are on a catalogued Offline Media volume
    that is not connected right now, and what to say about it. §3a: "on
    **<name>** (offline, scanned <date>) - plug it in to open".

    **Online rows are absent from the returned dict entirely.** They open
    normally through 1b's ordinary resolution and 3a asks for nothing to
    be said about them - a decoration on every row of a drive that is
    plugged in right now would be noise, not information.

    **Worker only** - `connected_volumes` is a live Windows volume check,
    the same reason `missing_paths` is worker-only. Never raises: a
    decoration that fails to compute costs a missing sentence, not the
    search that found the row.
    """
    rows = [row for row in (results or ()) if getattr(row, "volume_id", None) is not None]
    if not rows:
        return {}
    from app.index.offline_media import connected_volumes

    try:
        online = connected_volumes(store)
    except Exception:                            # noqa: BLE001 - a decoration, not the search
        online = {}

    marks: dict[int, dict] = {}
    volumes_seen: dict[int, Any] = {}
    for row in rows:
        volume_id = int(row.volume_id)
        if volume_id in online:
            continue
        if volume_id not in volumes_seen:
            try:
                volumes_seen[volume_id] = store.get_volume(volume_id)
            except Exception:                     # noqa: BLE001
                volumes_seen[volume_id] = None
        record = volumes_seen[volume_id]
        if record is None:
            continue
        scanned_at = int(getattr(record, "last_scanned_at", 0) or 0)
        marks[int(getattr(row, "file_id", 0))] = {
            "name": record.name,
            "scanned": format_when(scanned_at * 1_000_000_000) if scanned_at else "",
        }
    return marks


def offline_volume_note(mark: Optional[dict]) -> str:
    r"""§3a's exact sentence for a row `offline_volume_marks` found
    offline. `""` for everything else - an online row, or an ordinary
    file not on a catalogued volume at all.
    """
    if not mark:
        return ""
    name = mark.get("name") or "that drive"
    scanned = mark.get("scanned")
    if scanned:
        return f"on {name} (offline, scanned {scanned}) - plug it in to open"
    return f"on {name} (offline) - plug it in to open"


def record_open(engine: Any, search_id: Any, chunk_id: Any) -> None:
    """A click is worth recording and never worth blocking on. **Worker only.**

    Everything in Layer 10 is derived from these, but this is a database
    *write*, and it was running between the double-click and the file opening -
    so a busy index made opening a result feel slow for a reason that has
    nothing to do with opening it.
    """
    try:
        engine.record_open(search_id, chunk_id)
    except Exception:                            # noqa: BLE001 - never block an open
        pass


def file_summary(total: int, shown: int = -1, text: str = "") -> str:
    """The line under the Files table.

    Here rather than in the view because it is three branches choosing a
    sentence, and a branch inside a Qt widget can only be checked by somebody
    typing the right thing at the right moment.
    """
    if shown >= 0:
        # **An empty box is browsing, not a search that found nothing.**
        # It used to read "7 file names contain ''", which is both wrong and
        # faintly alarming - the list is now filled on open, so this is the
        # sentence somebody sees first.
        if not str(text or "").strip():
            if not shown:
                return "No files indexed yet — run an index first."
            return (f"{shown:,} file{'s' if shown != 1 else ''}, most recently "
                    f"changed first — type to filter")
        if not shown:
            return f"No file name contains '{text}'."
        return f"{shown:,} file name{'s' if shown != 1 else ''} contain '{text}'"
    if not total:
        return "No file names indexed yet — run an index first."
    return f"{total:,} file names indexed."


def missing_paths(paths: Any) -> set[str]:
    """Which of these no longer exist on disk. **Worker thread only.**

    `Path.exists()` is a filesystem stat: microseconds on a warm local disk,
    *seconds* on a network share or a drive that has spun down. It was being
    called once per row while filling the results model - twenty stats for a
    normal page, five hundred for a full one - on the UI thread, inside the
    virtualisation work whose whole purpose was to make that list cheap.

    Done once per result set, off-thread, and passed in. Never raises: a
    disconnected drive means "cannot open it", not a crash, and a result whose
    file has vanished is a real finding that must still be shown.
    """
    from pathlib import Path as _Path

    missing = set()
    for path in paths or ():
        text = str(path or "")
        # A message lives inside a .pst and has no file of its own; statting a
        # synthetic key would report every message as missing.
        if not text or text.startswith("pst://"):
            continue
        try:
            if not _Path(text).exists():
                missing.add(text)
        except OSError:
            continue
    return missing


#: §5b. The path convention `email_pst.py`'s `_attachment_documents` writes:
#: `f"{message_key}/attachments/{name}"`. Read back here rather than carried
#: as a column, because no schema holds the link - the file's own `path`
#: already says everything needed, and reading it beats a migration nobody
#: asked this order to make.
_ATTACHMENT_MARKER = "/attachments/"


def _attachment_parent_path(path: str) -> str:
    """The message this attachment belongs to, or `""` if `path` is not one.

    **Only the PST-via-Outlook attachment convention produces this shape.**
    A standalone `.eml`/`.msg` or an mbox message never separately indexes
    its attachments - only their *names*, inside the message's own text and
    `has_attach` - so those never reach here at all; `/has attachment` still
    finds the message, just never gets a row of its own for what was
    attached to it.
    """
    text = str(path or "")
    index = text.find(_ATTACHMENT_MARKER)
    return text[:index] if index > 0 else ""


def mail_details(store: Any, results: Any) -> dict:
    """Subjects and senders for the messages on one page of results.

    **One query for the page, never one per row.** At the fetch depth grouping
    needs, a per-row lookup is fifty queries per keystroke - the shape of
    slowness that gets blamed on the search itself.

    **§5b's exception, and it is a real one.** An attachment's own file_id
    has no row in `messages` - it is not itself a message - so its *parent's*
    row is what supplies "its message is the context" (§5b). The parent is
    found by path (`_attachment_parent_path`), which costs one indexed
    `get_file` lookup per *distinct attachment* on the page - never per row,
    and zero when a page holds no attachments at all, which is nearly every
    page. A bulk by-path lookup in `sqlite_store.py` would remove even that,
    and is the natural next step for whoever next has that file open; it is
    outside this order's file scope today.

    Never raises. A missing subtitle is a cosmetic loss; failing the search that
    produced it is not, and a store that has been closed underneath a worker is
    a normal condition during shutdown rather than an error.
    """
    if store is None or not hasattr(store, "messages_for"):
        return {}
    try:
        results = list(results or ())
        file_ids = [getattr(r, "file_id", 0) for r in results]

        parent_id_of: dict[int, int] = {}
        if hasattr(store, "get_file"):
            # **Keyed by path, not by row.** Several attachments can share one
            # parent message - a reply with the same two files re-attached is
            # the ordinary case - and resolving each would be exactly the
            # per-row lookup this function's own docstring exists to avoid.
            resolved: dict[str, Optional[int]] = {}
            for result in results:
                file_id = getattr(result, "file_id", 0)
                parent_path = _attachment_parent_path(getattr(result, "path", ""))
                if not parent_path:
                    continue
                if parent_path not in resolved:
                    try:
                        record = store.get_file(parent_path)
                    except Exception:              # noqa: BLE001 - a subtitle, not the search
                        record = None
                    resolved[parent_path] = record.id if record is not None else None
                parent_id = resolved[parent_path]
                if parent_id is not None:
                    parent_id_of[file_id] = parent_id

        wanted = list(dict.fromkeys([*file_ids, *parent_id_of.values()]))
        details = dict(store.messages_for(wanted))
        for file_id, parent_id in parent_id_of.items():
            parent_detail = details.get(parent_id)
            if parent_detail:
                # Marked so `_build_group` draws this as an attachment whose
                # *parent's* detail this is, never as a message in its own
                # right - the two share every other key.
                details[file_id] = {**parent_detail, "attachment_of": parent_id}
        return details
    except Exception:                            # noqa: BLE001 - see docstring
        return {}


def results_terminator(count: int) -> str:
    """The quiet end-of-list message.

    Item 5c: shows that the list has ended and the count, so scrolling to the
    bottom reads as an answer rather than a stall. Faint, because it is
    context that *nobody needs while reading*, but answers the implicit
    question "are there more if I scroll?" when there aren't.
    """
    if count == 0:
        return ""
    if count == 1:
        return "That's all — 1 result."
    return f"That's all — {count:,} results."


@dataclass(frozen=True, slots=True)
class Terminator:
    """The end-of-list row itself - item 5c.

    A model row of its own, painted by `result_delegate.ResultDelegate`,
    rather than text bolted onto the summary label above the list: the point
    is that scrolling *to the bottom* is what answers "are there more", and
    that only reads as an answer from inside the list somebody is already
    scrolling.
    """
    text: str


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

    # **The most actionable thing that can be said, so it outranks the rest.**
    # A word matching nothing is usually the entire explanation for a baffling
    # list: a typo, a name spelled differently in the documents, or something
    # not indexed yet. Twenty results with no clue why is what this replaces.
    unmatched = list(getattr(response, "unmatched", ()) or ())
    if unmatched:
        words = ", ".join(f"'{word}'" for word in unmatched)
        found = "no document contains" if len(unmatched) == 1 else "no document contains any of"
        return summary, (
            f"{found} {words} — the rest of your words were searched for, "
            f"which is why these results may look unrelated."
        )

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

    # **`seen` is not a total until the walk ends, and using it as one put the
    # bar at 100% within seconds of starting.**
    #
    # The work queue is bounded - that is what stops a fast walker building a
    # million-entry list in memory - so the walker can never get more than a
    # queue-length ahead of the workers. `seen` is therefore always roughly
    # `done` plus a queue, and `done / seen` climbs to near 1 almost immediately
    # and stays there: at 10,000 files done and 10,256 seen it reads 97%, with
    # the entire rest of the corpus still to come. The bar was measuring how
    # full the queue was, not how far through the run it was.
    #
    # `(0, 0)` is Qt's indeterminate range: a moving barber pole, which is the
    # honest answer to "how far through are we" while the size of the job is
    # still unknown. It is only tolerable because the text beside it carries
    # live counts - an indeterminate bar on its own does read as stuck.
    #
    # A caller that genuinely knows the total - from a counting pre-pass - can
    # still pass `total_estimate` and get a real percentage from the first tick.
    if not total_estimate and not getattr(stats, "walk_complete", False):
        return 0, 0

    total = max(int(total_estimate or 0), seen, done, 1)
    # Clamped: `seen` can lag `done` by a tick, and a bar drawn past its own
    # maximum is a Qt warning on the console and a full bar on screen while the
    # run is plainly still going.
    return min(done, total), total


# ---------------------------------------------------------------------------
# A run this window did not start
#
# `app.cli index` and the window are two processes now that the run lock is
# separate from the window lock, so an index may be under way with nothing in
# this process knowing about it. `is_running()` was `self._worker is not None`,
# which meant the bar sat at zero and Start stayed enabled while a run was
# plainly in progress - and pressing Start then hit the lock and produced an
# error for something the window should simply have been showing.
#
# The pipeline publishes a snapshot on every checkpoint. These turn that record
# into what goes on screen, and they are here rather than in the view because
# every one of them is a decision with an edge case worth a test.
# ---------------------------------------------------------------------------

#: A published run older than this is treated as finished, whatever it says.
#:
#: The lock is the authority and this is only a safety net for the gap between
#: a process dying and anything noticing: the mutex is released immediately, but
#: a window polling on a timer can still hold the last record it read. Generous,
#: because a checkpoint is every 50 files or two seconds and a single huge file
#: - a 30GB archive - can legitimately sit between two of them for minutes.
STALE_RUN_S = 900.0


def external_snapshot(record: Any) -> Any:
    """The published `stats` dict as something `progress_for` can read.

    `progress_for` and `progress_text` take an object and use `getattr`, which
    is right for the in-process tick. Converting here rather than teaching them
    about dictionaries keeps one set of progress rules for both paths - the
    alternative is two, which drift, and the bar is the thing that has already
    been wrong three times.
    """
    from types import SimpleNamespace

    found = (record or {}).get("stats") if isinstance(record, dict) else None
    values = dict(found or {})
    # The two the progress rules need and a published record may predate.
    values.setdefault("walk_complete", False)
    values.setdefault("skipped_by_code", {})
    values.setdefault("skipped_roots", ())
    values.setdefault("notices", ())
    return SimpleNamespace(**values)


def external_is_live(record: Any, *, now: Optional[float] = None) -> bool:
    """Is this published record describing a run that is still going?

    Answers on the record alone. The caller pairs it with the lock, which is the
    part an operating system maintains and therefore the part that is true.
    """
    import time as _t

    if not isinstance(record, dict):
        return False
    updated = record.get("updated_at") or record.get("started_at")
    try:
        age = (now if now is not None else _t.time()) - float(updated)
    except (TypeError, ValueError):
        return False
    return age <= STALE_RUN_S


def external_run_text(record: Any) -> tuple[str, str]:
    """Headline and detail for a run belonging to another process.

    Deliberately says **whose** run it is. "Indexing…" while the person is
    looking at a window they did not start it from invites them to press Stop
    expecting it to be theirs - and Stop does now reach it, so the sentence has
    to make clear what will be stopped.
    """
    owner = str((record or {}).get("owner") or "another process")
    headline, detail = progress_text(external_snapshot(record))
    return f"{headline} — started by {owner}", detail


def start_blocked_reason(record: Any, *, locked: bool) -> str:
    """Why Start is unavailable, or `""` when it is available.

    **`locked` comes from the mutex and settles it**; the record only supplies
    the words. A record with the lock free is a process that died, which must
    never be a reason to refuse - that is a paper lock, and it is broken by hand
    at the worst possible moment.
    """
    if not locked:
        return ""
    owner = str((record or {}).get("owner") or "another process")
    return (f"An index run started by {owner} is already in progress. "
            f"Stop will end it; searching is unaffected.")


# ---------------------------------------------------------------------------
# Worker bodies
#
# Here rather than in `shell.py` for the reason `read_index_summary` is here:
# `test_ui_never_blocks` reads the window's source and refuses any store call it
# cannot prove is inside a worker, and it cannot prove that about a module-level
# function - correctly, since nothing in the file says so. Keeping the bodies in
# this module makes the rule mechanical rather than a matter of trust.
# ---------------------------------------------------------------------------

def _read_external_run(store: Any) -> dict:
    r"""Is another process indexing, and what does it say about itself?

    **Two questions, and only one of them is authoritative.** `is_indexing`
    asks the mutex, which an operating system releases when a process dies.
    `active_run` reads the row that process last wrote, which survives a crash.
    A record without a lock is a crash rather than a run, and treating it as a
    run would refuse Start until somebody edited a database by hand.

    On a worker, because it runs on a timer for as long as the window is open
    and both halves touch something outside this process.
    """
    from app.core.deeplink import take_pending
    from app.core.run_lock import active_run, is_indexing, take_front_request

    found: dict = {"locked": False, "record": None, "link": None, "front_requested": False}
    try:
        found["locked"] = is_indexing(store)
        found["record"] = active_run(store)
    except Exception as exc:                     # noqa: BLE001 - a watcher, not a run
        _log.debug("could not read the external run state: {}", exc)

    # **Adoptions §7a rides this watcher rather than bringing a timer.** A
    # `leasha://` link is delivered by a second process writing one
    # `index_state` row and exiting - the channel `run_lock` already uses to
    # say "please stop" - and something has to notice. Polling the database
    # every second for the life of every session, so that a link somebody
    # clicks once a week arrives instantly, is not a trade this codebase
    # would make anywhere else. The cost is that a link takes up to the
    # watcher's interval to land, which is said in the order's note.
    try:
        found["link"] = take_pending(store)
    except Exception as exc:                     # noqa: BLE001 - a link, not a run
        _log.debug("could not read the pending link: {}", exc)

    # A second launch that found the window already open writes the same
    # flag `request_stop` uses, then exits. This watcher is the only thing
    # polling `index_state` for as long as the window is open, so fronting
    # rides it rather than adding a timer of its own - see the note above.
    try:
        found["front_requested"] = take_front_request(store)
    except Exception as exc:                     # noqa: BLE001 - a front request, not a run
        _log.debug("could not read the front request: {}", exc)
    return found


def _scan_and_save(store: Any, roots: list[str]) -> dict:
    """Count the corpus and save the total, so the bar has a denominator.

    The same work `app.cli scan` does, which until now was the only way to get
    one - and the reason a GUI-started index never had a percentage.
    """
    import json

    from app.index.scan import SCAN_STATE_KEY, ScanConfig, scan

    result = scan(ScanConfig(roots=[Path(root) for root in roots]))
    store.set_states({SCAN_STATE_KEY: json.dumps({
        "at": int(_time.time()),
        "roots": list(result.roots),
        "files": result.indexable.files,
        "bytes": result.indexable.bytes,
    })})
    return {"files": result.indexable.files, "bytes": result.indexable.bytes}


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

    if getattr(stats, "paused", False):
        # **A pause froze the bar and said nothing.** The resource governor
        # waits for memory to settle or for the machine to be idle, and one
        # observed pause ran for six minutes. Nothing moved and nothing
        # explained why, which is indistinguishable from a hang - and the
        # correct response to a hang is to kill the run.
        #
        # The reason comes from the governor itself rather than being invented
        # here, so it names the real ceiling and the real number.
        return (
            "Paused - waiting for the machine",
            f"{getattr(stats, 'pause_reason', '') or 'Waiting for resources.'} "
            f"{format_count(getattr(stats, 'indexed', 0))} indexed so far; "
            "it will continue on its own.",
        )

    done, _total = progress_for(stats, total_estimate=total_estimate)

    # **The rate over the last quarter of an hour, not since the start.**
    #
    # An average over four days barely moves, so a run that has slowed to a
    # crawl still reports the number it managed on day one - and that is
    # exactly the moment somebody needs to know. `None` while there is no
    # measurement yet, which `format_eta` turns into "estimating…" rather than
    # into a confident zero.
    recent = getattr(stats, "recent_files_per_minute", None)
    rate = recent if recent is not None else (
        getattr(stats, "files_per_minute", 0) or 0)

    # **An ETA that is allowed to say it does not know.** Without a `scan` there
    # is no total, so the only honest answer to "how much longer" is that
    # nothing here can tell - and no number is better than a wrong one on a job
    # measured in days.
    if total_estimate:
        eta = format_eta(max(0, total_estimate - done), files_per_minute=rate)
    else:
        eta = "time remaining unknown - run `app.cli scan` for a real estimate"

    headline = (
        f"{format_count(getattr(stats, 'indexed', 0))} documents  ·  "
        f"{format_count(getattr(stats, 'seen', 0))} files seen  ·  "
        f"{format_count(getattr(stats, 'skipped', 0))} skipped"
    )

    current = getattr(stats, "current", "") or ""
    # **Say when it is OCR.** A run reading text moves at hundreds of files a
    # minute; OCR moves at seconds *per page*, so the same progress line reads
    # as a stall - and a run that looks stalled gets killed, which is how a
    # four-hour job becomes four hours wasted.
    verb = "reading" if getattr(stats, "ocr_mode", "") != "images" else "reading with OCR"
    reading = f"  ·  {verb} {current}" if current else ""
    if current and getattr(stats, "current_item", 0):
        reading += f" [{stats.current_item:,}]"

    measured = (
        f"{rate:,.0f} files/min (last 15 min)" if recent is not None
        else f"{rate:,.0f} files/min"
    )
    detail = (
        f"{format_count(getattr(stats, 'chunks', 0))} chunks  ·  "
        f"{measured}  ·  {eta}{reading}"
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
    #: What to call this row. **The field name is `name` because that is what
    #: every consumer of a row already reads** - `FileRow` and `ResultRow` both
    #: have one, and the preview pane's heading is `row.name`. Without it a
    #: message was headed with its synthetic path, which is the one string here
    #: that means nothing to anybody.
    name: str = ""
    #: How much of the body was a quoted reply or signature, or `None` for a
    #: message indexed before schema v12. **`None` is not zero** - the preview
    #: says nothing rather than claiming nothing was removed.
    quoted_removed: Optional[int] = None
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
        # An empty subject is common and meaningful. Blank looks like a
        # rendering fault; saying so does not.
        subject = str(row.get("subject") or "").strip() or "(no subject)"
        out.append(MailRow(
            file_id=int(row.get("file_id") or 0),
            sender=format_address(row.get("sender")),
            recipients=format_recipients(row.get("recipients")),
            sent=format_sent(sent_at, now=now),
            subject=subject,
            # The same string, under the name every row consumer reads.
            name=subject,
            attachment="Yes" if attached else "",
            size=format_size(size_bytes),
            path=str(row.get("path") or ""),
            sent_at=sent_at,
            size_bytes=size_bytes,
            has_attachment=attached,
            quoted_removed=row.get("quoted_removed"),
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
        # `/newest` is what this tab already did, so only `/oldest` changes the
        # order - but it is passed either way, because the catalogue offers the
        # switch here and a switch that is offered has to be honoured.
        "sort": str(getattr(parsed, "sort", "") or "") or None,
    }

    # **The file-level switches, which this tab never had.** A message is a row
    # in `messages` joined to the file it came from, so `/type msg`, `/path`,
    # `/name` and `/size` are all answerable here and were simply never asked.
    # Built by the same `file_filter_sql` every other surface uses, so they mean
    # the same thing on this tab as on the others.
    #
    # The mail columns are cleared first, and both reasons matter: `sender` and
    # friends are already handled above through the trigram header index, which
    # is faster than the subquery this would emit; and `after`/`before` belong
    # on `m.sent_at` here - the date a message was *sent* - not on the file's
    # modification time, which for a PST is the date the whole archive last
    # changed and would filter every message in it identically.
    file_where, file_params = "", []
    try:
        from dataclasses import replace as _replace

        from app.storage.filters import file_filter_sql

        file_where, file_params = file_filter_sql(_replace(
            parsed, senders=(), recipients=(), subjects=(),
            has_attachment=None, after=None, before=None,
        ))
    except Exception:                    # noqa: BLE001 - a filter, not the tab
        file_where, file_params = "", []
    if file_where:
        filters["file_where"] = file_where
        filters["file_params"] = file_params

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


def install_package(package: str, version: str = "", timeout_s: int = 600) -> dict:
    """pip install into this venv. **Blocking - call it from a worker.**

    Lives here rather than in the wizard that wants it, because this module is
    the one allowed to block: `test_ui_never_blocks` skips it by name and
    enforces the guarantee at the call site instead. A pip install can take a
    minute on a cold cache, which on the UI thread is a white window.

    Never raises. The result says what happened and, on failure, the exact
    command to run by hand - an install that fails quietly leaves a reader that
    can never work and nobody knowing why.
    """
    # **`sys` as well as `subprocess`.** This module imports neither at the
    # top, and the reference to `sys.executable` below was a `NameError`
    # waiting on the one path nobody runs in a test - the button that installs
    # a missing reader. "Never raises" was written above it and was not true.
    import subprocess
    import sys

    target = f"{package}=={version}" if version else package
    fix = f"venv\\Scripts\\pip install {target}"

    try:
        finished = subprocess.run(
            [sys.executable, "-m", "pip", "install", target],
            capture_output=True, text=True, timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "package": target,
                "detail": f"pip did not finish within {timeout_s}s",
                "fix": fix}
    except Exception as exc:                     # noqa: BLE001 - boundary
        return {"ok": False, "package": target,
                "detail": f"{type(exc).__name__}: {exc}", "fix": fix}

    if finished.returncode == 0:
        return {"ok": True, "package": target, "detail": "", "fix": ""}

    output = (finished.stderr or finished.stdout or "").strip().splitlines()
    return {
        "ok": False,
        "package": target,
        # The last few lines carry the reason; the rest is resolver noise.
        "detail": " ".join(output[-3:]) if output else f"pip exited {finished.returncode}",
        "fix": fix,
    }


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


#: Suffixes a log folder may contain. **An allow-list, not a deny-list**, and
#: that direction is the whole safety of `clear_logs`: LOG_PATH defaults to a
#: folder inside the project, an owner may well point it somewhere sharing space
#: with something else, and a routine that deletes "everything here" would one
#: day be pointed at a folder that was not only logs. Anything not named here
#: survives, including `README.txt`, which describes the folder structure and is
#: regenerated rather than discarded.
LOG_SUFFIXES: frozenset[str] = frozenset({".log", ".jsonl", ".zip", ".json"})


def log_files(log_path: Any) -> list:
    """Every log file under `log_path`, at any depth. Never raises.

    Returns paths rather than a count, because both callers - the summary and
    the deletion - must agree exactly about what counts as a log, and the only
    way to guarantee that is for them to ask the same function.
    """
    from pathlib import Path as _Path

    if not log_path:
        return []
    found = []
    try:
        for item in _Path(log_path).rglob("*"):
            try:
                if item.is_file() and item.suffix.lower() in LOG_SUFFIXES:
                    found.append(item)
            except OSError:
                continue
    except OSError:
        return []
    return found


def logs_summary(log_path: Any) -> str:
    """The sentence beside the Clear button: how much is there, and where.

    Named before it is deleted. "Clear logs" with no indication of what that
    means is a button people either never press or press and regret, and the
    number is also the answer to *"is this what is filling my disk"*, which is
    the question that makes somebody look for the button at all.
    """
    files = log_files(log_path)
    if not files:
        return f"No log files in {log_path}."
    total = 0
    for item in files:
        try:
            total += item.stat().st_size
        except OSError:
            continue
    return (f"{len(files):,} log file{'' if len(files) == 1 else 's'}, "
            f"{format_size(total)}, in {log_path}")


def clear_logs(log_path: Any, *, keep: Any = ()) -> dict[str, Any]:
    """Delete the log files under `log_path`. Returns what happened.

    **Today's files are kept, and not out of caution.** The application is
    writing to them at the moment the button is pressed: on Windows an open file
    cannot be unlinked, so deleting the current run log fails, and deleting the
    current *day's* application log would succeed on POSIX and leave loguru
    writing into a file with no directory entry - the session's own logging
    silently going nowhere. `keep` is how the caller names those.

    Empty folders are left in place. `ensure_log_dirs` would recreate them on
    the next start, so removing them buys nothing and risks racing a writer.

    Never raises. A file that will not delete is counted and named; the rest are
    still removed, because "clear the logs" failing wholesale over one locked
    file is the least useful outcome available.
    """
    from pathlib import Path as _Path

    protected = {_Path(one).resolve() for one in (keep or ()) if one}
    removed = 0
    freed = 0
    failed: list[str] = []

    kept = 0
    for item in log_files(log_path):
        try:
            if item.resolve() in protected:
                # Counted where it is skipped, not from `len(keep)`: a caller
                # may name a file that does not exist yet - the errors sink
                # writes nothing until the first warning - and claiming to have
                # kept a file that was never there is a small lie in the one
                # sentence somebody reads to check what happened.
                kept += 1
                continue
            size = item.stat().st_size
            item.unlink()
        except OSError as exc:
            failed.append(f"{item.name} ({exc.strerror or exc})")
            continue
        removed += 1
        freed += size

    return {"removed": removed, "freed": freed, "failed": failed, "kept": kept}


def logs_cleared_message(outcome: Any) -> str:
    """What to say afterwards. **Always names the next thing to do or know.**

    Three outcomes, three sentences - and the one that matters most is the
    middle: files that would not delete are almost always the ones this session
    is writing to, which is not a fault and must not read as one.
    """
    if not isinstance(outcome, dict):
        return "The logs were cleared."
    removed = int(outcome.get("removed") or 0)
    freed = int(outcome.get("freed") or 0)
    failed = list(outcome.get("failed") or ())
    kept = int(outcome.get("kept") or 0)

    if not removed and not failed:
        return "There were no old log files to remove."

    said = f"Removed {removed:,} log file{'' if removed == 1 else 's'}"
    if freed:
        said += f", {format_size(freed)} freed"
    if kept == 1:
        said += ". One file this session is still writing to was kept"
    elif kept:
        said += f". {kept} files this session is still writing to were kept"
    if failed:
        shown = ", ".join(failed[:3])
        said += (f". {len(failed)} could not be removed ({shown}) - they are in "
                 f"use by another program; close it and clear again")
    return said + "."


def index_bytes(store: Any, settings: Any) -> int:
    """What the index itself weighs: the database, its journal, the vectors.

    **Not `folder_size(data_path)`, and that difference is the whole point.**
    The Indexing page shows the size of the entire data folder, which includes
    the ~130MB embedding-model cache - and the model cache correctly survives a
    reset, since throwing it away would turn the next start into a silent
    download. So on a modest index the headline number barely moves after a
    reset, which is exactly what was reported: *"i reset the index the index
    size remained the same"*.

    This weighs only the two things a reset removes, so the difference across
    one is a true statement about what was given back. Never raises: it exists
    to make a sentence, and no sentence is better than a failed reset.
    """
    total = 0
    try:
        total += int(store.file_bytes())
    except Exception:                            # noqa: BLE001 - one number
        pass
    # Uncapped, unlike `folder_size`: a LanceDB table is a handful of large
    # files, not the thousands that cap exists to bound.
    try:
        from pathlib import Path as _Path

        for item in _Path(str(getattr(settings, "vector_path", "") or ".")).rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


#: The profile folders offered on a first run, in the order they are shown.
#:
#: Ordinary places ordinary documents live. Not the whole profile, which drags
#: in AppData and every cache in it, and not a drive.
SUGGESTED_FOLDERS = ("Documents", "Desktop", "Downloads", "Pictures")


def owns_path(candidate: str, home: Optional[str] = None) -> bool:
    r"""Is `candidate` inside *this* account's profile?

    The guard behind "no root that resolves inside another user's profile is
    ever *suggested*". Typing one in stays allowed - the machine belongs to the
    person using it - but a default that reaches into `C:\Users\someone-else`
    is the one thing this order exists to make impossible.

    Compared case-insensitively and separator-insensitively, because
    `C:/Users/jaymin` and `C:\Users\Jaymin` are the same folder and a string
    comparison that says otherwise would wave through exactly the case that
    matters.
    """
    if not candidate:
        return False
    root = (home if home is not None else os.path.expanduser("~"))
    if not root:
        return False

    def flatten(value: str) -> str:
        return value.replace("/", "\\").rstrip("\\").lower()

    flat_root, flat = flatten(root), flatten(candidate)
    return flat == flat_root or flat.startswith(flat_root + "\\")


def suggested_roots(home: Optional[str] = None,
                    exists: Optional[Any] = None) -> list[str]:
    r"""Folders to offer on a first run. **Offered, never added.**

    Returns only those that exist, in `SUGGESTED_FOLDERS` order, and only ones
    inside this account's own profile - so the list is safe to present without
    anybody checking it, which is the point of computing it here rather than in
    a view.

    `exists` is injectable so this is testable without creating directories,
    and `home` so a test can pretend to be somebody else.
    """
    root = home if home is not None else os.path.expanduser("~")
    if not root:
        return []
    here = exists if exists is not None else (lambda p: Path(p).is_dir())

    # Joined with the separator the root already uses rather than with
    # `Path`, which on Linux leaves `C:\Users\jaymin/Documents` - correct
    # enough to open, and not something to show anybody.
    separator = "\\" if "\\" in root else "/"
    out: list[str] = []
    for name in SUGGESTED_FOLDERS:
        candidate = root.rstrip("\\/") + separator + name
        if not owns_path(candidate, home=root):
            continue                     # unreachable today; the guard is the point
        if here(candidate):
            out.append(candidate)
    return out


def nothing_indexed_yet(roots: Sequence[str]) -> str:
    """What the window says when no folder has been chosen.

    **A blank list and a broken app look identical**, which is the failure this
    project keeps finding in other places. One sentence, and it says what to do.
    """
    if roots:
        return ""
    return ("No folders are being indexed yet, so there is nothing to search. "
            "Choose a folder below and Leasha will index it.")


def index_counts(store: Any) -> str:
    """The status bar's sentence. **Runs on a worker; see `_refresh_status`.**

    Here rather than in the window because it is two numbers and a sentence,
    and because a view that is under a 250-line guard should not be the place
    the wording lives - `test_every_qt_view_keeps_its_logic_in_the_presenter`.
    """
    stats = store.stats()
    return (f"{stats['files_total']:,} files  ·  "
            f"{stats['chunks_total']:,} chunks indexed")


def cleared_message(outcome: Any) -> str:
    """What the status bar says after a reset. **Always names an amount.**

    Three different things can have happened and they need three different
    sentences. "Index cleared - 0 documents removed" after resetting an index
    that was already empty reads as a failure; a reset that removed thousands of
    documents and freed nothing is a real fault worth pointing at the log for;
    and the ordinary case should say how much disk came back, because that is
    the question somebody resets an index to answer.
    """
    if not isinstance(outcome, dict):            # an older signal shape
        outcome = {"removed": int(outcome or 0), "freed": 0}
    removed = int(outcome.get("removed") or 0)
    freed = int(outcome.get("freed") or 0)

    if not removed:
        return "The index was already empty - there was nothing to remove."
    if freed <= 0:
        return (
            f"Index cleared - {removed:,} documents removed, but the disk space "
            "has not come back yet. Close any other Leasha window or command "
            "line run and reset again; the log line says which step failed.")
    return (f"Index cleared - {removed:,} documents removed, "
            f"{format_size(freed)} freed. Press Start indexing to rebuild.")


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


def mail_summary(shown: int, leftover: str = "", *, page_size: int = 500) -> str:
    """The line under the Mail table: how many, what was capped, what was dropped.

    Moved out of `mail_view.py`, which had **one line** of headroom under the
    250-line guard - and a rule with one line of headroom is a rule about to be
    broken by the next feature, at the moment when the pressure to raise the
    number is highest and the reasoning worst.

    It belongs here anyway: three branches of string formatting inside a Qt
    widget can only be checked by a person looking at a mail tab at the right
    moment, which is how every UI fault in this project has been found.

    **No count when nothing matched, deliberately.** `COUNT(*)` over `messages`
    is instant on a test corpus and is not on two hundred thousand of them, and
    this runs inside the handler that paints results - so the wording covers
    both "no mail indexed" and "no match" rather than paying a query to tell
    them apart.
    """
    if not shown:
        return ("No message matches those filters. If no mail is indexed "
                "yet, add a .pst in Settings and run an index.")

    parts = [f"{shown:,} message{'s' if shown != 1 else ''}"]
    if shown >= page_size:
        parts.append(
            f"showing the newest {page_size:,} — narrow the filters to see more")
    if leftover:
        parts.append(
            f"'{leftover}' was ignored — this tab filters on the header fields "
            "only. Use Search to look inside messages."
        )
    return "  ·  ".join(parts)


def archive_summary(rows: Any) -> tuple[str, list[str]]:
    r"""`(title, lines)` for the folders a run deliberately did not walk.

    **The counts and the dates are the point.** An archival root is skipped for
    the best of reasons - it turns an incremental pass over a settled 1.5TB
    corpus from hours of fruitless `stat()`ing into seconds - but a folder
    skipped in silence looks exactly like one that was never indexed, and the
    person who reaches that conclusion deletes their index and starts a
    fortnight over.

    So every line says how many files the folder holds and when it was last
    read in full. `("", [])` when nothing was skipped, which the panel reads as
    "hide yourself".
    """
    entries = list(rows or [])
    if not entries:
        return "", []

    lines: list[str] = []
    for row in entries:
        stamp = int(row.get("archived_at", 0) or 0)
        when = format_when(stamp * 1_000_000_000) if stamp else "an unknown date"
        files = int(row.get("files", 0) or 0)
        lines.append(
            f"{row.get('root', '?')} — not walked: {row.get('reason', 'archived')}. "
            f"{format_count(files)} files, last fully indexed {when}."
        )
    lines.append(
        "These are indexed and searchable. They are re-walked when the folder "
        "itself changes, when the interval in Settings elapses, or when you ask."
    )
    plural = "" if len(entries) == 1 else "s"
    return f"{len(entries)} archived folder{plural} skipped", lines


# ---------------------------------------------------------------------------
# Repositories, for the Code tab
# ---------------------------------------------------------------------------

#: The backend's `repos.kind` values, in words somebody reads. Modelled on
#: `kind_tag`: the store keeps a short token, the person sees a noun.
REPO_KINDS = {
    "work": "Repository",
    "submodule": "Submodule",
    "worktree": "Worktree",
}


@dataclass(frozen=True, slots=True)
class RepoRow:
    """One repository, formatted for the Code table."""

    name: str
    files: str          # "48,301"
    kind: str           # "Repository" | "Submodule" | "Worktree"
    seen: str           # "2 hours ago"
    path: str           # shortened, for the column
    root: str           # the full path, for the menu and the tooltip
    #: The unformatted originals, so the table can sort on real values rather
    #: than on what it displays - "9" must sort below "10", and a date must
    #: sort chronologically rather than alphabetically. `SortableItem` reads
    #: these; see `widgets/sortable_item.py` for what happens without them.
    file_count: int = 0
    seen_at: int = 0
    #: The store's row id, so a repository's files can be fetched by the column
    #: that is indexed rather than by matching path strings.
    repo_id: int = 0


@dataclass(frozen=True, slots=True)
class RepoFileRow:
    """One indexed file **inside** a repository, for the Code tree.

    The same five columns as the repository above it, read the way a file wants
    them: what it is called, how big it is, what kind it is, when it changed,
    where it lives. A tree whose two levels disagreed about what column three
    meant would be harder to read than two separate tables.
    """

    name: str           # the file name alone; the folder is in `path`
    size: str           # "12.4 KB"
    kind: str           # "py"
    seen: str           # "2 hours ago"
    path: str           # shortened, for the column
    full_path: str      # for opening, revealing and the tooltip
    ext: str = ""       # lower-case, no dot - what `type:` matches
    size_bytes: int = 0
    seen_at: int = 0
    #: Which repository the file is in. **Empty until the Code tab became one
    #: list**: a tree already answered that with the row it hung under, and a
    #: flat list has to say it in a column - "where is that file" is the
    #: question somebody arrives with, and the repository is the answer.
    repo: str = ""
    #: INDEXED, SKIPPED, FAILED or PENDING - straight from the store. Shown as
    #: a column because "it is in the list but I cannot search inside it" is a
    #: real and useful thing to know, and hiding it invites the same search
    #: twice.
    status: str = ""
    #: Text the preview pane should draw instead of reading the file. Filled
    #: for a *historical* hit, whose version no longer exists on disk - see
    #: `preview_loader.load_preview_for`. Empty for a file in the checkout,
    #: which is read normally.
    preview_text: str = ""


def repo_file_rows(
    records: Iterable[Any], *, now: Optional[float] = None
) -> list[RepoFileRow]:
    """Store rows - or `FileRecord`s - to display rows for a repository's files.

    Takes either shape because the store may hand back dictionaries from a
    dedicated query or dataclasses from `iter_files`, and which one arrives is
    an implementation detail of `read_repo_files`, not of the tree.
    """
    def field(record: Any, key: str, default: Any = "") -> Any:
        if isinstance(record, Mapping):
            return record.get(key, default)
        return getattr(record, key, default)

    out: list[RepoFileRow] = []
    for record in records:
        path = str(field(record, "path", ""))
        if not path:
            continue
        mtime = int(field(record, "mtime_ns", 0) or 0)
        size = int(field(record, "size_bytes", 0) or 0)
        ext = str(field(record, "ext", "") or "").lower().lstrip(".")
        out.append(RepoFileRow(
            repo=str(field(record, "repo", "") or ""),
            status=str(field(record, "status", "") or ""),
            name=path.replace("\\", "/").rstrip("/").rpartition("/")[2] or path,
            size=format_size(size),
            kind=ext,
            seen=format_when(mtime, now=now) if mtime else "",
            path=shorten_path(path, limit=60),
            full_path=path,
            ext=ext,
            size_bytes=size,
            seen_at=mtime,
        ))
    return out


#: How many files one repository contributes to the tree at a time. A tree is
#: read, not scrolled through: a repository with 48,000 files would freeze the
#: window building items nobody will look at, and the search box - one tab away,
#: with `repo:` already set - is the right tool past this point. The tree says
#: so when it truncates rather than quietly showing part of the truth.
REPO_FILE_LIMIT = 500


def read_repo_files(store: Any, row: "RepoRow", *, limit: int = REPO_FILE_LIMIT) -> list[Any]:
    """Every indexed file in one repository. **Runs on a worker, never inline.**

    Prefers `store.repo_files`, which reads the indexed `files.repo_id` column
    directly. Falls back to walking `iter_files` and matching the repository
    root as a path prefix, so the tab works against a store that predates that
    accessor - correct either way, and only the speed differs.

    The fallback asks for `source_kind="file"` so an archive of 200,000 emails
    is excluded in SQL rather than turned into 200,000 objects and discarded.
    """
    dedicated = getattr(store, "repo_files", None)
    if callable(dedicated) and row.repo_id:
        return list(dedicated(row.repo_id, limit=limit + 1))

    root = _comparable(row.root)
    if not root:
        return []
    found: list[Any] = []
    for record in store.iter_files(source_kind="file"):
        if _comparable(getattr(record, "path", "")).startswith(root):
            found.append(record)
            if len(found) > limit:
                break
    return found


def _comparable(path: str) -> str:
    """A path in the one shape prefix comparisons can trust.

    Windows gives back `D:\\Code\\Leasha` and `d:/code/leasha` for the same
    folder, and a prefix test on the raw strings answers no to both.
    """
    return str(path or "").replace("\\", "/").rstrip("/").lower()


def repo_files_summary(shown: int, *, limit: int = REPO_FILE_LIMIT) -> str:
    """The child row shown when a repository has more files than the tree lists."""
    return (
        f"Showing the first {format_count(limit)} files. "
        f"Press Enter on the repository to search all {format_count(shown)}."
    )


def repo_rows(
    rows: Iterable[Mapping[str, Any]], *, now: Optional[float] = None
) -> list[RepoRow]:
    """Store rows to display rows for the Code list.

    `now` is a parameter for the same reason `format_when` takes one: a
    relative time is untestable otherwise.
    """
    out: list[RepoRow] = []
    for row in rows:
        root = str(row.get("root_path", ""))
        # The store already orders by file count; the name falls back to the
        # folder so a repository with no recorded name is still identifiable.
        name = str(row.get("name", "")) or root.replace("\\", "/").rstrip("/").rpartition("/")[2]
        seen_at = _as_ns(row.get("last_seen"))
        count = int(row.get("files", 0) or 0)
        out.append(RepoRow(
            name=name,
            files=format_count(count),
            kind=REPO_KINDS.get(str(row.get("kind", "")), "Repository"),
            seen=format_when(seen_at, now=now) if seen_at else "",
            path=shorten_path(root, limit=60),
            root=root,
            file_count=count,
            seen_at=seen_at,
            repo_id=int(row.get("id", 0) or 0),
        ))
    return out


def _as_ns(value: Any) -> int:
    """`last_seen` as nanoseconds, whatever shape the store gave it.

    Accepts an ISO string, seconds, or nanoseconds, because a column read
    through `dict(row)` arrives as whatever SQLite stored and a preview panel
    is not the place to discover a type mismatch.
    """
    if not value:
        return 0
    if isinstance(value, (int, float)):
        number = int(value)
        # Anything this small is seconds; nanoseconds since 1970 are ~1e18.
        return number * 1_000_000_000 if number < 1e12 else number

    # Imported here, as the other date helper in this module does: `presenter`
    # is deliberately import-light because every view depends on it.
    from datetime import datetime

    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return 0
    return int(moment.timestamp() * 1e9)


def repo_summary(repos: int, files: int) -> str:
    """The line above the table. Says nothing clever when there is nothing."""
    if not repos:
        return ""
    return (
        f"{format_count(repos)} repositor{'y' if repos == 1 else 'ies'}, "
        f"{format_count(files)} file{'s' if files != 1 else ''} indexed"
    )


def repo_empty_state(anything_indexed: bool) -> str:
    """What to say when the Code tab has no repositories to show.

    **Two different questions, two different answers.** A generic "no results"
    would waste the one that matters: somebody with an indexed corpus and no
    repositories needs to be told what a repository is here - a folder holding
    `.git` - and that adding its parent as an indexed root is what makes it
    appear. Somebody with nothing indexed at all needs a route to the Indexing
    tab instead, and being told about `.git` would be noise.
    """
    if anything_indexed:
        return (
            "No code repositories found under your indexed folders.\n\n"
            "A repository is any folder containing a .git folder. If one is "
            "missing from this list, its folder is not inside an indexed root - "
            "add the folder above it in Settings, then index again."
        )
    return (
        "Nothing is indexed yet, so there is nothing to show here.\n\n"
        '<a href="#index">Go to the Indexing tab</a> to add a folder and start.'
    )


@dataclass(frozen=True, slots=True)
class GitPass:
    """Whether a search should also read repository history, and what to say.

    **A decision, so it lives here.** The view's job is to start a worker; which
    switches make a search slow, and how to describe that to somebody waiting,
    are questions with right answers and belong where they can be tested without
    a display. `search_view.py` is held under 250 lines by
    `test_every_qt_view_keeps_its_logic_in_the_presenter`, and that guard was
    right to fire when this logic first went in there.
    """

    switches: tuple[str, ...] = ()
    #: Where the git rows' ranks start, so the two halves read as one list.
    rank_base: int = 1

    @property
    def wanted(self) -> bool:
        return bool(self.switches)

    @property
    def status(self) -> str:
        if not self.switches:
            return ""
        return "Reading repository history for /" + ", /".join(self.switches) + "…"


def git_pass(query: str, tier: str, *, shown: int = 0) -> GitPass:
    r"""Should this search also read history, and from which rank?

    **Full tier only.** `git log -S` walks every commit it is given and takes
    seconds; the first non-negotiable in this application is that no unbounded
    work sits behind a keystroke. An interim search is a keystroke by another
    name, so it never qualifies however the query is written.

    Cheap on the searches that will never touch git, which is nearly all of
    them: `wants_git` is a regex and two dictionary lookups.
    """
    if tier != Tier.FULL:
        return GitPass()

    from app.search.gitquery import wants_git

    return GitPass(switches=wants_git(query), rank_base=max(0, int(shown)) + 1)


def federated_summary(index_rows: int, git_rows: int) -> str:
    """The status line once both halves have answered.

    Said as two numbers rather than one total, because *"41 results"* hides that
    thirty of them are commits from 2016 - and which half a result came from is
    the first thing somebody wants to know when history is involved.
    """
    if not git_rows:
        return ""
    return (f"{index_rows:,} from the index, "
            f"{git_rows:,} from repository history")


#: Words next to which a kind word means a file type rather than a subject.
#:
#: *"excel formula"* and *"word count"* are real searches about spreadsheets and
#: writing; *"excel file"* and *"as a word document"* are somebody naming a type.
#: The difference is the noun beside it, which is why this exists rather than a
#: bare list of kind words.
_DOCUMENT_NOUNS = frozenset({
    "document", "documents", "doc", "docs", "file", "files", "format",
    "attachment", "attachments", "spreadsheet", "presentation", "deck",
})


def kind_suggestion(raw: str) -> tuple[str, str]:
    r"""`(kind word, the switch to offer)`, or `("", "")`.

    From `WORKORDER-202626081059-search-quality.md` F2. `_EXT_GROUPS` has known
    twelve kind words - `excel`, `word`, `mail`, `code` - since Layer 4, and
    `/type excel` works perfectly. **"get me all excel files" does nothing**: the
    word sits in `terms` and no `ext` filter is produced. The vocabulary is
    there; the bridge from prose to filter is not, and that bridge is what the
    owner expected.

    **This offers, and never applies.** A non-negotiable says a query the
    application altered must be visible and editable, because invisible
    narrowing makes search unpredictable - and silently filtering on a guessed
    word is how somebody loses a document and never learns why. So this returns
    something to *show*; applying it is a click.

    The kind word must sit next to a document noun, which is what separates
    *"as a word document"* from *"word count"*.
    """
    from app.search.query import _EXT_GROUPS

    words = [word.strip(".,;:!?").lower() for word in str(raw or "").split()]
    for index, word in enumerate(words):
        if word not in _EXT_GROUPS:
            continue
        neighbours = words[max(0, index - 1):index] + words[index + 1:index + 2]
        if any(near in _DOCUMENT_NOUNS for near in neighbours):
            return word, f"type:{word}"
    return "", ""


def interpret_hint(raw: str, *, enabled: bool) -> str:
    r"""Whether to point at the Interpret button, and what to say.

    F7: Interpret is the designed answer to a sentence like *"find a project
    execution plan as a word document"* - turning it into `type:docx` is
    precisely its job - and the owner did not press it. **That is a
    discoverability fault, not a user error**: a button whose value is invisible
    until pressed will not be pressed.

    Offered only when it would actually help: a long query, no operators
    already typed, and a kind word in it. Anything looser is a hint on every
    search, which is a hint nobody reads.

    This does not replace the parsing fixes. Search must work with Ollama
    stopped - that is the first non-negotiable - so Interpret makes good queries
    better rather than making bad parsing acceptable.
    """
    text = str(raw or "").strip()
    if not enabled or len(text.split()) < 5 or ":" in text or "/" in text:
        return ""
    if not kind_suggestion(text)[0]:
        return ""
    return "This looks like a sentence — press Interpret to turn it into filters."


#: Codes for the two notices the *window* raises, as opposed to the engine.
NOTICE_KIND_SUGGESTION = "NOTICE_KIND_SUGGESTION"
NOTICE_INTERPRET_HINT = "NOTICE_INTERPRET_HINT"


@dataclass(frozen=True, slots=True)
class _Hint:
    """Shaped like `engine.Notice`, because the bar branches on `code`."""

    code: str
    message: str


def result_view_state(response: Any, raw: str, *,
                      interpret_enabled: bool = False) -> tuple:
    """Everything the results handler needs, decided in one place.

    Returns `(notices, terms, summary, status)`. Four small decisions that were
    four statements in the view - and `search_view.py` is held under 250 lines
    by `test_every_qt_view_keeps_its_logic_in_the_presenter`, which fired when
    the kind-word suggestion went in. None of these needs a window to compute
    and none of them was ever view logic.
    """
    parsed = getattr(response, "parsed", None)
    notices = [
        *getattr(response, "notices", ()),
        *window_notices(raw, parsed, interpret_enabled=interpret_enabled),
    ]
    terms = (list(parsed.terms) + list(parsed.phrases)) if parsed else []
    summary, status = results_message(response)
    return notices, terms, summary, (status or summary)


def window_notices(raw: str, parsed: Any = None, *,
                   interpret_enabled: bool = False) -> list[Any]:
    r"""The suggestions the window adds to the engine's own notices.

    Both are **offers**, and that is the whole design. A non-negotiable says a
    query the application altered must be visible and editable; silently
    filtering on a guessed kind word is how somebody loses a document and never
    learns why. So these are things to show, and applying one is a click.

    Nothing is suggested once the person has already said it - a query carrying
    `type:` needs no help choosing a type, and a hint on every search is a hint
    nobody reads.
    """
    found: list[Any] = []
    if not getattr(parsed, "ext", ()):
        word, switch = kind_suggestion(raw)
        if word:
            found.append(_Hint(
                NOTICE_KIND_SUGGESTION,
                f'Search <a href="apply:{switch}">{word} files only</a>? '
                f"Your results are not filtered by type."))
    hint = interpret_hint(raw, enabled=interpret_enabled)
    if hint:
        found.append(_Hint(NOTICE_INTERPRET_HINT, hint))
    return found


def _switch_catalogue() -> tuple[Any, ...]:
    """`app.search.commands.COMMANDS`, imported at call time.

    A module-level import would put Layer 4 into the import graph of a module
    that runs on every keystroke, which
    `test_nothing_that_runs_on_a_keystroke_imports_this` exists to prevent.
    Called once, at import, to build `ALL_COMMANDS`.
    """
    from app.search.commands import COMMANDS

    return COMMANDS


# -- what each box offers on `/` ---------------------------------------------
#
# **The search box is the union; the focused tabs are subsets of it.** Search is
# the generic one and must offer every command there is, because somebody who
# does not yet know which tab they want types there. A focused tab offers what
# it can honour and nothing else.
#
# Here rather than in `widgets/command_popup.py` because the rule is about the
# grammar, not about Qt: `test_command_subsets.py` checks each offered command
# against the field its tab's query function actually consumes, and that check
# should not need a display to run.

#: Every switch there is, in the catalogue's own order.
#:
#: **The subsets used to be much smaller, and that was the bug.** Files offered
#: two of eleven and Code three; the other eight were typed, parsed, and then
#: dropped on the floor by a tab that had no idea what to do with them. The
#: owner's report was exactly that: *"the switches Search should have all
#: switches, files should have all switches... and the results should be same
#: across but only applicable to the tab, this is not the case"*.
#:
#: What made the subsets necessary was that each tab wrote its own filtering.
#: Now they share one - `storage/filters.file_filter_sql` - so a switch means
#: the same thing everywhere and the only question left is which *rows* a tab is
#: about. That question has an answer for every switch on every tab, so the
#: subsets collapse into this.
#: Read from the catalogue rather than restated, so a switch added there is
#: offered everywhere without a second list to remember.
ALL_COMMANDS: tuple[str, ...] = tuple(
    command.name for command in _switch_catalogue()
)

#: Files: every file row. A `/from` here is not a mail search - it narrows to
#: the mail *files* on disk whose sender matches, which is a question about
#: files and belongs on the tab about files.
FILES_COMMANDS = ALL_COMMANDS

#: Mail: every switch too, for the same reason in reverse. The five mail
#: columns are what `browse_messages` was built for, and the file-level ones
#: narrow by the message's own file - its name, folder, type and size.
MAIL_COMMANDS = ALL_COMMANDS

#: Code: what Files offers. `repo:` is not special here, it is simply the switch
#: this tab is most often used with - and it worked on the other tabs already.
CODE_COMMANDS = ALL_COMMANDS


@dataclass(frozen=True, slots=True)
class CodeRoute:
    """What one line typed into the Code box means.

    **One box, two engines.** The owner's correction: *"the code search page is
    all wrong it should be a combined one search box with the git code files in
    the list"*. A tree of repositories above a separate git box made somebody
    choose an engine before they had a question - and the question is nearly
    always "where is that file", which is answered from the index in
    milliseconds.

    So the grammar decides, not the person. A line with no git switch in it is
    an index search: instant, as you type, over every repository at once. A line
    carrying `/history`, `/branch`, `/introduced` and the rest is a git run, on
    Enter, because `git log -S` diffs every commit it walks.
    """

    #: "index" or "git".
    engine: str = "index"
    #: Free text, with the switches removed - **for the index path only.**
    #: A git run is handed the raw line and re-read by `parse_git_query`, which
    #: is the module that knows what `/class OrderService` means; copying half
    #: of that decision here is how the two would come to disagree.
    text: str = ""
    #: `/repo` - a name, not a path.
    repo: str = ""
    #: `/type` or `/extension`, lower-case and without dots.
    extensions: tuple[str, ...] = ()
    #: Why it chose git, for the line above the results. Empty for the index.
    because: str = ""
    #: The whole parse, for the index engine. **Added because `text`, `repo`
    #: and `extensions` were not enough and quietly pretended to be.**
    #:
    #: The Code dropdown offered `/name` and `/path`; `parse_query` filled
    #: `names` and `paths` correctly; and this route carried neither, so both
    #: narrowed nothing while looking like they had. `test_command_subsets.py`
    #: could not catch it - it asserts the *parser* fills a field, not that the
    #: tab consumes it.
    #:
    #: `None` on the git path, where the raw line is re-read by
    #: `parse_git_query` instead. Typed `Any` because `ParsedQuery` is Layer 4
    #: and this module runs on a keystroke.
    parsed: Any = None


#: Switches only the repository engine has. **Moved to `app/search/gitquery.py`**
#: and re-exported here under the name the Code tab already imports.
#:
#: It moved because the main search box needs the same question answered - "is
#: this line worth a subprocess?" - and asking it should not require importing a
#: UI module. `gitquery` is pure: a catalogue and a parser, no `git` anywhere.
def _git_only() -> frozenset[str]:
    from app.search.gitquery import GIT_ONLY as _names

    return _names


GIT_ONLY: frozenset[str] = _git_only()


@dataclass(frozen=True, slots=True)
class GitScope:
    """What the tree on the left has narrowed the file list to.

    **A scope, not a query.** The tree says *where to look*; the box still says
    *what to look for*, and the two compose - asked for as *"dont forget the
    code switches apply there too"*. Selecting `main` and then typing
    `/type cs order` means "`.cs` files matching order, as of main", and neither
    half overrides the other.
    """

    #: "" (everything), "repo", "worktree", "branch" or "commit".
    kind: str = ""
    #: Repository name, as `repos.name` holds it.
    repo: str = ""
    #: Its path on disk - a git run needs this, the index needs the name.
    root: str = ""
    #: Branch name or commit sha. Empty for a repository or the working tree.
    ref: str = ""
    #: What the commit said, for the line above the list.
    subject: str = ""

    @property
    def from_git(self) -> bool:
        """Whether answering this needs git rather than the index.

        **The index cannot answer a branch.** It holds the working tree: a file
        deleted on `main` but alive on a feature branch has no row, and one that
        only ever existed on a branch never had one. That is the whole reason
        this pane is worth building rather than being a filter over names.
        """
        return self.kind in ("branch", "commit")

    def describe(self) -> str:
        if self.kind == "branch":
            return f"{self.repo} · branch {self.ref}"
        if self.kind == "commit":
            subject = f" · {self.subject}" if self.subject else ""
            return f"{self.repo} · commit {self.ref}{subject}"
        if self.kind in ("repo", "worktree"):
            return f"{self.repo} · working tree"
        return "every repository"


def git_rows_matching(
    rows: Any, *, text: str = "", extensions: Any = (),
    types: Any = None, paths: Any = (), names: Any = (),
) -> list[Any]:
    r"""Narrow git-sourced rows by the same switches the index path honours.

    **The switches have to work here too, and git cannot apply them.** A branch
    listing comes back from `git ls-tree` as paths; `/type cs` and free text are
    then a filter over that list rather than a clause in a query. Capped at
    `TREE_FILE_LIMIT` before it arrives, so this is a pass over a few thousand
    strings.

    `extensions` is what the person typed and wins outright; `types` is the
    configured "what counts as code" set, used only when they typed nothing -
    the same precedence as the index path, so a branch and the working tree
    filter identically.

    `paths` and `names` are `/path` and `/name`, and they are here because the
    Code dropdown has always offered both while nothing consumed either. They
    are the same two questions the index path asks and must be asked the same
    way: `/path` is any part of the folder, `/name` is the basename only.

    Matching is substring and case-insensitive on the whole path, because
    `order` should find `src/OrderService.cs` - the rule `code_files` already
    uses for the index.
    """
    wanted = str(text or "").strip().lower()
    typed = {str(e).lstrip(".").lower() for e in (extensions or ()) if str(e).strip()}
    allowed = typed or {str(e).lstrip(".").lower() for e in (types or ())}

    found = []
    for row in rows or ():
        # **Either shape.** A `GitRow` straight from a reader, or the mapping
        # the pane shapes for the table - `repo_file_rows` takes both for the
        # same reason, and reading only attributes silently filtered every
        # mapping out, which looked exactly like a branch with no files in it.
        path = str((row.get("path") if isinstance(row, Mapping)
                    else getattr(row, "path", "")) or "")
        if not path:
            continue
        lowered = path.lower()
        if wanted and wanted not in lowered:
            continue
        # `/path` asks about the folder, so the basename is cut off first -
        # otherwise `path:service` matches `OrderService.cs` sitting in the
        # repository root, which is the answer to the other switch.
        folder = lowered.replace("\\", "/").rpartition("/")[0]
        if any(str(one).lower() not in folder for one in (paths or ())):
            continue
        base = lowered.replace("\\", "/").rpartition("/")[2]
        if any(str(one).lower() not in base for one in (names or ())):
            continue
        if allowed:
            # The same two questions `indexed_ext` answers, in the same order:
            # a suffix, or the whole name for `Makefile` and `Dockerfile`.
            name = path.replace("\\", "/").rpartition("/")[2].lower()
            stem, dot, suffix = name.rpartition(".")
            kind = suffix if (dot and stem) else name.lstrip(".")
            if kind not in allowed:
                continue
        found.append(row)
    return found


def code_rows_for(store: Any, scope: Any, route: Any,
                  *, cached: Any = None, limit: int = 500) -> list[dict]:
    r"""The Code list, for the current scope and the current query.

    **Two engines, one shape**, so the table, the preview and the row menu do
    not know which one ran. Both end as the mappings `repo_file_rows` reads.

    **But only one of them may run behind a keystroke.** A branch scope is
    answered by `git ls-tree`, a subprocess: fetching it per keystroke is the
    first non-negotiable broken, and `test_nothing_that_runs_on_a_keystroke_
    imports_this` caught exactly that in the first version of this function.
    So the listing is fetched **once per selection**, by the pane that owns the
    tree, and handed in as `cached`; typing then filters it in memory. The
    listing does not change while somebody types, so this is both correct and
    far faster than the version the guard rejected.

    The index path has no such problem - it is an indexed query that applies
    the text in SQL - so it still runs per keystroke.

    **The switches compose with the scope** - *"dont forget the code switches
    apply there too"*. The tree says where to look, the box says what to look
    for. A typed `/type` beats the configured code types on either path, so a
    branch and the working tree filter identically.
    """
    types = code_type_filter(store)
    text = str(getattr(route, "text", "") or "")
    typed = tuple(getattr(route, "extensions", ()) or ())
    parsed = getattr(route, "parsed", None)

    if cached is not None:
        return git_rows_matching(
            cached, text=text, extensions=typed, types=types,
            paths=tuple(getattr(parsed, "paths", ()) or ()),
            names=tuple(getattr(parsed, "names", ()) or ()),
        )[:limit]

    repo = (str(getattr(scope, "repo", "") or "")
            or str(getattr(route, "repo", "") or ""))
    if parsed is None:                   # a git route: no parse to apply
        return list(store.code_files(
            text, repo=repo, ext=list(typed) or types, limit=limit))

    # **One query, and that is the point.** `code_files` understood `repo`,
    # `text` and `ext`; `browse_files` understands every switch, through the
    # same definition Files and the search box use. Keeping both would have
    # meant two answers to "what does `/path` mean here", which is the split
    # this whole change exists to close.
    #
    # The tree's selection is injected as `repos` rather than passed beside the
    # parse, so a repository chosen on the left and one typed as `/repo` reach
    # the filter by the same route and cannot disagree.
    #
    # **Scoped to `code`, which is what keeps this the Code tab.**
    # `file_filter_sql` reads that scope as `f.repo_id IS NOT NULL` - "in a
    # repository", the same narrowing the search box's Code chip applies.
    # Without it this would list the whole index, which is the one thing a tab
    # about repositories must not do.
    if repo:
        from dataclasses import replace as _replace

        parsed = _replace(parsed, repos=(repo,))
    return list(store.browse_files(
        parsed.scoped("code"), limit=limit,
        extra_ext=None if typed else types))


def code_rows_and_repos(store: Any, scope: Any, route: Any, *, cached: Any = None,
                        repos: Any = (), limit: int = 500) -> dict:
    r"""The Code list **and** which repositories hold a match, in one worker.

    Two things the panes need, fetched together on purpose. The order's
    constraint is that *"the count must come from the same query the list ran"* -
    a second, separately-scheduled count is how a tree and a list come to
    disagree, which is the code-tab order's §5.

    It also keeps the aggregate off the UI thread. Narrowing the tree in the
    slot that paints the rows would put a store read on the keystroke path,
    which `test_ui_never_blocks` refuses and is right to.
    """
    rows = code_rows_for(store, scope, route, cached=cached, limit=limit)
    return {"rows": rows,
            "matching": matching_repos(store, route, cached=cached, repos=repos)}


def matching_repos(store: Any, route: Any, *, cached: Any = None,
                   repos: Any = ()) -> Optional[set]:
    r"""Which repositories hold a match, or None for "no criteria - show all".

    **The git tree listed every repository whatever was typed.** Type a term
    matching files in one checkout and the other three sat there as though they
    matched too - and a tree is read as *"these are the repositories that have
    what you asked for"*, which made it the most misleading pane here. Same
    fault as `code_type_filter` and the empty-list states in the code-tab order:
    a pane showing something the query did not ask for, with nothing saying why.

    `None` rather than "all of them" is the distinction that matters. An empty
    box asks nothing, so every repository is shown and no count is claimed; a
    box with criteria narrows, and says `2 of 4`. Collapsing the two would put
    a meaningless "4 of 4" on screen for somebody who has typed nothing.

    **Never a subprocess.** A branch scope is answered from the listing the tree
    already fetched for the selection - `cached` - because fetching one per
    keystroke is the first non-negotiable broken, and the guard in
    `test_nothing_that_runs_on_a_keystroke_imports_this` caught exactly that
    once already.
    """
    parsed = getattr(route, "parsed", None)
    text = str(getattr(route, "text", "") or "").strip()
    typed = tuple(getattr(route, "extensions", ()) or ())
    has_criteria = bool(text or typed or (parsed is not None and parsed.has_filters))
    if not has_criteria:
        return None

    if cached is not None:
        # A branch listing, already in memory. Filtering it is the same work the
        # list does, so the two cannot disagree.
        rows = git_rows_matching(
            cached, text=text, extensions=typed,
            types=code_type_filter(store),
            paths=tuple(getattr(parsed, "paths", ()) or ()),
            names=tuple(getattr(parsed, "names", ()) or ()),
        )
        found = {str(row.get("repo") or "") for row in rows}
        return {name for name in found if name}

    if parsed is None:
        return None                      # a git route: the tree is not narrowed

    try:
        ids = store.repos_with_matches(
            parsed.scoped("code"),
            extra_ext=None if typed else code_type_filter(store))
    except Exception as exc:             # noqa: BLE001 - a tree, not a search
        _log.debug("could not narrow the repository tree: {}", exc)
        return None

    def field(row: Any, key: str) -> Any:
        """Rows arrive as `sqlite3.Row` here and as objects in the tests."""
        try:
            return row[key]
        except (TypeError, KeyError, IndexError):
            return getattr(row, key, None)

    by_id = {int(field(repo, "id") or 0): str(field(repo, "name") or "")
             for repo in (repos or ())}
    return {by_id[one] for one in ids if one in by_id and by_id[one]}


def repo_tree_summary(matching: Optional[set], total: int) -> str:
    """`2 of 4 repositories match`, or `""` when nothing was asked.

    Said out loud because a tree that has silently shrunk is the same fault as
    one that silently shows everything - the person cannot tell whether three
    repositories are missing or were never there.
    """
    if matching is None or total <= 0:
        return ""
    count = len(matching)
    if count == total:
        return f"All {total} repositories match"
    if count == 0:
        return f"No repositories match — {total} indexed"
    return f"{count} of {total} repositories match"


def code_type_filter(store: Any) -> Optional[list[str]]:
    r"""The configured "what counts as code" set, or None for no filter.

    **The Code tab listed by location, not by type.** `code_files` narrows on
    `repo_id` and `source_kind` and nothing else, so every PDF, spreadsheet,
    image and log that happens to live in a repository folder appeared in it -
    reported from the window as *"its bringing files which are not code"*, and
    warned about by `app.cli repos` ever since repositories were added:
    *"`scope:code` will match your whole corpus rather than just code"*.

    **It changes what is listed, never what is indexed.** Unticking a group
    here removes those types from one tab; the main search still finds every one
    of them, which is the whole reason this is a view preference rather than a
    reading rule. See `app/core/code_types.py`.

    Read per search rather than cached, so the setting takes effect on the next
    keystroke rather than the next restart - it is one `get_state` and a set
    union over a list already in memory.

    Never raises. An unreadable preference costs the filter, never the tab, and
    the failure direction is "show everything" - a Code tab that has silently
    hidden a language is far harder to notice than one showing a stray PDF.
    """
    from app.core.code_types import choice_from, extensions_for

    try:
        preset, chosen = choice_from(store)
        wanted = extensions_for(preset, chosen)
    except Exception as exc:                     # noqa: BLE001 - see the docstring
        _log.debug("code types not read: {}", exc)
        return None
    return sorted(wanted) if wanted is not None else None


def code_route(text: str) -> CodeRoute:
    """Read the Code box: which engine, and what to give it.

    Qt-free and here rather than in the view, because "which engine" is the
    decision the whole tab turns on and a decision made inside a widget is one
    nobody can test. It is also the decision that must never be made *wrongly
    towards git*: an index lookup taken as a repository search costs somebody
    two seconds and a subprocess for a question that had a 3ms answer.
    """
    from app.search.commands import expand_slashes
    from app.search.gitquery import git_command_for, parse_git_query
    from app.search.query import parse_query

    raw = str(text or "").strip()
    if not raw:
        return CodeRoute()

    # Which git-only switches are present. Read through `git_command_for` so an
    # alias - `/hist`, `/b`, `/by` - counts exactly as its canonical name does.
    named: list[str] = []
    for token in raw.split():
        if not token.startswith("/") and ":" not in token:
            continue
        name = token.lstrip("/").partition(":")[0].partition("=")[0]
        command = git_command_for(name)
        if command is not None and command.name in GIT_ONLY:
            named.append(command.name)

    if named:
        query = parse_git_query(raw)
        # **`/repo` is consumed by the git parser too**, and has to be: git is
        # *run inside* a checkout, so which one is the caller's business - but
        # left in the free text it becomes part of the pattern, and
        # `/repo leasha CustomerId /history` searched for the literal string
        # "/repo leasha CustomerId". The symptom before that was "name a
        # repository first" in answer to a line that named one.
        return CodeRoute(
            engine="git", text=query.text, repo=query.repo,
            extensions=query.extensions,
            because="/" + ", /".join(sorted(set(named))),
        )

    parsed = parse_query(expand_slashes(raw))
    return CodeRoute(
        engine="index",
        text=(parsed.text or "").strip(),
        repo=(parsed.repos[0] if getattr(parsed, "repos", ()) else ""),
        extensions=tuple(getattr(parsed, "ext", ()) or ()),
        parsed=parsed,
    )


#: How many values a dropdown offers. Enough to cover a real corpus's file
#: types and a person's regular correspondents; few enough that the list is
#: still something you scan rather than search.
VALUE_LIMIT = 40

#: Per-source ceilings, where the general one is wrong.
#:
#: **`ext` is the only bounded source, and this bounds only the indexed half.**
#: A machine has perhaps eighty file types and never more; senders and folders
#: have no ceiling at all, which is what the general limit is protecting
#: against. Applying one number to both meant the safe cap for an unbounded
#: column was silently truncating a list that fits on a screen - and truncating
#: it by frequency, so the types somebody had just switched on were the first
#: to be cut.
#:
#: The *configured* half has its own, much tighter ceiling below. One number
#: for both was defensible while the application read 34 text extensions; it
#: reads 405 now, and see `catalogue_limit` for why that changes the answer
#: rather than merely the number.
VALUE_LIMITS: dict[str, int] = {"ext": 120}

#: Characters of prefix before the configured-but-not-indexed tail is offered.
CATALOGUE_PREFIX_CHARS = 2

#: How many of that tail may then be shown.
CATALOGUE_LIMIT_PREFIXED = 40


def catalogue_limit(prefix: str) -> int:
    r"""How many configured-but-unindexed types to offer for this prefix.

    **Zero until two characters are typed, and the reason is ordering rather
    than volume.**

    The indexed suggestions are sorted by frequency: the type somebody wants is
    nearly always one of the three they have thousands of, so the head of the
    menu is genuinely useful. The configured tail has no frequency to sort by -
    nothing has been indexed - so `enabled_extensions` returns it
    alphabetically. With 405 enabled formats that makes the first twelve
    `abap, ada, adb, ads, adoc, ahk...`: not a shortlist, just the front of an
    alphabet, and it would sit above `pdf` and `docx` in the one menu that
    exists to answer "what can I filter by".

    Truncating it to a smaller arbitrary number does not fix that - it is the
    same noise, shorter. What fixes it is a prefix: two characters narrow 405
    formats to a handful, and typing them is exactly the gesture somebody makes
    to check that a format they just switched on is really there. **That check
    is the whole reason the tail exists**, and it still works.

    A pure function so the rule can be argued with, and read, without a menu.
    """
    return CATALOGUE_LIMIT_PREFIXED if len(str(prefix or "").strip()) >= CATALOGUE_PREFIX_CHARS else 0

#: Parsed once. `load_rules` reads and validates two TOML files, and this is
#: reached from a keystroke - see `enabled_extensions`.
_CATALOGUE: tuple[str, ...] | None = None


def clear_format_catalogue() -> None:
    """Forget the cached format list, so the next menu rebuilds it.

    Called when the file-types editor saves. Without it a format switched on
    stays missing from `/type` until the window is restarted, which is the same
    complaint this fixed one layer down.
    """
    global _CATALOGUE
    _CATALOGUE = None


def enabled_extensions() -> tuple[str, ...]:
    """Every file type currently switched on, without dots, alphabetically.

    The registry is the authority on what can be read and configuration is an
    override on top of it - so this asks `FormatRules.describe(REGISTRY)`, which
    is the same answer `app.cli formats` prints. A second list built from either
    half alone would disagree with that command, and a filter menu that
    disagrees with the format report is worse than one that is merely short.

    Imported inside the function: `app.extract` pulls in a registry that imports
    optional libraries, and `presenter` is on the window's startup path. Same
    trade as `preview_loader._extractable`.

    Never raises. A missing or invalid config file costs the extra suggestions
    and nothing else - the index-backed ones are unaffected.
    """
    global _CATALOGUE
    if _CATALOGUE is not None:
        return _CATALOGUE

    found: list[str] = []
    try:
        from app.core.config import load_settings
        from app.core.formats import load_rules
        from app.extract.base import REGISTRY

        rules = load_rules(data_path=load_settings().data_path)
        found = sorted(
            str(row["extension"]).lstrip(".").lower()
            for row in rules.describe(REGISTRY)
            if row.get("enabled") and str(row.get("extension") or "").strip(".")
        )
    except Exception:                           # broad by design - see the docstring
        found = []

    _CATALOGUE = tuple(found)
    return _CATALOGUE


class SlashContext(NamedTuple):
    """What the box is asking for, and what it has already been told.

    A NamedTuple rather than a plain tuple because `context` is the fourth
    element and reading `slash_context(...)[3]` at a call site is how the
    meaning of a position gets forgotten. Unpacking three still fails loudly,
    which is the right way for this to change - silently dropping a filter
    would give a menu that looks right and answers the wrong question.
    """

    head: str
    mode: str
    partial: str
    context: Any = None


def slash_context(text: str, resolve: Any = None) -> SlashContext:
    """Read the word being typed: `(head, mode, partial, context)`.

    `mode` is:

    * ``"command"`` while a `/name` is being typed - offer the filters;
    * ``"value"`` once a `name:` has been settled on - offer its values;
    * ``""`` when neither applies - close the menu.

    `head` is everything before the word, so a caller can rewrite the word
    without touching what was typed before it.

    **One reader for both menus.** The alternative - a pattern per menu, each
    with its own idea of where a word begins - is how `12/03` and `D:/docs`
    end up opening a dropdown over a date and a path. Those are the cases worth
    remembering: a search box that silently rewrites what somebody typed is a
    search box they stop trusting, and this is the function that decides
    whether it is about to.

    Qt-free and here rather than in the widget, so both can be checked without
    a display.
    """
    # `resolve` is how the Code tab reads its own catalogue with this same
    # function: repository search has different switches over a different
    # engine, and one of them - `/api` for `endpoint` - collides with a path in
    # a way the index's catalogue never does.
    if resolve is None:
        from app.search.commands import command_for as resolve

    if not text or text.endswith(" "):
        return SlashContext("", "", "")

    word = text.rpartition(" ")[2]
    head = text[: len(text) - len(word)]

    if ":" in word:
        name, _sep, partial = word.partition(":")
        # A colon that is not one of ours - `D:/docs`, `http://…`, `12:30` -
        # is somebody's text and is left alone.
        if resolve(name) is not None:
            return SlashContext(head, "value", partial, _settled(head))
        return SlashContext("", "", "")

    if word.startswith("/"):
        return SlashContext(head, "command", word[1:])
    return SlashContext("", "", "")


def _settled(head: str) -> Any:
    """The filters already typed, parsed by the one parser this project has.

    **Parsed only in value mode**, which is where it is used: this function
    runs on every keystroke on the UI thread, and the command menu has no use
    for it. Never raises - a half-typed query is the normal state here, and a
    dropdown that throws while somebody is typing is worse than one that offers
    unscoped values.
    """
    text = (head or "").strip()
    if not text:
        return None
    try:
        from app.search.query import parse_query

        return parse_query(text)
    except Exception:                            # noqa: BLE001 - see docstring
        return None


def value_suggestions(store: Any, name: str, prefix: str = "",
                      limit: int = VALUE_LIMIT, resolve: Any = None,
                      lookup: Any = None, catalogue: Any = None,
                      context: Any = None,
                      notes: Optional[list[str]] = None,
                      counts: Optional[dict] = None) -> list[str]:
    """What to offer after `/type `, `/from `, `/repo `, `/after `…

    **The half of the `/` menu that was missing.** The menu said which filters
    exist and then left somebody to guess a value - and a value guessed wrong
    returns nothing, which is indistinguishable from a filter that does not
    work. Offering what is actually in the index closes that gap.

    Three sources, in this order:

    * `command.source` - read from the index, commonest first, through
      `distinct_values`, which is bounded and index-backed because this is
      reached from a keystroke.
    * `command.values` - fixed by the grammar. `/has` has exactly two answers,
      `/type` has the kind words (`excel`, `code`, `mail`) that `_EXT_GROUPS`
      expands, and a date has a handful of spellings easier to pick than to
      recall.
    * `catalogue` - for `/type` only: every format currently switched on,
      whether or not one has been indexed yet. Defaults to
      `enabled_extensions()`; injectable so the merge can be tested without a
      config file on disk.

    Qt-free and here rather than in the widget, so the rule about *what* is
    offered can be tested without a display - which is the same reason
    `FILES_COMMANDS` and its siblings live in this file.

    Never raises. A suggestion list is a convenience; a store that is mid-index,
    locked or closed must cost the suggestions and nothing else.
    """
    if resolve is None:
        from app.search.commands import command_for as resolve

    command = resolve(name)
    if command is None:
        return []

    wanted = str(prefix or "").strip().lower()
    # **Only when the caller left the default.** The per-source ceiling raises
    # a general cap that is too low for `ext`; it must never override a limit
    # somebody asked for, and `max()` on its own did exactly that - a caller
    # asking for seven got a hundred and twenty, and the query it was trying to
    # keep small was pushed down to the database at the larger size.
    if int(limit or 0) == VALUE_LIMIT:
        limit = VALUE_LIMITS.get(command.source, VALUE_LIMIT)
    limit = max(int(limit or 0), 1)
    found: list[str] = []

    #: value -> the `ValueCount` it came from, when it came from one. The
    #: merged list stays strings so every existing caller keeps working; the
    #: counts ride alongside for `value_rows` to pick up.
    counted: dict[str, Any] = {}

    def offer(values: Any, cap: Optional[int] = None) -> None:
        """Add what matches the prefix, keeping the first spelling seen.

        `cap` bounds how many *new* values this source may contribute, which is
        not the same as slicing the input: a source whose first twenty entries
        are already on the list would otherwise spend its whole allowance
        adding nothing.
        """
        added = 0
        for value in values or ():
            if cap is not None and added >= cap:
                return
            plain = isinstance(value, str)
            text = (value if plain else str(getattr(value, "value", value))).strip()
            if not text or (wanted and wanted not in text.lower()):
                continue
            if text not in found:
                found.append(text)
                if not plain:
                    counted[text] = value
                added += 1

    # **Both readers, in order, not one or the other.** The Code tab is a
    # single box over two engines, so its switches are sourced from two places:
    # `/branch` and `/author` only git can answer, `/type` and `/repo` only the
    # index can. Taking `lookup` *instead of* the store left `/type` offering
    # nothing on the one tab where code file types matter most - the menu was
    # there, it opened, and it was empty.
    # **Only the filters this command says may narrow it.** `Command.scoped_by`
    # is the catalogue's answer, so the dropdown, the CLI and the model grammar
    # cannot disagree about it. Narrowing only ever removes values, so a wrong
    # entry there costs a suggestion rather than a wrong answer.
    scope = scope_for(command, context)
    repo = _scope_repo(scope)

    readers = []
    if lookup is not None:
        # git's values are per-checkout; `repo:leasha branch:` means leasha's
        # branches. The lookup already wanted this argument implicitly.
        readers.append(
            (lambda kind, prefix, limit: lookup(kind, prefix, limit, repo=repo))
            if repo and _takes_repo(lookup) else lookup)
    if store is not None:
        # **`within` is passed only when there is one.** Sending `within=None`
        # to a store that has never heard of it raises `TypeError`, which the
        # broad `except` below then swallows - so the menu would quietly lose
        # every indexed value rather than say anything. That is the silent
        # degradation this project has a standing rule against, and it showed
        # up immediately: four existing tests use a double with the old
        # signature, and all four went from offering `pdf` to offering `word`.
        readers.append(lambda kind, prefix, limit: _counted_values(
            store, kind, prefix, limit, scope))

    # 1. What is actually indexed, commonest first. Still first, because the
    #    extension somebody wants is nearly always one of the three they have
    #    thousands of.
    for reader in readers if command.source else ():
        try:
            offer(reader(command.source, wanted, limit))
        except Exception as exc:                # noqa: BLE001 - see the docstring
            _log.debug("no {} suggestions: {}", command.source, exc)
        if len(found) >= limit:
            break

    # **What the *index* said under the scope**, which is not the same as what
    # the menu ends up holding: the grammar's own values and the format
    # catalogue below are facts about the language, and no scope narrows them.
    # `1e` is about the scoped lookup coming back empty, so this is the number
    # it has to watch - the first version gated on the merged list, and a
    # `/type` under a repository with nothing in it still showed `excel` and
    # `word`, so the fallback could not fire on the case it was written for.
    from_index = len(found)

    # 2. The grammar's own values. **After the index, not before**, which is
    #    the one ordering change here: `type:excel` is a real filter and it
    #    should not outrank `pdf` when there are four thousand PDFs. Commands
    #    with no `source` are unaffected - `/has` and `/size` reach this first
    #    and behave exactly as they did.
    offer(command.values)

    # 3. Configured but not yet indexed.
    #
    #    **The gap this whole function existed to close, reopened at the other
    #    end.** `distinct_values` reads `files.ext`, so it can only ever offer a
    #    type somebody has already indexed - and a format switched on in the
    #    file-types editor is invisible in `/type` until the next index run
    #    finds one. Switching a format on and finding no trace of it in the
    #    filter menu reads as the setting not having worked.
    #
    #    Deliberately last. These may return nothing, and the original argument
    #    against offering them stands: a value that matches nothing is how a
    #    working filter looks broken. Ordering answers it - what is in the index
    #    is what is at the top - without going back to pretending the type does
    #    not exist.
    #    **Gated on there being a store, which is not about the store.** The
    #    menu is built twice: once instantly on the interface thread with no
    #    store, and again on a worker once the index has answered. Reading two
    #    TOML files is I/O, and the interface thread does not do I/O - so the
    #    catalogue rides with the pass that is already on a worker. An explicit
    #    `catalogue` overrides the gate, which is what the tests pass.
    #
    #    **And bounded separately from the index, because 34 became 405.**
    #    `3b29b7f` took the readable text types from thirty-four to four
    #    hundred and five. Sharing one ceiling with the indexed half then
    #    inverts the ordering this was built around: a corpus holding forty
    #    types would have a tail of three hundred and sixty-five sitting behind
    #    it, alphabetically, and the menu fills with types the machine does not
    #    have. See `catalogue_limit`.
    tail = catalogue_limit(wanted)
    if tail and command.source == "ext" and (catalogue is not None or store is not None):
        try:
            offer(catalogue() if catalogue is not None else enabled_extensions(),
                  cap=tail)
        except Exception as exc:                # broad by design - see the docstring
            _log.debug("no format catalogue: {}", exc)

    # **1e: an empty menu is indistinguishable from a broken one.** A scope
    # that removes everything is the one case where narrowing has made things
    # worse, so it is undone and said: the unscoped values, with a note the
    # widget renders as a dimmed "(all)". Silence here would be the failure
    # this whole widget exists to prevent, arrived at from the other end.
    if not from_index and scope is not None and command.source:
        _log.debug("no {} values under the typed filters; offering all",
                   command.source or command.name)
        unscoped = value_suggestions(
            store, name, prefix, limit, resolve, lookup, catalogue,
            context=None, notes=None)
        if unscoped:
            _note_all(notes)
        return unscoped

    # **3b, and it is a label rather than a feature.** Typing a date by hand
    # has always worked. Nothing said so, so the menu read as the only way in -
    # and a person who wanted `after:2019-04-01` had no sign the box would take
    # it. Last, because it is the way out rather than an answer.
    if getattr(command, "is_date", False) and not wanted and len(found) < limit:
        found.append(CUSTOM_ROW)

    if counts is not None:
        counts.update(counted)
    return found[:limit]


def _counted_values(store: Any, kind: str, prefix: str, limit: int,
                    scope: Any) -> list[Any]:
    """`ValueCount`s where the store offers them, strings where it does not.

    A store that predates `distinct_value_counts` - a test double, an older
    object - still answers `distinct_values`, and losing its values because it
    has not grown a method is the silent degradation `within=` already had to
    be taught to avoid.
    """
    extra = {"within": scope} if scope is not None else {}
    counts_of = getattr(store, "distinct_value_counts", None)
    if callable(counts_of):
        return counts_of(kind, prefix=prefix, limit=limit, **extra)
    return store.distinct_values(kind, prefix=prefix, limit=limit, **extra)


#: What the widget shows when a scope was dropped. One sentence, in the plain
#: register the rest of the menu uses.
ALL_VALUES_NOTE = "(all)"


def _note_all(notes: Optional[list[str]]) -> None:
    """Record that the scope was dropped. **Never raises**; it is a label."""
    if notes is None:
        return
    try:
        if ALL_VALUES_NOTE not in notes:
            notes.append(ALL_VALUES_NOTE)
    except Exception:                            # noqa: BLE001 - see docstring
        return


def scope_for(command: Any, context: Any) -> Any:
    r"""The part of `context` this command is allowed to be narrowed by.

    Returns None when there is nothing to narrow by - which is the same thing
    `distinct_values` is handed for an unfiltered query, so the unscoped shape
    stays exactly as it was.

    **Built by blanking the fields, not by re-parsing.** A second parser is the
    failure this order's opening paragraph names, and `ParsedQuery` is frozen,
    so `replace` on the fields `scoped_by` does *not* mention is the honest way
    to say "only these".
    """
    if context is None or not getattr(command, "scoped_by", ()):
        return None

    allowed = {name for spelling in command.scoped_by
               for name in _SCOPE_FIELDS.get(spelling, ())}
    if not allowed:
        return None

    try:
        blanks = {field: neutral for field, neutral in _FILTER_FIELDS.items()
                  if field not in allowed}
        narrowed = replace(context, **blanks)
    except Exception as exc:                     # noqa: BLE001 - a menu, not a search
        _log.debug("could not narrow the value scope: {}", exc)
        return None

    return narrowed if getattr(narrowed, "has_filters", False) else None


#: Which `ParsedQuery` fields each `scoped_by` name owns. The catalogue speaks
#: in command names; the parser speaks in field names, and this is the one
#: place the two are put side by side.
_SCOPE_FIELDS: dict[str, tuple[str, ...]] = {
    "repo": ("repos",),
    "type": ("ext",),
    "after": ("after",),
    "before": ("before",),
    "path": ("paths",),
    "from": ("senders",),
    "to": ("recipients",),
}

#: Every filter field, with the value that means "no restriction".
#:
#: **This must mirror `ParsedQuery.has_filters`**, and a test asserts it,
#: because the two are answering the same question from opposite ends: that
#: property lists what counts as a filter, and this lists how to remove one.
#:
#: `scope` is the one that is not empty when it is neutral - it is `"all"`, and
#: the first version of this blanked it to `""`, which `has_filters` then read
#: as a filter. Narrowing `/type` by a `path:` that `scoped_by` does not permit
#: produced a query with no filters in it that nonetheless claimed to have one.
_FILTER_FIELDS: dict[str, Any] = {
    "ext": (),
    "after": None,
    "before": None,
    "paths": (),
    "repos": (),
    "senders": (),
    "recipients": (),
    "subjects": (),
    "names": (),
    "sizes": (),
    "has_attachment": None,
    "scope": "all",
}


#: A value row: the value, then whatever is worth knowing about it.
#:
#: The same shape the command rows use (`_ROW` in `command_popup`), because the
#: two lists sit in the same popup and a second alignment would read as a bug.
VALUE_ROW = "{value:<30} {meta}"

#: What a count counts, per value source. `dave@acme.com  316 files` would be
#: wrong in a way somebody would notice and not be able to explain.
VALUE_NOUNS: dict[str, str] = {
    "sender": "messages",
    "repo": "files",
    "ext": "files",
    "folder": "files",
    # **Adoptions §3, and the noun is the whole honesty of the row.** The
    # number beside a saved search is how many times it has been run, not how
    # many files it would find - counting the second would mean running every
    # saved search behind a keystroke. `invoices   12 runs` says what it is;
    # `invoices   12 files` would be a number somebody would believe and act
    # on, and it would be wrong.
    "saved": "runs",
}


#: The row that leaves the value tier and hands the box back to the person.
#:
#: `3b`: "which is what happens today, made explicit". Typing a date by hand
#: has always worked; nothing said so, so the menu looked like the only way in.
CUSTOM_ROW = "custom…"


def kind_expansion(value: str) -> tuple[str, ...]:
    r"""The extensions a `/type` kind word stands for, or `()`.

    `3a`'s second page. Read from `_EXT_GROUPS` - the parser's own table, which
    is what `type:excel` already expands to - so the page cannot offer a
    spelling the filter would then not match. A second copy of this mapping is
    exactly the "nested sub-key dictionary" this order rules out.
    """
    from app.search.query import _EXT_GROUPS

    return tuple(_EXT_GROUPS.get(str(value or "").strip().lower(), ()))


def value_page(name: str, chosen: str = "", *, resolve: Any = None) -> str:
    """The breadcrumb above a second value page, or `""` on the first.

    Without it the second page is a list of extensions with nothing saying
    which kind they belong to or how to get back - and this order's whole
    subject is a menu that does not say what it is answering.
    """
    if not chosen:
        return ""
    if not kind_expansion(chosen):
        return ""
    return f"/{name} {chosen} — Backspace to go back"


def value_row(value: str, *, count: Optional[int] = None, exact: bool = True,
              noun: str = "files", hint: str = "") -> str:
    """One row of the value menu.

    **The count is shown only when it is exact.** A scoped menu counts the
    first `VALUE_SAMPLE` matching rows, and at that ceiling the number is a
    fraction of the truth - `pdf   200 files` for a corpus holding five
    thousand. Omitting it loses information; printing it states something
    false, and this project's rule is that a label says what is so.

    `hint` wins when both are available, because a date's resolved range says
    more than a count of the files that would match it.
    """
    text = str(value or "")
    meta = str(hint or "")
    if not meta and count is not None and exact:
        number = int(count)
        # "1 files" is the kind of thing that makes a careful interface look
        # careless, and this row sits under somebody's cursor.
        word = noun[:-1] if number == 1 and noun.endswith("s") else noun
        meta = f"{number:,} {word}"
    if not meta:
        return text
    return VALUE_ROW.format(value=text, meta=meta).rstrip()


def value_rows(name: str, values: Sequence[Any], *, resolve: Any = None,
               today: Any = None) -> list[str]:
    r"""The value menu's rows, from either strings or `ValueCount`s.

    Both, because the menu is filled twice: once instantly from the grammar
    with no store, and again from the index on a worker. The first pass has no
    counts to show and must not wait for any.

    Qt-free, so the wording is testable without a display.
    """
    if resolve is None:
        from app.search.commands import command_for as resolve

    command = resolve(name)
    is_date = bool(getattr(command, "is_date", False))
    noun = VALUE_NOUNS.get(getattr(command, "source", "") or "", "files")

    rows = []
    for entry in values or ():
        # **`isinstance` before `getattr`.** A plain string has a `.count` -
        # the method - so `getattr(entry, "count", None)` returns a callable
        # for every value on the instant pass, and `f"{int(count):,}"` then
        # raises. The two shapes are told apart by what they are, not by which
        # attributes they happen to answer to.
        plain = isinstance(entry, str)
        value = entry if plain else str(getattr(entry, "value", entry))
        rows.append(value_row(
            value,
            count=None if plain else getattr(entry, "count", None),
            exact=True if plain else bool(getattr(entry, "exact", True)),
            noun=noun,
            hint=resolved_date(value, today=today) if is_date else "",
        ))
    return rows


def as_typed_value(value: str) -> str:
    r"""A value, spelled so the parser reads it back as one value.

    **Quoted when it contains a space**, because the tokenizer splits on
    whitespace: picking `last month` from the menu inserted `after:last month`,
    which parses as `after:last` - not a date, so no filter at all - plus a
    loose search for the word *month*. Somebody chose a date from a list and
    got a query that filtered nothing and searched for the wrong thing, with
    nothing on screen to say so.

    Found while building the resolved-date hint (`2c`), which is the point of
    that hint: showing what a value resolves to is how a value that resolves to
    nothing becomes visible.
    """
    text = str(value or "").strip()
    if not text or ('"' in text):
        return text
    return f'"{text}"' if any(c.isspace() for c in text) else text


def resolved_date(value: str, *, today: Any = None) -> str:
    r"""What a date value actually means, for the hint beside it.

    `/after 30d   (since 28 Jul)`. Qt-free and here rather than in the widget,
    so the wording can be checked without a display - the same reason the rest
    of the menu's decisions live in this file.

    `""` when the value is not a date the parser accepts, which is the honest
    answer and is what tells somebody that `after:lst week` is not going to do
    what they meant.
    """
    from app.search.query import _parse_date

    text = str(value or "").strip().strip('"')
    if not text:
        return ""
    try:
        found = _parse_date(text, today=today)
    except Exception:                            # noqa: BLE001 - a hint
        return ""
    if found is None:
        return ""
    # **No `%-d`.** That is a glibc extension: it strips the leading zero on
    # Linux and raises `ValueError` on Windows, which is the only platform this
    # ships to. The day is formatted by hand instead.
    return f"(since {found.day} {found:%b %Y})"


def scope_key(name: str, context: Any, resolve: Any = None) -> str:
    """A stable string for the scope `name`'s values would be fetched under.

    The TTL cache in the popup is keyed on this alongside the command name.
    Keyed on the name alone, a global answer fetched for `/from` would be
    served under `repo:leasha from:` for the next two minutes - and a wrong
    answer with a lifetime is worse than a slow one, because nothing about it
    looks wrong.

    Built from the *narrowed* scope rather than from the whole query, so typing
    more free text after a filter does not throw the cache away for no reason.
    """
    if resolve is None:
        from app.search.commands import command_for as resolve

    command = resolve(name)
    if command is None:
        return ""
    scope = scope_for(command, context)
    if scope is None:
        return ""
    return "|".join(
        f"{field}={getattr(scope, field, None)!r}"
        for field in sorted(_FILTER_FIELDS)
        if getattr(scope, field, None) != _FILTER_FIELDS[field]
    )


def _scope_repo(scope: Any) -> str:
    """The single repository a scope names, if it names exactly one."""
    names = tuple(getattr(scope, "repos", ()) or ())
    return str(names[0]) if len(names) == 1 else ""


def _takes_repo(lookup: Any) -> bool:
    """Does this reader accept the repository argument it implicitly wanted?

    Asked rather than assumed, because `lookup` is injected by three callers
    and one of them is a test double.
    """
    import inspect

    try:
        return "repo" in inspect.signature(lookup).parameters
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class RepoFilter:
    """What the Code tab's box asked for, once the grammar has read it."""

    #: `repo:` values, lower-cased. Empty means "any repository".
    names: tuple[str, ...] = ()
    #: Free text, matched against what is on screen.
    text: str = ""
    #: `type:` values, lower-cased and without dots. Honoured because the tree
    #: lists files: before it did, this was in `ignored`, and offering `/type`
    #: on a list with no file in it would have been an offer nothing kept.
    exts: tuple[str, ...] = ()
    #: Operators this list cannot answer, named so the tab can say so rather
    #: than filtering to nothing and looking broken.
    ignored: tuple[str, ...] = ()

    def matches(self, name: str, haystack: str) -> bool:
        """A repository row. `exts` is deliberately not applied here.

        Which extensions a repository contains is not known until its files are
        loaded, and hiding it on a guess would remove the row somebody was about
        to expand. `type:` narrows the children instead, and the summary says
        so - see `matches_file`.
        """
        if self.names and name.lower() not in self.names:
            return False
        return not self.text or self.text in haystack

    def matches_file(self, ext: str, haystack: str) -> bool:
        """A file row under a repository that has already passed `matches`."""
        if self.exts and (ext or "").lower().lstrip(".") not in self.exts:
            return False
        return not self.text or self.text in haystack


def repo_visibility(
    chosen: "RepoFilter",
    repo_name: str,
    repo_haystack: str,
    files: Sequence[tuple[str, str]] = (),
) -> tuple[bool, list[bool]]:
    """Which repository row and which of its file rows survive the filter.

    Returns `(show the repository, one flag per file)`. Pure, so the rule can be
    argued with in a test rather than inferred from a tree on screen.

    Three decisions are worth stating, because each has an obvious-looking
    alternative that reads worse:

    * **`repo:` hides the whole subtree.** Naming a repository is a statement
      about which repository you want, so the others go entirely.
    * **A repository whose name does not match stays if one of its files does.**
      Typing "readme" should find the README, and hiding its parent would hide
      the answer. This is what makes the tree searchable rather than merely
      collapsible.
    * **A repository that matches shows all its files.** Having found `leasha`
      by typing "leasha", being shown only the files with "leasha" in the name
      is a second filter nobody asked for.
    """
    if chosen.names and repo_name.lower() not in chosen.names:
        return False, [False] * len(files)

    matched_name = not chosen.text or chosen.text in repo_haystack
    if matched_name:
        # Only `type:` narrows the children now; the text has done its work.
        flags = [not chosen.exts or (ext or "").lower().lstrip(".") in chosen.exts
                 for ext, _haystack in files]
        return True, flags

    flags = [chosen.matches_file(ext, haystack) for ext, haystack in files]
    return any(flags), flags


def repo_filter(text: str) -> RepoFilter:
    """Read the Code tab's filter box through the one grammar.

    **The same parser as everywhere else**, so `/repo leasha` means here what it
    means in the search box, and nothing had to be invented for this tab.

    A repository list cannot answer `from:` or `after:`, so those are reported
    rather than silently dropped: a filter that quietly ignores half of what was
    typed produces an empty list and no explanation, which reads as the tab
    being broken.
    """
    from app.search.commands import expand_slashes
    from app.search.query import parse_query

    parsed = parse_query(expand_slashes(text or ""))

    ignored = tuple(sorted({
        name for name, value in (
            ("from", parsed.senders), ("to", parsed.recipients),
            ("subject", parsed.subjects),
            ("path", parsed.paths), ("after", parsed.after),
            ("before", parsed.before), ("size", parsed.sizes),
            ("has", parsed.has_attachment),
        ) if value
    }))
    # `name:` is free text here. The tree matches on the name column already,
    # so `/name utils` and typing `utils` should not behave differently.
    words = " ".join((*parsed.terms, *parsed.names)).strip() or (parsed.text or "")
    return RepoFilter(
        names=tuple(name.lower() for name in parsed.repos),
        text=words.strip().lower(),
        exts=tuple(str(e).lower().lstrip(".") for e in parsed.ext),
        ignored=ignored,
    )


def repo_filter_summary(
    chosen: "RepoFilter", *, shown: int, total: int, files: int
) -> str:
    """The line above the Code table, whatever the filter is doing.

    **An ignored operator is named, not swallowed.** `from:dave` cannot mean
    anything to a list of repositories; filtering to nothing and saying "no
    matches" would send somebody looking for a repository that is right there.
    Mail says the same thing about free text for the same reason.
    """
    note = ""
    if chosen.ignored:
        spelled = ", ".join(f"{name}:" for name in chosen.ignored)
        note = (
            f"  ·  {spelled} ignored - this list has repositories and their "
            "files. Press Enter on a repository to search inside it."
        )
    if chosen.exts:
        # Said plainly, because a repository stays visible under `type:` even
        # when none of its files match - the tree cannot know until it is
        # expanded. Without this line an empty repository would look like a bug.
        kinds = ", ".join(f".{ext}" for ext in chosen.exts)
        note += f"  ·  expanding a repository shows its {kinds} files only"

    if not chosen.names and not chosen.text:
        return repo_summary(total, files) + note
    if not shown:
        return f"No repository matches that.{note}"
    return f"{format_count(shown)} of {format_count(total)} repositories{note}"


# ---------------------------------------------------------------------------
# Search notices
# ---------------------------------------------------------------------------

def notice_line(notices: Any) -> str:
    """One line for the notice bar, or "" when the search was healthy.

    **Here rather than in the widget, so it can be tested.** `QtWidgets` needs
    a display, and the last time UI logic was verified by reading it instead of
    running it, `QPdfView()` shipped without its parent argument and crashed
    the window on startup. Everything that decides *what* is shown lives in
    this module; the widget only draws it.

    Reads `message` and nothing else. The wording is the backend's and is free
    to change; `code` is what anything branching must use, and there is a test
    asserting the UI never parses a message string to decide anything.
    """
    if not notices:
        return ""
    messages = [
        str(getattr(notice, "message", "")).strip()
        for notice in notices
        if str(getattr(notice, "message", "")).strip()
    ]
    if not messages:
        return ""
    # Two spaces between, not a newline: the bar is one line that wraps, and a
    # hard break makes a single notice and two notices look like different
    # kinds of thing.
    return "  ".join(f"\u26a0 {message}" for message in messages)


# ---------------------------------------------------------------------------
# The Code tab's wording and row shapes
#
# Here rather than in `code_view.py` for the reason every other decision is:
# what a summary says and what a git result looks like as a row are choices,
# and a choice made inside a widget is one nobody can test without a display.
# ---------------------------------------------------------------------------

def repo_root_for(repos: Iterable[Mapping[str, Any]], name: str) -> str:
    """The folder for a repository name, or the only one there is.

    **Only one is a fact; several is a question.** With no name given and
    exactly one repository known, that is plainly the one meant. With several,
    picking the first would be a guess presented as an answer - so it returns
    nothing and the caller asks.
    """
    wanted = (name or "").strip().lower()
    rows = list(repos or ())
    for row in rows:
        if wanted and wanted in str(row.get("name", "")).lower():
            return str(row.get("root_path", "") or "")
    if not wanted and len(rows) == 1:
        return str(rows[0].get("root_path", "") or "")
    return ""


def preset_label(preset: str) -> str:
    """A preset's name, in words. `""` for one nobody configured."""
    from app.core.code_types import PRESET_LABELS

    return PRESET_LABELS.get(str(preset or ""), "your code-type filter")


def repo_list_empty(*, indexed_files: int, hidden: int, has_query: bool,
                    preset: str = "") -> str:
    r"""Why this repository's list is empty. Three states, three sentences.

    From `WORKORDER-202626081149-code-tab.md` §5. All three used to render
    identically, as nothing:

    * the repository has no indexed files at all - its folder is probably not
      under an indexed root, which is a *configuration* problem;
    * it has files and the type filter hid every one - a *setting* problem, and
      the number is the thing that makes it obvious;
    * it has matching files and the query excluded them - which is search
      working correctly.

    `repo_empty_state` already does exactly this one level up, for *no
    repositories at all*, and its docstring explains why a generic "no results"
    would waste the answer that matters. The same care had not been applied
    here.
    """
    if indexed_files <= 0:
        return ("No indexed files in this repository.\n\n"
                "Its folder may not be under an indexed root — add the folder "
                "above it in Settings, then index again.")
    if hidden and hidden >= indexed_files:
        return (f"0 of {indexed_files:,} shown — all hidden by "
                f"{preset_label(preset).lower()}.\n\n"
                f"Change it with the code-types button above.")
    if has_query:
        return "No file matches that query in this repository."
    return "No files to show."


def code_preset(store: Any) -> str:
    """Which code-type preset is in force. Never raises - it is a caption.

    Reads `choice_from` rather than a second state key, so the caption cannot
    name a preset the filter is not using.
    """
    from app.core.code_types import choice_from

    preset, _chosen = choice_from(store)
    return preset


def code_summary(rows: list[Any], repos: list[Any], scope: Any = None,
                 *, preset: str = "") -> str:
    r"""What is on screen, and what there is. Both, because "40 files" over a
    corpus of 48,000 and over one of 40 mean different things.

    **And what it is scoped to**, when the tree has narrowed it. A list showing
    a branch with nothing on screen saying so is the same failure as a skipped
    archive that says nothing: the numbers look ordinary and mean something
    else. `git ls-tree` also answers from the repository rather than the index,
    so "2 files" beside "48,000 indexed" would otherwise be simply confusing.
    """
    total = sum(int(row.get("files", 0) or 0) for row in repos)
    count = len(repos)
    # **The arithmetic is done here, not handed in.** The view had to compute
    # it, which put a sum and a `max(0, ...)` into a module the length guard
    # keeps short - and the numbers it needs are the two already passed.
    hidden = max(0, total - len(rows)) if preset else 0
    parts = [f"{len(rows):,} file{'s' if len(rows) != 1 else ''}"]
    # **The arithmetic, whenever a type filter is hiding something.**
    #
    # `DEFAULT_PRESET` is `build`, which excludes `.md`, `.txt`, `.json`, `.yml`
    # and `.csv` - so `README.md`, `package.json` and `requirements.txt` are
    # filtered out of the list on a fresh install and nothing on screen says a
    # filter is active. `code_type_filter`'s own docstring warns that "a Code
    # tab that has silently hidden a language is far harder to notice than one
    # showing a stray PDF", and then the default did exactly that. Combined
    # with a repository holding no indexed files it is the second reason the
    # owner saw an empty list and could not tell why.
    if hidden > 0:
        parts.append(f"{hidden:,} hidden by {preset_label(preset).lower()}")
    if scope is not None and str(getattr(scope, "kind", "")):
        parts.append(scope.describe())
    if len(rows) >= REPO_FILE_LIMIT:
        parts.append(f"showing the newest {REPO_FILE_LIMIT:,} — narrow it with "
                     f"/repo or /type")
    parts.append(f"{count:,} repositor{'ies' if count != 1 else 'y'}, "
                 f"{total:,} indexed file{'s' if total != 1 else ''}")
    return "  ·  ".join(parts)


def git_summary(found: Any) -> str:
    parts = [f"{len(found.rows):,} result{'s' if len(found.rows) != 1 else ''} "
             f"{found.explain}", f"{found.elapsed_s:.2f}s"]
    if found.truncated:
        parts.append("stopped at the limit — narrow it with /path, /extension "
                     "or /depth")
    if not found.rows:
        parts.append(f"command: {' '.join(found.command)}")
    return "  ·  ".join(parts)


def git_result_row(row: Any, repo_root: str) -> Any:
    """One git result, in the shape the table and the preview already read.

    A hit in the *checkout* has a file to open and preview; a hit in history
    does not - the version that matched no longer exists on disk. Handing the
    pane a path that is not there would show "file missing" for every history
    result, which reads as a broken preview rather than as a file that is
    genuinely gone.
    """
    historical = bool(row.commit) and row.kind != "content"
    full = ("" if historical or not row.path
            else str(Path(repo_root) / row.path) if repo_root else row.path)
    where = row.path or ""
    if row.line_no:
        where = f"{where}:{row.line_no}"

    name = row.path.rsplit("/", 1)[-1] if row.path else (row.subject or row.commit[:8])
    return RepoFileRow(
        name=name,
        # A history row has no size on disk: the version that matched is gone,
        # and a number invented for the column would be a number somebody
        # believes.
        size="",
        repo=row.commit[:8] if row.kind == "commit" else (row.author or ""),
        kind={"commit": "commit", "change": row.status or "change"}.get(
            row.kind, row.status or "line"),
        seen=row.date or "",
        path=where,
        full_path=full,
        preview_text=(f"{row.subject}\n\n{row.author}   {row.date}   {row.commit}"
                      if row.kind == "commit" else row.text),
    )


# ---------------------------------------------------------------------------
# §3b — the filters the rules recognised, offered beside what was typed
# ---------------------------------------------------------------------------

def chips_for(store: Any, sentence: str, policy: Any = None) -> tuple:
    r"""Filters the rules translator read out of a sentence. **Never raises.**

    **Here rather than in the engine, and that is a rule not a preference.**
    `test_the_search_engine_cannot_reach_the_translator` forbids `engine.py`
    from knowing translation exists, because the retrieval path must never be
    able to spend a second on a model. The first version of this put chips on
    the response and tripped that guard - the same trap §1 hit with a module
    called `translate`, and the guard was right both times. Chips are a thing
    said *about* a query, not part of running one, so they belong with the
    translator, on the presenter side, where the Interpret button already is.

    **The policy still decides**, so no view carries a rule of its own: this
    reads `auto_chips` exactly as the engine reads the other five behaviours.

    Deterministic and offline - no model, no network - which is what makes
    this the half of Interpret that works on every machine.
    """
    if policy is not None and not getattr(policy, "auto_chips", True):
        return ()
    try:
        from app.search.translate_rules import read

        return tuple(read(sentence, store).chips)
    except Exception as exc:                       # noqa: BLE001 - a helper
        _log.debug("no filter chips for this query: {}", exc)
        return ()


# ---------------------------------------------------------------------------
# Adoptions §1 — "why is this result here?"
# ---------------------------------------------------------------------------

#: How recent a document has to be before recency is worth mentioning.
#:
#: 0.5 is one half-life - about six months. Below that the blend contributed
#: almost nothing and saying so would be **noise dressed as an explanation**,
#: which is the failure this whole feature is trying to avoid.
RECENT_ENOUGH = 0.5


def _matched_words(result: Any, parsed: Any) -> tuple:
    """Which of the typed words actually appear in this passage.

    **Read off the text rather than inferred from the score**, because that
    is the difference between a fact and a guess - and a guess in the
    explanation is worse than no explanation at all.
    """
    text = str(getattr(result, "text", "") or "").lower()
    if not text:
        return ()
    found = []
    seen = set()
    for term in getattr(parsed, "terms", ()) or ():
        # **Shown as typed.** `SearchEngine` handed back as `searchengine`
        # reads as a correction of something that was not wrong, which is the
        # same discourtesy the recent-searches list was careful to avoid.
        typed = str(term or "").strip().strip("*")
        word = typed.lower()
        if len(word) > 1 and word in text and word not in seen:
            seen.add(word)
            found.append(typed)
    return tuple(found)


def _when(mtime_ns: Any) -> str:
    """A date somebody can read, or "" if there is none to show."""
    try:
        stamp = int(mtime_ns or 0)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    from datetime import datetime

    return datetime.fromtimestamp(stamp / 1_000_000_000).strftime("%d %B %Y")


def why_result(result: Any, parsed: Any = None, *, fold: Any = None,
               opens: int = 0, register: str = "plain") -> tuple:
    r"""The plain-words reasons this result is on the page. **Never raises.**

    Adoptions §1, and the constraint is the whole point: **state facts, never
    scores.** Every line below is read from something already recorded -
    which retriever found it, whether the text contains the typed words, the
    freshness the blend used, the usage log, the fold - and no line invents a
    number or a percentage. "87% relevant" is a sentence nobody can check and
    everybody would believe.

    An empty tuple is a legitimate answer: a plain keyword hit with nothing
    else to say about it should say nothing rather than pad.

    `register` follows the notice pattern - `plain` for the everyday tab,
    `technical` where the power surfaces have earned the detail.
    """
    try:
        lines: list = []
        technical = str(register or "plain").lower() == "technical"

        words = _matched_words(result, parsed)
        if words:
            lines.append("Your words are in it: " + ", ".join(words))

        sources = tuple(getattr(result, "sources", ()) or ())
        if 0 in sources and 1 in sources:
            lines.append("Found both by your words and by meaning - the "
                         "strongest signal this search has.")
        elif sources == (1,):
            # **Said out loud, because it looks like a mistake otherwise.**
            # A row with none of the typed words in it reads as a bug to
            # somebody who does not know the search understands meaning.
            lines.append("Found by meaning rather than by matching your "
                         "words.")
        elif sources == (0,) and not words:
            lines.append("Your words are in this file, though not in the "
                         "part shown here.")

        if getattr(result, "declares", False):
            lines.append("This is where it is defined, not just used.")

        freshness = float(getattr(result, "recency", 0.0) or 0.0)
        if freshness >= RECENT_ENOUGH:
            # Shot date before copy date - the same substitution `to_row`
            # makes, so the date named here agrees with the one on the row.
            when = _when(getattr(result, "taken_at_ns", 0) or getattr(result, "mtime_ns", 0))
            lines.append(
                f"Recent, so it came slightly ahead of equally good older "
                f"ones{f' - {when}' if when else ''}.")

        if opens > 0:
            # **A fact about them, not a score.** "You have opened this
            # before" is checkable; "popular" is not.
            lines.append("You have opened this before."
                         if opens == 1 else
                         f"You have opened this {opens} times before.")

        older = tuple(getattr(fold, "older", ()) or ()) if fold else ()
        if older:
            lines.append(f"{len(older)} other cop{'y' if len(older) == 1 else 'ies'}"
                         f" of this were folded into this row.")

        if technical and getattr(result, "rerank_score", None) is not None:
            lines.append("Reranked by the cross-encoder.")
        return tuple(lines)
    except Exception as exc:                       # noqa: BLE001 - a courtesy
        _log.debug("no explanation for this result: {}", exc)
        return ()


def explain_for(result: Any, parsed: Any = None, policy: Any = None, *,
                fold: Any = None, opens: int = 0) -> tuple:
    """`why_result`, with the switch and the register read off the policy.

    **The policy decides here rather than in the view**, exactly as
    `chips_for` does - so no surface carries a rule of its own and the
    off-switch cannot be honoured in three places and forgotten in a fourth.
    """
    if policy is not None and not getattr(policy, "explain_results", True):
        return ()
    register = str(getattr(policy, "notice_register", "plain") or "plain")
    return why_result(result, parsed, fold=fold, opens=opens,
                      register=register)


# ---------------------------------------------------------------------------
# Adoptions §2 — how it matched, not only that it did
# ---------------------------------------------------------------------------

#: What a result says about the way it was found. **Words, never colour
#: alone**: the accessibility rule this codebase already applies to the focus
#: ring applies here too, and a marker that is only a hue is a marker several
#: people on any given day cannot see.
MEANING_MARKER = "meaning match"


def match_marker(row: Any, policy: Any = None) -> str:
    r"""`"meaning match"` for a result no typed word appears in, else `""`.

    **A keyword hit keeps today's highlight and says nothing extra.** The
    marker exists for the row that has none of the person's words in it,
    which reads as a mistake to anybody who does not know the search
    understands meaning - and adding a badge to every row would make the one
    that matters invisible.

    Rides `explain_results`, deliberately: this is the shortest possible
    answer to *why is this here*, and a separate eighth switch for one word
    would be a preference nobody could tell apart from the seventh.
    """
    if policy is not None and not getattr(policy, "explain_results", True):
        return ""
    lanes = tuple(getattr(row, "sources", ()) or ())
    return MEANING_MARKER if lanes == (1,) else ""


@dataclass(frozen=True, slots=True)
class VolumeRow:
    """One Offline Media source, for the tab's list. 2a: name, status, size,
    counts, snapshot date."""

    volume_id: int
    name: str
    kind: str            # "drive" | "network" | "cloud" | "phone" | "archived"
    status: str           # the sentence the row shows - see `volume_rows`
    status_code: str      # ONLINE | OFFLINE | LOCKED, raw - for an icon or colour
    size: str             # "128.4 GB"
    files: str             # "48,301"
    scanned: str           # "12 Nov 2025", or "never" before a first Scan finishes
    description: str
    #: Unformatted, for a table that wants to sort on the real value.
    file_count: int = 0
    size_bytes: int = 0
    last_scanned_at: int = 0
    last_seen: int = 0


def volume_rows(rows: Iterable[Mapping[str, Any]],
                online: Optional[Mapping[int, Any]] = None, *,
                now: Optional[float] = None) -> list[VolumeRow]:
    r"""Store rows to display rows for the Offline Media tab.

    `online` is `app.index.offline_media.connected_volumes(store)`'s result -
    fetched on a worker, never here, because it is a live Windows volume
    enumeration and this function must stay a pure formatter like every
    other `*_rows` in this module. `status_code` is read from the row's own
    `status` column, which `refresh_volume_statuses` already wrote before
    this is called - this function only turns a code into the sentence a
    person reads; it never decides ONLINE/OFFLINE/LOCKED itself.

    **"online as F:" - the letter shown as a transient fact only (2a).** It
    is read from `online`, not stored anywhere, and a rescan a moment later
    at a different letter simply reads differently next time this runs.
    """
    online = online or {}
    out: list[VolumeRow] = []
    for row in rows:
        volume_id = int(row.get("id", 0) or 0)
        status_code = str(row.get("status") or "OFFLINE").upper()
        root = online.get(volume_id)
        seen = int(row.get("last_seen") or 0)
        if status_code == "ONLINE" and root is not None:
            letter = str(root).rstrip("\\/") or str(root)
            status = f"Online as {letter}"
        elif status_code == "LOCKED":
            status = "Locked (BitLocker)"
        elif seen:
            status = f"Offline - last seen {format_when(seen * 1_000_000_000, now=now)}"
        else:
            status = "Offline"
        scanned_at = int(row.get("last_scanned_at") or 0)
        count = int(row.get("indexed_files", row.get("file_count", 0)) or 0)
        out.append(VolumeRow(
            volume_id=volume_id,
            name=str(row.get("name", "")),
            kind=str(row.get("kind", "")),
            status=status,
            status_code=status_code,
            size=format_size(int(row.get("size_bytes") or 0)),
            files=format_count(count),
            scanned=format_when(scanned_at * 1_000_000_000, now=now) if scanned_at else "never",
            description=str(row.get("description") or ""),
            file_count=count,
            size_bytes=int(row.get("size_bytes") or 0),
            last_scanned_at=scanned_at,
            last_seen=seen,
        ))
    return out


def offline_media_empty_state() -> str:
    r"""2a's empty list: what to type and press, before there is anything to show."""
    return (
        "No drives catalogued yet. Press “Scan a drive…” and choose "
        "the drive or folder to catalogue - nothing happens to any drive until "
        "you do."
    )


def delete_volume_confirmation(name: str, file_count: int) -> tuple[str, str]:
    r"""2c's confirmation text: the count, and the crucial sentence, verbatim.

    A `(title, body)` pair so the dialog and any headless caller (a test, a
    future CLI `--yes` prompt) render the identical words - the wording
    lives here once, the way every notice and error message in this project
    does, rather than typed again at each call site.
    """
    title = f"Forget {name!r}?"
    body = (
        f"This removes {file_count:,} file(s) catalogued under {name!r} "
        f"from Leasha's index.\n\n"
        "This removes the catalogue from Leasha's index. Nothing on the "
        "drive itself is touched."
    )
    return title, body


def offline_media_run_summary(result: Any) -> str:
    r"""The status-bar sentence after a Scan, Rescan or Delete completes -
    2a's whole "what happened" story, since the tab shows no progress bar
    while one runs. **Always names an amount**, the same rule
    `cleared_message` follows: a silent success is indistinguishable from
    nothing having happened.
    """
    if not isinstance(result, dict):
        return "Done."
    if "deleted" in result:
        name = result.get("name") or "that source"
        files = int(result.get("files") or 0)
        return (f"Forgot {name!r}: {files:,} file(s) removed from the index. "
                "Nothing on the drive itself was touched.")
    stats = result.get("stats")
    indexed = int(getattr(stats, "indexed", 0) or 0)
    seen = int(getattr(stats, "seen", 0) or 0)
    deleted = int(getattr(stats, "deleted", 0) or 0)
    moved = int(result.get("moved") or 0)
    pieces = [f"{indexed:,} new/changed document(s)", f"{seen:,} file(s) seen"]
    if moved:
        pieces.append(f"{moved:,} moved on disk and repaired without re-extraction")
    if deleted:
        pieces.append(f"{deleted:,} row(s) removed for files genuinely gone")
    return "Scanned: " + ", ".join(pieces) + "."


def resolve_open_path(store: Any, row: Any) -> str:
    r"""The real path to open for one result row. **Worker only.**

    Straight through for an ordinary file. For one on a catalogued volume
    (Offline Media, order 202626270513), `row.path` is never a real
    filesystem path - it is the letter-free key `volume_synthetic_path`
    builds - and 1b requires resolution through the volume's *current*
    mount point, every time, which means a live Windows volume check and
    therefore never the interface thread.

    Raises `AppErrorException` (`ERR_FILE_CORRUPT`) when a volume-backed
    row's volume is not connected right now - the same code
    `open_in_explorer` already raises for an ordinary missing file, so
    "cannot reach it" reads the same way in the error box regardless of
    which kind of unreachable produced it.
    """
    volume_id = getattr(row, "volume_id", None)
    if volume_id is None:
        return str(getattr(row, "path", ""))

    from app.core.errors import raise_error
    from app.index.offline_media import resolve_file_path

    resolved = resolve_file_path(store, row)
    if resolved is None:
        raise_error(
            "ERR_FILE_CORRUPT", "ui.open",
            path=str(getattr(row, "path", "")),
            suggestion="This file is on a catalogued drive that is not "
                      "plugged in right now. Plug it in and try again.",
            details="Volume not currently connected.",
        )
    return str(resolved)
