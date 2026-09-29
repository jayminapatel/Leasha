r"""One word for where a file stands in the index.

Layer: L0

Asked for in the owner's words: *"in the results it must show the single word
status ... and this should be visible in the results"*. Every results list
(Search, Files, Mail, Code) shows it in a Status column, and the Indexing page
counts files by it. **One vocabulary, here, for all five places** - a list that
says "Skipped" where another says "SKIPPED" and a third says "could not be
read" is three answers to one question, and somebody comparing them concludes
that at least two are wrong.

**Derived, never stored.** Everything a word needs is already on the `files`
row or the `volumes` row - `status`, `skip_code`, `volume_id` - so there is no
column to migrate and nothing that can drift out of step with the pipeline
that writes those. `derive` is the whole mapping, in precedence order:

| Word | Source |
|---|---|
| Offline | the row's `volume_id` names a volume that is not connected |
| TimedOut | `skip_code` in `TIMEOUT_CODES` on a SKIPPED or FAILED row |
| Deferred | `skip_code` in `DEFERRED_CODES` on a SKIPPED or FAILED row |
| Indexed | status `INDEXED`, or `PARTIAL` (see below) |
| NameOnly | status `NAME_ONLY` |
| Skipped | status `SKIPPED` |
| Failed | status `FAILED` |
| Queued | status `PENDING` |
| Reading | live only - a reader has the file open (`IndexStats.workers`) |
| Discovered | live only - the walk found it and no reader has had it yet |
| Duplicate | no source yet - see `DUPLICATE_CODES` |

**`PARTIAL` reads as Indexed**, on purpose. Its chunks are written and its
contents are keyword-searchable now; only the vectors are still catching up
(`FileStatus.PARTIAL`). The question the column answers is "can I search
inside this", and for a partial file the answer is yes. Calling it Queued
would tell somebody to wait for something they already have.

**A skip code counts only on a SKIPPED or FAILED row.** `upsert_file` does not
clear `skip_code`, so a row that was skipped once and later written as
`NAME_ONLY` or `PENDING` can still carry the old code. `mark_indexed` clears
it; nothing else is guaranteed to.

**Offline beats everything.** A file on a drive that is in a drawer cannot be
opened or previewed whatever its status says, and that is the first thing
somebody looking at the row needs to know.

No Qt and no store here: the functions take plain values, so the Files and
Mail presenters, the search worker and the store's grouped count all share one
definition, and the tests need no database.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

__all__ = [
    "DISCOVERED", "QUEUED", "READING", "INDEXED", "SKIPPED", "FAILED", "TIMED_OUT",
    "OFFLINE", "NAME_ONLY", "DUPLICATE", "DEFERRED",
    "WORDS", "FUNNEL_ORDER", "EXPLANATIONS", "DEFERRED_CODES", "TIMEOUT_CODES",
    "DUPLICATE_CODES", "derive", "explain", "funnel_counts", "funnel_line",
]

DISCOVERED = "Discovered"
QUEUED = "Queued"
READING = "Reading"
INDEXED = "Indexed"
SKIPPED = "Skipped"
FAILED = "Failed"
TIMED_OUT = "TimedOut"
OFFLINE = "Offline"
NAME_ONLY = "NameOnly"
DUPLICATE = "Duplicate"
DEFERRED = "Deferred"

#: Every word, in the order a file meets them. The order the owner listed them.
WORDS: tuple[str, ...] = (
    DISCOVERED, QUEUED, READING, INDEXED, SKIPPED, FAILED, TIMED_OUT, OFFLINE,
    NAME_ONLY, DUPLICATE, DEFERRED,
)

#: The Indexing page's funnel reads left to right as "done, then waiting, then
#: not read": the number somebody opens the page for first, the work still
#: coming next, and the files that will not be searchable inside after it.
FUNNEL_ORDER: tuple[str, ...] = (
    INDEXED, DISCOVERED, QUEUED, READING, DEFERRED, NAME_ONLY, SKIPPED, TIMED_OUT,
    FAILED, OFFLINE, DUPLICATE,
)

#: One plain sentence per word - the tooltip on every Status cell and on the
#: funnel. Written for the eight-year-old benchmark: no codes, no jargon.
EXPLANATIONS: dict[str, str] = {
    DISCOVERED: "Found on disk by the current run; nothing has read it yet.",
    QUEUED: "Waiting its turn to be read by the indexer.",
    READING: "Being read by the indexer right now.",
    INDEXED: "Read in full - you can search for the words inside it.",
    SKIPPED: "Could not be read, so only its name can be found; the Indexing page says why.",
    FAILED: "Reading it went wrong; it is findable by name and the Indexing page says why.",
    TIMED_OUT: "Took longer than the time limit for one file, so it was set aside.",
    OFFLINE: "On a drive or share that is not connected right now - plug it in to open it.",
    NAME_ONLY: "Findable by its name only - there is no reader for this kind of file.",
    DUPLICATE: "An exact copy of a file already indexed, so its contents were not read twice.",
    DEFERRED: "Saved for later - a slower pass (pictures, recordings) will read it.",
}

#: Skip codes that are a queue, not a verdict: the file will be read by a
#: later pass or run. The same set as `Pipeline.DEFERRED_SKIP_CODES`, which
#: `tests/unit/test_file_state.py` holds these two to - the pipeline is L3 and
#: cannot be imported from here.
DEFERRED_CODES: frozenset[str] = frozenset({
    "ERR_OCR_HELD", "ERR_FILE_LOCKED", "ERR_CLOUD_ONLY",
    "ERR_MEDIA_HELD", "ERR_MEDIA_INTERRUPTED", "ERR_MEDIA_BACKLOG",
})

#: The per-file time limit's code. **Nothing writes it yet** - the time limit
#: is being added separately and will record this code - so the word is wired
#: now and lights up the moment the first file is set aside with it.
TIMEOUT_CODES: frozenset[str] = frozenset({"ERR_FILE_TIMEOUT"})

#: **Empty: nothing in the index records a duplicate today.** A PST message
#: whose bytes were already indexed is skipped by the reader before it ever
#: gets a row (`email_pst.py`, `pst_libpff.py`), so there is nothing to count.
#: The word and its sentence exist so a later reader that does record one has
#: somewhere to put it; add its code here.
DUPLICATE_CODES: frozenset[str] = frozenset()

#: Store status -> word, before skip codes and volumes are considered.
_BY_STATUS: dict[str, str] = {
    "INDEXED": INDEXED,
    "PARTIAL": INDEXED,
    "NAME_ONLY": NAME_ONLY,
    "SKIPPED": SKIPPED,
    "FAILED": FAILED,
    "PENDING": QUEUED,
}

#: The two statuses whose `skip_code` is current - see the module docstring.
_CODED = ("SKIPPED", "FAILED")


def derive(status: Any, skip_code: Any = None, *, offline: bool = False) -> str:
    """The one word for a file, from what its row already carries.

    `status` is `files.status`; `skip_code` is `files.skip_code`; `offline` is
    whether the row's volume is disconnected, decided by the caller on a worker
    (it is a live machine check, never done here). `""` for a status this does
    not know - a blank cell rather than a guess.
    """
    if offline:
        return OFFLINE
    raw = str(status or "").upper()
    code = str(skip_code or "")
    if code and raw in _CODED:
        if code in TIMEOUT_CODES:
            return TIMED_OUT
        if code in DEFERRED_CODES:
            return DEFERRED
        if code in DUPLICATE_CODES:
            return DUPLICATE
    return _BY_STATUS.get(raw, "")


def explain(word: Any) -> str:
    """The tooltip sentence for a word, or `""` for a blank or unknown one."""
    return EXPLANATIONS.get(str(word or ""), "")


def funnel_counts(
    by_status: Mapping[str, int],
    coded: Iterable[tuple[str, str, int]] = (),
    offline: Optional[Mapping[str, int]] = None,
    *,
    reading: int = 0,
    discovered: int = 0,
) -> dict[str, int]:
    """Counts per word, from the store's grouped counts.

    * `by_status` - `{files.status: n}` for the whole table.
    * `coded` - `(status, skip_code, n)` for the rows whose code changes their
      word (deferred, timed out, duplicate). Moved out of their status's bucket
      into the word's.
    * `offline` - `{files.status: n}` for rows on disconnected volumes. Moved
      out of their status's bucket into Offline. An offline row with a deferred
      code is counted once, as Offline - the caller's query for `coded` excludes
      offline rows so the two moves never take the same row twice.
    * `reading` / `discovered` - live numbers from the run, which the store
      cannot know.

    Every word is present, zero or not, so a caller never has to `.get`.
    """
    counts = {word: 0 for word in WORDS}
    for status, n in (by_status or {}).items():
        word = _BY_STATUS.get(str(status or "").upper())
        if word:
            counts[word] += int(n or 0)
    for status, code, n in coded or ():
        moved_to = derive(status, code)
        moved_from = _BY_STATUS.get(str(status or "").upper())
        if moved_to and moved_from and moved_to != moved_from:
            counts[moved_from] -= int(n or 0)
            counts[moved_to] += int(n or 0)
    for status, n in (offline or {}).items():
        moved_from = _BY_STATUS.get(str(status or "").upper())
        if moved_from:
            counts[moved_from] -= int(n or 0)
            counts[OFFLINE] += int(n or 0)
    counts[READING] += max(0, int(reading or 0))
    counts[DISCOVERED] += max(0, int(discovered or 0))
    return {word: max(0, n) for word, n in counts.items()}


def funnel_line(counts: Mapping[str, int]) -> str:
    """`Indexed 448,210 · Queued 1,200 · Reading 4 · Skipped 310`.

    Indexed always shows - it is the number somebody opened the page for, and
    "Indexed 0" is an answer. Every other word shows only when it has files:
    eleven words across the page, most of them zero, is a line nobody reads.
    """
    parts = []
    for word in FUNNEL_ORDER:
        n = int((counts or {}).get(word, 0) or 0)
        if n or word == INDEXED:
            parts.append(f"{word} {n:,}")
    return " · ".join(parts)
