r"""Where in a spreadsheet a hit came from. Adoptions §6a.

Layer: L2 — pure. No spreadsheet library, no I/O, so the one thing that has to
be right can be tested in a millisecond.

**A spreadsheet is the one file type where "which page" is not enough.** A
40,000-row workbook that matches your search is not an answer; the row is.
Every other format either has no interior addresses (a text file) or has ones
a person cannot act on (a chunk ordinal), and a sheet has one everybody
already knows how to read: `Q3!A14`.

**The locator is stored, the sentence is not.** `Q3!A14` is what goes in the
database; `presenter.cell_location` turns it into *Sheet 'Q3' · near A14*.
The same rule the notices follow — the store keeps a code, the words are
written where words live, and nothing has to parse a sentence back apart.
"""

from __future__ import annotations

from typing import Optional

__all__ = ["column_letter", "column_number", "locator", "parse", "MAX_COLUMN"]

#: Excel's own ceiling: XFD, 16,384 columns. Beyond it there is no spelling to
#: produce, so `column_letter` says so rather than inventing a four-letter one.
MAX_COLUMN = 16_384


def column_letter(index: int) -> str:
    r"""1 -> A, 26 -> Z, 27 -> AA. `""` for anything outside a real sheet.

    **Bijective base-26, which is not ordinary base-26** and is the reason this
    is a function rather than a two-line expression somewhere: there is no
    zero digit, so 26 is `Z` and not `A0`, and the borrow has to happen before
    the divide. Getting it wrong is off-by-one from column 26 onwards - which
    a fixture with five columns would never catch.

    >>> column_letter(1), column_letter(26), column_letter(27), column_letter(52)
    ('A', 'Z', 'AA', 'AZ')
    """
    number = int(index)
    if number < 1 or number > MAX_COLUMN:
        return ""
    out = ""
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        out = chr(ord("A") + remainder) + out
    return out


#: `column_letter`, memoised. A sheet has at most a few dozen distinct written
#: columns and tens of thousands of rows, so the same handful of answers are
#: asked for over and over - and this runs inside the row loop of an extractor
#: that has to stay out of the way of a corpus-sized run.
_LETTER_CACHE: dict[int, str] = {}


def cached_letter(index: int) -> str:
    """`column_letter` with a memo. Same answer, no recomputation."""
    found = _LETTER_CACHE.get(index)
    if found is None:
        found = column_letter(index)
        _LETTER_CACHE[index] = found
    return found


def column_number(letters: str) -> int:
    """`A` -> 1, `AA` -> 27. 0 when it is not a column reference."""
    text = str(letters or "").strip().upper()
    if not text or not text.isalpha():
        return 0
    number = 0
    for character in text:
        number = number * 26 + (ord(character) - ord("A") + 1)
    return number if 1 <= number <= MAX_COLUMN else 0


def locator(sheet: str, row: int, column: int = 1) -> str:
    r"""`Q3!A14` - the address people already know, stored as one string.

    **One column rather than three.** A sheet name, a row and a column in
    three columns of `chunks` would be three migrations, three nullable
    fields and three places to forget, for a value that is only ever read
    whole. The `!` is the spreadsheet's own separator, so the stored form is
    also the form somebody could paste into the Name Box.

    `""` when there is nothing worth saying - which is what stops a sheet with
    no name from producing `!A14`, a locator that points nowhere and looks
    like a bug.

    >>> locator("Q3", 14, 4)
    'Q3!D14'
    """
    name = " ".join(str(sheet or "").split())
    number = int(row or 0)
    if not name or number < 1:
        return ""
    return f"{name}!{column_letter(column) or 'A'}{number}"


def parse(text: str) -> Optional[tuple[str, str, int]]:
    r"""`Q3!D14` -> `("Q3", "D", 14)`, or None.

    Never raises. This reads a value out of the database, and a row written by
    a future version - or corrupted - must cost the locator on one result
    rather than the page it is on.
    """
    raw = str(text or "").strip()
    sheet, sep, cell = raw.rpartition("!")
    if not sep or not sheet or not cell:
        return None
    # **Split on the last `!`, and fold the cell's case.** A sheet may be
    # called `Q3!draft` - the separator is not reserved - and a locator typed
    # or written by hand may be `q3!d14`. Neither is worth losing a row over.
    cell = cell.upper()
    letters = cell[:len(cell) - len(cell.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))]
    digits = cell[len(letters):]
    if not letters or not digits.isdigit():
        return None
    return sheet, letters, int(digits)
