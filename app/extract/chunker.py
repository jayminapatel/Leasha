"""Chunking: ~512-token pieces with ~64-token overlap, on natural boundaries.

Layer: L2

Chunk size is an embedding constraint, not a preference. bge-small-en-v1.5 takes
512 tokens and silently truncates beyond that, so a chunk that overshoots loses
its tail with no error anywhere - the worst kind of bug, because search simply
gets quietly worse. Overlap exists so that a sentence spanning a boundary is
still wholly present in one chunk, and is therefore still findable.

**How it works.** The text is split once into *atoms* - words, with their exact
spans - each tagged with the strongest boundary that precedes it (paragraph,
sentence, or nothing). A chunk is then a contiguous range of atoms, so:

  * `text[chunk.char_start:chunk.char_end]` is the chunk, verbatim;
  * a chunk can never begin or end mid-word, because atoms are whole words.

Both properties are tested. Ranges are chosen greedily up to the token budget,
then walked backwards a little to land on a paragraph break if one is near, or a
sentence break otherwise - the boundary preference the spec asks for, without a
second pass over the text.

**Counting tokens without a tokenizer.** Loading the real one would drag the
embedding model into extraction, which must run with no model present. So a word
costs `max(1.35, len/4)` tokens: 1.35 is a deliberate over-estimate for English
prose, and the length term catches the words that are not words at all - a
base64 blob or a minified bundle, where one "word" can be thousands of tokens.
The bias points high on purpose, because guessing low means silent truncation at
embed time while guessing high only means slightly smaller chunks.

`token_cost` is the single definition, and `estimate_tokens` is its sum, so the
budget the chunker spends and the size it reports cannot drift apart. Layer 3 can
pass a real per-word `count_tokens` if exactness ever matters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

__all__ = [
    "Chunk",
    "chunk_text",
    "chunk_document",
    "estimate_tokens",
    "token_cost",
    "TARGET_TOKENS",
    "OVERLAP_TOKENS",
    "MIN_CHUNK_CHARS",
]

TARGET_TOKENS = 512
OVERLAP_TOKENS = 64
#: A trailing fragment shorter than this is folded into the previous chunk
#: rather than indexed alone - a 4-word chunk is noise in the results list.
MIN_CHUNK_CHARS = 120

#: Over-estimates on purpose. See the module docstring.
TOKENS_PER_WORD = 1.35

_WORD = re.compile(r"\S+")
_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
#: Sentence end: . ! ? or : possibly closed by a quote or bracket, then space.
_SENTENCE_END = re.compile(r'[.!?:]["\')\]]*$')

_BREAK_NONE = 0
_BREAK_SENTENCE = 1
_BREAK_PARAGRAPH = 2


#: Rough bytes-per-token for a word with no spaces in it - a hash, a base64
#: blob, a minified bundle. Without this a 200k-character "word" would be
#: costed as 1.35 tokens and blow through any budget.
CHARS_PER_TOKEN = 4.0


def token_cost(word: str) -> float:
    """Estimated tokens for one whitespace-delimited word.

    This is the single definition of cost. `estimate_tokens` sums it and the
    chunker's budget spends it, so the two can never disagree - which they did:
    costing each word at 1 token while estimating a whole chunk at 1.35 per word
    let every chunk run ~35% over budget, and straight past the model's limit.
    """
    return max(TOKENS_PER_WORD, len(word) / CHARS_PER_TOKEN)


def estimate_tokens(text: str) -> int:
    """Approximate token count. Biased high; see the module docstring.

    Exactly the sum of `token_cost` over the words, rounded up.
    """
    import math

    words = text.split()
    if not words:
        return 0
    return max(1, math.ceil(sum(token_cost(word) for word in words)))


@dataclass(frozen=True)
class Chunk:
    """One chunk, located exactly within the text it came from."""

    text: str
    ordinal: int
    char_start: int
    char_end: int
    page: Optional[int] = None

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


@dataclass(frozen=True)
class _Atom:
    """One word, its span, and the strongest boundary immediately before it."""

    start: int
    end: int
    tokens: float
    break_level: int


def _atomise(text: str, count_tokens: Callable[[str], float]) -> list[_Atom]:
    """Split into words with spans, tagging the boundary that precedes each."""
    paragraph_starts: set[int] = {0}
    for match in _PARAGRAPH_BREAK.finditer(text):
        paragraph_starts.add(match.end())

    atoms: list[_Atom] = []
    previous_word: Optional[str] = None
    previous_end = 0

    for match in _WORD.finditer(text):
        start, end = match.start(), match.end()
        word = match.group()

        if any(start >= position > previous_end for position in paragraph_starts) or start == 0:
            level = _BREAK_PARAGRAPH
        elif previous_word is not None and _SENTENCE_END.search(previous_word):
            level = _BREAK_SENTENCE
        else:
            level = _BREAK_NONE

        atoms.append(_Atom(start=start, end=end, tokens=max(1.0, float(count_tokens(word))), break_level=level))
        previous_word = word
        previous_end = end

    return atoms


def _find_break(
    atoms: Sequence[_Atom],
    start: int,
    end: int,
    *,
    tolerance: int,
) -> int:
    """Pull `end` back to a natural boundary, if one is within `tolerance` atoms.

    Returns the exclusive end index. Prefers a paragraph break, then a sentence
    break, then leaves the greedy end alone. Never returns a range shorter than
    half the window, so boundary-seeking cannot collapse a chunk to nothing.
    """
    if end >= len(atoms):
        return end

    floor = max(start + 1, end - tolerance, start + (end - start) // 2)

    for level in (_BREAK_PARAGRAPH, _BREAK_SENTENCE):
        for candidate in range(end, floor - 1, -1):
            # A chunk ends where the next atom begins a new paragraph/sentence.
            if candidate < len(atoms) and atoms[candidate].break_level >= level:
                return candidate
    return end


def _overlap_start(atoms: Sequence[_Atom], chunk_start: int, chunk_end: int, overlap: float) -> int:
    """Where the next chunk begins, backing up ~`overlap` tokens from the end.

    Snapped forward to a sentence start when one falls inside the overlap window,
    so the repeated text reads as a unit rather than starting mid-clause. Always
    strictly greater than `chunk_start`, which is what guarantees progress.
    """
    if overlap <= 0:
        return chunk_end

    budget = float(overlap)
    position = chunk_end
    while position > chunk_start + 1 and budget > 0:
        position -= 1
        budget -= atoms[position].tokens

    for candidate in range(position, chunk_end):
        if atoms[candidate].break_level >= _BREAK_SENTENCE:
            position = candidate
            break

    return max(position, chunk_start + 1)


def chunk_text(
    text: str,
    *,
    target_tokens: int = TARGET_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
    page_lookup: Optional[Callable[[int], Optional[int]]] = None,
    count_tokens: Callable[[str], float] = token_cost,
    min_chunk_chars: int = MIN_CHUNK_CHARS,
) -> list[Chunk]:
    """Split `text` into overlapping chunks on natural boundaries.

    Every returned chunk satisfies `text[c.char_start:c.char_end] == c.text`.
    """
    if target_tokens <= 0:
        raise ValueError(f"target_tokens must be positive, got {target_tokens}")
    if overlap_tokens < 0:
        raise ValueError(f"overlap_tokens must not be negative, got {overlap_tokens}")
    if overlap_tokens >= target_tokens:
        raise ValueError(
            f"overlap_tokens ({overlap_tokens}) must be smaller than "
            f"target_tokens ({target_tokens}), or chunking cannot advance"
        )

    atoms = _atomise(text, count_tokens)
    if not atoms:
        return []

    tolerance = max(1, target_tokens // 5)
    chunks: list[Chunk] = []
    cursor = 0
    ordinal = 0

    while cursor < len(atoms):
        budget = float(target_tokens)
        end = cursor
        while end < len(atoms) and budget - atoms[end].tokens >= 0:
            budget -= atoms[end].tokens
            end += 1
        if end == cursor:                     # one atom larger than the whole budget
            end = cursor + 1

        end = _find_break(atoms, cursor, end, tolerance=tolerance)

        char_start = atoms[cursor].start
        char_end = atoms[end - 1].end
        body = text[char_start:char_end]

        # Fold a runt tail into its predecessor rather than emitting it alone.
        if (
            end >= len(atoms)
            and chunks
            and len(body) < min_chunk_chars
            and char_end > chunks[-1].char_end
        ):
            previous = chunks[-1]
            chunks[-1] = Chunk(
                text=text[previous.char_start:char_end],
                ordinal=previous.ordinal,
                char_start=previous.char_start,
                char_end=char_end,
                page=previous.page,
            )
            break

        chunks.append(
            Chunk(
                text=body,
                ordinal=ordinal,
                char_start=char_start,
                char_end=char_end,
                page=page_lookup(char_start) if page_lookup else None,
            )
        )
        ordinal += 1

        if end >= len(atoms):
            break
        cursor = _overlap_start(atoms, cursor, end, overlap_tokens)

    return chunks


def chunk_document(
    document: "object",
    *,
    target_tokens: int = TARGET_TOKENS,
    overlap_tokens: int = OVERLAP_TOKENS,
    count_tokens: Callable[[str], float] = token_cost,
) -> list[Chunk]:
    """`chunk_text` over a `Document`, stamping each chunk with its page.

    Typed loosely to keep this module free of an import from `base`, so the
    chunker can be tested and reasoned about entirely on its own.
    """
    return chunk_text(
        document.text,                                    # type: ignore[attr-defined]
        target_tokens=target_tokens,
        overlap_tokens=overlap_tokens,
        page_lookup=document.page_lookup(),                # type: ignore[attr-defined]
        count_tokens=count_tokens,
    )
