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
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from app.ui.presenter import IDLE_DEBOUNCE_MS, TYPING_DEBOUNCE_MS, Tier, tier_for
from app.ui.results_view import ResultsView
from app.ui.workers import SearchWorker

__all__ = ["SearchView"]


class SearchView(QWidget):
    """Search bar, filter chips, and the results beneath them."""

    result_opened = pyqtSignal(object)
    reveal_requested = pyqtSignal(object)
    add_to_document = pyqtSignal(object)
    reindex_requested = pyqtSignal(object)
    error = pyqtSignal(object)

    def __init__(self, engine: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._engine = engine
        self._pool = QThreadPool.globalInstance()
        self._generation = 0
        self._shown_generation = -1
        self._last_keystroke = time.monotonic()
        self._last_search_id: Optional[int] = None

        self.input = QLineEdit()
        self.input.setPlaceholderText(
            'Search…    type:pdf  after:2024  path:Projects  "exact phrase"  -exclude'
        )
        self.input.setClearButtonEnabled(True)
        self.input.textChanged.connect(self._on_text_changed)
        self.input.returnPressed.connect(self._on_submitted)

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
        self.results.add_to_document.connect(self.add_to_document)
        self.results.reindex_requested.connect(self.reindex_requested)

        top = QHBoxLayout()
        top.addWidget(self.input, stretch=1)
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
        query = self.input.text().strip()
        if not query:
            self.results.clear()
            self.status.setText("")
            return

        self._generation += 1
        options: dict[str, Any] = {}
        if tier == Tier.FULL:
            options["rerank"] = self.rerank_toggle.isChecked()

        worker = SearchWorker(
            self._engine, query, tier=tier, generation=self._generation, **options
        )
        worker.signals.finished.connect(self._on_results)
        worker.signals.failed.connect(self.error)
        self._pool.start(worker)

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
            return

        self.results.show_results(response.results, terms, summary=self._status_line(response))
        self.status.setText("")

        if response.parsed and response.parsed.unknown_operators:
            self.status.setText(
                "Ignored: " + ", ".join(response.parsed.unknown_operators)
            )

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
