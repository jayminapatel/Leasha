"""The search bar and its two timers.

Layer: L5

Two debounce timers, not one. Typing is a different question from having
finished: 150ms of stillness runs the keyword tier, 400ms runs the full hybrid
pipeline, and pressing Enter runs it immediately whatever the timers think.
The decision itself is `presenter.tier_for`, so it is tested; this module owns
the timers and the plumbing.

**Late results are dropped, not shown.** Every dispatch carries a generation
number. A slow search that lands after the person has typed more is recognised
by its stale generation and discarded - otherwise an older result overwrites a
newer one and the list flickers backwards, which looks like broken ranking
rather than a race.
"""

from __future__ import annotations

import time
from typing import Any

from PyQt6.QtCore import QEvent, Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtWidgets import QWidget

from app.ui.presenter import (
    IDLE_DEBOUNCE_MS, TYPING_DEBOUNCE_MS, Tier, federated_summary, notice_register_for,
    result_view_state, search_options, search_shape, tier_for,
)
from app.ui.widgets.history_pass import run_history_pass
from app.ui.widgets.interpret import interpret_into
from app.ui.widgets.result_table import offer_filters, redraw_with_details
from app.ui.widgets.skeleton import arm as arm_skeleton, gone as results_gone
from app.ui.widgets.result_tools import build_results_pane
from app.ui.widgets.search_bar import (
    build_controls, build_input, build_toolbar, scope_value, select_scope,
)
from app.ui.workers import (
    SearchWorker,
    decorate_results_async,
    record_open_async,
    run,
    stop_timers,
)

__all__ = ["SearchView"]


