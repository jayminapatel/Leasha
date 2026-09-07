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

**Three grounds, and they are not equally safe.**

*Identical bytes* is a fact. The same `content_hash` is the same document,
wherever it sits, and folding those can only ever help.

*The same photo, recompressed* (work order 0h §2b) sits between the two -
firmer than a filename guess, softer than a byte-for-byte fact. A WhatsApp
copy, a resave, a different export of the same shot changes every byte of
`content_hash` but leaves a perceptual hash a handful of bits from the
original's - see `PHASH_NEAR_THRESHOLD` below. Run as its own pass, between
the copies pass and the version pass: after copies (a photo identical down
to the byte has already folded and needs no second look) and before
versions (a burst of near-duplicate photos rarely carries a filename marker
at all, so leaving this to the version pass would simply never fire for
photos).

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

__all__ = ["Fold", "family", "fold", "COPIES", "VERSIONS", "BURST",
           "PHASH_NEAR_THRESHOLD", "phash_distance"]

#: Folded because the bytes are identical.
COPIES = "copies"
#: Work order 0h §2b. Folded because they are the same photo, near enough -
#: a recompression, a resize, a re-export - even though the bytes differ.
BURST = "burst"
#: Folded because they look like versions of one document.
VERSIONS = "versions"

#: Work order 0h §2a/§2b. Hamming distance, out of the 64 bits
#: `imagehash.phash`'s default hash size produces (`app/index/phash.py`'s
#: `PHASH_HASH_SIZE`), below which two photos are treated as the same shot.
#:
#: **Measured, not guessed - and the first number tried (8) was measured
#: too low and replaced.** The literature's rule of thumb ("a handful of
#: bits" for a near-duplicate, "about 32" - half the bits - for an unrelated
#: pair, by chance) suggested 8 at spec time; building `tests/unit/
#: test_phash.py` against real JPEG re-encodes at several quality levels and
#: a resize (simulating a WhatsApp-style recompression) on synthetic
#: broadband photo-like fixtures measured same-photo distances up to **16**
#: - not 8 - while genuinely unrelated fixtures never landed below **26**.
#: 16 is set at the measured ceiling of "the same photo, recompressed",
#: which still leaves a ten-bit margin before the measured floor of
#: "different photo" - see that test file for the exact numbers and the
#: fixtures that produced them.
#:
#: **Still not measured against real camera photographs**, and said so
#: rather than presented as settled: the fixtures above are synthetic
#: broadband textures standing in for a real photo's spatial-frequency
#: content, chosen because a *smooth* synthetic image (a flat gradient, a
#: fine checkerboard) turned out to be a poor stand-in - its DCT energy
#: concentrates in too few coefficients, so ordinary recompression noise
#: swings the hash far more than it would on a real photograph, which was
#: the first, wrong version of this same measurement. Revisit once real
#: near-duplicate photos are available, per this project's standing
#: "measure, don't assume" rule - the same treatment `_growth_needed` in
#: `vector_store.py` gives its own unmeasured constant.
PHASH_NEAR_THRESHOLD = 16

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
        if self.reason == BURST:
            return (f"{count} similar photo" if count == 1
                    else f"{count} similar photos")
        return (f"{count} older version" if count == 1
                else f"{count} older versions")


def _newest_first(results: Sequence) -> list:
    r"""The group's members, shot-date-or-mtime newest first.

    **`taken_at_ns` before `mtime_ns` - work order 0f §3a's third clause.**
    This decides which member becomes the fold's `head`, which is the row
    actually shown - "the head is the *newest* of the group ... which is the
    whole point" per `fold`'s own docstring. A burst of the same photograph,
    scanned once and exported several times at different dates, must show
    the shot itself as the head rather than whichever export happened to be
    saved most recently.
    """
    return sorted(
        results,
        key=lambda r: int(getattr(r, "taken_at_ns", 0) or getattr(r, "mtime_ns", 0) or 0),
        reverse=True)


def phash_distance(a: str, b: str) -> int:
    r"""Hamming distance between two hex pHash strings.

    Work order 0h §2b/§2c. **Hand-rolled rather than
    `imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)`, on purpose.** This
    module is Layer 4 - pure, no store, no Qt, imported on every search
    result list this application draws, `version_folding` included. Taking a
    third-party hashing library as a hard import here would put a photo-only
    dependency on the path of every search, including a corpus with no
    photos in it at all. XOR-and-popcount is exactly what `ImageHash.__sub__`
    does internally in any case, so nothing is lost by not importing it.

    Returns a very large number, never a real match, for anything that will
    not parse as hex - a missing or malformed hash must degrade to "not the
    same picture", not raise and take the rest of the result page down with
    it.
    """
    try:
        return bin(int(str(a), 16) ^ int(str(b), 16)).count("1")
    except (TypeError, ValueError):
        return 999


