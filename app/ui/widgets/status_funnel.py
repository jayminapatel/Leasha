r"""The Indexing page's funnel: one line of counts per status word.

Layer: L5

`Indexed 448,210 · Queued 1,200 · Reading 4 · Skipped 310 · Failed 12 ·
Deferred 45` - the owner chose it as the second place the one-word status
shows (the first is the Status column in every results list). The words are
`app.core.file_state`'s, so a file counted as Deferred here is the file whose
row says Deferred in Search, Files, Mail and Code.

**When it refreshes.** With the page's existing refreshes, and no timer of its
own: `paint_totals` hands it the counts `read_index_summary` already read on
its worker (a tab switch, the end of a run, a reset), and each progress tick
`paint_progress` lets through asks for a fresh read - at most once every
`REFRESH_MIN_S`, and never while the previous read is still out. During a run
that adds the two numbers only the run knows, Reading and Discovered.

**The read is a worker's** (`tasks.status_funnel_counts`), never this
widget's: three indexed statements (`store.status_counts`), but on the thread
that paints, while an index run is writing, even an indexed read can wait on
the write lock.
"""

from __future__ import annotations

import time
from typing import Any, Mapping, Optional

from PySide6.QtCore import QEvent, QSize, Qt, QThreadPool
from PySide6.QtWidgets import QLabel, QSizePolicy

from app.core.file_state import FUNNEL_ORDER, explain, funnel_line
from app.ui.tasks import funnel_and_pictures
from app.ui.workers import CallableWorker, run

__all__ = ["StatusFunnel", "REFRESH_MIN_S", "OBJECT_NAME", "picture_line"]


def picture_line(counts: Optional[Mapping[str, int]]) -> str:
    """`Pictures: 40 of 15,011 read · faces looked for in 40 · 120 faces in 8
    people, 5 still to sort · 300 waiting to be described`.

    2026-10-04, the owner: "status of pictures indexing ... like it has for
    files". "" when there are no pictures, and each part only when it says
    something, as the files line does.
    """
    c = {k: int(v or 0) for k, v in (counts or {}).items()}
    total = c.get("pictures", 0)
    if not total:
        return ""
    parts = [f"Pictures: {c.get('read', 0):,} of {total:,} read"]
    if c.get("faces_looked"):
        parts.append(f"faces looked for in {c['faces_looked']:,}")
    if c.get("faces"):
        people = c.get("people", 0)
        face_part = (f"{c['faces']:,} face{'s' if c['faces'] != 1 else ''} in "
                     f"{people:,} {'person' if people == 1 else 'people'}")
        if c.get("unsorted"):
            face_part += f", {c['unsorted']:,} still to sort"
        parts.append(face_part)
    if c.get("to_describe"):
        parts.append(f"{c['to_describe']:,} waiting to be described")
    if c.get("text_to_read"):
        parts.append(f"text still to read in {c['text_to_read']:,}")
    return " · ".join(parts)

OBJECT_NAME = "indexFunnel"

#: The fastest the funnel re-reads the store during a run.
#:
#: **Fixed, not a setting** (non-negotiable 11): five seconds is well under the
#: time it takes somebody to glance at the page and back, and three grouped
#: reads every five seconds is nothing beside the run's own writes. Nobody has
#: a reason to change it. What would: a measurement on a twenty-million-row
#: index showing `store.status_counts` costing more than a few hundred
#: milliseconds - then this goes up, or the statements change.
REFRESH_MIN_S = 5.0


#: Lines the funnel can show: the files line and the pictures line.
FUNNEL_LINES = 2


