r"""A near-identical photo borrows its sibling's description instead of asking Florence.

Layer: L3 (`app/index`), used by `Pipeline._drain_photo_tags` and
`Pipeline._drain_picture_text`.

**2026-10-10, work order model-sequencing item 4c.** Florence-2 costs 4-10 s a
photo, and a burst - five frames of the same child on the same swing, taken in
the same second - paid for it five times, to be told five times that a child is
on a swing. The perceptual hash was already computed for every photo
(`files.phash`, `Pipeline._maybe_compute_phash`) and used only to fold search
results; the shot time was already stored (`files.taken_at_ns`). Here they
decide whether a photo needs Florence at all.

**The rule, all three together:** the same folder (`files.parent_dir`), taken
within `REUSE_WINDOW_NS` of each other by the camera's own clock (EXIF - a date
guessed from the folder's name or the file's is not a shot time and does not
count, `taken_at_is_hint`), and a pHash distance under `REUSE_PHASH_DISTANCE`.
Any one alone is not enough: a folder holds a whole holiday, a minute holds a
turn of the head towards something else, and two photos of different white
walls can share a hash.

**Only an original is ever copied from.** A photo whose description is itself a
copy is never a source, so a description cannot drift down a chain of
photos each a little different from the last - every copy is one step from a
photo Florence actually looked at.

**Marked, so it can be redone.** A copy is the same "AI description" passage
(the label the search filters and the Photos page read, so nothing downstream
needs to know), plus an `index_state` row `description_copied_from:<file id>`
holding the original's id. To redo one: delete that photo's "AI description"
passage and its marker row, and set it back to waiting - the next run's end
describes it with Florence. `copied_descriptions` lists them all.

**Cheap by construction.** One query for each batch of photos the drain reads
(their own facts), and one for each folder the first time a photo in it needs a
description; the pairs compared are those within one folder, never across the
library. Never raises: anything that goes wrong here means Florence describes
the photo, exactly as before.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from app.core.logging import logger

__all__ = [
    "COPIED_STATE_PREFIX",
    "DESCRIPTION_LABEL",
    "DescriptionReuse",
    "REUSE_PHASH_DISTANCE",
    "REUSE_WINDOW_NS",
    "copied_descriptions",
]

_log = logger.bind(component="index.photo_reuse")

#: Hamming distance, of the 64 bits `files.phash` holds, *under* which two
#: photos of one folder and one minute share a description.
#:
#: **A judgement, not a measurement - and said so.** Search folds "the same
#: shot" at 16 (`app.search.folding.PHASH_NEAR_THRESHOLD`, measured on
#: synthetic re-encodes: the same photo recompressed reaches 16, unrelated
#: fixtures never fell below 26). Folding a result list wrongly costs a click;
#: copying a description wrongly costs a search that misses a photo, so this is
#: kept to half of that - under 8 bits, the "handful of bits" the literature
#: gives for a near-duplicate - on top of the folder and the minute. It has not
#: been measured against real bursts; that, and the share of the owner's
#: library it skips, are for the order to record once it has been run there.
REUSE_PHASH_DISTANCE = 8

#: How far apart two shots may be, by the camera's clock: a minute, the order's
#: own figure. Nanoseconds, as `files.taken_at_ns` is.
REUSE_WINDOW_NS = 60 * 1_000_000_000

#: The label every photo description is stored under (`add_caption_chunk`),
#: the original's and the copy's alike. Matches
#: `SqliteStore.PHOTO_DESCRIPTION_LABEL`.
DESCRIPTION_LABEL = "AI description"

#: `index_state` key prefix marking a copied description; the value is the id
#: of the photo it was copied from.
COPIED_STATE_PREFIX = "description_copied_from:"


def _bits(value: Any) -> Optional[int]:
    """A stored hex pHash as an integer, or None when it will not parse."""
    try:
        return int(str(value), 16)
    except (TypeError, ValueError):
        return None


class DescriptionReuse:
    """One drain's worth of "has a sibling already been described?".

    Built once per drain; `prepare` once per batch the drain reads; `sibling`
    once per photo; `remember` after Florence describes one; `mark` after a
    description is copied.
    """

    def __init__(self, conn: Any, *, distance: int = REUSE_PHASH_DISTANCE,
                 window_ns: int = REUSE_WINDOW_NS) -> None:
        self._conn = conn
        self.distance = int(distance)
        self.window_ns = int(window_ns)
        #: `file_id -> (folder, phash bits, taken_at_ns)` for this batch's
        #: photos that can take part at all.
        self._facts: dict[int, tuple[str, int, int]] = {}
        #: `folder -> [(file_id, phash bits, taken_at_ns, description)]` -
        #: the originals described there, loaded once per folder.
        self._folders: dict[str, list[tuple[int, int, int, str]]] = {}
        #: Copies made by this drain, for the run log.
        self.copied = 0

    def prepare(self, file_ids: Sequence[int]) -> None:
        """Read this batch's own folder, hash and shot time, in one query."""
        self._facts = {}
        ids = [int(i) for i in file_ids]
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        try:
            rows = self._conn.execute(
                f"SELECT id, parent_dir, phash, taken_at_ns, taken_at_is_hint "
                f"FROM files WHERE id IN ({marks})", ids).fetchall()
        except Exception as exc:                     # noqa: BLE001 - Florence then
            _log.debug("no near-identical check for this batch: {}", exc)
            return
        for file_id, folder, phash, taken, hint in rows:
            bits = _bits(phash)
            if bits is None or taken is None or hint or folder is None:
                continue
            self._facts[int(file_id)] = (str(folder), bits, int(taken))

    def sibling(self, file_id: int) -> Optional[tuple[int, str]]:
        """`(original's id, its description)` for a near-identical photo already
        described in the same folder within the minute, or None."""
        facts = self._facts.get(int(file_id))
        if facts is None:
            return None
        folder, bits, taken = facts
        best: Optional[tuple[int, int, int, str]] = None
        for other_id, other_bits, other_taken, body in self._originals(folder):
            if other_id == int(file_id):
                continue
            gap = abs(other_taken - taken)
            if gap > self.window_ns:
                continue
            apart = (other_bits ^ bits).bit_count()
            if apart >= self.distance:
                continue
            if best is None or (apart, gap) < (best[0], best[1]):
                best = (apart, gap, other_id, body)
        return None if best is None else (best[2], best[3])

    def remember(self, file_id: int, body: str) -> None:
        """Florence has just described this photo: it is an original the rest
        of its folder can now borrow from, in this same drain."""
        facts = self._facts.get(int(file_id))
        if facts is None or not body:
            return
        folder, bits, taken = facts
        self._originals(folder).append((int(file_id), bits, taken, str(body)))

    def mark(self, store: Any, file_id: int, source_id: int) -> None:
        """Record that `file_id`'s description is a copy of `source_id`'s.
        Never raises: an unmarked copy is still a correct description."""
        self.copied += 1
        try:
            store.set_state(f"{COPIED_STATE_PREFIX}{int(file_id)}", str(int(source_id)))
        except Exception as exc:                     # noqa: BLE001 - a marker, not the job
            _log.warning("the copied description of photo {} could not be marked: {}",
                         file_id, exc)

    def _originals(self, folder: str) -> list[tuple[int, int, int, str]]:
        """The described originals of one folder - one query, the first time."""
        found = self._folders.get(folder)
        if found is not None:
            return found
        found = []
        try:
            rows = self._conn.execute(
                "SELECT f.id, f.phash, f.taken_at_ns, c.text FROM files f "
                "JOIN chunks c ON c.file_id = f.id AND c.label = ? "
                "WHERE f.parent_dir = ? AND f.phash IS NOT NULL "
                "AND f.taken_at_ns IS NOT NULL AND f.taken_at_is_hint = 0 "
                "AND NOT EXISTS (SELECT 1 FROM index_state s WHERE s.key = ? || f.id) "
                "ORDER BY f.id, c.ordinal",
                (DESCRIPTION_LABEL, folder, COPIED_STATE_PREFIX)).fetchall()
        except Exception as exc:                     # noqa: BLE001 - Florence then
            _log.debug("no near-identical check in {}: {}", folder, exc)
            rows = []
        seen: set[int] = set()
        prefix = f"{DESCRIPTION_LABEL}: "
        for file_id, phash, taken, text in rows:
            bits = _bits(phash)
            if bits is None or int(file_id) in seen:
                continue
            seen.add(int(file_id))
            body = str(text or "")
            if body.startswith(prefix):
                body = body[len(prefix):]
            if body:
                found.append((int(file_id), bits, int(taken), body))
        self._folders[folder] = found
        return found


def copied_descriptions(conn: Any) -> dict[int, int]:
    """Every copied description: `{photo id: the original's id}`. For a later
    pass, or a person, that wants them redone by Florence."""
    rows = conn.execute(
        "SELECT key, value FROM index_state WHERE key LIKE ?",
        (COPIED_STATE_PREFIX + "%",)).fetchall()
    out: dict[int, int] = {}
    for key, value in rows:
        try:
            out[int(str(key)[len(COPIED_STATE_PREFIX):])] = int(value)
        except (TypeError, ValueError):
            continue
    return out
