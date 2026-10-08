"""Natural prose in, only what the files support out.

Layer: L8b - no Qt, no network, no model. Owner requirement 2026-09-20, point 3:
"a claim about what is IN the archive must be supported by a retrieved passage
(keep the verifier for archive claims; drop or soften unsupported archive claims
instead of blocking the whole answer)".

An archive answer is now written as flowing prose and **streamed as it is
generated**. It is checked when it is complete, one sentence at a time, by the same
`Verifier` the extract-and-quote design used - so the guarantee moved from "no
unverified word is ever on screen" to "**no unverified claim about the files is in the
answer that is kept**". This module is where it is kept:

* a sentence with source markers `[n]` is verified against the sources it cites
  (words found together in the passage, negation agrees, every figure, date, name
  and quotation exists - see `app/chat/verify.py`); a sentence that fails is
  **dropped**, not the answer - or, **softened**: a fluent model adds a tail the
  passage does not carry ("Your landlord is Margaret Okafor, as stated in the deposit
  letter and her reply"), and when only its *support* falls short (never a figure, a
  name, a quotation or a negation) the sentence is cut back to the clause that the
  passage does support and that clause is kept;
* a sentence with *no* marker is a claim about the files in disguise if it says what
  a passage says - so it is looked for in the sources: if it is close to a passage it
  must pass the same verification against it (and then gets that passage's marker),
  and if it fails - a reversed meaning, a changed figure - it is dropped;
* a sentence that matches no passage is general prose ("Let's take it one step at a
  time") and stays, **unless it states a figure, month, name or quotation that is
  found in none of the sources** - the shape of an invented specific - in which case
  it is dropped too. General knowledge belongs in a paragraph that starts "In
  general," (the prompt says so), which is exempt from that last check because it is
  labelled as not being from the files - but not from the first two;
* what survives is renumbered by first mention, and each number gets a `Receipt`
  whose quote is a slice of the stored text chosen by the system, never the model's.

Markdown structure (bullets, numbered items, headings, tables, code fences) is kept:
only the sentences inside a line are judged, and a line left empty is removed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

from app.chat.text import fold_quotes, normalise_space, sentence_spans, snap_span
from app.chat.types import Receipt
from app.chat.verify import (
    MARKER, MIN_TOKENS, Verdict, Verifier, _months_in, _name_present, _names_in,
    _evidence_tokens, extract_numbers, normalise_marker_placement, split_markers,
    support_tokens,
)

__all__ = ["Reconciled", "reconcile", "audit_answer", "GENERAL_LEAD"]

#: A paragraph or line that starts like this is background knowledge, not a claim
#: about the files. It may not carry a source marker's authority (a marker on it is
#: still verified), but its unmarked sentences are not searched for invented specifics.
GENERAL_LEAD = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:\*\*|_)?(?:in general|generally|from the web)\b", re.I)

_LINE_PREFIX = re.compile(r"^(\s*(?:[-*+]\s+|\d+[.)]\s+|#{1,6}\s+|>\s*)*)(.*)$", re.S)
_FENCE = re.compile(r"^\s*(```|~~~)")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_RULE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_STRIP_MD = re.compile(r"(\*\*|__|`|(?<!\w)\*(?=\w)|(?<=\w)\*(?!\w)|(?<!\w)_(?=\w)|(?<=\w)_(?!\w))")
_TAIL = re.compile(r"^(.*?)([.!?]+)?((?:\*\*|__|[)\"'”’*_`])*)\s*$", re.S)
_QUOTED = re.compile(r'"([^"]{6,})"')
#: Models write "[1], [2]" and "[1] [2]"; the verifier and the tab read "[1][2]".
_MARKER_GAP = re.compile(r"(\])\s*[,;]?\s*(?=\[\d)")
#: The reasons a sentence fails on *how much* of it the passage carries - the only failures
#: softening may act on. A wrong figure, a name, a quotation or a reversed meaning is never
#: cut around: that sentence is wrong, not long.
_SHORT_OF_SUPPORT = ("is only ", "strings together words")
#: An unmarked sentence with at least this share of its meaningful words in one passage is a claim
#: about the files, not conversation - even if it does not clear the verification threshold.
_CLAIM_LIKE = 0.5
#: Where a fluent tail may be cut: at a comma, semicolon or dash, or where a trailing
#: attribution or elaboration begins.
_CLAUSE_CUT = re.compile(
    r"\s*[,;\u2013\u2014]\s+|\s+-\s+|\s+(?=(?:as (?:stated|shown|mentioned|described|noted|per|set out)"
    r"|according to|based on|which|where|including|so that|because|and (?:the|her|his|their|its)\b))",
    re.I)
_NAME_REFUSAL = re.compile(r"names '(.+)', who is not in the source")


@dataclass
class Reconciled:
    """The kept answer."""

    text: str = ""
    receipts: list[Receipt] = field(default_factory=list)
    #: Sentences kept because a passage supports them.
    supported: int = 0
    #: Sentences kept that make no claim about the files.
    general: int = 0
    #: `(sentence, reason)` for every sentence that was taken out.
    dropped: list[tuple[str, str]] = field(default_factory=list)

    @property
    def partial(self) -> bool:
        """Whether any sentence was taken out."""
        return bool(self.dropped)

    @property
    def has_support(self) -> bool:
        """Whether at least one kept sentence stands on a passage."""
        return self.supported > 0


_ORDINAL = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\b", re.I)


def _plain(text: str) -> str:
    """The sentence as words, for checking: markdown emphasis removed, and "1st of March"
    read as "1 of March" - a fluent model says the ordinal, the document says the number
    (seen with mistral, 2026-09-20), and it is the same date."""
    return normalise_space(_ORDINAL.sub(r"\1", _STRIP_MD.sub("", text)))


def _with_marks(sentence: str, numbers: Sequence[int]) -> str:
    """`sentence` without its markers, and with `numbers` put where the full stop is."""
    bare = normalise_space(MARKER.sub("", sentence))
    bare = re.sub(r"\s+([.!?,;:])", r"\1", bare)
    if not numbers:
        return bare
    marks = "".join(f"[{n}]" for n in dict.fromkeys(numbers))
    found = _TAIL.match(bare)
    if found is None:
        return f"{bare} {marks}"
    head, stop, close = found.group(1), found.group(2) or "", found.group(3) or ""
    return f"{head.rstrip()} {marks}{stop}{close}"


class _Judge:
    """One answer's sentences, judged against its sources."""

    def __init__(self, verifier: Verifier, known_text: str) -> None:
        self.verifier = verifier
        self.sources = verifier.sources
        self._evidence = " ".join(
            " ".join(p.text for p in s.pieces) + " " + s.meta + " " + s.name
            for s in self.sources.values()) + " " + known_text
        self._typed_words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'-]*", known_text)}
        self._tokens = _evidence_tokens(self._evidence)
        self._numbers = extract_numbers(self._evidence)
        self._months = _months_in(self._evidence)

    # -- a sentence that carries its own markers ----------------------------------

    def cited(self, sentence: str) -> tuple[Verdict, str]:
        """`(verdict, the sentence as kept)`: the verifier's verdict, possibly after softening
        (see the module docstring), with one allowance: **a name the person typed
        themselves is not an invented one.** "What is the rent for Bartholomew?" is
        answered "The rent for Bartholomew is ..." - the verifier, which knows only the
        passages, would refuse the name. So a sentence that fails *only* because of a name
        that is in the conversation is judged again without that name (the sentence that is
        kept still has it; every other word, figure and quotation is still checked)."""
        plain = _plain(sentence)
        verdict = self.verifier.verify(plain)
        for _attempt in range(3):
            found = _NAME_REFUSAL.match(verdict.reason or "")
            if verdict.ok or found is None or not self._typed(found.group(1)):
                break
            plain = re.sub(re.escape(found.group(1)) + r"[\s,]*", "", plain, count=1,
                           flags=re.IGNORECASE).strip()
            verdict = self.verifier.verify(plain)
        if verdict.ok or not (verdict.reason or "").startswith(_SHORT_OF_SUPPORT):
            return verdict, sentence
        return self._softened(sentence, verdict)

    def _softened(self, sentence: str, failed: Verdict) -> tuple[Verdict, str]:
        """The longest head of `sentence` that the passage does support, or the failure."""
        body, numbers = split_markers(sentence)            # as written: the ordinal and the emphasis stay
        marks = "".join(f"[{n}]" for n in dict.fromkeys(numbers))
        body = body.rstrip(".!? ")
        cuts = [m.start() for m in _CLAUSE_CUT.finditer(body)]
        for at in reversed(cuts):
            head = body[:at].rstrip(" ,;-\u2013\u2014").strip()
            if len(head.split()) < 3:
                continue
            trial = self.verifier.verify(f"{_plain(head)} {marks}.")
            if trial.ok:
                return trial, f"{head}."
        return failed, sentence

    def _typed(self, name: str) -> bool:
        """Every word of `name` is one the person has already written."""
        return all(word.lower() in self._typed_words for word in re.findall(r"[A-Za-z][A-Za-z'-]*", name))

    # -- a sentence with none -------------------------------------------------------

    def uncited(self, sentence: str, *, labelled_general: bool) -> tuple[str, Optional[Verdict], str, str]:
        """`("keep"|"attach"|"drop", verdict, reason, the sentence as kept)` for a sentence
        with no marker."""
        body = _plain(sentence)
        folded = fold_quotes(body)
        for quoted in _QUOTED.findall(folded):
            if len(quoted.split()) < 2 and len(quoted) < 12:
                continue
            if self.verifier._find_verbatim(quoted, list(self.sources)) is None:
                return "drop", None, "puts words in quotation marks that no source contains", sentence

        wanted = support_tokens(body)
        if len(wanted) >= MIN_TOKENS and self.sources:
            best_n, best = 0, 0.0
            for n in self.sources:
                score = self.verifier._best_window(n, wanted)[0]
                if score > best:
                    best_n, best = n, score
            terminator = body[-1] if body and body[-1] in ".!?" else "."
            if best >= self.verifier.threshold - 1e-9:
                trial = self.verifier.verify(f"{body.rstrip('.!? ')} [{best_n}]{terminator}")
                if trial.ok:
                    return "attach", trial, "", sentence
                return "drop", None, "says something close to a passage but not what it says", sentence
            if best >= _CLAIM_LIKE and not labelled_general:
                # More than half of it is in a passage and the rest is not: a claim about the
                # files with padding on it. Keep the clause the passage supports, or nothing.
                failed = Verdict(False, body, reason="is only partly supported")
                soft, shortened = self._softened(f"{body.rstrip('.!? ')} [{best_n}]{terminator}", failed)
                if soft.ok:
                    return "attach", soft, "", shortened
                return "drop", None, "says part of what a passage says and adds what it does not", sentence

        if labelled_general:
            return "keep", None, "", sentence
        for number in extract_numbers(body):
            if number not in self._numbers:
                return "drop", None, f"states the figure {number}, which is in none of the sources", sentence
        for month in _months_in(body):
            if month not in self._months:
                return "drop", None, "names a month that is in none of the sources", sentence
        for name in _names_in(body):
            if not _name_present(name, self._tokens, self._evidence):
                return "drop", None, f"names '{name}', who is in none of the sources", sentence
        return "keep", None, "", sentence


