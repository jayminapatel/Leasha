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
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

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
