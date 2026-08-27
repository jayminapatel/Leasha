r"""£40,000 and 40000 and 40k are the same amount. Search does not think so.

Layer: L4 — pure. Given the words of a query, returns the other ways the same
quantity could have been written, for the existing expansion mechanism to OR
together. No store, no engine.

**Measured before it was built**, because §5c asks for exactly that. Five
documents that all name the same amount, and what each query found:

    document text                       indexed as
    "total 40000 for the site"          40000
    "awarded at 40,000 pounds"          40 · 000
    "we agreed £40k for the package"    40k
    "Invoice total: 40,000.00 GBP"      40 · 000 · 00

    query        found
    40000        budget only
    40,000       report, invoice
    40k          email only
    40000.00     nothing at all
    £40k         email only

**No query found more than two of the five, and one found nothing** - not even
the document containing its exact characters, because `40,000.00` is three
tokens and `40000.00` is one. The gap is total, so the item's condition to
build is met.

**Query-side, so no re-index is needed.** The documents are already tokenised;
what changes is the question. `"40000" OR "40 000" OR "40k"` finds all four
of the documents above - `"40 000"` is an FTS5 phrase, which is exactly what a
comma-separated number became when it was indexed.

**Only where a number is unambiguous.** A year is left alone (searching for
2024 must not drag in `2 024`), and so is anything below a thousand, where
there is no comma or `k` form to reach for.
"""

from __future__ import annotations

import re
from typing import Optional, Sequence

__all__ = ["variants", "expansions_for", "quantity", "MIN_VALUE", "MAX_VALUE"]

#: Below this there is nothing to expand: 300 has no comma form and "0.3k" is
#: not something anybody writes.
MIN_VALUE = 1_000

#: Above this a number is an identifier, not a quantity - an order reference, a
#: part number, a phone number. Expanding those produces alternatives nobody
#: wrote and widens the query for nothing.
MAX_VALUE = 999_999_999_999

#: Years are excluded by range rather than by cleverness [TUNE]. "2024" is
#: overwhelmingly a year in a search box, and `"20 24"` is not a thing anybody
#: has ever written.
_YEAR_RANGE = (1500, 2200)

_SUFFIX = {"k": 1_000, "m": 1_000_000, "bn": 1_000_000_000, "b": 1_000_000_000}

#: `40k`, `£40k`, `40,000`, `40000.00`, `1.5m`.
_QUANTITY = re.compile(r"""(?ix)
    ^[£$€]?                      # a currency symbol is decoration, not data
    (?P<number>\d{1,3}(?:,\d{3})+ | \d+ )
    (?:\.(?P<decimals>\d+))?
    (?P<suffix>bn|[kmb])?
    $""")


def quantity(word: str) -> Optional[int]:
    r"""The whole number this word names, or None if it does not name one.

    Returns whole units only: `40,000.50` is 40000, because the pence are not
    what makes two documents the same document, and a search for a rounded
    figure should still find the exact one.
    """
    found = _QUANTITY.match(str(word or "").strip())
    if not found:
        return None
    digits = found.group("number").replace(",", "")
    if not digits.isdigit():
        return None
    suffix = (found.group("suffix") or "").lower()
    decimals = found.group("decimals") or ""
    if suffix:
        # **The decimals belong to the multiplier.** `1.5m` is one and a half
        # million, and reading it as one million - which is what dropping them
        # first does - is a wrong answer that looks like a right one.
        scale = _SUFFIX.get(suffix, 1)
        value = int(round(float(f"{digits}.{decimals or 0}") * scale))
    else:
        # `40,000.00` - pence, dropped, because they are not what makes two
        # documents the same document and a search for the round figure should
        # still find the exact one.
        value = int(digits)
    if not MIN_VALUE <= value <= MAX_VALUE:
        return None
    if _YEAR_RANGE[0] <= value <= _YEAR_RANGE[1] and not suffix:
        # **A year is not a quantity.** `2024` must not drag in `2 024`.
        return None
    return value


def variants(value: int) -> tuple:
    r"""Every form this amount could have been indexed as, commonest first.

    `40000` gives `("40000", "40 000", "40k")`. The middle one is a phrase -
    two adjacent tokens - because that is what `40,000` became when the
    document was indexed, and it is the form the plain digits could never
    reach.

    A `k` or `m` form is offered only when the number is a round one: nobody
    writes `40123` as `40k`, so offering it would match documents about a
    different amount.
    """
    if not MIN_VALUE <= int(value) <= MAX_VALUE:
        return ()
    number = int(value)
    plain = str(number)
    found = [plain]

    # The comma form, as the phrase it becomes once indexed.
    grouped = f"{number:,}".replace(",", " ")
    if grouped != plain:
        found.append(grouped)

    for suffix, size in (("k", 1_000), ("m", 1_000_000), ("bn", 1_000_000_000)):
        if number < size or number % size:
            continue
        short = number // size
        # **`1000k` is not a form anybody writes**, and offering it widens the
        # query for a string no document contains. Above a thousand of a unit,
        # the next unit up is the one people reach for.
        if short < 1_000:
            found.append(f"{short}{suffix}")
    return tuple(dict.fromkeys(found))


def _joined(terms: Sequence[str]) -> list:
    r"""Digit runs the parser split on a comma, put back together.

    **`40,000` reaches the engine as two terms, `40` and `000`**, because the
    parser splits on punctuation and cannot know that this comma was part of a
    number rather than between two of them. A trailing group of exactly three
    digits is the giveaway, and it is the only shape this reassembles - `40`
    beside `12` stays two numbers.

    Returns `(index, count, value)` for each run found.
    """
    found: list = []
    position = 0
    while position < len(terms):
        head = str(terms[position])
        if not head.isdigit() or not 1 <= len(head) <= 3:
            position += 1
            continue
        digits = head
        span = 1
        while (position + span < len(terms)
               and str(terms[position + span]).isdigit()
               and len(str(terms[position + span])) == 3):
            digits += str(terms[position + span])
            span += 1
        if span > 1:
            value = int(digits)
            if MIN_VALUE <= value <= MAX_VALUE:
                found.append((position, span, value))
        position += span
    return found


def expansions_for(terms: Sequence[str]) -> tuple:
    r"""`(term, alternatives)` pairs for every quantity among these words.

    Shaped exactly like the wildcard expansions this feeds, so the FTS builder
    needs no new rule: each becomes a parenthesised `OR` occupying the
    position its term had.

    Both shapes are handled - a single word that is a number, and a run the
    parser split on a comma. **The comma run replaces only its first term**:
    the second, `000`, would otherwise stay in the query as a word of its own,
    matching every document with a `000` in it.
    """
    words = [str(term or "") for term in terms]
    found: dict = {}

    for position, span, value in _joined(words):
        alternatives = variants(value)
        if alternatives:
            found[words[position]] = alternatives
            for offset in range(1, span):
                # Absorbed into the run above, so it must not also be searched
                # for on its own.
                found.setdefault(words[position + offset], ())

    for word in words:
        if word in found:
            continue
        value = quantity(word)
        if value is None:
            continue
        alternatives = variants(value)
        if alternatives:
            found[word] = alternatives
    return tuple(found.items())