def reconcile(raw: str, verifier: Verifier, *, known_text: str = "") -> Reconciled:
    """Judge a finished prose answer. **Never raises**; an internal failure keeps
    nothing (fails closed), which the caller reads as "no supported answer".

    `known_text` is everything the person has already said in this conversation
    (the question, earlier questions): a name or figure they typed themselves is not
    an invented one.
    """
    try:
        return _reconcile(raw, verifier, known_text)
    except Exception as exc:                                   # noqa: BLE001 - fail closed
        return Reconciled(dropped=[(str(raw)[:120], f"could not be checked ({type(exc).__name__})")])


def _reconcile(raw: str, verifier: Verifier, known_text: str) -> Reconciled:
    judge = _Judge(verifier, known_text)
    out = Reconciled()
    order: list[int] = []                       # source numbers, by first mention
    receipts: dict[int, Receipt] = {}
    lines_out: list[str] = []
    in_fence = False
    general_block = False

    def show(verdict: Verdict) -> list[int]:
        shown: list[int] = []
        for n in verdict.cited:
            if n not in receipts:
                piece, start, end = verdict.quotes[n]
                source = verifier.sources[n]
                start, end = snap_span(source.pieces[piece].text, start, end)
                receipts[n] = source.receipt(piece, start, end)
                order.append(n)
            shown.append(order.index(n) + 1)
        return shown

    for line in normalise_marker_placement(_MARKER_GAP.sub(r"\1", raw or "")).replace("\r\n", "\n").split("\n"):
        if _FENCE.match(line):
            in_fence = not in_fence
            lines_out.append(line)
            continue
        if in_fence or not line.strip():
            lines_out.append(line)
            if not line.strip():
                general_block = False
            continue
        if _TABLE_RULE.match(line):
            lines_out.append(line)
            continue
        prefix, body = _LINE_PREFIX.match(line).groups()
        if GENERAL_LEAD.match(line):
            general_block = True
        pieces: list[str] = []
        for start, end in (sentence_spans(body) or [(0, len(body))]):
            sentence = body[start:end].strip()
            if not sentence:
                continue
            _plain_body, numbers = split_markers(sentence)
            if numbers:
                verdict, as_kept = judge.cited(sentence)
                if verdict.ok:
                    pieces.append(_with_marks(as_kept, show(verdict)))
                    out.supported += 1
                else:
                    out.dropped.append((sentence, verdict.reason))
                continue
            action, verdict, reason, as_kept = judge.uncited(sentence, labelled_general=general_block)
            if action == "drop":
                out.dropped.append((sentence, reason))
            elif action == "attach" and verdict is not None:
                pieces.append(_with_marks(as_kept, show(verdict)))
                out.supported += 1
            else:
                pieces.append(_with_marks(sentence, ()))
                out.general += 1
        if pieces:
            lines_out.append(prefix + " ".join(pieces))
        elif _TABLE_ROW.match(line):
            pass
    text = "\n".join(lines_out)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    out.text = text
    out.receipts = [receipts[n] for n in order]
    return out


