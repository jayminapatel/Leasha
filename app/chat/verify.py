r"""§2a: no sentence without a receipt - the order's own load-bearing mechanism.

Layer: L8b — `WORKORDER-202626270611-chat-tab.md` §2a.

**The model proposes, the system verifies, only verified content reaches
the screen** - the translator precedent (`app/search/translate.py`:
quarantined output, validated against something the model cannot talk its
way past) applied to answers rather than to queries. A generated answer
carries inline markers - `[1]`, `[2]` - naming which retrieved chunk each
sentence draws from; every sentence is checked against its own citation
before it is allowed to render, never trusted because it sounds right.

**The check, in order, and any one failing drops the sentence:**

1. **The marker exists.** A sentence with no `[N]` at all is unsupported by
   construction - "the model never speaks unverified" admits no exception
   for a claim that simply forgot to cite anything.
2. **The marker points at a real chunk.** `[5]` when only three chunks were
   retrieved is the model fabricating a citation, not making a typo -
   treated exactly as gravely as fabricating the claim itself.
3. **Embedding similarity above `SIMILARITY_THRESHOLD`** between the
   sentence and its cited chunk - the "cheap entailment proxy" the order
   names. Cosine similarity, computed as a plain dot product because
   `Embedder.embed` already returns unit vectors (`l2_normalise`) - no
   second normalisation to get wrong here.
4. **§2c: every number the sentence states is in its cited source**
   (`cross_checked`) - measured to be *necessary*, not a belt-and-braces
   extra: changing "500 pounds" to "5,000 pounds" in an otherwise-correct
   sentence barely moves its embedding similarity, because the topic and
   wording are almost unchanged. A cheap semantic proxy was never going to
   notice one digit; this is the check that does.

A sentence citing more than one chunk survives if *any* of them clears the
threshold - citing two sources for one claim is an ordinary, legitimate
shape, and it should not be punished relative to citing one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, Sequence

__all__ = [
    "SIMILARITY_THRESHOLD",
    "VerifiedSentence",
    "VerifiedAnswer",
    "split_sentences",
    "extract_citations",
    "cross_checked",
    "verify_answer",
    "resolve_quotes",
]

#: **Measured against the real embedder this project ships** (`BAAI/bge-
#: small-en-v1.5`), five hand-built (chunk, genuinely-supported,
#: genuinely-fabricated) triples - a first guess of `0.55` was tried and
#: rejected before this one, because it would have let four of the five
#: fabrications through:
#:
#:     pair   supported   fabricated
#:     1        0.831        0.475
#:     2        0.861        0.561
#:     3        0.900        0.552
#:     4        0.847        0.585
#:     5        0.870        0.681
#:
#: `0.75` sits between the lowest genuine support (0.831) and the highest
#: topical fabrication (0.681) - roughly equidistant, favouring neither a
#: flood of false rejections nor a leak of false acceptances.
#:
#: **This alone cannot catch a fabricated number in an otherwise-correct
#: sentence.** "The deposit was 500 pounds" against its source scores
#: 0.831; changing only the number to "5,000 pounds" scores 0.774 - still
#: comfortably above this threshold, because the topic and wording barely
#: moved. That is not a bug in the threshold; it is exactly why §2c
#: (`cross_checked`, this module) exists as a *second*, mandatory gate
#: rather than a nice-to-have - a cheap semantic proxy was never going to
#: notice one digit changing, and the order never claimed it would.
SIMILARITY_THRESHOLD = 0.75

#: A run of whitespace immediately after sentence-ending punctuation. Not a
#: general-purpose sentence tokeniser - this only ever splits the
#: application's own generated answers, which are short, plain and written
#: to this prompt's own instructions, not arbitrary uploaded prose.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+")

#: `[1]`, `[12]` - a citation marker. `\[` and `\]` are literal; digits only,
#: so a sentence that happens to contain `[note]` is left alone rather than
#: misread as a citation.
_MARKER = re.compile(r"\[(\d+)\]")


class _Embedder(Protocol):
    """Just enough of `app.index.embedder.Embedder` to be faked in a test."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True, slots=True)
class VerifiedSentence:
    """One sentence, checked. `text` has its citation markers stripped -
    ready to render if `supported`, kept even when not for the debug pane
    and for `dropped_count`-style accounting."""

    text: str
    markers: tuple[int, ...]
    supported: bool
    #: The best similarity across `markers`, or `0.0` when there was no
    #: valid marker to compare against at all - a fabricated or missing
    #: citation never gets a similarity score, because there is nothing
    #: real to score it against.
    similarity: float = 0.0


@dataclass(frozen=True, slots=True)
class VerifiedAnswer:
    """`rendered` is the only part a screen should ever show - `sentences`
    is every one of them, supported or not, for the debug pane and for
    deciding whether the answer thinned too far to be worth showing at
    all."""

    sentences: tuple[VerifiedSentence, ...] = ()
    rendered: tuple[str, ...] = ()

    @property
    def dropped_count(self) -> int:
        return sum(1 for sentence in self.sentences if not sentence.supported)


