"""No sentence without a receipt: the verification that the whole order rests on.

Layer: L8b - no Qt, no network, no model. Work order section 2.

The model proposes. This module disposes. A sentence a model wrote reaches the
screen only if **all** of these hold, each checked against the text of the
documents that were actually retrieved:

**2a - a marker, pointing at a real source, that supports it.**
  * it ends in a source marker `[n]`, and every `n` is a source that was shown;
  * the sentence's meaningful words are found in the passage it cites - in the
    best window of one or two adjacent sentences, not anywhere in the chunk, so a
    sentence stitched together from words scattered over a page does not pass
    (`SUPPORT`, tuned on `tests/fixtures/chat_eval.py`);
  * a negation must agree: "was not returned" is not supported by "was returned".

**2b - quotes are verbatim.** A quotation mark in the answer is a claim about the
document's exact words, so text inside quotation marks must be a verbatim
substring of a cited source, or the sentence dies. And the quote on a *receipt*
is never the model's: it is a slice of the stored text, chosen by the system as
the sentence(s) that best support the answer, snapped to sentence boundaries
(`text.snap_span`, work order 4e-1).

**2c - dates, numbers and names must exist in the evidence.** Every figure, year,
month and capitalised name in a sentence is looked for in the supporting window
(and the document's own metadata - a sender's name is in the header, not the
body). A fabricated "£5,000" or an invented "Jonathan" is not lexical-overlap
noise, it is exactly the error a reader cannot see, so it is checked separately
and exactly.

**What is deliberately not done: paraphrase.** A sentence that says the right
thing in entirely different words is dropped. That is the price of a guarantee
that does not depend on the model behaving; the prompts ask for the source's own
words, and an embedding-similarity rescue (`similarity=`) can widen the net for
the borderline cases when an embedder is available. It can only rescue a
sentence that is *close* lexically, and it never bypasses 2b or 2c.

The guarantee is structural: `ChatTurn.text` of an `answer` is built **only** from
`AnswerAssembler.accepted` - there is no code path that appends model text to a
turn any other way. `audit_turn` re-checks a finished turn independently.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from app.chat.context import Source
from app.chat.text import (
    content_tokens,
    fold_quotes,
    normalise_space,
    sentence_spans,
    snap_span,
)
from app.chat.types import ChatTurn, Receipt

__all__ = [
    "Verdict",
    "Verifier",
    "AnswerAssembler",
    "audit_turn",
    "split_markers",
    "normalise_marker_placement",
    "MARKER",
    "SUPPORT",
    "extract_numbers",
    "support_tokens",
]

#: The default share of a sentence's meaningful words and quantities that must be
#: found in the window it cites. **Measured, not guessed**: see
#: `tests/unit/test_chat_evaluate.py` for the sweep over the fixture set this was
#: chosen from. At 0.7 a three-word sentence needs all three, four or five words
#: may miss one, and a sentence built from words scattered across a passage
#: clears nothing - while a near-verbatim or lightly reworded one passes.
SUPPORT = 0.7

#: A sentence with fewer meaningful words than this cannot be checked, and
#: "Yes." is a claim like any other. It is dropped.
MIN_TOKENS = 2

#: Of a sentence's words (three or more), the share that must sit in a run of two
#: or more that also sit side by side in the passage it cites. **This is what
#: separates a sentence from a collage.** "Letter and deposit and scheme and
#: deductions" has every word in the letter and no two of them together, and a
#: bag-of-words score cannot tell that from a sentence; adjacency can. A
#: paraphrase that keeps some of the source's phrases passes; one that keeps none
#: of them is not a restatement of that passage.
ADJACENT_MIN = 0.4

#: How close two words must be in the source to count as sitting together, and how
#: many words a sentence needs before this is asked at all.
ADJACENT_REACH = 2
ADJACENT_MIN_WORDS = 4

#: Cosine similarity at or above which an embedding says two texts mean the same.
RESCUE_SIMILARITY = 0.82

#: A first sentence this short, in a document of more than one, is a title.
TITLE_MAX_CHARS = 100

MARKER = re.compile(r"\[(\d+(?:\s*[,;]\s*\d+)*)\]")
_MARKER_AFTER_STOP = re.compile(r"([.!?])((?:\s*\[\d+(?:\s*[,;]\s*\d+)*\])+)")
_PARTIAL_MARKER = re.compile(r"\s*\[[\d\s,;]*$")
_ANSWER_LABEL = re.compile(r"^\s*(?:answer|a)\s*:\s*", re.I)

_NEGATION = re.compile(
    r"\b(?:not|no|never|none|nothing|neither|nor|without|cannot)\b|n't\b|n’t\b", re.I)

# ---------------------------------------------------------------------------
# markers
# ---------------------------------------------------------------------------


_GLUED = re.compile(r"(?<=[a-z\]])([.!?])(?=[A-Z][a-z])")


def normalise_marker_placement(text: str) -> str:
    """`"... £500. [1]"` -> `"... £500 [1]."` so the marker belongs to the
    sentence it follows instead of starting the next one - and `"[1].The next"`
    (a model that streams "sentence. [1]" and then starts the next with no space)
    gets its space back, so the two are two sentences."""
    return _GLUED.sub(r"\1 ", _MARKER_AFTER_STOP.sub(r"\2\1", text))


def split_markers(sentence: str) -> tuple[str, list[int]]:
    """`(body, [n, ...])` - the sentence without its markers, and the numbers."""
    numbers: list[int] = []
    for match in MARKER.finditer(sentence):
        numbers += [int(n) for n in re.split(r"[,;]", match.group(1)) if n.strip()]
    body = MARKER.sub("", sentence)
    body = re.sub(r"\s+([.!?,;:])", r"\1", body)
    return normalise_space(body), numbers


# ---------------------------------------------------------------------------
# facts: numbers, months, names
# ---------------------------------------------------------------------------

_MONTHS = ("january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december")
_MONTH_NUMBER = {name: i for i, name in enumerate(_MONTHS, start=1)}
_MONTH_NUMBER.update({name[:3]: i for i, name in enumerate(_MONTHS, start=1)})
_MONTH_NUMBER["sept"] = 9

_DAYS = frozenset("monday tuesday wednesday thursday friday saturday sunday".split())

#: Capitalised words that are not names, so they are not looked for.
_NOT_NAMES = frozenset("""
i i'm i've i'll i'd the a an and or but if in on at to of for by with from as it its
this that these those there here he she they we you my our your his her their
yes no not
""".split()) | frozenset(_MONTHS) | _DAYS | frozenset(m for m in _MONTH_NUMBER)

_NUMBER = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s?(k\b|m\b|million\b|thousand\b|bn\b|billion\b)?",
                     re.I)
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_SLASH = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})\b")
_NAME_TOKEN = re.compile(r"[A-Za-z][A-Za-z'’\-]*")

_UNITS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate(
    "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()) if w != "_"}
_SCALE = {"hundred": 100, "thousand": 1000, "million": 1_000_000}


def support_tokens(text: str) -> set[str]:
    """The evidence a sentence is compared on: its meaningful words, and its
    quantities as *values* (`#12000`), so "12,000" and "twelve thousand" match
    and a figure counts as one thing rather than as "12" and "000"."""
    words = {t for t in content_tokens(_without_number_words(text)) if not t.isdigit()}
    return words | {"#" + n for n in extract_numbers(text)} | {f"@{m}" for m in _months_in(text)}


_NUMBER_WORD = re.compile(r"\b(?:" + "|".join(sorted(
    [*_UNITS, *_TENS, *_SCALE], key=len, reverse=True)) + r")\b", re.I)
_ONE_IS_NOT_A_QUANTITY = re.compile(
    r"\b(?:no|any|every|some|each|this|that|which|the|a|another|one)\s+one\b|\bone\s+(?:of|another|who)\b",
    re.I)


def _without_number_words(text: str) -> str:
    """`text` with spelled-out quantities removed - they are compared as values
    (`#6`), not as words, so "six" and "two" are different figures."""
    return _NUMBER_WORD.sub(" ", _ONE_IS_NOT_A_QUANTITY.sub(" ", text))


def _word_sequence(text: str) -> list[str]:
    """The meaningful words of `text` in order, without figures - the order is
    what adjacency is judged on."""
    return [t for t in content_tokens(text) if not t.isdigit()]


def _adjacent_share(body: Sequence[str], windows: Sequence[Sequence[str]]) -> float:
    """The share of `body`'s words that belong to a pair of neighbours which are
    also *near* each other, in either order, somewhere in `windows`.

    Near means within `ADJACENT_REACH` words: a restatement reorders ("the deposit
    is returned" / "return the deposit") and skips ("returned within 10 days" /
    "returned in 10 days"), a collage does neither - its words come from wherever.
    Sentences of fewer than `ADJACENT_MIN_WORDS` words are not judged: a short
    sentence has too few pairs to be a collage of anything."""
    if len(body) < ADJACENT_MIN_WORDS:
        return 1.0
    near: set[frozenset[str]] = set()
    for window in windows:
        for i, word in enumerate(window):
            for other in window[i + 1:i + 1 + ADJACENT_REACH]:
                near.add(frozenset((word, other)))
    covered: set[int] = set()
    for i in range(len(body) - 1):
        if frozenset((body[i], body[i + 1])) in near:
            covered.update((i, i + 1))
    return len(covered) / len(body)


def _canonical(number: float) -> str:
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return f"{number:.6f}".rstrip("0").rstrip(".")


def _word_numbers(text: str) -> set[str]:
    """Numbers written as words ("twelve thousand" -> 12000, "six" -> 6),
    canonicalised. "One" is a quantity only where it is used as one."""
    found: set[str] = set()
    words = re.findall(r"[a-z]+", text.lower())
    i = 0
    while i < len(words):
        if words[i] in _UNITS or words[i] in _TENS or words[i] in _SCALE:
            total, current, used = 0, 0, 0
            j = i
            while j < len(words):
                w = words[j]
                if w in _UNITS:
                    current += _UNITS[w]
                elif w in _TENS:
                    current += _TENS[w]
                elif w in _SCALE:
                    current = max(current, 1) * _SCALE[w]
                    if _SCALE[w] >= 1000:
                        total, current = total + current, 0
                elif w == "and" and used and j + 1 < len(words) and (
                        words[j + 1] in _UNITS or words[j + 1] in _TENS):
                    j += 1
                    continue
                else:
                    break
                used += 1
                j += 1
            value = total + current
            lone_one = used == 1 and value == 1
            if used and (value >= 10 or used > 1 or (not lone_one) or _quantity_one(text, words, i)):
                found.add(_canonical(value))
            i = max(j, i + 1)
        else:
            i += 1
    return found


def _quantity_one(text: str, words: list[str], at: int) -> bool:
    """Is this "one" a quantity ("one month") rather than a pronoun ("no one",
    "one of the")? Judged on its neighbours."""
    before = words[at - 1] if at > 0 else ""
    after = words[at + 1] if at + 1 < len(words) else ""
    if before in ("no", "any", "every", "some", "each", "this", "that", "which", "the", "a",
                  "another") or after in ("of", "another", "who"):
        return False
    return True


def extract_numbers(text: str) -> set[str]:
    """Every quantity in `text`, canonical: commas removed, `40k` -> `40000`,
    `03` -> `3`, `12.50` -> `12.5`. Dates contribute their parts."""
    source = fold_quotes(str(text or ""))
    found: set[str] = set()
    for match in _NUMBER.finditer(source):
        raw = match.group(1).replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        suffix = (match.group(2) or "").lower()
        factor = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6,
                  "bn": 1e9, "billion": 1e9}.get(suffix, 1)
        found.add(_canonical(value * factor))
        if factor != 1:
            found.add(_canonical(value))
    found |= _word_numbers(source)
    return found


def _months_in(text: str) -> set[int]:
    lowered = text.lower()
    # "may" is a month only when written as one - the modal verb is everywhere.
    found = {number for name, number in _MONTH_NUMBER.items()
             if name != "may" and re.search(rf"\b{name}\b", lowered)}
    if re.search(r"\bMay\b(?=\s+\d)|(?<=\d )May\b|(?<=\dth )May\b", text):
        found.add(5)
    for match in _DATE_ISO.finditer(text):
        found.add(int(match.group(2)))
    for match in _DATE_SLASH.finditer(text):
        for part in match.groups()[:2]:
            if 1 <= int(part) <= 12:
                found.add(int(part))
    return found


def _names_in(body: str) -> list[str]:
    """Capitalised words that are probably names: not sentence-initial, not a
    month or day, plus upper-case acronyms of two letters or more."""
    names: list[str] = []
    tokens = list(_NAME_TOKEN.finditer(body))
    for index, match in enumerate(tokens):
        word = match.group(0).strip("'’-")
        if not word:
            continue
        low = word.lower().replace("’", "'")
        if low.endswith("'s"):
            low = low[:-2]
        if low in _NOT_NAMES or len(low) < 2:
            continue
        acronym = word.isupper() and len(word) >= 2
        # Sentence-initial words are capitalised whatever they are.
        if index == 0 and not acronym:
            continue
        before = body[:match.start()].rstrip()
        if before.endswith((".", "!", "?", ":")) and not acronym:
            continue
        if word[0].isupper() or acronym:
            names.append(low)
    return names


def _evidence_tokens(text: str) -> set[str]:
    """Words of the evidence, and the parts of hyphenated ones ("line-up" gives
    `line-up`, `line` and `up`), so `GA-4471` is found as `ga`."""
    found: set[str] = set()
    for token in _NAME_TOKEN.findall(text):
        low = token.lower().replace("’", "'").strip("-'")
        if not low:
            continue
        found.add(low)
        if "-" in low:
            found.update(part for part in low.split("-") if part)
    return found


def _name_present(name: str, evidence: set[str], evidence_text: str) -> bool:
    if name in evidence or name + "s" in evidence or name.rstrip("s") in evidence:
        return True
    if f"{name}'s" in evidence:
        return True
    # A word that is a piece of an address or a hyphenated compound.
    if any(name in token for token in evidence if len(token) > len(name) and len(name) >= 4):
        return True
    # Fuzzy: one slip of the pen in the document ("Yeats"/"Yates") is not a lie.
    if len(name) >= 5:
        for token in evidence:
            if abs(len(token) - len(name)) <= 2 and token[:1] == name[:1] \
                    and difflib.SequenceMatcher(None, name, token).ratio() >= 0.86:
                return True
    return False


# ---------------------------------------------------------------------------
# the verdict
# ---------------------------------------------------------------------------

#: Reasons a sentence failed that a different source number could cure.
_REPAIRABLE = ("its source does not contain what it says",)
_REPAIRABLE_PREFIXES = ("is only ", "strings together words", "cites source ")


@dataclass(frozen=True)
class Verdict:
    """One sentence, judged."""

    ok: bool
    sentence: str
    reason: str = ""
    #: The sentence without markers or its full stop.
    body: str = ""
    terminator: str = "."
    #: Source numbers (as the model wrote them) whose support was verified.
    cited: tuple[int, ...] = ()
    support: float = 0.0
    #: `{source number: (piece index, start, end)}` - the receipt quote spans.
    quotes: dict = field(default_factory=dict)


_HEADER_LINE = re.compile(r"^\s*(?:subject|from|to|cc|bcc|date|sent)\s*:", re.I)


def _windows(text: str) -> list[tuple[int, int]]:
    """Every sentence, and every pair of adjacent sentences, as spans.

    **Mail header lines are not windows.** "Subject: Approved: licence purchase" and
    "From: priya.n@acme.com" sit side by side and would make "approval and purchase
    and priya" look like a phrase. They still count as evidence for *names* (the
    source's metadata carries the sender), and never as a place a claim about what a
    message says can be supported.
    """
    spans = [span for span in sentence_spans(text)
             if not _HEADER_LINE.match(text[span[0]:span[1]])]
    out = list(spans)
    out += [(spans[i][0], spans[i + 1][1]) for i in range(len(spans) - 1)]
    return out


class Verifier:
    """Judges sentences against the sources of one answer."""

    def __init__(self, sources: Sequence[Source], *, threshold: float = SUPPORT,
                 similarity: Optional[Callable[[str, str], float]] = None) -> None:
        self.sources = {s.n: s for s in sources}
        self.threshold = float(threshold)
        self.similarity = similarity
        self._windows: dict[tuple[int, int], list[tuple[int, int, int, set[str]]]] = {}
        self._context: dict[int, set[str]] = {}

    # -- the windows of a source, with their token sets ------------------------------

    def _title_text(self, n: int) -> str:
        """The document's title line, if it has one - as evidence for names and
        figures ("Leeds site safety report" is what makes the observations in the
        next sentence Leeds's)."""
        first = self.sources[n].pieces[0].text if self.sources[n].pieces else ""
        spans = sentence_spans(first)
        if len(spans) > 1 and spans[0][1] - spans[0][0] <= TITLE_MAX_CHARS:
            return first[spans[0][0]:spans[0][1]]
        return ""

    def _rescued(self, body: str, per_source: dict, accepted: Sequence[int]) -> bool:
        """Does the embedding similarity, where there is one, say this sentence is
        close enough in meaning to the passage it cites? A rescue for a synonym
        ("purchase" for the source's "buy"), never for a figure or a name."""
        if self.similarity is None:
            return False
        try:
            best = max(accepted, key=lambda k: per_source[k][0])
            idx, s, e = per_source[best][1]
            return self.similarity(body, self.sources[best].pieces[idx].text[s:e]) >= RESCUE_SIMILARITY
        except Exception:                                 # noqa: BLE001 - a rescue only
            return False

    def _context_of(self, n: int) -> set[str]:
        """Words a sentence about this document may take for granted: its file name
        and mail subject, and its title line. "The excess is 250 pounds" is
        supported by a passage that says "Excess is 250 pounds" under a heading that
        says "Car insurance renewal" - the heading is what makes it about the car."""
        if n not in self._context:
            source = self.sources[n]
            tokens = support_tokens(source.meta)
            first = source.pieces[0].text if source.pieces else ""
            spans = sentence_spans(first)
            if len(spans) > 1 and spans[0][1] - spans[0][0] <= TITLE_MAX_CHARS:
                tokens |= support_tokens(first[spans[0][0]:spans[0][1]])
            self._context[n] = tokens
        return self._context[n]

    def _source_windows(self, n: int) -> list[tuple[int, int, int, set[str]]]:
        """`[(piece, start, end, tokens)]` for every one- and two-sentence window."""
        key = (n, 0)
        if key not in self._windows:
            built: list[tuple[int, int, int, set[str]]] = []
            for index, piece in enumerate(self.sources[n].pieces):
                for start, end in _windows(piece.text):
                    built.append((index, start, end, support_tokens(piece.text[start:end])))
            self._windows[key] = built
        return self._windows[key]

    def _best_window(self, n: int, wanted: set[str]) -> tuple[float, tuple[int, int, int], set[str]]:
        """`(score, (piece, start, end), tokens)` of the window of source `n` that
        best supports `wanted`.

        The window is **chosen on its own words**; the document's title and metadata
        are then allowed to make up the rest. (Chosen the other way round, a title
        that every window shares makes every window score the same and the receipt
        points at whichever is shortest.) Nothing is supported by context alone: a
        source none of whose sentences says anything relevant supports nothing.
        """
        best_raw, best_span, best_tokens = 0.0, (0, 0, 0), set()
        for index, start, end, tokens in self._source_windows(n):
            raw = len(tokens & wanted) / len(wanted)
            # Shorter windows win ties: a receipt should point at one sentence
            # when one sentence is enough.
            length = end - start
            if raw > best_raw + 1e-9 or (
                    abs(raw - best_raw) <= 1e-9 and raw > 0
                    and length < best_span[2] - best_span[1]):
                best_raw, best_span, best_tokens = raw, (index, start, end), tokens
        if best_raw <= 0:
            return 0.0, best_span, set()
        covered = best_tokens | self._context_of(n)
        return len(covered & wanted) / len(wanted), best_span, covered

    # -- the judgement -------------------------------------------------------------

    def verify(self, sentence: str) -> Verdict:
        """Judge one sentence. Never raises.

        **A wrong number is repaired, a wrong claim is not.** A small model often
        writes a true sentence and puts the wrong source number after it. If the
        cited source does not support the sentence but exactly one *other* shown
        source does - by the same full test, figures and names included - the
        sentence is kept and its receipt is the source that actually supports it.
        The guarantee is that the receipt supports the sentence; it never depended
        on the model numbering its sources correctly.
        """
        try:
            verdict = self._verify(sentence)
            if not verdict.ok and verdict.reason in _REPAIRABLE or (
                    not verdict.ok and verdict.reason.startswith(_REPAIRABLE_PREFIXES)):
                repaired = self._repair(sentence, verdict)
                if repaired is not None:
                    return repaired
            return verdict
        except Exception as exc:                          # noqa: BLE001 - a verifier that
            # crashes must fail CLOSED: an unjudged sentence is a dropped one.
            return Verdict(False, sentence, reason=f"could not be checked ({type(exc).__name__})")

    def _repair(self, sentence: str, failed: Verdict) -> Optional[Verdict]:
        body, numbers = split_markers(_ANSWER_LABEL.sub("", sentence.strip()))
        if not numbers or len(self.sources) > 12:
            return None
        passing: list[Verdict] = []
        for n in self.sources:
            if n in numbers:
                continue
            trial = self._verify(f"{failed.body} [{n}]{failed.terminator}")
            if trial.ok:
                passing.append(trial)
        if len(passing) != 1:
            return None                     # none, or more than one: guessing is not repairing
        return passing[0]

    def _verify(self, sentence: str) -> Verdict:
        raw = _ANSWER_LABEL.sub("", sentence.strip())
        body, numbers = split_markers(raw)
        terminator = body[-1] if body and body[-1] in ".!?" else "."
        body = body.rstrip(".!? ").strip()
        if not body:
            return Verdict(False, sentence, reason="empty")
        if not numbers:
            return Verdict(False, sentence, body=body, reason="no source marker")
        unknown = [n for n in numbers if n not in self.sources]
        if unknown:
            return Verdict(False, sentence, body=body,
                           reason=f"cites source {unknown[0]}, which was not shown")

        wanted = support_tokens(body)
        if len(wanted) < MIN_TOKENS:
            return Verdict(False, sentence, body=body, reason="too little to check")

        cited = list(dict.fromkeys(numbers))

        # 2b - quotation marks are a claim about exact words.
        folded_body = fold_quotes(body)
        quote_spans: dict[int, tuple[int, int, int]] = {}
        for quoted in re.findall(r'"([^"]{6,})"', folded_body):
            if len(quoted.split()) < 2 and len(quoted) < 12:
                continue
            found = self._find_verbatim(quoted, cited)
            if found is None:
                return Verdict(False, sentence, body=body, terminator=terminator,
                               reason="puts words in quotation marks that the source does not contain")
            quote_spans.setdefault(found[0], found[1:])

        # 2a - support, by the best window of each cited source.
        per_source: dict[int, tuple[float, tuple[int, int, int], set[str]]] = {}
        for n in cited:
            per_source[n] = self._best_window(n, wanted)
        union: set[str] = set()
        for _score, _span, tokens in per_source.values():
            union |= tokens & wanted
        support = len(union) / len(wanted)

        rescued = False
        if support < self.threshold - 1e-9:
            if self.similarity is not None and support >= self.threshold * 0.6:
                try:
                    best_n = max(per_source, key=lambda k: per_source[k][0])
                    idx, s, e = per_source[best_n][1]
                    if self.similarity(body, self.sources[best_n].pieces[idx].text[s:e]) >= RESCUE_SIMILARITY:
                        rescued = True
                except Exception:                         # noqa: BLE001 - a rescue only
                    rescued = False
            if not rescued:
                return Verdict(False, sentence, body=body, terminator=terminator, support=support,
                               reason=f"is only {support:.0%} supported by the source it cites")

        # A source is kept only if it contributes something.
        accepted = [n for n in cited if per_source[n][0] >= 0.2 or n in quote_spans]
        if not accepted:
            return Verdict(False, sentence, body=body, terminator=terminator, support=support,
                           reason="its source does not contain what it says")

        evidence = " ".join(
            self.sources[n].pieces[per_source[n][1][0]].text[per_source[n][1][1]:per_source[n][1][2]]
            for n in accepted)
        metas = " ".join(self.sources[n].meta + " " + self._title_text(n) for n in accepted)
        everything = evidence + " " + metas

        # ...and its words must sit together in it, not merely be in it.
        share = _adjacent_share(
            _word_sequence(body),
            [_word_sequence(self.sources[n].pieces[per_source[n][1][0]].text[
                per_source[n][1][1]:per_source[n][1][2]]) for n in accepted])
        if share < ADJACENT_MIN - 1e-9 and not self._rescued(body, per_source, accepted):
            return Verdict(False, sentence, body=body, terminator=terminator, support=support,
                           reason="strings together words from its source without saying what it says")

        # negation must agree with at least one supporting window
        body_negated = bool(_NEGATION.search(body))
        windows_negated = [bool(_NEGATION.search(self.sources[n].pieces[per_source[n][1][0]].text[
            per_source[n][1][1]:per_source[n][1][2]])) for n in accepted]
        if body_negated not in windows_negated:
            return Verdict(False, sentence, body=body, terminator=terminator, support=support,
                           reason="says the opposite of its source")

        # 2c - numbers, months and names must be in the evidence.
        have_numbers = extract_numbers(everything)
        for number in extract_numbers(body):
            if number not in have_numbers:
                return Verdict(False, sentence, body=body, terminator=terminator, support=support,
                               reason=f"contains the figure {number}, which the source does not")
        have_months = _months_in(everything)
        for month in _months_in(body):
            if month not in have_months:
                return Verdict(False, sentence, body=body, terminator=terminator, support=support,
                               reason=f"names a month ({_MONTHS[month - 1].title()}) the source does not")
        evidence_tokens = _evidence_tokens(everything)
        for name in _names_in(body):
            if not _name_present(name, evidence_tokens, everything):
                return Verdict(False, sentence, body=body, terminator=terminator, support=support,
                               reason=f"names '{name}', who is not in the source")

        quotes = {n: quote_spans.get(n, per_source[n][1]) for n in accepted}
        return Verdict(True, sentence, body=body, terminator=terminator, cited=tuple(accepted),
                       support=support, quotes=quotes)

    def _find_verbatim(self, quoted: str, cited: Sequence[int]) -> Optional[tuple[int, int, int, int]]:
        """`(source, piece, start, end)` of `quoted` inside a cited source's own
        text - exact, modulo whitespace and typography."""
        needle = normalise_space(fold_quotes(quoted))
        for n in cited:
            for index, piece in enumerate(self.sources[n].pieces):
                haystack, mapping = _fold_with_map(piece.text)
                at = haystack.find(needle)
                if at != -1:
                    start = mapping[at]
                    end = mapping[at + len(needle) - 1] + 1
                    return n, index, start, end
        return None


def _fold_with_map(text: str) -> tuple[str, list[int]]:
    """The text with quotes folded and whitespace collapsed, and for each folded
    character the index of the original it came from."""
    folded = fold_quotes(text)
    out: list[str] = []
    mapping: list[int] = []
    previous_space = True
    for index, ch in enumerate(folded):
        if ch.isspace():
            if not previous_space:
                out.append(" ")
                mapping.append(index)
            previous_space = True
        else:
            out.append(ch)
            mapping.append(index)
            previous_space = False
    return "".join(out).rstrip(), mapping


# ---------------------------------------------------------------------------
# streaming assembly: sentences in, verified text and receipts out
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Accepted:
    """A verified sentence, ready to show."""

    text: str                  # `body [1][2].` with display numbers
    body: str
    display: tuple[int, ...]   # display numbers it carries
    new_receipts: tuple[Receipt, ...] = ()
    verdict: Optional[Verdict] = None


class AnswerAssembler:
    """Takes raw model text a piece at a time; gives back only verified sentences.

    Sentences are judged as they complete, so the first verified one can be shown
    while the model is still writing the rest. **Numbering is by first mention and
    never changes**: the first source cited in the answer is `[1]` for the rest of
    it, whatever number the model gave it (work order 4e-2).
    """

    def __init__(self, verifier: Verifier) -> None:
        self.verifier = verifier
        self._buffer = ""
        self._order: list[int] = []
        self._receipts: dict[int, Receipt] = {}
        self.accepted: list[Accepted] = []
        self.dropped: list[tuple[str, str]] = []
        self._seen: set[str] = set()
        self.raw = ""

    # -- feeding ------------------------------------------------------------------------

    def feed(self, piece: str) -> list[Accepted]:
        self.raw += piece
        self._buffer += piece
        return self._judge(self._drain(final=False))

    def flush(self) -> list[Accepted]:
        return self._judge(self._drain(final=True))

    def _drain(self, *, final: bool) -> list[str]:
        # **A marker that is half-arrived is not part of the text yet.** "ends. [" is
        # a full stop followed by an opening bracket, which the sentence splitter
        # reads as the start of the next sentence - so the sentence would be judged
        # without the marker that is about to complete it.
        tail = ""
        head = self._buffer
        partial = _PARTIAL_MARKER.search(head)
        if partial is not None and not final:
            head, tail = head[:partial.start()], head[partial.start():]
        text = normalise_marker_placement(head)
        spans = sentence_spans(text)
        if not spans:
            if final:
                self._buffer = ""
            return []
        done: list[str] = []
        upto = 0
        for index, (start, end) in enumerate(spans):
            last = index == len(spans) - 1
            if last and not final and not self._finished(text, start, end):
                break
            done.append(text[start:end])
            upto = end
        self._buffer = "" if final else text[upto:] + tail
        return done

    @staticmethod
    def _finished(text: str, start: int, end: int) -> bool:
        """Is the last sentence in the buffer surely complete?

        Only if it ends in a marker (and its full stop) and something has already
        arrived after it - a model that writes "...500. [1]" would otherwise be
        judged before its marker turned up.
        """
        sentence = text[start:end]
        closed = re.search(r"\]\s*[.!?][\"')\]]*$", sentence) or re.search(r"\]$", sentence)
        return bool(closed) and end < len(text)

    def _judge(self, sentences: Sequence[str]) -> list[Accepted]:
        out: list[Accepted] = []
        for sentence in sentences:
            verdict = self.verifier.verify(sentence)
            if not verdict.ok:
                if sentence.strip():
                    self.dropped.append((sentence.strip(), verdict.reason))
                continue
            key = normalise_space(verdict.body).lower()
            if key in self._seen:
                continue
            self._seen.add(key)
            accepted = self._accept(verdict)
            self.accepted.append(accepted)
            out.append(accepted)
        return out

    def _accept(self, verdict: Verdict) -> Accepted:
        display: list[int] = []
        fresh: list[Receipt] = []
        for n in verdict.cited:
            if n not in self._receipts:
                piece, start, end = verdict.quotes[n]
                source = self.verifier.sources[n]
                start, end = snap_span(source.pieces[piece].text, start, end)
                self._receipts[n] = source.receipt(piece, start, end)
                self._order.append(n)
                fresh.append(self._receipts[n])
            display.append(self._order.index(n) + 1)
        marks = "".join(f"[{k}]" for k in dict.fromkeys(display))
        return Accepted(text=f"{verdict.body} {marks}{verdict.terminator}", body=verdict.body,
                        display=tuple(dict.fromkeys(display)), new_receipts=tuple(fresh),
                        verdict=verdict)

    # -- the result ---------------------------------------------------------------------

    def text(self) -> str:
        return " ".join(a.text for a in self.accepted)

    def receipts(self) -> list[Receipt]:
        """Receipts in display order: `receipts()[n - 1]` is what `[n]` cites."""
        return [self._receipts[n] for n in self._order]

    def source_order(self) -> list[int]:
        return list(self._order)


# ---------------------------------------------------------------------------
# the independent audit
# ---------------------------------------------------------------------------


def audit_turn(turn: ChatTurn) -> list[str]:
    """The sentences of a finished turn that lack a receipt. **Empty is the goal.**

    Checks the *finished* turn, not the process that made it: for an assistant
    turn of `kind="answer"`, every sentence must carry a marker `[n]` with
    `1 <= n <= len(receipts)`, and every receipt must hold a non-empty quote.
    Other kinds carry text the system wrote from a template - a count, "that's
    all 7", the searched-and-found-nothing protocol - and make no claim about what
    any document says, so they are not audited here (`app/chat/absence.py` and
    `aggregate.py` test their own templates).
    """
    if turn.role != "assistant" or turn.kind != "answer":
        return []
    problems: list[str] = []
    text = normalise_marker_placement(turn.text or "")
    for start, end in sentence_spans(text):
        sentence = text[start:end]
        _body, numbers = split_markers(sentence)
        if not numbers:
            problems.append(sentence)
            continue
        if any(n < 1 or n > len(turn.receipts) for n in numbers):
            problems.append(sentence)
            continue
        if any(not turn.receipts[n - 1].quote.strip() for n in numbers):
            problems.append(sentence)
    return problems