def audit_answer(turn) -> list[str]:
    """The sentences of a finished conversational answer that make an unreceipted claim.
    **Empty is the goal.** The independent re-check, from the finished turn alone.

    The contract of an `answer` turn (see the module docstring): a sentence with a
    marker `[n]` must point at a receipt (`1 <= n <= len(receipts)`) whose quote is
    non-empty; a sentence with no marker is allowed, but not if it states a figure,
    month, name or quotation that no receipt's quote contains - the shape of an
    invented specific - unless it sits in a paragraph that says it is general
    knowledge (`In general, ...`). (`audit_turn` in `verify.py` is the stricter check
    for the extract-and-quote shape, where *every* sentence carries a marker.)
    """
    if getattr(turn, "role", "") != "assistant" or getattr(turn, "kind", "") != "answer":
        return []
    receipts = list(getattr(turn, "receipts", None) or [])
    evidence = " ".join(str(getattr(r, "quote", "") or "") + " " + str(getattr(r, "name", "") or "")
                        for r in receipts)
    tokens = _evidence_tokens(evidence)
    numbers, months = extract_numbers(evidence), _months_in(evidence)
    problems: list[str] = []
    general = False
    for line in normalise_marker_placement(str(getattr(turn, "text", "") or "")).split("\n"):
        if not line.strip():
            general = False
            continue
        if GENERAL_LEAD.match(line):
            general = True
        for start, end in (sentence_spans(_LINE_PREFIX.match(line).group(2)) or []):
            sentence = _LINE_PREFIX.match(line).group(2)[start:end]
            _body, marks = split_markers(sentence)
            if marks:
                if any(n < 1 or n > len(receipts) for n in marks) or any(
                        not str(getattr(receipts[n - 1], "quote", "")).strip() for n in marks):
                    problems.append(sentence)
                continue
            plain = _plain(sentence)
            if any(v not in numbers for v in extract_numbers(plain)) and not general:
                problems.append(sentence)
            elif any(m not in months for m in _months_in(plain)) and not general:
                problems.append(sentence)
            elif not general and any(not _name_present(name, tokens, evidence)
                                     for name in _names_in(plain)):
                problems.append(sentence)
    return problems