def split_sentences(text: str) -> list[str]:
    """The answer, broken into sentences. See `_SENTENCE_BREAK`'s own
    docstring for why this is deliberately simple rather than a general
    tokeniser."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return []
    return [part.strip() for part in _SENTENCE_BREAK.split(cleaned) if part.strip()]


def extract_citations(sentence: str) -> tuple[str, list[int]]:
    """`(clean_text, markers)` - the sentence with every `[N]` removed, and
    the 1-based chunk numbers it named, in the order they appeared."""
    markers = [int(match.group(1)) for match in _MARKER.finditer(sentence)]
    clean = _MARKER.sub("", sentence)
    clean = " ".join(clean.split()).strip()
    return clean, markers


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


#: A run of digits, comma-grouped or not, with an optional decimal part -
#: `500`, `5,000`, `3.14`, `2019`. Deliberately over-inclusive: a year and an
#: amount look the same to this regex, and that is fine, because the check
#: is "does this number appear in the source", not "what kind of number is
#: it".
_NUMBER_TOKEN = re.compile(r"\b\d[\d,]*(?:\.\d+)?\b")


def _numbers_in(text: str) -> set[str]:
    """Every numeric token in `text`, commas stripped so `5,000` and `5000`
    are the same fact stated two ways."""
    return {token.replace(",", "") for token in _NUMBER_TOKEN.findall(text or "")}


def cross_checked(sentence: str, source: str) -> bool:
    """§2c: every number the sentence states appears in its cited source.

    **Necessary because §2a's embedding check cannot see this at all** - see
    `SIMILARITY_THRESHOLD`'s own docstring for the measured proof: changing
    "500 pounds" to "5,000 pounds" in an otherwise-correct sentence barely
    moves its similarity to the source, because the topic and wording are
    almost unchanged. A cheap semantic proxy was never going to notice one
    digit; this is the check that does.

    A sentence with no numbers at all passes trivially - this gate exists to
    catch a fabricated *number*, not to demand that every sentence contain
    one.
    """
    wanted = _numbers_in(sentence)
    if not wanted:
        return True
    return wanted <= _numbers_in(source)


#: `{{quote:chunk:start:end}}` - a quote directive: chunk number (1-based,
#: matching the citation markers), then the character range to copy
#: verbatim. Curly braces rather than `[N]`'s square ones, so a quote
#: directive can never be mistaken for a citation marker; doubled so it
#: cannot collide with a model's own occasional single-brace output.
_QUOTE = re.compile(r"\{\{quote:(\d+):(\d+):(\d+)\}\}")


def resolve_quotes(text: str, chunks: Sequence[str]) -> str:
    r"""§2b: every quote directive replaced with the literal chunk text at
    that range - **never the model's own rendition of it.**

    "the model selects offsets; the system prints the text" - the model
    never gets to type the quoted words at all, so there is no wording for
    it to get wrong. An out-of-range chunk, a range outside the chunk's own
    length, or `start >= end` is not "quoted approximately" - the directive
    is simply removed, nothing substituted, because a quote that might not
    be exactly what is in the source is worse than no quote.
    """
    def replace(match: "re.Match[str]") -> str:
        chunk_index, start, end = (int(match.group(1)), int(match.group(2)),
                                   int(match.group(3)))
        if not (1 <= chunk_index <= len(chunks)):
            return ""
        source = chunks[chunk_index - 1]
        if not (0 <= start < end <= len(source)):
            return ""
        return f'"{source[start:end]}"'

    return _QUOTE.sub(replace, text or "")


def verify_answer(
    raw_answer: str,
    chunks: Sequence[str],
    *,
    embedder: _Embedder,
    threshold: float = SIMILARITY_THRESHOLD,
) -> VerifiedAnswer:
    """Resolve quotes, split, check every sentence against its citation,
    keep only what passes. **Never renders a sentence this function did not
    itself verify** - that is the whole contract, and the reason nothing
    calling this needs its own second opinion about what is safe to show.

    **Quotes resolve first, then citations are checked** - a sentence
    carrying a quote directive is verified against its *actual* quoted text
    (§2b's real substitution), never against the placeholder, which is
    meaningless to compare for similarity and would fail §2c's number check
    for no reason connected to whether the claim is true.
    """
    resolved = resolve_quotes(raw_answer, chunks)
    sentences = split_sentences(resolved)
    if not sentences:
        return VerifiedAnswer()

    parsed = [extract_citations(sentence) for sentence in sentences]
    texts = [clean for clean, _markers in parsed]
    sentence_vectors = embedder.embed(texts) if texts else []
    chunk_vectors = embedder.embed(list(chunks)) if chunks else []

    checked: list[VerifiedSentence] = []
    for (clean, markers), vector in zip(parsed, sentence_vectors):
        valid = [m for m in markers if 1 <= m <= len(chunk_vectors)]
        if not valid:
            # No marker at all, or every marker named a chunk that was
            # never retrieved - both are a citation this sentence cannot
            # actually stand on.
            checked.append(VerifiedSentence(text=clean, markers=tuple(markers), supported=False))
            continue
        best = max(_dot(vector, chunk_vectors[marker - 1]) for marker in valid)
        # **Both gates, not either** - §2a's similarity and §2c's number
        # cross-check catch different failures (wording versus fact), and a
        # sentence has to clear both to render. The source text is every
        # cited chunk joined, so a sentence legitimately combining a date
        # from one citation and an amount from another still checks out.
        source_text = " ".join(chunks[marker - 1] for marker in valid)
        supported = best >= threshold and cross_checked(clean, source_text)
        checked.append(VerifiedSentence(
            text=clean, markers=tuple(valid), supported=supported, similarity=best,
        ))

    rendered = tuple(sentence.text for sentence in checked if sentence.supported)
    return VerifiedAnswer(sentences=tuple(checked), rendered=rendered)
