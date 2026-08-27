r"""Somebody pasted an error message. Do not treat it as a bag of words.

Layer: L4 — pure. Takes the raw text of a query and decides whether it was
typed or pasted. No store, no engine.

**The most common search a developer makes, and tokenisation breaks it.**
Pasting `AttributeError: 'NoneType' object has no attribute 'chunk_id'` into a
box that splits on punctuation and ORs the pieces asks for every document
containing *object*, or *has*, or *no*. Measured on a four-document corpus
where two files genuinely contain that line:

    today, as a bag of ORed words   4 of 4 documents - both irrelevant ones
    as a phrase, order preserved    2 of 2 - exactly the files that have it

**What this is not.** §4a asks for "verbatim substring matching over the
existing trigram index". That index does not exist for content: `files_fts`
is trigram but covers file *names* and folders, and `chunks_fts` - the text -
is `porter unicode61`, which is token-based and cannot answer a substring
query at all. True verbatim matching over content would need a second FTS
table with a trigram tokeniser over every chunk: a schema change, a full
re-index of the whole corpus, and roughly the same storage again. That is the
owner's call, not one to make inside a delivery.

**So this preserves order rather than punctuation**, which is the half that
matters for finding a pasted line: the distinctive thing about a traceback is
the sequence of its words, not its colons. It needs no re-index and it is
already a large improvement on ORing them.
"""

from __future__ import annotations

import re
from typing import Sequence

__all__ = ["looks_pasted", "as_phrase", "MIN_WORDS", "MIN_LENGTH"]

#: Shorter than this is a query, whatever punctuation it carries.
#:
#: `foo()` and `a:b` are things people type on purpose; a pasted line is long.
#: **Four words**, because three is `raise ValueError x` - still plausibly
#: typed - and five would miss `File "x.py", line 5`.
MIN_WORDS = 4

#: And this many characters. Both apply: a run of very short words is not a
#: pasted line either.
MIN_LENGTH = 24

#: Words that only appear in something pasted [TUNE]. Each is a strong enough
#: signal on its own that the shape tests below are not consulted.
_TELLS = re.compile(r"""(?ix)
    \b(?:
        traceback | stacktrace | exception \s+ in | caused \s+ by
      | at \s+ [\w.$]+ \( [\w.]+ : \d+ \)      # a Java frame
      | [\w./\\-]+ \. (?:py|js|ts|java|cs|rb|go|rs|cpp|c|h) : \d+
    )\b""")

#: `File "app/search/engine.py", line 512` and `engine.py:512`.
_PATH_LINE = re.compile(r"""(?ix)
    (?: \bline \s+ \d+ \b
      | [\w./\\-]+ \.\w{1,5} : \d+ )""")

_WORD = re.compile(r"[A-Za-z0-9_]+")

#: Punctuation that is structural in code and rare in a typed question.
_CODEY = re.compile(r"""[(){}\[\]<>;=|]""")


def _quote_pairs(text: str) -> int:
    """Balanced quote pairs. Two of them is a signature of pasted code."""
    return min(text.count("'") // 2, 3) + min(text.count('"') // 2, 3)


def looks_pasted(text: str) -> bool:
    r"""Whether this arrived from a clipboard rather than from a keyboard.

    **Deliberately hard to trigger.** Getting this wrong in the false-positive
    direction turns an ordinary multi-word search into a phrase search, which
    silently narrows it - the failure this whole order exists to remove. So a
    named tell (`Traceback`, a `file.py:512`) is enough on its own, and
    everything else has to be long *and* carry two independent signs of being
    code.
    """
    raw = str(text or "").strip()
    if not raw:
        return False

    # **A named tell outranks the size gates**, because `engine.py:512` is
    # unambiguous at twenty-three characters and the length rule would have
    # thrown it away - which it did, until a run of real examples showed it.
    # Nothing else somebody types looks like a path and a line number.
    if _TELLS.search(raw):
        return True

    if len(raw) < MIN_LENGTH:
        return False
    words = _WORD.findall(raw)
    if len(words) < MIN_WORDS:
        return False

    signs = 0
    if "\n" in raw:
        # Nobody types a newline into a search box.
        signs += 2
    if _PATH_LINE.search(raw):
        signs += 2
    if _quote_pairs(raw) >= 2:
        signs += 1
    if len(_CODEY.findall(raw)) >= 2:
        signs += 1
    if ":" in raw and any(char in raw for char in "()[]{}"):
        signs += 1
    # **A camelCase or PascalCase identifier beside punctuation.**
    # `AttributeError` in a sentence somebody typed is already unusual.
    if re.search(r"\b[a-z]+[A-Z]\w*|\b[A-Z][a-z]+[A-Z]\w*", raw):
        signs += 1
    return signs >= 2


def as_phrase(text: str, *, limit: int = 24) -> str:
    r"""The pasted text as one phrase: its words, in order.

    **Order is what makes it findable.** The distinctive thing about a
    traceback line is its sequence, not its colons - and FTS5 can match a
    sequence of tokens even though it cannot match the punctuation between
    them.

    Capped, because a whole pasted stack trace is hundreds of tokens and an
    adjacency query that long can match nothing at all once one word was
    chunked away from the next. The first `limit` words of a paste are the
    error line; the rest is the frames below it.
    """
    words = _WORD.findall(str(text or ""))[:max(1, int(limit))]
    return " ".join(words)


def phrase_words(text: str, *, limit: int = 24) -> Sequence[str]:
    """The words `as_phrase` would use, for a caller that wants them apart."""
    return tuple(_WORD.findall(str(text or ""))[:max(1, int(limit))])