class StatusFunnel(QLabel):
    """One line of counts per status word, in a fixed two-line slot.

    2026-10-14, the owner: *"keep the counts on top, rest in tabs is fine"*. It
    sits under the cockpit's header row and must never move what is below it,
    so it is always two lines tall, never word-wraps and is never hidden - it
    is empty until it has counts. A line that does not fit ends in "…" and the
    whole text leads the tooltip. `text()` is still the whole text.
    """

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__("", parent)
        self._full = ""
        self._explained = ""
        self.setObjectName(OBJECT_NAME)
        super().setWordWrap(False)
        self.setAccessibleName("Files by status")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setFixedHeight(self._slot_height())
        self._busy = False
        self._last_read = 0.0
        self.counts: dict[str, int] = {}
        self.pictures: dict[str, int] = {}

    # -- painting ------------------------------------------------------------

    def show_counts(self, counts: Optional[Mapping[str, int]]) -> None:
        """Paint one set of counts. UI thread, no I/O. `None` changes nothing:
        a failed read is not evidence that every count is zero."""
        if not isinstance(counts, Mapping):
            return
        self.counts = {word: int(n or 0) for word, n in counts.items()}
        self._paint()
        # Every word on the line, with its sentence - "what does Deferred
        # mean" is answered where the word is.
        self._explained = "\n".join(
            f"{word}: {explain(word)}" for word in FUNNEL_ORDER
            if self.counts.get(word) or word == "Indexed")
        self._elide()

    # -- the fixed slot (2026-10-14) -----------------------------------------

    def _slot_height(self) -> int:
        margins = self.contentsMargins()
        return (self.fontMetrics().lineSpacing() * FUNNEL_LINES
                + margins.top() + margins.bottom())

    def setText(self, text: Any) -> None:            # noqa: N802 - Qt's name
        """Keep the whole text; paint each line cut to the width."""
        self._full = "" if text is None else str(text)
        self._elide()

    def text(self) -> str:                           # noqa: D102 - Qt's name
        return self._full

    def setWordWrap(self, on: bool) -> None:         # noqa: N802 - Qt's name
        """Two fixed lines by design: wrapping is what moved the page."""
        super().setWordWrap(False)

    def sizeHint(self) -> QSize:                     # noqa: N802 - Qt's name
        return QSize(0, self._slot_height())

    def minimumSizeHint(self) -> QSize:              # noqa: N802 - Qt's name
        return self.sizeHint()

    def resizeEvent(self, event: Any) -> None:       # noqa: N802 - Qt's name
        super().resizeEvent(event)
        self._elide()

    def changeEvent(self, event: Any) -> None:       # noqa: N802 - Qt's name
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self.setFixedHeight(self._slot_height())
            self._elide()

    def _elide(self) -> None:
        """Paint at most `FUNNEL_LINES` lines, each cut with "…" to fit."""
        full = self._full.split("\n") if self._full else []
        width = self.contentsRect().width()
        metrics = self.fontMetrics()
        shown = [metrics.elidedText(line, Qt.TextElideMode.ElideRight, width)
                 if width > 0 else line for line in full[:FUNNEL_LINES]]
        QLabel.setText(self, "\n".join(shown))
        cut = shown != full
        tip = "\n\n".join(part for part in (self._full if cut else "", self._explained)
                          if part)
        if self.toolTip() != tip:
            self.setToolTip(tip)

    def show_pictures(self, counts: Optional[Mapping[str, int]]) -> None:
        """The picture line under the files line. UI thread, no I/O; `None`
        changes nothing, as for `show_counts`."""
        if not isinstance(counts, Mapping):
            return
        self.pictures = {key: int(n or 0) for key, n in counts.items()}
        self._paint()

    def _paint(self) -> None:
        """Both lines (files, then pictures when there are any) into the one label."""
        lines = [funnel_line(self.counts)]
        pictures = picture_line(self.pictures)
        if pictures:
            lines.append(pictures)
        self.setText("\n".join(lines))

    def _show_both(self, result: Any) -> None:
        """UI thread: the worker's counts for files and pictures."""
        if isinstance(result, Mapping):
            self.pictures = dict(result.get("pictures") or self.pictures)
            self.show_counts(result.get("files"))

    # -- reading -------------------------------------------------------------

    def refresh(self, store: Any, stats: Any = None, *, force: bool = False) -> bool:
        """Ask a worker for fresh counts. Returns whether one was sent.

        Throttled to `REFRESH_MIN_S` unless `force`, and never two at once: a
        read still out when the next tick comes is the answer that tick wanted.
        """
        if store is None or self._busy:
            return False
        now = time.monotonic()
        if not force and now - self._last_read < REFRESH_MIN_S:
            return False
        self._busy = True
        self._last_read = now
        worker = CallableWorker(funnel_and_pictures, store, stats,
                                component="ui.index.funnel")
        worker.signals.finished.connect(self._show_both)
        worker.signals.done.connect(self._read_done)
        run(QThreadPool.globalInstance(), worker)
        return True

    def _read_done(self) -> None:
        """The read finished, however it ended: the next tick may ask again."""
        self._busy = False

    def tick(self, view: Any, stats: Any) -> bool:
        """A progress tick from `paint_progress`: refresh from the run's store.

        The run's own pipeline holds the store it writes to; its connection is
        per thread (`SqliteStore.conn`), so the worker reads through its own.
        A run in another process (`ChildIndexRun`) holds no store to write to;
        it carries the window's own as `read_store`, and the counts are read
        from that while the other process writes (2026-09-30 - before this a
        separate-process run left the line unchanged until the run ended).
        """
        worker = getattr(view, "_worker", None)
        run = getattr(worker, "pipeline", None)
        store = getattr(run, "store", None)
        if store is None:
            store = getattr(run, "read_store", None)
        return self.refresh(store, stats)
