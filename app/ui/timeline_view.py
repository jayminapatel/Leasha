r"""The Life Timeline: a place to wander, not a search.

Layer: L5, driving L4 (`app/reports/timeline.py`)

Order 202626270602 (0n) section 4. Pick a year, then a month - or type any
range - and see everything from that time, wherever it lives now: photographs
as thumbnails, documents, videos and mail as rows, in the order it happened,
with the ones on a drive in a drawer marked as such.

**No query box, on purpose (4d).** The search box already understands dates;
this is for the moments when you do not know what you are looking for, only
roughly when. So there is nothing to type but a date range.

**Nothing on this thread touches the index.** Every read is one
`CallableWorker` (`test_ui_never_blocks.py`): the counts for the picker, and
one page of the timeline at a time - the next asked for as the scroll bar nears
the bottom, so a month of four thousand photographs never loads all at once and
never holds the window still. A result that arrives after the person has
already picked another month is dropped (`_generation`), the same stamp
`SearchWorker` uses.

What goes on the list and every sentence on it is `app/ui/presenter/timeline.py`
and `app/reports/timeline_words.py`; the painting is `widgets/timeline_list.py`
and the controls are `widgets/timeline_picker.py`.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import QThreadPool, pyqtSignal
from PyQt6.QtWidgets import QLabel, QVBoxLayout, QWidget

from app.reports.timeline import PAGE_SIZE, Period, month_of_ns, refresh_overview, timeline_page
from app.reports.timeline_words import (
    NOTHING_INDEXED, empty_period_sentence, period_words, summary_sentence, thin_data_notes,
)
from app.ui.presenter.timeline import blocks_for, loading_sentence, shown_sentence
from app.ui.widgets.file_menu import FileActions, build_menu
from app.ui.widgets.timeline_list import TimelineList
from app.ui.widgets.timeline_picker import TimelinePicker
from app.ui.workers import CallableWorker, run

__all__ = ["TimelineView"]


class TimelineView(QWidget):
    """Year -> month -> everything from then. Read-only, worker-fed."""

    error = pyqtSignal(object)
    opened = pyqtSignal(object)              # a TimelineEntry - it has `path`, `volume_id`...
    reveal_requested = pyqtSignal(object)

    def __init__(self, store: Any = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._overview: Any = None
        self._period: Optional[Period] = None
        self._generation = 0
        self._loading = False
        self._done = True
        self._cursor: Any = None
        self._items: list = []
        self._last_day: Any = None
        self._connected: Optional[dict] = None

        self.summary = QLabel(NOTHING_INDEXED)
        self.summary.setWordWrap(True)
        self.notes = QLabel("")
        self.notes.setObjectName("resultsSummary")
        self.notes.setWordWrap(True)
        self.notes.hide()
        self.picker = TimelinePicker(store)
        self.picker.period_chosen.connect(self.browse)
        self.picker.kind_changed.connect(self._kind_changed)
        self.picker.fold_changed.connect(lambda _on: self._reload())
        self.picker.bad_date.connect(lambda sentence: self.status.setText(sentence))
        self.heading = QLabel("")
        self.heading.setObjectName("resultsSummary")
        heading_font = self.heading.font()               # the period is the page's title
        heading_font.setBold(True)
        heading_font.setPointSizeF(max(9.0, heading_font.pointSizeF()) * 1.25)
        self.heading.setFont(heading_font)
        self.list = TimelineList()
        self.list.near_end.connect(self._fetch_more)
        self.list.opened.connect(self.opened)
        self.list.menu_requested.connect(self._show_menu)
        self.status = QLabel("")
        self.status.setObjectName("resultsSummary")
        self.status.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for item in (self.summary, self.notes, self.picker, self.heading):
            layout.addWidget(item)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.status)

    # -- the counts -------------------------------------------------------------

    def refresh(self) -> None:
        """Recount what there is - only when something has been indexed since."""
        if self._store is None:
            return
        same = self._overview is not None and self._overview.kind == self.picker.kind()
        known = self._overview.generated_at if same else None
        worker = CallableWorker(refresh_overview, self._store, self.picker.kind(), known,
                                component="ui.timeline")
        worker.signals.finished.connect(self._overview_ready)
        worker.signals.failed.connect(self.error.emit)
        run(QThreadPool.globalInstance(), worker)

    def _overview_ready(self, overview: Any) -> None:
        if overview is None or overview.kind != self.picker.kind():
            return                                   # unchanged, or for a kind no longer chosen
        self._overview = overview
        self.summary.setText(summary_sentence(overview))
        self.notes.setText("  ".join(thin_data_notes(overview)))
        self.notes.setVisible(bool(self.notes.text()))
        self.picker.set_overview(overview)

    def _kind_changed(self, _kind: str) -> None:
        self._overview = None
        self.refresh()
        self._reload()

    def _reload(self) -> None:
        if self._period is not None:
            self.browse(self._period)

    # -- entry points from other pages ------------------------------------------

    def browse_month_of(self, when_ns: int) -> None:
        """The month a date falls in - what "see everything from this month" asks."""
        year, month = month_of_ns(when_ns)
        self.picker.show_month(year, month)
        self.browse(Period.month(year, month))

    def browse_range(self, after: str, before: str) -> None:
        """A range given as `after:`/`before:` text - what the timeline strip speaks."""
        self.picker.set_range_text(after, before)
        self.picker.choose_range()

    # -- the list -----------------------------------------------------------------

    def browse(self, period: Period) -> None:
        """Show everything from `period`, first page now, the rest as it is scrolled to."""
        self._period = period
        self._generation += 1
        self._cursor, self._items, self._last_day = None, [], None
        self._connected = None
        self._done, self._loading = False, False
        self.list.clear_blocks()
        self.heading.setText(period_words(period))
        self.status.setText(loading_sentence(period_words(period)))
        self._fetch_more()

    def _fetch_more(self) -> None:
        if self._loading or self._done or self._period is None or self._store is None:
            return
        self._loading = True
        generation = self._generation
        worker = CallableWorker(timeline_page, self._store, self._period, self._cursor, PAGE_SIZE,
                                self.picker.kind(), self.picker.fold(), self._connected,
                                component="ui.timeline")
        worker.signals.finished.connect(lambda page, g=generation: self._page_ready(g, page))
        worker.signals.failed.connect(lambda error, g=generation: self._page_failed(g, error))
        run(QThreadPool.globalInstance(), worker)

    def _page_failed(self, generation: int, error: Any) -> None:
        if generation == self._generation:
            self._loading = False
            self.error.emit(error)

    def _page_ready(self, generation: int, page: Any) -> None:
        if generation != self._generation:
            return                                   # another month was picked meanwhile
        self._loading = False
        self._connected = page.connected or self._connected
        self._cursor, self._done = page.cursor, page.done
        blocks, self._last_day = blocks_for(page.items, self._last_day)
        self._items.extend(page.items)
        self.list.append_blocks(blocks)
        shown = sum(1 + len(fold.older) for fold in self._items)
        self.status.setText(shown_sentence(shown, self._done) if shown
                            else empty_period_sentence(self._period))

    # -- right-click ------------------------------------------------------------

    def _show_menu(self, fold: Any, where: Any) -> None:
        entry = fold.head
        actions = FileActions(open_file=lambda: self.opened.emit(entry),
                              reveal=lambda: self.reveal_requested.emit(entry),
                              offline=not entry.reachable)     # never a stat (2026-10-04)
        menu = build_menu(self, entry.real_path or entry.path, actions)
        if fold.folded:
            menu.addSeparator()
            show = menu.addAction(f"Show all {len(fold.older) + 1} separately")
            show.setToolTip("Take this group apart so every photo or copy has its own line.")
            show.triggered.connect(lambda: self.unfold(fold))
        menu.exec(where)

    def unfold(self, fold: Any) -> None:
        """Replace one group with its members, oldest first, keeping the scroll position."""
        from app.search.folding import Fold

        at = next((i for i, item in enumerate(self._items) if item is fold), None)
        if at is None:
            return
        members = sorted([fold.head, *fold.older], key=lambda e: (e.when_ns, e.file_id))
        self._items[at:at + 1] = [Fold(head=member) for member in members]
        keep = self.list.verticalScrollBar().value()
        self.list.clear_blocks()
        blocks, _day = blocks_for(self._items, None)
        self.list.append_blocks(blocks)
        self.list.verticalScrollBar().setValue(keep)
