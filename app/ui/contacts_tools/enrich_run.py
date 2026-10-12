"""The enrichment run: it belongs to the page, not to the window that asked for it.

Layer: L5. "When I press enrich and close the window the enrichment should continue and I should be
able to go back and accept" (the owner, 2026-10-11). The look-up runs on a worker owned by this object;
closing the Enrich window drops only the window. When the run finishes the page says so, and the
suggestions wait here until they are accepted or looked for again.
"""

from __future__ import annotations

import threading
from typing import Any

from PySide6.QtCore import QObject, Signal

from app.ui.contacts_tools._common import Job

__all__ = ["EnrichRun", "run_for"]


class EnrichRun(QObject):
    #: The state changed: running, ready, empty or failed.
    changed = Signal()

    def __init__(self, owner: Any = None) -> None:
        super().__init__(owner if isinstance(owner, QObject) else None)
        self.state = "idle"              # idle | running | ready | empty | failed
        self.data: Any = None
        self.error = ""
        #: Indexes into `data.proposals` already accepted: they stay put so the others keep their numbers.
        self.done: set[int] = set()
        #: How far a look-up has got: `{"done", "total", "found"}`, or None before the first contact.
        self.progress: Any = None
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        return self.state == "running"

    def pending(self) -> int:
        return sum(1 for i in range(len(self.data.proposals)) if i not in self.done) if self.data else 0

    def start(self, ctx: Any, force: bool = False) -> bool:
        """Begin a look-up unless one is going. Returns whether it began. `force` is Look again."""
        if self.running:
            return False
        from app.ui.contacts_tools.enrich_dialog import gather_proposals     # noqa: PLC0415

        self.state, self.data, self.error, self.done, self.progress = "running", None, "", set(), None
        self._notify = getattr(ctx, "notify", None)
        self._stop.clear()
        stop = self._stop
        Job(self, lambda on_progress: gather_proposals(ctx, on_progress, stop.is_set, force=force), self._finished,
            self._failed, self._progressed)
        self.changed.emit()
        return True

    def stop(self) -> None:
        """Stop looking at the next contact; what was found so far is kept and shown."""
        self._stop.set()

    def _progressed(self, stage: Any) -> None:
        if isinstance(stage, dict):
            self.progress = stage
            self.changed.emit()

    def _say(self, text: str) -> None:
        notify = getattr(self, "_notify", None)
        if callable(notify):
            try:
                notify(text)
            except Exception:                              # noqa: BLE001 - a notice that cannot show is not an error
                pass

    def _finished(self, data: Any) -> None:
        self.data = data
        n = len(data.proposals)
        self.state = "ready" if n else "empty"
        stopped = self._stop.is_set()
        notes = " ".join(getattr(data, "notes", None) or [])
        if n:
            people = len({getattr(p, "contact_id", None) for p in data.proposals})
            self._say(f"Enrichment {'stopped' if stopped else 'found'} {n} suggestion{'s' if n != 1 else ''} for "
                      f"{people} contact{'s' if people != 1 else ''}. Open Enrich contacts to accept them."
                      + (" " + notes if notes else ""))
        else:
            self._say("Enrichment finished: no suggestions this time." + (" " + notes if notes else ""))
        self.changed.emit()

    def _failed(self, error: Any) -> None:
        self.state = "failed"
        self.error = getattr(error, "render", lambda: str(error))()
        self._say("Enrichment stopped: " + self.error)
        self.changed.emit()

    def accepted(self, indexes: Any) -> None:
        self.done |= set(indexes)
        if self.data is not None and self.pending() == 0:
            self.state = "empty"
        self.changed.emit()


def run_for(parent: Any, ctx: Any) -> EnrichRun:
    """The one run of the page that owns `parent` (or of `ctx` when there is no page)."""
    owner = parent if parent is not None else ctx
    run = getattr(owner, "_enrich_run", None)
    try:
        if run is not None:
            run.state                                     # raises if the object was deleted
    except RuntimeError:
        run = None
    if run is None:
        run = EnrichRun(owner)
        try:
            owner._enrich_run = run
        except AttributeError:
            pass
    return run
