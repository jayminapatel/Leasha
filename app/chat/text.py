r"""Sentences, tokens and quotes: the text handling every other chat module shares.

Layer: L8b - pure functions, no Qt, no network, no store.

Two jobs, and both exist because the verification guarantee rests on them:

**Sentences with offsets.** `sentence_spans` cuts a passage into sentences and
says *where* each one starts and ends, so a quote can be printed as
`text[start:end]` - a slice of the indexed text, which is verbatim by
construction - instead of as a string somebody typed.

**Sentence-boundary snapping** (work order section 4e-1). A span is widened or
narrowed to real sentence boundaries before it is printed: never mid-word,
preferring whole sentences. Snapping only ever moves an edge to another position
in the same text, so the result is always a substring of the chunk. A quote that
reads like a sentence is trusted like one; a quote that starts "…ceed the
deposit by" is not, and is not shown.

`content_tokens` is the cheap comparison that "is this sentence supported by that
passage" is built on. It is deliberately dull - lower-case, no stop words, a very
light stem - because everything downstream is a threshold on it, and a threshold
on something clever is a threshold nobody can explain.
"""

from __future__ import annotations

import re
from typing import Iterable

__all__ = [
    "sentence_spans",
    "sentences",
    "snap_span",
    "content_tokens",
    "stem",
    "normalise_space",
    "fold_quotes",
    "STOPWORDS",
    "DEFAULT_QUOTE_MAX",
]

#: The longest a printed quote may be, in characters. About three lines of a
#: result card; longer than that stops being a receipt and starts being a page.
DEFAULT_QUOTE_MAX = 320

#: Words that end in a full stop without ending a sentence.
_ABBREVIATIONS = frozenset("""
mr mrs ms dr prof sr jr st no nos vs etc fig figs approx inc ltd co corp dept
est ref refs vol pp cf viz al ca
jan feb mar apr jun jul aug sep sept oct nov dec
e.g i.e
""".split())

_TERMINATORS = ".!?"
_CLOSERS = "\"'”’)]}»"
_OPENERS = "\"'“‘([{«"
_BULLET = re.compile(r"^\s*(?:[-*•–]|\d{1,3}[.)])\s+")


def normalise_space(text: str) -> str:
    """Runs of whitespace collapsed to one space, ends trimmed."""
    return " ".join(str(text or "").split())


_QUOTE_FOLD = str.maketrans({
    "“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "‚": "'",
    "–": "-", "—": "-", " ": " ",
})


def fold_quotes(text: str) -> str:
    """Curly quotes and dashes flattened, so a quote compares equal to its source
    whichever typography the document or the model used."""
    return str(text or "").translate(_QUOTE_FOLD)


def _is_abbreviation(text: str, dot: int) -> bool:
    """Is the full stop at `dot` the end of an abbreviation, an initial or a
    decimal rather than the end of a sentence?"""
    start = dot
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] == "."):
        start -= 1
    word = text[start:dot].lower()
    if not word:
        return False
    if word in _ABBREVIATIONS:
        return True
    # A single capital letter: "J. Smith". A single lower-case letter is far
    # more often the end of a sentence ("...plan b.") than an initial.
    if len(word) == 1 and word.isalpha() and text[start].isupper():
        return True
    # 3.5, 2.14, v1.2 - a dot between digits is not a stop.
    return False


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """`[(start, end), ...]` - `text[start:end]` is one sentence, exactly.

    Sentences are trimmed of surrounding whitespace, and the spans never overlap.
    A blank line always ends one; a single line break ends one only when it looks
    like a heading, a header line or a list item rather than a hard wrap in the
    middle of a sentence (PDF text is wrapped at column 70 and full of them).
    """
    source = str(text or "")
    length = len(source)
    spans: list[tuple[int, int]] = []
    begin = 0
    i = 0

    def close(end: int) -> None:
        nonlocal begin
        stop = end
        while stop > begin and source[stop - 1].isspace():
            stop -= 1
        start = begin
        while start < stop and source[start].isspace():
            start += 1
        if stop > start:
            spans.append((start, stop))
        begin = end

    while i < length:
        ch = source[i]

        if ch == "\n":
            j = i
            while j < length and source[j] in " \t\r\n":
                j += 1
            newlines = source.count("\n", i, j)
            if newlines >= 2:
                close(i)
                begin = j
                i = j
                continue
            # One line break: heading / header / list item, or a hard wrap?
            line_start = source.rfind("\n", 0, i) + 1
            line = source[line_start:i].strip()
            following = source[j:j + 1]
            ended = bool(line) and line[-1] in _TERMINATORS + ":" + _CLOSERS
            short_heading = (len(line) < 60 and following[:1].isupper()
                             and not line.endswith(","))
            bullet_next = bool(_BULLET.match(source[j:j + 12]))
            if ended or short_heading or bullet_next:
                close(i)
                begin = j
            i = j
            continue

        if ch in _TERMINATORS:
            # Swallow a run like "?!" or "...", then any closing quote/bracket.
            k = i + 1
            while k < length and source[k] in _TERMINATORS:
                k += 1
            while k < length and source[k] in _CLOSERS:
                k += 1
            nxt = k
            while nxt < length and source[nxt] in " \t":
                nxt += 1
            at_end = nxt >= length or source[nxt] == "\n"
            spaced = nxt > k
            if ch == "." and k == i + 1 and _is_abbreviation(source, i):
                i = k
                continue
            if at_end:
                close(k)
                i = k
                continue
            if spaced:
                lead = source[nxt]
                if lead.isupper() or lead.isdigit() or lead in _OPENERS or lead in "•-*":
                    close(k)
                    begin = nxt
                    i = nxt
                    continue
            i = k
            continue

        i += 1

    close(length)
    return spans


