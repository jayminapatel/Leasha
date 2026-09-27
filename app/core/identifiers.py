r"""Splitting `getUserName` into `get user name`, in both directions.

Layer: L0 (used by both storage and search, so it sits below both)

**Measured before it was written, because most of this problem does not exist.**
FTS5's `unicode61` tokenizer already splits on every non-alphanumeric character,
so `get_user_name`, `reset-password` and `Order.Service` are *already* three
tokens each and already findable word by word. Verified against sqlite:

    query "user"     matches "get_user_name"
    query "user"     does not match "getUserName"
    query "password" does not match "ResetPasswordHandler"

So the whole gap is **camelCase and PascalCase** - identifiers whose word
boundaries are carried by capitalisation rather than punctuation. That is a much
smaller thing to fix than "code search needs a tokenizer", and it is the reason
this module is a hundred lines rather than a dependency.

## Both directions, and they need different work

**Document side.** `ResetPasswordHandler` is stored as one token, so no search
for `password` will ever reach it. Fixed by indexing the split parts alongside
the text - see `symbol_tokens`, and the `symbols` column it feeds.

**Query side.** Somebody who types `getUserName` should find `get_user_name`,
whose tokens are `get`, `user`, `name` and which therefore does not contain the
token `getusername`. Fixed by expanding the query - see `expand_term`.

Neither alone is enough, and the two are not symmetrical: the first costs index
space on every chunk, the second costs nothing until somebody types a compound
word.

## What is deliberately not split

**A word with no internal case change is left alone.** `password` splits to
nothing, `HTTP` splits to nothing. Emitting a copy of every ordinary word would
double the index for no gain, and at 600GB that is the difference between a
feature and a regret.

**Acronyms keep their run.** `XMLHttpRequest` is `XML`, `Http`, `Request` - not
`X`, `M`, `L`. The rule is that a run of capitals ends one word before the last
capital when a lowercase letter follows, which is the convention every language
in this corpus uses.

**Digits are boundaries but not words.** `parseJSON2Data` gives `parse`, `JSON`,
`2`, `Data`; the `2` is kept because version and format numbers are searched for
(`utf8`, `sha256`, `base64`) and dropping them loses more than it saves.
"""

from __future__ import annotations

import re
from typing import Iterable

__all__ = [
    "split_identifier",
    "symbol_tokens",
    "expand_term",
    "has_case_boundary",
    "MIN_PART_LENGTH",
    "MAX_SYMBOL_CHARS",
]

#: Parts shorter than this are dropped from the *indexed* forms.
#:
#: A single letter is noise: `aB` would contribute `a`, which matches an
#: enormous number of chunks and helps nobody. Two is the shortest thing worth
#: keeping - `id`, `db`, `ok`, `io` are all real and all searched for.
MIN_PART_LENGTH = 2

#: Ceiling on the symbols text stored per chunk.
#:
#: A minified JavaScript bundle or a generated file can be one long line of
#: camelCase, where the split forms approach the size of the text itself.
#: Doubling the FTS index across 600GB to serve a generated file nobody reads is
#: the wrong trade, so this truncates and the chunk keeps its ordinary tokens.
MAX_SYMBOL_CHARS = 4_000

#: A candidate identifier: letters and digits, at least two characters. Anything
#: with punctuation in it has already been split by the tokenizer, so this only
#: ever sees the pieces that are left.
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]+")

#: A word from `_WORD` that *could* have a boundary: one with a capital or a
#: digit somewhere after its first letter. Work order 0x item 5d - see
#: `symbol_tokens` for why this exists and why it finds exactly the same words.
#:
#: **The `(?<![A-Za-z])` at the front keeps it linear.** Without it, a long run
#: of lowercase letters (a DNA sequence, a squashed log line) is tried from
#: every one of its letters, each attempt reading to the end of the run - the
#: time grows with the square of the run's length. With it, only the first
#: letter of a run can start a match, which is also the only place a `_WORD`
#: match can start (a letter after a letter is always inside the word that
#: began before it).
_CANDIDATE = re.compile(r"(?<![A-Za-z])[A-Za-z][a-z]*[A-Z0-9][A-Za-z0-9]*")

#: The three boundaries, in one pass:
#:   lower|digit -> upper      getUser   -> get|User
#:   upper       -> upper+lower  XMLHttp -> XML|Http
#:   letter      -> digit        utf8    -> utf|8
_BOUNDARY = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])"
    r"|(?<=[A-Z])(?=[A-Z][a-z])"
    r"|(?<=[A-Za-z])(?=[0-9])"
)


