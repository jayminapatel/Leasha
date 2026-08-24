"""Layer 2: chunking.

Three properties matter more than any individual assertion, because search
quality depends on them silently:

1. **Offsets are exact.** `text[c.char_start:c.char_end] == c.text`, always.
   Layer 5 highlights hits using those numbers; if they drift, it highlights the
   wrong sentence and nobody notices until a user says the app is "a bit odd".
2. **No text is lost at a boundary.** Every word of the source appears in at
   least one chunk. A word that falls between two chunks is unfindable forever,
   and nothing in the system would ever report it.
3. **Chunks fit the model.** bge-small truncates past 512 tokens without an
   error, so an oversized chunk loses its tail invisibly.

The tests below are written as properties over generated text rather than as
assertions about one hand-picked paragraph, because the failure modes are all
about edge cases and none of them are visible in a happy-path example.
"""

from __future__ import annotations

import re
from itertools import pairwise

import pytest

from app.extract.chunker import (
    OVERLAP_TOKENS,
    TARGET_TOKENS,
    Chunk,
    chunk_text,
    estimate_tokens,
)

WORD = re.compile(r"\S+")


def make_prose(paragraphs: int = 12, sentences: int = 6, words: int = 14) -> str:
    """Deterministic prose with real paragraph and sentence structure."""
    blocks = []
    counter = 0
    for paragraph in range(paragraphs):
        lines = []
        for _sentence in range(sentences):
            terms = []
            for _ in range(words):
                counter += 1
                terms.append(f"word{counter}")
            lines.append(" ".join(terms) + ".")
        blocks.append(f"Section {paragraph}. " + " ".join(lines))
    return "\n\n".join(blocks)


PROSE = make_prose()


# --- the three properties ---------------------------------------------------

@pytest.mark.parametrize("target,overlap", [(512, 64), (128, 16), (64, 8), (32, 0)])
def test_offsets_are_exact(target: int, overlap: int) -> None:
    for chunk in chunk_text(PROSE, target_tokens=target, overlap_tokens=overlap):
        assert PROSE[chunk.char_start:chunk.char_end] == chunk.text


@pytest.mark.parametrize("target,overlap", [(512, 64), (128, 16), (64, 8), (32, 0)])
def test_no_word_is_lost_at_a_boundary(target: int, overlap: int) -> None:
    """Every word of the source must live inside at least one chunk."""
    chunks = chunk_text(PROSE, target_tokens=target, overlap_tokens=overlap)
    spans = [(chunk.char_start, chunk.char_end) for chunk in chunks]

    missing = [
        match.group()
        for match in WORD.finditer(PROSE)
        if not any(start <= match.start() and match.end() <= end for start, end in spans)
    ]
    assert not missing, f"{len(missing)} word(s) fell between chunks: {missing[:5]}"


@pytest.mark.parametrize("target", [512, 256, 128, 64])
def test_chunks_respect_the_token_budget(target: int) -> None:
    """Only a single word longer than the whole budget may exceed it."""
    for chunk in chunk_text(PROSE, target_tokens=target, overlap_tokens=target // 8):
        assert chunk.tokens <= target or len(chunk.text.split()) == 1


# --- boundaries -------------------------------------------------------------

def test_chunks_never_split_a_word() -> None:
    for chunk in chunk_text(PROSE, target_tokens=64, overlap_tokens=8):
        assert chunk.text == chunk.text.strip()
        assert not chunk.text[0].isspace()
        # A chunk that began mid-word would have a non-space character directly
        # before it in the source.
        if chunk.char_start > 0:
            assert PROSE[chunk.char_start - 1].isspace()
        if chunk.char_end < len(PROSE):
            assert PROSE[chunk.char_end].isspace()


def test_boundaries_prefer_sentence_ends() -> None:
    """Most chunks should end on a full stop, not mid-clause."""
    chunks = chunk_text(PROSE, target_tokens=128, overlap_tokens=16)
    ending_cleanly = sum(1 for chunk in chunks if chunk.text.rstrip().endswith("."))
    assert ending_cleanly >= len(chunks) * 0.8, (
        f"only {ending_cleanly}/{len(chunks)} chunks ended on a sentence boundary"
    )


def test_paragraph_breaks_are_preferred_when_they_are_near() -> None:
    text = "\n\n".join(" ".join(f"w{i}_{j}" for j in range(40)) for i in range(6))
    for chunk in chunk_text(text, target_tokens=60, overlap_tokens=0):
        assert not chunk.text.startswith(" ")


# --- overlap ----------------------------------------------------------------

def test_consecutive_chunks_overlap() -> None:
    chunks = chunk_text(PROSE, target_tokens=128, overlap_tokens=32)
    assert len(chunks) > 2
    for previous, following in pairwise(chunks):
        assert following.char_start < previous.char_end, "chunks must share a tail"


def test_zero_overlap_produces_a_clean_partition() -> None:
    chunks = chunk_text(PROSE, target_tokens=128, overlap_tokens=0)
    for previous, following in pairwise(chunks):
        assert following.char_start >= previous.char_end


def test_overlap_larger_than_target_is_rejected() -> None:
    """Otherwise chunking cannot advance and the indexer hangs on one file."""
    with pytest.raises(ValueError, match="smaller than"):
        chunk_text(PROSE, target_tokens=64, overlap_tokens=64)


def test_invalid_target_is_rejected() -> None:
    with pytest.raises(ValueError):
        chunk_text(PROSE, target_tokens=0)


# --- ordering and shape -----------------------------------------------------

def test_ordinals_are_sequential_from_zero() -> None:
    chunks = chunk_text(PROSE, target_tokens=128, overlap_tokens=16)
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))


