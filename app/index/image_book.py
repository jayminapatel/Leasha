r"""The junk-image filter's book, kept in the index across archives and runs.

Layer: L3

Order 0z lane D, item D1: *one hash list for images across every archive and
run*. The rules live in `app/extract/junk_images.py` and work on an in-memory
`ImageBook`; this is the part that knows about the store. It loads the book
from `image_hashes` (schema v29) the first time a reader asks for it - a run
with no mail pictures in it never reads the table - and writes back what
changed when the run ends, on the run's own thread, like `held_archives`.

**A cache, not a cursor.** A run that is killed before `save` loses only what
this run learnt; the next run reads those pictures once more and learns it
again. Nothing indexed depends on this table being complete.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from app.core.logging import logger
from app.extract.junk_images import BookEntry, ImageBook

__all__ = ["PersistentImageBook"]

_log = logger.bind(component="index.image_book")


class PersistentImageBook:
    """One run's `ImageBook`, loaded on first use and saved with `save`."""

    def __init__(self, store: Any) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._book: Optional[ImageBook] = None

    def book(self) -> ImageBook:
        """The book, loading it from the store the first time. Never raises."""
        with self._lock:
            if self._book is None:
                self._book = ImageBook(self._load())
            return self._book

    def __getattr__(self, name: str) -> Any:
        """`saw`, `read`, `repeated_without_words` ... go to the book, loaded on first use.

        So the pipeline can hand this to every read (`reading(junk=...)`) and a
        run that never meets a picture inside mail never reads the table.
        """
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.book(), name)

    def _load(self) -> list[tuple[str, BookEntry]]:
        try:
            rows = self._store.image_hashes()
        except Exception as exc:                 # noqa: BLE001 - an empty book is safe
            _log.debug("image hash book unreadable, starting empty: {}", exc)
            return []
        out = []
        for digest, seen, words, phash, width, height in rows:
            try:
                value = int(phash, 16) if phash else None
            except ValueError:
                value = None
            out.append((digest, BookEntry(seen=seen, words=words, phash=value,
                                          width=width, height=height)))
        return out

    def save(self) -> int:
        """Write what changed. Returns how many rows. Never raises."""
        with self._lock:
            book = self._book
        if book is None:
            return 0
        changed = book.dirty()
        if not changed:
            return 0
        rows = [(digest, entry.seen, entry.words,
                 None if entry.phash is None else f"{entry.phash:016x}",
                 entry.width, entry.height) for digest, entry in changed]
        try:
            self._store.save_image_hashes(rows)
        except Exception as exc:                 # noqa: BLE001 - retried at the next save
            _log.warning("could not save the image hash book: {}", exc)
            return 0
        book.clean(digest for digest, _entry in changed)
        return len(rows)