def has_case_boundary(word: str) -> bool:
    """True if `word` is camelCase, PascalCase or letter-then-digit.

    The cheap test that decides whether a word is worth splitting at all. Most
    words in most documents are not, and this is what keeps the index from
    doubling.
    """
    return bool(_BOUNDARY.search(word))


def split_identifier(word: str) -> list[str]:
    """`getUserName` -> `['get', 'User', 'Name']`. Case is preserved.

    Returns `[]` for a word with no internal boundary, so callers can treat
    "nothing to add" and "not an identifier" the same way.

    Case is kept rather than lowered because the caller decides: the index
    lowercases through the tokenizer anyway, and a query expansion reads better
    in the log with the original spelling.
    """
    if not has_case_boundary(word):
        return []
    return [part for part in _BOUNDARY.split(word) if part]


def symbol_tokens(text: str, *, limit: int = MAX_SYMBOL_CHARS) -> str:
    r"""The extra tokens to index beside `text`, as one space-joined string.

    Only the parts of words that carry a case boundary, deduplicated, in first
    appearance order. A chunk of prose returns `""` and costs nothing.

    Order is preserved rather than sorted purely so the column is readable when
    somebody is working out why a search did or did not match.

    **Only the words that could split are looked at in Python.** Work order 0x
    item 5d. This runs for every passage the indexer writes, and it used to
    hand *every* word to `split_identifier` - a Python function call and a
    regex search per word, for prose where almost no word splits. Sampling
    the indexer's main thread while it wrote the benchmark corpus showed this
    one function holding Python's interpreter lock for over half of that
    thread's time, and while it held the lock the reading threads could not
    run (py-spy `--gil`, `app.cli bench-pipeline` small corpus, fake
    embedder, Linux sandbox, 4 CPUs, 2026-09-27).

    `_CANDIDATE` now picks out, inside the regex engine, only the words with a
    capital or a digit after their first letter. **It returns exactly the
    words that can contribute**, so the output is unchanged, character for
    character:

    * every one of the three boundaries needs a capital or a digit *after*
      the first letter (`getUser`, `XMLHttp`, `utf8`), so a word without one
      splits to nothing and never added anything here;
    * a word that has one is matched whole, from the same first letter to the
      same last character `_WORD` would have given: the match starts at the
      first letter of the run (it cannot start at a digit or straight after a
      letter, and failing at the first letter means there is no capital or
      digit after it, so no later letter in the run could succeed), and the
      greedy tail runs to the end of the letters and digits.

    Each such word still goes through `split_identifier`, so what is kept is
    decided by exactly the code that decided it before.
    `test_identifiers.py` compares the two over generated text to hold this.
    """
    seen: set[str] = set()
    out: list[str] = []
    length = 0

    for match in _CANDIDATE.finditer(text):
        word = match.group(0)
        parts = split_identifier(word)
        if not parts:
            continue
        for part in parts:
            if len(part) < MIN_PART_LENGTH:
                continue
            key = part.lower()
            if key in seen:
                continue
            seen.add(key)
            # +1 for the space that will join it.
            if length + len(part) + 1 > limit:
                return " ".join(out)
            out.append(part)
            length += len(part) + 1

    return " ".join(out)


def expand_term(term: str) -> str:
    """One query word as an FTS5 expression that matches both spellings.

    `getUserName` becomes `("getUserName" OR ("get" AND "user" AND "name"))`, so
    it finds the document that spells it `get_user_name` as well as the one that
    does not. A word with no case boundary is returned quoted and unchanged -
    the overwhelming majority of searches, and they must not get slower or
    stranger for this.

    **Quoted, always.** An unquoted FTS5 term is parsed as an expression, so a
    word that happens to contain `OR`, `NOT` or `NEAR` changes the meaning of
    the query. Quoting is also what makes a word ending in a digit safe.
    """
    parts = [p for p in split_identifier(term) if len(p) >= MIN_PART_LENGTH]
    if not parts:
        return f'"{_escape(term)}"'

    conjunction = " AND ".join(f'"{_escape(p)}"' for p in parts)
    return f'("{_escape(term)}" OR ({conjunction}))'


def expand_terms(terms: Iterable[str]) -> list[str]:
    """`expand_term` over a sequence, keeping order."""
    return [expand_term(term) for term in terms]


def _escape(term: str) -> str:
    """FTS5 quotes a double quote by doubling it."""
    return term.replace('"', '""')
