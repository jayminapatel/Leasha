r"""The line that declares a symbol, ranked above the lines that use it.

Layer: L4 — pure. Text in, boolean out. No store, no engine.

**Measured before it was built.** Five files, one of them declaring
`SearchEngine` and four using it:

    1. app/cli.py            uses it twice
    2. app/ui/shell.py       uses it three times
    3. tests/test_engine.py  uses it twice
    4. docs/DESIGN.md        mentions it twice, in prose
    5. app/search/engine.py  DECLARES it            <- last

BM25 rewards term frequency, and a declaration appears **once** while every
caller repeats the name. So the one file that answers *"where is this
defined"* ranked below a Markdown file that merely talked about it.

**Deliberately approximate, and it says so.** A real answer needs a parser per
language. This is one regex family covering the C family, Python, Java, C#,
Go, Rust and TypeScript, and it will occasionally match a comment - the same
trade `gitquery.SYMBOL_PATTERNS` already made for the repository switches, and
the right one for a search box, where the alternative is twelve parsers or
nothing.

**It only ever reorders.** A file that declares nothing is not demoted and
nothing is hidden; a declaration that was already first stays first.
"""

from __future__ import annotations

import re
from typing import Optional, Sequence

__all__ = ["declares", "looks_like_symbol", "boost", "WEIGHT"]

#: How much a declaration may add to its own fused score, as a fraction.
#:
#: **Measured, not chosen** - see the §4c note in
#: `WORKORDER-202626270157-search-experience.md`. Larger than the recency
#: blend's 0.03 because this is a much stronger claim: recency says "these are
#: equally good, prefer the newer", and this says "this one is the answer to
#: the question that was asked".
WEIGHT = 0.40

#: One declaration pattern per shape, `{name}` filled with the symbol.
#:
#: The same family as `gitquery.SYMBOL_PATTERNS`, kept here rather than
#: imported because that module is about building a `git` command line and
#: this one is about scoring a chunk - and a shared table that two callers
#: pull in opposite directions is worse than two that agree.
_SHAPES: tuple = (
    # class Foo / struct Foo / record Foo / interface Foo / trait Foo
    r"\b(?:class|struct|record|interface|protocol|trait|enum|type)\s+{name}\b",
    # def foo / fn foo / func foo / function foo / sub foo
    r"\b(?:def|fn|func|function|sub|proc)\s+{name}\b",
    # foo(...) {  |  foo(...):  |  foo(...) =>
    r"\b{name}\s*\([^)\n]*\)\s*(?:\{|:|=>|->)",
    # const foo = / let foo = / var foo = / foo := / FOO =
    r"\b(?:const|let|var|val|static|final)\s+{name}\b",
    r"^\s*{name}\s*(?::=|=[^=])",
)

#: A word that could be an identifier. Bare English words pass this too, and
#: that is fine: the patterns above then match nothing in prose, so the boost
#: is a no-op rather than a wrong answer.
_SYMBOLIC = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{2,}$")


def looks_like_symbol(terms: Sequence[str]) -> Optional[str]:
    r"""The one symbol this query is asking about, or None.

    **Exactly one term.** Two words is a description, and boosting a
    declaration of the first of them answers a question nobody asked. This is
    what keeps the whole feature free for ordinary searching: a sentence never
    reaches it.
    """
    words = [str(term or "") for term in terms if str(term or "").strip()]
    if len(words) != 1:
        return None
    word = words[0]
    return word if _SYMBOLIC.match(word) else None


def declares(text: str, name: str) -> bool:
    """Whether this text contains a line declaring `name`. **Never raises.**"""
    body = str(text or "")
    symbol = str(name or "").strip()
    if not body or not symbol:
        return False
    quoted = re.escape(symbol)
    for shape in _SHAPES:
        try:
            if re.search(shape.replace("{name}", quoted), body,
                         re.IGNORECASE | re.MULTILINE):
                return True
        except re.error:                           # pragma: no cover - defensive
            continue
    return False


def boost(hits: Sequence[dict], name: str, *,
          weight: Optional[float] = None,
          score_key: str = "rrf_score") -> list:
    r"""Re-order fused hits, lifting the ones that declare `name`.

    Returns a new list; a hit that declares the symbol is annotated with
    `declares` so the reason for an order is inspectable rather than
    mysterious - the same rule the scores column follows.

    **Stable**, so hits that agree keep the order fusion gave them, and a
    corpus where nothing declares the symbol comes back exactly as it went in.
    """
    weight = WEIGHT if weight is None else float(weight)
    if not hits or not name or weight <= 0:
        return list(hits)

    ranked = []
    for position, hit in enumerate(hits):
        try:
            score = float(hit.get(score_key) or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        found = declares(hit.get("text", ""), name)
        hit["declares"] = found
        ranked.append((-(score * (1.0 + weight)) if found else -score,
                       position, hit))
    ranked.sort(key=lambda row: (row[0], row[1]))
    return [row[2] for row in ranked]