class SearchView(QWidget):
    """Search bar, filter chips, and the results beneath them."""

    result_opened = pyqtSignal(object)
    reveal_requested = pyqtSignal(object)
    reindex_requested = pyqtSignal(object)
    #: A new text size or spacing for the results pane, for the window to save.
    view_preferences_changed = pyqtSignal(object)
    error = pyqtSignal(object)

    #: One search, described by shape only - never the text of the query. The
    #: debug recorder listens to this; see `debug_recorder.py` for why a session
    #: file that contained somebody's actual searches would be a file nobody
    #: would ever send.
    searched = pyqtSignal(dict)

    #: Emitted after an interpretation, with the `Translation`. The window uses
    #: it for the status bar and the debug recorder.
    interpreted = pyqtSignal(object)

    def __init__(self, engine: Any, translator: Any = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._translator = translator
        # Empty means "follow this surface's contract" - see policy.py.
        self._search_preferences: dict = {}
        self._pool = QThreadPool.globalInstance()
        self._generation = 0
        self._shown_generation = -1
        self._shown_query = ""          # order 0q S8: the stable-update anchor
        #: Whether this session has ever drawn a result. Until it has, an empty
        #: answer is genuinely empty and should say so; afterwards, blanking a
        #: list somebody is reading is the worse of the two mistakes.
        self._shown_anything = False
        self._last_keystroke = time.monotonic()
        self._last_search_id: int | None = None
        #: How many rows the index answered with. The repository half appends
        #: to `ResultsView`, which owns the rows; this is only what its ranks
        #: continue from and what the status line counts.
        self._index_count = 0
        self._last_terms: list = []

        # `self.saved` is built by the box rather than here - it belongs to
        # the box, and this file is at the 250-line guard. It decides what an
        # empty box offers (§2e) and what `saved:name` expands to (§3).
        # Through the engine, which is what this view is given. Only the
        # *value* half of the `/` menu uses it, and `getattr` because an
        # engine without one is a menu offering the grammar's own values
        # rather than a view that fails to build.
        self.input, self.commands, self.saved = build_input(
            self, self._on_text_changed, self._on_submitted,
            store=getattr(engine, "store", None), on_scope=self.set_scope)

        (self.scope, self.interpret_button, self.rerank_toggle,
         self.view_button, self.status) = build_controls(
            self, on_scope=self._on_scope_changed, on_interpret=self.interpret,
            on_rerank=lambda: self._dispatch(Tier.FULL), on_view=self._view_changed)

        self.results, self.preview, self.split = build_results_pane(
            on_opened=self._on_opened, on_reveal=self.reveal_requested,
            on_reindex=self.reindex_requested, on_error=self.error,
            store=getattr(engine, "store", None), search_box=self.input, on_filter=self.search_now, engine=engine)
        self.input.installEventFilter(self)                    # item 6a
        self.results.explain_context = lambda: (self._last_terms, self._search_preferences)

        self.notices = build_toolbar(
            self, status=self.status, body=self.split,
            controls=(self.interpret_button, self.scope, self.rerank_toggle, self.view_button))
        self.notices.chosen.connect(self._apply_suggestion)

        # Two timers, because the two tiers answer different questions.
        self._interim_timer = QTimer(self)
        self._interim_timer.setSingleShot(True)
        self._interim_timer.setInterval(TYPING_DEBOUNCE_MS)
        self._interim_timer.timeout.connect(lambda: self._maybe_dispatch(submitted=False))

        self._full_timer = QTimer(self)
        self._full_timer.setSingleShot(True)
        self._full_timer.setInterval(IDLE_DEBOUNCE_MS)
        self._full_timer.timeout.connect(lambda: self._maybe_dispatch(submitted=False))

    def set_interpret_enabled(self, enabled: bool) -> None:
        """Show the Interpret button only when it can do something.

        Hidden rather than greyed - see `search_bar.build_interpret`.
        """
        self.interpret_button.setVisible(bool(enabled))

    def shutdown(self) -> None:
        """Stop the debounce timers - see `workers.stop_timers`."""
        # The names are found by suffix now; passing them was how this came to
        # name three timers that never existed. See `workers.stop_timers`.
        stop_timers(self)
        self.preview.shutdown()

    def focus(self) -> None:
        self.input.setFocus()
        self.input.selectAll()

    # Item 6a: ↓/↑ move the result selection without the box losing focus;
    # Enter opens it, Ctrl+Enter reveals it. Compact by necessity - this file
    # sits at the 250-line view guard - so the actual arithmetic is on
    # `ResultsView` (`forward_key`, `open_current`); this is only dispatch.
    def eventFilter(self, obj: Any, event: Any) -> bool:
        if obj is not self.input or event.type() != QEvent.Type.KeyPress: return False
        if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up):
            self.results.forward_key(event); return True
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.results.current_row():
            self.results.open_current(reveal=bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)); return True
        if event.key() == Qt.Key.Key_Escape and self.input.text():
            # order 0m Section1 item b: the M9 fix (`_on_text_changed`'s empty
            # branch, below) has always cleared results and status once the
            # box goes empty - "or pressing Esc" in that comment named the
            # intent, but nothing ever actually emptied the box on Escape.
            # `clear()` fires `textChanged`, the same path clearing the box
            # by hand already takes.
            self.input.clear(); return True
        return False

    # -- dispatch -----------------------------------------------------------

    def set_search_preferences(self, found: dict) -> None:
        # Pushed in, never read here: `Settings` belongs to the window.
        self._search_preferences = dict(found or {})
        self.saved.set_settings(found)

    def current_scope(self) -> str:
        return scope_value(self.scope)

    def set_scope(self, value: str) -> None:
        """Select a scope by value; unknown values are ignored."""
        select_scope(self.scope, value)

    def _view_changed(self, prefs: Any) -> None:
        self.results.set_view_preferences(prefs)
        self._apply_preview(prefs)
        self.view_preferences_changed.emit(prefs)

    def set_view_preferences(self, prefs: Any) -> None:
        """Applied by the window on startup, from what was saved last time."""
        self.view_button.prefs = prefs
        self.results.set_view_preferences(prefs)
        self._apply_preview(prefs)

    def _apply_preview(self, prefs: Any) -> None:
        self.preview.apply_preference(prefs, self.results.current_row())

    def _on_scope_changed(self, _index: int) -> None:
        """Re-run immediately rather than waiting for the next keystroke.

        Changing the scope is an explicit instruction about results already on
        screen. Leaving them there until something else is typed makes the
        control look broken.
        """
        if self.input.text().strip():
            self._dispatch(Tier.FULL)

    def search_now(self) -> None:
        """Run the full search immediately, as if Enter had been pressed.

        For anything that starts a search on the user's behalf - a filter chosen
        from a menu, a suggestion accepted. An explicit choice should not wait
        out a debounce meant for someone still typing.
        """
        self._dispatch(Tier.FULL)

    def _on_text_changed(self, text: str) -> None:
        # **An empty box is an instruction, not a search to debounce.**
        # `_dispatch` has always had a branch that clears the list and the
        # status line, and typing could never reach it: `_maybe_dispatch` only
        # dispatches when `tier_for` returns something other than `Tier.NONE`,
        # and `tier_for("")` returns exactly `Tier.NONE`. So clearing the box -
        # or pressing Esc - left the previous results on screen under a status
        # line still claiming a count for a query that no longer existed.
        if not text.strip():
            self._interim_timer.stop()
            self._full_timer.stop()
            self._dispatch(Tier.FULL)          # the empty branch; the tier is unread
            return
        self._last_keystroke = time.monotonic()
        self._interim_timer.start()
        self._full_timer.start()

    def _on_submitted(self) -> None:
        self._interim_timer.stop(); self._full_timer.stop()
        self._dispatch(Tier.FULL)

    def _maybe_dispatch(self, *, submitted: bool) -> None:
        still_for_ms = int((time.monotonic() - self._last_keystroke) * 1000)
        tier = tier_for(self.input.text(), still_for_ms=still_for_ms, submitted=submitted)
        if tier != Tier.NONE: self._dispatch(tier)

    def _dispatch(self, tier: str) -> None:
        # `/type pdf` becomes `type:pdf` here, so nothing below this line -
        # and nothing in the parser - has to know slashes exist. `saved:name`
        # becomes the query it stands for at the same moment, and for the same
        # reason: one doorway, one grammar. See `saved_box.SavedSearches`.
        query = self.saved.expand(self.input.text())
        if not query:
            # Whatever is still out there is now stale: without this a search
            # dispatched a moment before Esc answered into the empty page.
            self._generation += 1; self._shown_generation = self._generation
            self._shown_anything = False
            self._index_count = 0
            self.results.clear()
            self.status.setText("")
            return

        self._generation += 1
        options = search_options(tier, scope=self.current_scope(), rerank=self.rerank_toggle.isChecked(),
                                 surface="search", preferences=self._search_preferences, declined=self.chips.declined)
        worker = SearchWorker(
            self._engine, query, tier=tier, generation=self._generation, **options
        )
        arm_skeleton(self.results)                 # §6d: bars if this takes >300ms
        worker.signals.finished.connect(self._on_results)
        # **The notice bar, not a modal.** This fires per debounced keystroke,
        # so a transiently locked database - which is exactly what an index run
        # produces - used to raise one `QMessageBox` per character typed, each
        # of which has to be dismissed before the next appears. The failure is
        # worth saying; it is not worth stopping the window for. `self.error`
        # stays for things the person actually asked for, like opening a file.
        worker.signals.failed.connect(self._search_failed)
        run(self._pool, worker)

        # The repository half. `run_history_pass` decides nothing: it asks
        # `presenter.git_pass` and starts a worker if the answer is yes.
        generation = self._generation
        run_history_pass(
            store=getattr(self._engine, "store", None), query=query, tier=tier,
            shown=self._index_count, pool=self._pool,
            set_status=self.status.setText,
            on_rows=lambda rows, g=generation: self._on_git(rows, g),
        )

    def _apply_suggestion(self, href: str) -> None:
        """Apply a suggestion the person clicked. **Only on a click.**"""
        if not str(href).startswith("apply:"):
            return
        self.input.setText(f"{self.input.text().strip()} {href[6:]}".strip())
        self._dispatch(Tier.FULL)

    def _on_git(self, rows: Any, generation: int) -> None:
        """Append the repository rows beneath what is already on screen."""
        if generation < self._shown_generation or not rows:
            return
        total = self.results.append_results(rows, self._last_terms)
        self.status.setText(
            federated_summary(self._index_count, total - self._index_count))

    # -- results ------------------------------------------------------------

    def _search_failed(self, error: Any) -> None:
        """A background search that raised. Said in the bar, never in a dialog."""
        self.notices.show_notices([error])
        self.status.setText("")

    def _on_results(self, payload: Any) -> None:
        generation, response = payload

        # Stale: the person has typed since this was dispatched. Showing it would
        # replace newer results with older ones.
        if generation < self._shown_generation:
            return
        self._shown_generation = generation

        self._last_search_id = response.search_id
        # **The window's half of "nothing fails silently".** The engine decides
        # a search was degraded; until this line the only place that reached
        # was a log file.
        notices, terms, summary, status = result_view_state(
            response, self.input.text(),
            interpret_enabled=self.interpret_button.isVisible())
        self.notices.show_notices(notices)
        # Offers for the filters the sentence contains arrive from a worker and
        # join the bar - see `presenter.filter_offers`. Only if still current.
        offer_filters(self, notices, generation, response)
        self._shown_anything = self._shown_anything or bool(response.results)
        self.status.setText(status)
        # The shape of the search, never its text - see `debug_recorder.py`.
        self.searched.emit(search_shape(
            response, query_len=len(self.input.text()), scope=self.current_scope()))

        if not response.results:
            # **Keep what is on screen.** Mail feels better than this tab
            # because it never blanks, and a search that momentarily finds
            # nothing - mid-word, or while the interim tier is still running -
            # should not empty a list somebody is reading.
            if not self._shown_anything:
                self.results.clear(summary)
            return

        # **The metadata is fetched on a worker.** `mail_details` is a SQLite
        # query and `missing_paths` is one filesystem stat per result; both were
        # running here, on the UI thread, in the handler that paints results.
        # The comment above `mail_details` says "one query, not fifty" - and I
        # never asked the prior question of whether it belonged on this thread
        # at all.
        #
        # The rows are drawn immediately with what is already known, and the
        # subtitles and missing-file marks arrive a moment later.
        self._index_count = len(response.results)
        self._last_terms = terms
        register = notice_register_for("search", self._search_preferences)
        # Order 0q S8: an interim->full swap of the SAME typed query keeps the
        # anchor (the M9-adjacent bug order 0m's real-window scenario found -
        # every render used to start at the top, tier swap or not); a change
        # of query text still starts at the top, unchanged from before.
        raw = response.parsed.raw if response.parsed else ""
        keep_scroll = bool(raw) and raw == self._shown_query
        self.results.show_results(response.results, terms, summary=summary,
                                  register=register, keep_scroll=keep_scroll)
        self._shown_query = raw

        generation = self._shown_generation
        decorate_results_async(
            getattr(self._engine, "store", None), response.results,
            lambda extra, g=generation: self._decorated(extra, g, terms, summary, response))

    def _decorated(self, extra: Any, generation: int, terms: Any,
                   summary: str, response: Any) -> None:
        # Painting moved to `widgets/result_table.redraw_with_details`; what is
        # left here is the staleness check, which is this view's business.
        if generation == self._shown_generation and not results_gone(self.results):
            redraw_with_details(self.results, response, terms, summary, extra)

    def interpret(self) -> None:
        """Translate the sentence in the box, then search what it produced."""
        interpret_into(self)

    def _on_opened(self, row: Any) -> None:
        """The click is the label: this result was the useful one.

        **Emitted first, recorded after.** Everything in Layer 10 is derived
        from these clicks, but `record_open` is a database *write* and it was
        running on the UI thread between the double-click and the file opening -
        so a busy or locked index made opening a result feel slow for a reason
        that has nothing to do with opening it.
        """
        self.result_opened.emit(row)

        # Extracted to `workers.record_open_async` - it is a database write
        # that must stay off this thread, every view needs it, and this file
        # was at the 250-line limit the presenter guard allows.
        record_open_async(self._engine, self._last_search_id, row.chunk_id)

    # -- interpreting a sentence --------------------------------------------