def _burst_groups(rows: Sequence) -> list:
    r"""Cluster rows whose `phash` is a near duplicate of another's.

    Work order 0h §2b: **pHash first, CLIP distance as the tiebreak "when
    pHash alone doesn't distinguish".** Greedy single-linkage against each
    cluster's first (representative) member - the same "first-seen wins"
    shape `_group` already uses for exact keys, adapted for a threshold
    rather than an equality test, since Hamming distance has no hashable key
    to bucket on.

    A row with no `phash` at all (every text result, and any photo not yet
    hashed) is its own cluster of one - **a shared blank is not a match**,
    the same rule `_group` states for `content_hash`.

    **The tiebreak is an approximation, and said so rather than overclaimed.**
    When a candidate lands within `PHASH_NEAR_THRESHOLD` of more than one
    existing cluster - pHash alone cannot say which it belongs to - the
    nearer cluster is chosen by comparing each side's `distance` (the CLIP
    ANN distance *to the search query*, already carried on a hydrated image
    result; see `SearchResult.distance`). That is not a true pairwise CLIP
    comparison between the two photos - this module holds no vectors, only
    the scalar fields a `SearchResult` already carries, and getting a real
    pairwise distance would mean handing raw vectors down into a module that
    is deliberately pure - but two photos both landing close to the same
    query in CLIP's embedding space is the best signal available here, and
    `PHASH_NEAR_THRESHOLD`'s conservative width means this branch is rare in
    practice. Missing on either side falls back to the first candidate found,
    which is the nearer pHash match by construction (clusters are checked in
    the order they were created).
    """
    clusters: list = []
    loners: list = []
    for row in rows:
        phash = str(getattr(row, "phash", "") or "")
        if not phash:
            loners.append(row)
            continue
        candidates = [
            cluster for cluster in clusters
            if phash_distance(
                phash, str(getattr(cluster[0], "phash", "") or "")
            ) <= PHASH_NEAR_THRESHOLD
        ]
        if not candidates:
            clusters.append([row])
        elif len(candidates) == 1:
            candidates[0].append(row)
        else:
            own_distance = getattr(row, "distance", None)
            if own_distance is not None:
                candidates = sorted(
                    candidates,
                    key=lambda c: (
                        abs((getattr(c[0], "distance", None) or 0.0)
                            - own_distance)
                        if getattr(c[0], "distance", None) is not None
                        else float("inf")
                    ),
                )
            candidates[0].append(row)
    return clusters + [[row] for row in loners]


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

    # **Three passes, in the order that keeps each guess honest.** Choosing
    # one key or the other per row meant identical bytes always won and
    # version folding never fired once - three drafts of the same report
    # have three different hashes and were therefore three groups of one.
    # Copies fold first because they are a fact; near-duplicate photos
    # (work order 0h §2b) fold second because a perceptual hash is firmer
    # than a filename guess but softer than an exact byte match; what
    # survives both is then considered for the version-marker guess.
    copies = _group(rows, lambda row: str(getattr(row, "content_hash", "")
                                          or "") or None)
    copy_heads = [members[0] for members in copies]
    bursts = _burst_groups(copy_heads)
    burst_heads = [cluster[0] for cluster in bursts]
    families = _group(burst_heads,
                      lambda row: family(str(getattr(row, "path", ""))))

    def _expand(heads: Sequence) -> list:
        """Every underlying row behind a set of burst-pass representatives -
        family -> burst -> copies, the reverse of how the three passes were
        built."""
        return [row for head in heads
                for cluster_row in _members_of(head, bursts)
                for row in _members_of(cluster_row, copies)]

    folded: list = []
    for group in families:
        members = _expand(group)
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
        for burst_head in group:
            burst_cluster = _members_of(burst_head, bursts)
            if len(burst_cluster) > 1:
                burst_members = _expand([burst_head])
                newest, *rest = _newest_first(burst_members)
                folded.append(Fold(head=newest, older=tuple(rest),
                                   reason=BURST))
                continue
            own = _members_of(burst_head, copies)
            if len(own) > 1:
                newest, *rest = _newest_first(own)
                folded.append(Fold(head=newest, older=tuple(rest),
                                   reason=COPIES))
            else:
                folded.append(Fold(head=burst_head))
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
