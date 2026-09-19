r"""The search box's saved searches: one object, held by the view.

Layer: L5. Adoptions §3.

**Not a widget, and that is the point.** The view is at the 250-line guard,
and the guard exists because a long view is where untested logic hides. So
everything about saved searches that is not a `QLineEdit` lives here: which
list is current, when it is re-read, what a `saved:` token expands to, and
what the empty box offers underneath the recent searches.

**No store call on this thread.** Every read and write goes through
`workers.saved_searches_async` / `workers.save_search_async`; this object only
ever holds the answer. `test_no_store_call_outside_a_worker` is the rule, and
the reason for it is that a query on the interface thread is a freeze waiting
for a busy index.

**The list is cached because the search path reads it.** `saved:invoices` is
resolved between a keystroke and a search, which is not somewhere a database
read may go. The cache is refreshed when the window opens and again after
anything is saved, renamed or deleted - the three moments it can change.
"""

from __future__ import annotations

from typing import Any, Callable, Optional, Sequence

__all__ = ["SavedSearches"]


class SavedSearches:
    """What the search box knows about saved searches.

    `on_scope` is called with a scope value when running a saved search
    changes it - handed in rather than reached for, so this stays testable
    without a window and the view keeps one line instead of three.
    """

    def __init__(self, store: Any = None,
                 on_scope: Optional[Callable[[str], None]] = None) -> None:
        self._store = store
        self._on_scope = on_scope
        #: The current list, most-run first. Empty until the first fetch lands,
        #: which is correct: `expand` then leaves `saved:x` exactly as typed,
        #: and typing a saved name in the first fifty milliseconds after the
        #: window opens is not a case anybody reaches.
        self._saved: tuple = ()
        #: The last few things this person searched for, newest first.
        #: Search-experience §2e - fetched the same way and held for the same
        #: reason: the box is being *focused*, which is not a moment to wait
        #: on a database.
        self._recent: tuple = ()
        #: Settings, pushed in. Read for one switch: `search_offer_recent`.
        self._settings: Any = None
        #: Told when the list changes, so a dropdown can redraw itself.
        self.changed: Optional[Callable[[tuple], None]] = None

    # -- what is in it -------------------------------------------------------

    @property
    def all(self) -> tuple:
        """The current list, as `SavedSearch`es."""
        return self._saved

    def rows(self, settings: Any = None) -> tuple:
        """`(label, insert)` pairs for the empty box. See `first_contact`."""
        from app.ui.first_contact import saved_rows

        return saved_rows(self._saved, settings=settings or self._settings)

    def sections(self) -> tuple:
        r"""What an empty, focused search box offers: recent, then saved.

        `((heading, ((label, insert), …)), …)`, empty when there is nothing to
        offer or when the person has switched it off. **§2e's last piece**, and
        the one the saved-search list was waiting on - both are the same
        dropdown, so building it twice would have been two ways for the same
        box to behave.
        """
        from app.ui.first_contact import sections

        return sections(self._recent, self._saved, self._settings)

    def set_settings(self, settings: Any) -> None:
        """The preferences, pushed in. Never read from here."""
        self._settings = settings

    # -- keeping it current --------------------------------------------------

    def refresh(self) -> None:
        """Re-read both lists on workers. Safe to call with no store."""
        if self._store is None:
            return
        from app.ui.workers import recent_searches_async, saved_searches_async

        saved_searches_async(self._store, self._took)
        # **Two workers rather than one, and they land independently.** The
        # dropdown draws whichever has arrived; a list that waited for both
        # would be empty for as long as the slower of two reads, on the
        # keystroke where somebody is least willing to wait.
        recent_searches_async(self._store, self._took_recent, self._settings)

    def _took_recent(self, rows: Any) -> None:
        """A fetched history has arrived. **Never raises.**"""
        try:
            self._recent = tuple(rows or ())
        except Exception:                        # noqa: BLE001 - a convenience
            self._recent = ()

    def save(self, name: str, query: str, scope: str = "all") -> None:
        """Store one, then refresh. §3b: only ever because somebody asked."""
        if self._store is None or not str(name or "").strip():
            return
        from app.ui.workers import save_search_async

        save_search_async(self._store, name, query, scope, self._took)

    def rename(self, old: str, new: str, on_done: Any = None) -> None:
        """Rename one, then refresh. Adoptions 3b - the dialog for this never existed."""
        self._change("rename", (old, new), on_done)

    def delete(self, name: str, on_done: Any = None) -> None:
        """Forget one, then refresh. Nothing is deleted except by being asked to."""
        self._change("delete", (name,), on_done)

    def _change(self, action: str, args: tuple, on_done: Any) -> None:
        if self._store is None:
            return
        from app.ui.workers import change_saved_search_async

        def landed(saved: Any) -> None:
            self._took(saved)
            if on_done is not None:
                on_done()

        change_saved_search_async(self._store, action, args, landed)

    def _took(self, saved: Any) -> None:
        """A fetched list has arrived. **Never raises**: it is a convenience."""
        try:
            self._saved = tuple(saved or ())
            if self.changed is not None:
                self.changed(self._saved)
        except Exception:                        # noqa: BLE001 - see docstring
            return

    # -- running one ---------------------------------------------------------

    def expand(self, text: str) -> str:
        r"""The query to run, with `saved:name` replaced by what it stands for.

        Also applies the saved scope, through `on_scope`, and records the run
        so the menu can order by it. Both only when something was expanded -
        an ordinary search must not touch the scope control or write to the
        database.

        **Slashes first, then saved.** `/saved invoices` becomes
        `saved:invoices` in `expand_slashes`, exactly as `/type pdf` becomes
        `type:pdf`, so there is one doorway and one grammar. Doing it the
        other way round would leave the slash form unexpanded.
        """
        from app.search.commands import expand_slashes
        from app.search.saved import (
            expand_saved, find, mentions_saved, names_in,
        )

        query = expand_slashes(str(text or "").strip())
        if not mentions_saved(query):
            return query

        # **Only the ones that resolved.** A name nobody saved is left in the
        # box as typed - `expand_saved`'s rule - and counting a run for it
        # would be recording that something happened which did not.
        names = tuple(name for name in names_in(query)
                      if find(self._saved, name) is not None)
        query, scope = expand_saved(query, self._saved)
        if scope and self._on_scope is not None:
            self._on_scope(scope)
        self._note(names)
        return query

    def _note(self, names: Sequence[str]) -> None:
        """Record that these were run, off this thread. **Never raises.**"""
        if self._store is None or not names:
            return
        try:
            from PyQt6.QtCore import QThreadPool

            from app.ui.workers import CallableWorker, run

            store = self._store

            def write() -> None:
                for name in names:
                    store.note_saved_search_run(name)

            run(QThreadPool.globalInstance(),
                CallableWorker(write, component="ui.search.saved"))
        except Exception:                        # noqa: BLE001 - a counter
            return
