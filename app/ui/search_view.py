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

from PyQt6.QtCore import QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from app.search.commands import expand_slashes
from app.ui.presenter import (
    IDLE_DEBOUNCE_MS,
    TYPING_DEBOUNCE_MS,
    Tier,
    results_message,
    search_options,
    search_shape,
    tier_for,
)
from app.ui.results_view import ResultsView
from app.ui.view_options import button as view_button
from app.ui.widgets.interpret import run_interpretation
from app.ui.widgets.notice_bar import NoticeBar
from app.ui.widgets.preview import attach_preview
from app.ui.widgets.search_bar import (
    build_input,
    build_interpret,
    build_rerank,
    build_scope,
    scope_value,
    select_scope,
)
from app.ui.workers import SearchWorker, decorate_results_async, record_open_async, run, stop_timers

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

    def __init__(
        self,
        engine: Any,
        translator: Any = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._translator = translator
        self._pool = QThreadPool.globalInstance()
        self._generation = 0
        self._shown_generation = -1
        #: Whether this session has ever drawn a result. Until it has, an empty
        #: answer is genuinely empty and should say so; afterwards, blanking a
        #: list somebody is reading is the worse of the two mistakes.
        self._shown_anything = False
        self._last_keystroke = time.monotonic()
        self._last_search_id: int | None = None

        self.input, self.commands = build_input(
            self, self._on_text_changed, self._on_submitted)

        # Scope chips. A filter, not a mode: you should never have to decide
        # whether a thing was an email or a document *before* typing, because
        # the usual answer is "I do not remember, that is why I am searching".
        # The four controls beside the box - see `widgets/search_bar.py` for
        # why each tooltip is load-bearing.
        self.scope = build_scope(self, self._on_scope_changed)
        self.interpret_button = build_interpret(self, lambda _c=False: self.interpret())
        self.rerank_toggle = build_rerank(self, lambda _state: self._dispatch(Tier.FULL))
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.interpret)
        QShortcut(QKeySequence("Ctrl+Enter"), self, activated=self.interpret)

        # Text size and spacing for the results pane. Results are the one place
        # in this window people *read* rather than scan, and the size that suits
        # a paragraph of snippet is not the size that suits a toolbar - so it is
        # this pane's own setting rather than an application-wide zoom.
        #
        # No columns: a result is not a table. The same widget as the Files and
        # Mail menus, so all three read identically.
        self.view_button = view_button(
            self, None, "", on_change=self._view_changed, grouping=True)

        self.status = QLabel("")
        self.status.setObjectName("searchStatus")

        self.results = ResultsView()
        self.results.opened.connect(self._on_opened)
        self.results.reveal_requested.connect(self.reveal_requested)
        self.results.reindex_requested.connect(self.reindex_requested)

        # Off until asked for - `Ctrl+P` or the View menu. See preview.py.
        self.preview, self.split = attach_preview(
            self.results, self._on_opened, self.error)

        top = QHBoxLayout()
        top.addWidget(self.input, stretch=1)
        top.addWidget(self.interpret_button)
        top.addWidget(self.scope)
        top.addWidget(self.rerank_toggle)
        top.addWidget(self.view_button)

        # Above the results and below the status line: a degradation is about
        # the results, so it belongs where the eye lands before reading them.
        self.notices = NoticeBar(self)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.status)
        layout.addWidget(self.notices)
        layout.addWidget(self.split, stretch=1)

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

    # -- dispatch -----------------------------------------------------------

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

    def _on_text_changed(self, _text: str) -> None:
        self._last_keystroke = time.monotonic()
        self._interim_timer.start()
        self._full_timer.start()

    def _on_submitted(self) -> None:
        self._interim_timer.stop()
        self._full_timer.stop()
        self._dispatch(Tier.FULL)

    def _maybe_dispatch(self, *, submitted: bool) -> None:
        still_for_ms = int((time.monotonic() - self._last_keystroke) * 1000)
        tier = tier_for(self.input.text(), still_for_ms=still_for_ms, submitted=submitted)
        if tier != Tier.NONE:
            self._dispatch(tier)

    def _dispatch(self, tier: str) -> None:
        # `/type pdf` becomes `type:pdf` here, so nothing below this line -
        # and nothing in the parser - has to know slashes exist.
        query = expand_slashes(self.input.text().strip())
        if not query:
            self._shown_anything = False
            self.results.clear()
            self.status.setText("")
            return

        self._generation += 1
        options = search_options(tier, scope=self.current_scope(),
                                 rerank=self.rerank_toggle.isChecked())
        worker = SearchWorker(
            self._engine, query, tier=tier, generation=self._generation, **options
        )
        worker.signals.finished.connect(self._on_results)
        worker.signals.failed.connect(self.error)
        run(self._pool, worker)

    # -- results ------------------------------------------------------------

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
        self.notices.show_notices(getattr(response, "notices", ()))
        self._shown_anything = self._shown_anything or bool(response.results)
        terms = list(response.parsed.terms) + list(response.parsed.phrases) if response.parsed else []
        summary, status = results_message(response)
        self.status.setText(status or summary)
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
        self.results.show_results(response.results, terms, summary=summary)

        generation = self._shown_generation
        decorate_results_async(
            getattr(self._engine, "store", None), response.results,
            lambda extra, g=generation: self._decorated(extra, g, terms, summary, response))

    def _decorated(self, extra: Any, generation: int, terms: Any,
                   summary: str, response: Any) -> None:
        """Redraw with the mail subtitles and missing-file marks."""
        if generation != self._shown_generation:
            return                               # a newer search has landed
        # Same results, a moment later - so this is not a new search and must
        # not move somebody who has started reading.
        self.results.show_results(
            response.results, terms, summary=summary,
            details=extra.get("details", {}), missing=extra.get("missing", set()),
            keep_scroll=True,
        )

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

    def interpret(self) -> None:
        """Translate the sentence in the box, then search what it produced.

        The feature lives in `widgets/interpret.py`; this is the wiring. Every
        outcome ends in a search - see that module for why.
        """
        started = run_interpretation(
            translator=self._translator,
            sentence=self.input.text(),
            button=self.interpret_button,
            pool=self._pool,
            set_text=self.input.setText,
            set_status=self.status.setText,
            on_done=self._interpreted,
        )
        if not started:
            self._dispatch(Tier.FULL)

    def _interpreted(self, translation: Any) -> None:
        # **Interpreting used to run the whole pipeline twice.** Writing the
        # translated query into the box fires `textChanged`, restarting both
        # debounce timers exactly as typing does - and then this dispatched
        # immediately. The second run landed 400ms later doing identical work.
        #
        # Cancelled here rather than writing the text with signals blocked: the
        # command popup listens to `textChanged` too and needs to see it.
        self._interim_timer.stop()
        self._full_timer.stop()

        if translation is not None:
            self.interpreted.emit(translation)
        self._dispatch(Tier.FULL)