def test_chunks_advance_monotonically() -> None:
    chunks = chunk_text(PROSE, target_tokens=96, overlap_tokens=24)
    starts = [chunk.char_start for chunk in chunks]
    assert starts == sorted(starts)
    assert len(set(starts)) == len(starts), "a repeated start means no progress"


def test_chunking_is_deterministic() -> None:
    assert chunk_text(PROSE, target_tokens=128) == chunk_text(PROSE, target_tokens=128)


# --- pathological input -----------------------------------------------------

def test_empty_and_whitespace_produce_no_chunks() -> None:
    assert chunk_text("") == []
    assert chunk_text("   \n\n\t  ") == []


def test_single_word_produces_one_chunk() -> None:
    chunks = chunk_text("survey")
    assert len(chunks) == 1
    assert chunks[0].text == "survey"


def test_one_enormous_word_does_not_hang() -> None:
    """A minified JS bundle is one 200k-character 'word'. It must terminate."""
    text = "x" * 200_000
    chunks = chunk_text(text, target_tokens=64, overlap_tokens=8)
    assert len(chunks) == 1
    assert chunks[0].text == text


def test_text_with_no_sentence_structure_still_chunks() -> None:
    text = " ".join(f"token{i}" for i in range(4000))
    chunks = chunk_text(text, target_tokens=128, overlap_tokens=16)
    assert len(chunks) > 5
    for chunk in chunks:
        assert text[chunk.char_start:chunk.char_end] == chunk.text


def test_runt_tail_is_folded_into_the_previous_chunk() -> None:
    """A four-word final chunk is noise in a results list."""
    text = " ".join(f"w{i}" for i in range(200)) + "\n\nEnd."
    chunks = chunk_text(text, target_tokens=100, overlap_tokens=10, min_chunk_chars=200)
    assert chunks[-1].text.endswith("End.")
    assert len(chunks[-1].text) >= 200


def test_crlf_text_keeps_exact_offsets() -> None:
    text = "First para line one.\r\n\r\nSecond para line two.\r\n"
    for chunk in chunk_text(text, target_tokens=8, overlap_tokens=0):
        assert text[chunk.char_start:chunk.char_end] == chunk.text


# --- token estimation -------------------------------------------------------

def test_estimator_is_biased_high() -> None:
    """Guessing low means silent truncation at embed time; guessing high costs
    a slightly smaller chunk. The bias must point the safe way."""
    assert estimate_tokens("one two three four five") >= 5


def test_estimator_handles_empty() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("   ") == 0


def test_spec_defaults() -> None:
    assert TARGET_TOKENS == 512
    assert OVERLAP_TOKENS == 64


# --- pages ------------------------------------------------------------------

def test_page_lookup_stamps_chunks() -> None:
    text = PROSE
    midpoint = len(text) // 2
    chunks = chunk_text(
        text,
        target_tokens=128,
        overlap_tokens=16,
        page_lookup=lambda offset: 1 if offset < midpoint else 2,
    )
    assert {chunk.page for chunk in chunks} == {1, 2}


def test_no_page_lookup_leaves_page_none() -> None:
    assert all(chunk.page is None for chunk in chunk_text(PROSE, target_tokens=128))


def test_chunk_is_hashable_and_comparable() -> None:
    chunk = Chunk(text="a", ordinal=0, char_start=0, char_end=1)
    assert chunk == Chunk(text="a", ordinal=0, char_start=0, char_end=1)
    assert len({chunk, chunk}) == 1