def sentences(text: str) -> list[str]:
    """The sentences of `text`, as plain strings."""
    return [text[start:end] for start, end in sentence_spans(text)]


def _is_boundary(text: str, i: int) -> bool:
    """Is position `i` between two words - not inside a run of letters/digits?"""
    return i <= 0 or i >= len(text) or not (text[i - 1].isalnum() and text[i].isalnum())


def _word_cut(text: str, start: int, end: int, focus: int, limit: int) -> tuple[int, int]:
    """A whole-word window of at most `limit` characters inside `[start, end)`,
    as close to `focus` as it can be. Used only for a single sentence longer
    than the limit, which is itself a rare thing to be quoting. Only a single
    unbroken run of letters longer than the limit is ever cut mid-word - there
    is nowhere else to cut it."""
    if end - start <= limit:
        return start, end
    lo = max(start, min(focus - limit // 3, end - limit))
    hi = min(end, lo + limit)
    while lo < hi and not _is_boundary(text, lo):
        lo += 1
    while hi > lo and not _is_boundary(text, hi):
        hi -= 1
    while lo < hi and text[lo].isspace():
        lo += 1
    while hi > lo and text[hi - 1].isspace():
        hi -= 1
    if hi <= lo:
        return start, min(end, start + limit)
    return lo, hi


def snap_span(text: str, start: int, end: int, *,
              max_chars: int = DEFAULT_QUOTE_MAX) -> tuple[int, int]:
    """Move `[start, end)` to sentence boundaries. **Work order 4e-1.**

    Whole sentences are preferred: the span is *widened* to cover every sentence
    it touches. If that is longer than `max_chars` it is *narrowed* instead, to
    the sentence that overlaps the request most (with its neighbours added while
    they fit), and only a single sentence that is itself too long is cut - at a
    word boundary, never in the middle of one.

    **The result is always a slice of `text`**: `text[s:e]` is what is printed,
    so verbatim is a property of the construction, not something to be checked
    afterwards. Returns `(0, 0)` for empty text.
    """
    source = str(text or "")
    spans = sentence_spans(source)
    if not spans:
        return (0, 0)
    n = len(source)
    start = max(0, min(int(start), n))
    end = max(start, min(int(end), n))
    if end == start:
        end = min(n, start + 1)

    touched = [span for span in spans if span[1] > start and span[0] < end]
    if not touched:
        later = [span for span in spans if span[0] >= start]
        touched = [later[0] if later else spans[-1]]

    first, last = touched[0][0], touched[-1][1]
    if last - first <= max_chars:
        return first, last

    def overlap(span: tuple[int, int]) -> int:
        return min(span[1], end) - max(span[0], start)

    best = max(touched, key=lambda span: (overlap(span), -span[0]))
    lo = hi = touched.index(best)
    s, e = best
    if e - s > max_chars:
        return _word_cut(source, s, e, max(start, s), max_chars)
    grew = True
    while grew:
        grew = False
        if hi + 1 < len(touched) and touched[hi + 1][1] - touched[lo][0] <= max_chars:
            hi += 1
            grew = True
        if lo - 1 >= 0 and touched[hi][1] - touched[lo - 1][0] <= max_chars:
            lo -= 1
            grew = True
    return touched[lo][0], touched[hi][1]


# --------------------------------------------------------------------------- tokens

#: Words that carry no evidence. A sentence "supported" only because it shares
#: "the", "was" and "of" with a passage is not supported.
STOPWORDS = frozenset("""
a an and are as at be been being but by can could did do does done for from had
has have having he her hers him his how i if in into is it its just me more most
my no not of on one or our out over per she should so some such than that the
their them then there these they this those to too under up us was we were what
when where which while who whom why will with would you your yours also about
after again all any because before between both each few further here itself
other same very s t don now only own off once
""".split())

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’\-]*")


def stem(word: str) -> str:
    """A very light stem, for comparing two spellings of one word.

    Not linguistics: enough that `licence`/`licences`, `approved`/`approve` and
    `raising`/`raise` compare equal, and nothing that could make two different
    words equal (`news` stays `news`).
    """
    w = word.lower().replace("’", "'")
    if w.endswith("'s"):
        w = w[:-2]
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    for suffix in ("ing", "ed", "ly", "es", "s", "e"):
        if len(w) - len(suffix) >= 4 and w.endswith(suffix):
            if suffix == "s" and w.endswith("ss"):
                continue
            return w[: -len(suffix)]
    return w


def content_tokens(text: str) -> list[str]:
    """Stemmed, lower-cased words that carry meaning, in order, duplicates kept.

    Numbers count as content (`12000` is exactly the kind of word an answer
    must not invent); words under two characters and stop words do not.
    """
    out: list[str] = []
    for match in _WORD.finditer(fold_quotes(str(text or ""))):
        raw = match.group(0).strip("-'")
        if not raw:
            continue
        low = raw.lower()
        if low in STOPWORDS:
            continue
        if len(low) < 2 and not low.isdigit():
            continue
        out.append(stem(low) if not low.isdigit() else low)
    return out


def token_set(texts: Iterable[str]) -> set[str]:
    """The union of `content_tokens` over several texts."""
    found: set[str] = set()
    for text in texts:
        found.update(content_tokens(text))
    return found
