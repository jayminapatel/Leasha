"""Cutting a chunk down to a snippet centred on the match.

Layer: L5. Part of the presenter package; imports no Qt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence

#: Characters shown per result. Enough to judge relevance, short enough that ten
#: results fit on a screen.
SNIPPET_CHARS = 240


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


def term_spans(text: str, terms: Sequence[str], *, utf16: bool = False) -> list[tuple[int, int]]:
    r"""Where the searched words are in `text`: `(start, end)` pairs, in order.

    Order 0y section 4b: the preview highlights the same characters a result's
    snippet does, so this is `_term_pattern` over the whole text rather than a
    second rule. Unlike `build_snippet` the text is not flattened first - the
    positions must be the document's own.

    `utf16=True` gives the positions Qt counts in. A `QTextCursor` position is
    a UTF-16 unit, and a character outside the basic plane (most emoji) is two
    of those and one Python character, so after one every highlight would sit a
    character early.
    """
    if not text:
        return []
    pattern = _term_pattern(terms)
    if pattern is None:
        return []
    spans = [(match.start(), match.end()) for match in pattern.finditer(text)
             if match.end() > match.start()]
    if not utf16 or not spans or text.isascii() or max(text) <= "￿":
        return spans
    offsets = [0]
    for character in text:
        offsets.append(offsets[-1] + (2 if character > "￿" else 1))
    return [(offsets[start], offsets[end]) for start, end in spans]


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
    """The opening of `text`, cut at a boundary, for a passage with no term to centre on."""
    if len(text) <= width:
        return Snippet(text)
    end = _snap_forward(text, width)
    return Snippet(text[:end], elided_end=end < len(text))
