"""The owner's own words: "Mum" means Jane Example, "the house" means 12 Example Road.

Layer: L8b - pure. No store, no model, no Qt.

Order `smarter-answers` section 4 (owner, 2026-10-12). A person talks about
"Mum" and "the house"; their files say "Jane Example" and "12 Example Road".
No embedding closes that gap, because it is not a matter of meaning - it is a
fact only the owner knows. So the owner says it once, and Chat searches with
the meaning in place of the phrase ("emails from Mum" becomes a sender filter
on the real name). The person's own words are kept for the answer: the model
is told what the phrase means and may still say "Mum".

Applied by Chat and the Search page's answer card only - never silently to the
plain search box, where a word typed is a word searched for (D4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence

__all__ = ["Taught", "apply", "learn", "note_for_model", "MAX_PHRASE_WORDS", "MAX_MEANING"]

#: A phrase is a name for something, not a sentence. Not settings: a longer
#: "phrase" is almost always a sentence that happened to contain "is".
MAX_PHRASE_WORDS = 5
MAX_MEANING = 120


@dataclass(frozen=True)
class Taught:
    """What a "remember ..." or "forget ..." sentence asked for."""

    action: str          # "set" or "forget"
    phrase: str
    meaning: str = ""


_REMEMBER = re.compile(
    r"^(?:please\s+)?remember(?:\s+that)?\s+(?P<phrase>.+?)\s+(?:is|are|means|=)\s+"
    r"(?P<meaning>.+?)[.!]*$", re.IGNORECASE)
_FORGET = re.compile(
    r"^(?:please\s+)?forget(?:\s+(?:about|what))?\s+(?P<phrase>.+?)(?:\s+means)?[.!]*$",
    re.IGNORECASE)


def _clean(text: str) -> str:
    return " ".join(str(text or "").split()).strip(" \"'“”‘’")


def learn(text: str) -> Optional[Taught]:
    """`remember Mum is Jane Example` -> set; `forget Mum` -> forget; else None."""
    sentence = _clean(text)
    match = _REMEMBER.match(sentence)
    if match:
        phrase, meaning = _clean(match["phrase"]), _clean(match["meaning"])
        if (phrase and meaning and len(phrase.split()) <= MAX_PHRASE_WORDS
                and len(meaning) <= MAX_MEANING and phrase.casefold() != meaning.casefold()):
            return Taught("set", phrase, meaning)
        return None
    match = _FORGET.match(sentence)
    if match:
        phrase = _clean(match["phrase"])
        if phrase and len(phrase.split()) <= MAX_PHRASE_WORDS:
            return Taught("forget", phrase)
    return None


def apply(text: str, words: Sequence[tuple[str, str]]) -> tuple[str, list[tuple[str, str]]]:
    """`text` with each phrase replaced by its meaning, and which ones were used.

    Whole words only and case-insensitive, longest phrase first (so "my mum's
    house" wins over "mum"), a possessive kept ("Mum's" -> "Jane Example's").
    A phrase is replaced where it stands, once per mention.
    """
    out = str(text or "")
    used: list[tuple[str, str]] = []
    for phrase, meaning in sorted(((p, m) for p, m, *_ in words if p and m),
                                  key=lambda pm: -len(pm[0])):
        pattern = re.compile(r"(?<![\w'])" + re.escape(phrase) + r"(?![\w])", re.IGNORECASE)
        if pattern.search(out):
            out = pattern.sub(lambda _m, m=meaning: m, out)
            used.append((phrase, meaning))
    return out, used


def note_for_model(used: Sequence[tuple[str, str]]) -> str:
    """The line the answering model is given: what the person's own words mean."""
    if not used:
        return ""
    said = "; ".join(f'"{phrase}" means {meaning}' for phrase, meaning in used)
    return f"In the person's question, {said}."
