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
from typing import Any, Optional

from PyQt6.QtCore import QThreadPool, QTimer, pyqtSignal
from PyQt6.QtGui import QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.search.commands import expand_slashes
from app.ui.presenter import (
    IDLE_DEBOUNCE_MS,
    TYPING_DEBOUNCE_MS,
    Tier,
    search_shape,
    semantic_health,
    tier_for,
)
from app.ui.results_view import ResultsView
from app.ui.widgets.command_popup import attach_to
from app.ui.workers import CallableWorker, SearchWorker, run

__all__ = ["SearchView"]


class SearchView(QWidget):
    """Search bar, filter chips, and the results beneath them."""

    result_opened = pyqtSignal(object)
    reveal_requested = pyqtSignal(object)
    reindex_requested = pyqtSignal(object)
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
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._translator = translator
        self._pool = QThreadPool.globalInstance()
        self._generation = 0
        self._shown_generation = -1
        self._last_keystroke = time.monotonic()
        self._last_search_id: Optional[int] = None

        self.input = QLineEdit()
        self.input.setPlaceholderText("Search…    press / for filters")
        self.input.setClearButtonEnabled(True)
        self.input.textChanged.connect(self._on_text_changed)
        self.input.returnPressed.connect(self._on_submitted)

        # Typing `/` lists the filters. They all worked already; nothing in the
        # app had ever mentioned them, so the box was in practice a bag of words.
        # The placeholder now advertises the doorway rather than trying to fit
        # five operators into it, which nobody read.
        self.commands = attach_to(self.input)

        # Scope chips. A filter, not a mode: you should never have to decide
        # whether a thing was an email or a document *before* typing, because
        # the usual answer is "I do not remember, that is why I am searching".
        self.scope = QComboBox()
        self.scope.addItem("Everything", "all")
        self.scope.addItem("Mail only", "mail")
        self.scope.addItem("Documents only", "documents")
        self.scope.setToolTip(
            "Narrow the search to mail or to files on disk.\n"
            "Mail results show who sent it and when instead of a file path."
        )
        self.scope.currentIndexChanged.connect(self._on_scope_changed)

        # Explicit, never automatic. Plain Enter runs what was typed, exactly as
        # it always has; this button is the user choosing to spend a second on a
        # model. Silently interpreting every search would make results
        # unpredictable, and unpredictable search over your own archive is worse
        # than blunt search because you stop trusting it.
        self.interpret_button = QPushButton("Interpret")
        self.interpret_button.setToolTip(
            "Turn a sentence into a search query using Ollama.  Ctrl+Enter\n\n"
            "The query it builds goes into the box so you can read and edit it.\n"
            "If Ollama is not running, your words are searched for unchanged."
        )
        self.interpret_button.clicked.connect(lambda _c=False: self.interpret())
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.interpret)
        QShortcut(QKeySequence("Ctrl+Enter"), self, activated=self.interpret)

        self.rerank_toggle = QCheckBox("Rerank")
        self.rerank_toggle.setToolTip(
            "Slower but more precise ordering. Turning it off does not need a restart."
        )
        self.rerank_toggle.setChecked(True)
        self.rerank_toggle.stateChanged.connect(lambda _state: self._dispatch(Tier.FULL))

        self.status = QLabel("")
        self.status.setObjectName("searchStatus")

        self.results = ResultsView()
        self.results.opened.connect(self._on_opened)
        self.results.reveal_requested.connect(self.reveal_requested)
        self.results.reindex_requested.connect(self.reindex_requested)

        top = QHBoxLayout()
        top.addWidget(self.input, stretch=1)
        top.addWidget(self.interpret_button)
        top.addWidget(self.scope)
        top.addWidget(self.rerank_toggle)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.status)
        layout.addWidget(self.results, stretch=1)

        # Two timers, because the two tiers answer different questions.
        self._interim_timer = QTimer(self)
        self._interim_timer.setSingleShot(True)
        self._interim_timer.setInterval(TYPING_DEBOUNCE_MS)
        self._interim_timer.timeout.connect(lambda: self._maybe_dispatch(submitted=False))

        self._full_timer = QTimer(self)
        self._full_timer.setSingleShot(True)
        self._full_timer.setInterval(IDLE_DEBOUNCE_MS)
        self._full_timer.timeout.connect(lambda: self._maybe_dispatch(submitted=False))

    def focus(self) -> None:
        self.input.setFocus()
        self.input.selectAll()

    # -- dispatch -----------------------------------------------------------

    def current_scope(self) -> str:
        return str(self.scope.currentData() or "all")

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
            self.results.clear()
            self.status.setText("")
            return

        self._generation += 1
        options: dict[str, Any] = {"scope": self.current_scope()}
        if tier == Tier.FULL:
            options["rerank"] = self.rerank_toggle.isChecked()

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
        terms = list(response.parsed.terms) + list(response.parsed.phrases) if response.parsed else []

        if not response.results:
            hint = ""
            if response.parsed and response.parsed.has_filters:
                hint = "  The filters may be excluding everything."
            self.results.clear(f"No results.{hint}")
            self.status.setText(self._status_line(response))
            self._announce(response)
            return

        self.results.show_results(response.results, terms, summary=self._status_line(response))
        # Say it when the semantic half returned nothing. Silent degradation is
        # how "search feels worse than it should" goes unreported for weeks.
        self.status.setText(semantic_health(response) or "")
        self._announce(response)

        if response.parsed and response.parsed.unknown_operators:
            self.status.setText(
                "Ignored: " + ", ".join(response.parsed.unknown_operators)
            )

    def _announce(self, response: Any) -> None:
        """Emit the shape of a completed search, for the debug recorder."""
        self.searched.emit(search_shape(
            response, query_len=len(self.input.text()), scope=self.current_scope()
        ))

    def _status_line(self, response: Any) -> str:
        bits = [f"{len(response.results)} result(s)", f"{response.elapsed_ms:.0f}ms"]
        if response.interim:
            bits.append("keyword only, still searching…")
        if response.from_cache:
            bits.append("cached")
        if response.reranked:
            bits.append("reranked")
        return "  ·  ".join(bits)

    def _on_opened(self, row: Any) -> None:
        # The click is the label: this result was the useful one. Everything in
        # Layer 10 is derived from these, so it is recorded before the file opens.
        try:
            self._engine.record_open(self._last_search_id, row.chunk_id)
        except Exception:                        # noqa: BLE001 - never block an open
            pass
        self.result_opened.emit(row)

    # -- interpreting a sentence --------------------------------------------

    def interpret(self) -> None:
        """Translate the sentence in the box, then search what it produced.

        **The translated query goes into the box.** That is a hard requirement,
        not a nicety: a bad translation must be a two-second correction rather
        than a mystery, and it can only be corrected if it can be seen. The next
        search then starts from it, like any other text.

        Off the UI thread, because it costs about a second and a second of
        frozen window is how this application has repeatedly looked broken.
        """
        sentence = self.input.text().strip()
        if not sentence or self._translator is None:
            self._dispatch(Tier.FULL)
            return

        self.interpret_button.setEnabled(False)
        self.status.setText("Interpreting…")

        worker = CallableWorker(
            self._translator.translate, sentence, component="ui.translate"
        )
        worker.signals.finished.connect(self._interpreted)
        worker.signals.failed.connect(self._interpret_failed)
        worker.signals.done.connect(
            lambda: self.interpret_button.setEnabled(True)
        )
        run(self._pool, worker)

    def _interpreted(self, translation: Any) -> None:
        if translation.changed:
            # Into the box, so it is visible and editable.
            self.input.setText(translation.query)
        self.status.setText(translation.note)
        self.interpreted.emit(translation)
        self._dispatch(Tier.FULL)

    def _interpret_failed(self, error: Any) -> None:
        """Even a failure searches. `translate` is not supposed to raise, but a
        button that does nothing is worse than one that does the plain thing."""
        self.status.setText(getattr(error, "message", "Could not interpret that."))
        self._dispatch(Tier.FULL)
