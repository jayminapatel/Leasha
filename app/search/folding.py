r"""Eight versions of the same letter, shown as one row.

Layer: L4 — pure. Takes result rows, returns how to draw them. No store, no
engine, no Qt.

**The corpus this exists for.** Fifteen years of a working life contains the
same document eight times: `report.docx`, `report v2.docx`,
`report FINAL.docx`, `report FINAL (2).docx`, and the copy that was dragged to
the desktop in 2019. A search for it returns all eight, the page looks like a
mess, and the one thing the person wanted - *the last one* - is somewhere in
the middle.

**Nothing is hidden.** The results list is unchanged; this describes how to
group it. A fold shows the newest of its group with the rest one click away,
so an expanded fold gives back exactly what an unfolded page showed. That
distinction matters more here than anywhere else in the order: a wrong fold
does not merely reorder a page, it puts a document behind a disclosure
triangle, and somebody who does not open it concludes their file is gone.

**Two grounds, and they are not equally safe.**

*Identical bytes* is a fact. The same `content_hash` is the same document,
wherever it sits, and folding those can only ever help.

*The same document, edited* is a guess, so it is fenced: the same folder, the
same extension, and a **version marker** - `v2`, `final`, `draft`, `rev 3`,
`(2)`, `copy`, a date - in at least one of the names. A bare trailing number
is deliberately **not** a version marker, because `chapter 1.docx` and
`chapter 2.docx` are two documents and folding them would hide half a book.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

__all__ = ["Fold", "family", "fold", "COPIES", "VERSIONS"]

#: Folded because the bytes are identical.
COPIES = "copies"
#: Folded because they look like versions of one document.
VERSIONS = "versions"

#: Version markers, stripped from a filename to find the family it belongs to.
#:
#: **Each one has to be a word people actually put in filenames**, and each
#: one has to be unambiguous enough that removing it cannot merge two real
#: documents. `v2`, `final`, `rev 3` and `(2)` qualify. A bare `2` does not.
#: **`(?<![A-Za-z])` rather than `\b`, and that distinction is not pedantry.**
#: The separator class is consumed first, so in `report_v04` the assertion is
#: tested between `_` and `v` - both word characters, so `\b` is false and the
#: marker went unrecognised. `report_final` and `report_rev2` failed the same
#: way. A lookbehind asks the question that was actually meant: is the thing
#: before this a letter? An underscore is not, so the marker is found; the `i`
#: of `semifinal` is, so it is left alone.
_MARKERS = re.compile(
    r"""(?ix)
    (?:
        [\s._-]*\(\s*\d{1,3}\s*\)                        # (1) (2) Windows copy
      | [\s._-]*\[\s*\d{1,3}\s*\]                        # [1]
      | [\s._-]*(?<![A-Za-z]) v \s* \d+ (?:[._]\d+)*     # v2  v1.3  _v04
      | [\s._-]*(?<![A-Za-z]) rev(?:ision)? \s* \d*      # rev  rev2  revision 3
      | [\s._-]*(?<![A-Za-z]) (?:final|draft|copy|old|new|latest|current)
      | [\s._-]*(?<![A-Za-z]) \d{4}[-_]?\d{2}[-_]?\d{2}  # 2024-01-05, 20240105
      | [\s._-]*(?<![A-Za-z]) \d{2}[-_]\d{2}[-_]\d{2,4}  # 05-01-24
    )+
    \s*$
    """)

#: A second pass, because markers stack: "report final v2 (1)".
_TRAILING_JUNK = re.compile(r"[\s._-]+$")


def _stem_and_folder(path: str) -> tuple:
    """The folder and the bare filename, for a Windows or POSIX path.

    **Split by hand rather than with `pathlib`.** Paths in this index are
    Windows paths, and the tests run on Linux where `PurePath` does not treat
    a backslash as a separator - it would return the whole path as the name
    and every family key would be unique. That is not hypothetical: it is the
    bug that made a measurement in this same order read 20/20 when the true
    figure was 14/20.
    """
    text = str(path or "").replace("\\", "/")
    folder, _, name = text.rpartition("/")
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    return folder.lower(), stem, ext.lower()


def family(path: str) -> tuple:
    """`(folder, base name, extension)` with version markers removed.

    Two files share a family when this matches **and** at least one of them
    carried a marker - `family` alone cannot tell you that, which is why
    `fold` checks it separately.
    """
    folder, stem, ext = _stem_and_folder(path)
    base = _MARKERS.sub("", stem)
    base = _TRAILING_JUNK.sub("", base)
    return folder, base.strip().lower(), ext


def _has_marker(path: str) -> bool:
    """Whether this filename carries a version marker at all."""
    _folder, stem, _ext = _stem_and_folder(path)
    return _MARKERS.sub("", stem).strip().lower() != stem.strip().lower()


@dataclass(frozen=True)
class Fold:
    """One row of the page: the result to show, and what is behind it."""

    #: The newest of the group, shown in the list.
    head: Any
    #: The rest, newest first, hidden until expanded. Empty for an ordinary
    #: unfolded result - **every row is a `Fold`**, so the view has one shape
    #: to draw rather than two.
    older: tuple = ()
    reason: str = ""

    @property
    def folded(self) -> bool:
        return bool(self.older)

    def label(self) -> str:
        """What the expander says. **Counts what is behind it, in plain
        words**, because "1 more" tells somebody nothing about whether it is
        worth opening."""
        count = len(self.older)
        if not count:
            return ""
        if self.reason == COPIES:
            return (f"{count} identical copy elsewhere" if count == 1
                    else f"{count} identical copies elsewhere")
        return (f"{count} older version" if count == 1
                else f"{count} older versions")


def _newest_first(results: Sequence) -> list:
    return sorted(results, key=lambda r: int(getattr(r, "mtime_ns", 0) or 0),
                  reverse=True)


def fold(results: Sequence, *, enabled: bool = True) -> list:
    r"""Group near-identical results, best rank first, newest of each shown.

    **The head keeps its group's best rank**, so folding never demotes an
    answer: if the fifth-best result is an older copy of the first, the fold
    sits where the first was. And the head is the *newest* of the group rather
    than the best-scoring, which is the whole point - the person asking for
    "the safety report" wants this year's.

    With `enabled=False` every result comes back as its own unfolded `Fold`,
    so the caller has one code path whatever the policy says.
    """
    rows = list(results)
    if not enabled or len(rows) < 2:
        return [Fold(head=row) for row in rows]

    # **Two passes, because every indexed file has a hash.** Choosing one key
    # or the other per row meant identical bytes always won and version
    # folding never fired once - three drafts of the same report have three
    # different hashes and were therefore three groups of one. Copies fold
    # first because they are a fact; what survives is then considered for the
    # guess.
    copies = _group(rows, lambda row: str(getattr(row, "content_hash", "")
                                          or "") or None)
    families = _group([members[0] for members in copies],
                      lambda row: family(str(getattr(row, "path", ""))))

    folded: list = []
    for group in families:
        members = [row for head in group
                   for row in _members_of(head, copies)]
        if len(group) > 1 and any(_has_marker(str(getattr(row, "path", "")))
                                  for row in group):
            newest, *rest = _newest_first(members)
            folded.append(Fold(head=newest, older=tuple(rest),
                               reason=VERSIONS))
            continue
        # **No marker, no version fold.** Same folder, same extension and the
        # same name after stripping nothing at all means these are two files
        # with similar names - `chapter 1` and `chapter 2` - and hiding one
        # behind the other would be the worst thing on this page.
        for head in group:
            own = _members_of(head, copies)
            if len(own) > 1:
                newest, *rest = _newest_first(own)
                folded.append(Fold(head=newest, older=tuple(rest),
                                   reason=COPIES))
            else:
                folded.append(Fold(head=head))
    return folded


def _group(rows: Sequence, key_of) -> list:
    """Rows bucketed by `key_of`, buckets in first-seen order.

    A `None` key means "belongs to nothing" - each such row is its own bucket,
    so an unhashed file never folds into another unhashed file. **A shared
    blank is not a match**, and treating it as one would fold together every
    document the index has not read yet.
    """
    buckets: dict = {}
    order: list = []
    loners: list = []
    for row in rows:
        key = key_of(row)
        if key is None:
            loners.append([row])
            order.append(loners[-1])
            continue
        if key not in buckets:
            buckets[key] = []
            order.append(buckets[key])
        buckets[key].append(row)
    return order


def _members_of(head, copies: list) -> list:
    """Every row that folded into `head` during the copies pass, `head`
    included."""
    for group in copies:
        if group and group[0] is head:
            return group
    return [head]
