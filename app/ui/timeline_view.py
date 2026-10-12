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

from PySide6.QtCore import QThreadPool, Signal
from PySide6.QtWidgets import QLabel, QStackedWidget, QVBoxLayout, QWidget

from app.reports.timeline import PAGE_SIZE, Period, month_of_ns, refresh_overview, timeline_page
from app.reports.timeline_words import (
    NOTHING_INDEXED, empty_period_sentence, period_words, summary_sentence, thin_data_notes,
)
from app.ui.presenter.timeline import blocks_for, loading_sentence, shown_sentence
from app.ui.widgets.file_menu import FileActions, build_menu
from app.ui.widgets.browse_table import BrowsePane
from app.ui.widgets.segmented import SegmentedControl
from app.ui.widgets.timeline_list import TimelineList
from app.ui.widgets.timeline_picker import TimelinePicker
from app.ui.workers import CallableWorker, run
from app.ui.qtsip import open_menu

__all__ = ["TimelineView"]


class TimelineView(QWidget):
    """Year -> month -> everything from then. Read-only, worker-fed."""

    error = Signal(object)
    opened = Signal(object)              # a TimelineEntry - it has `path`, `volume_id`...
    reveal_requested = Signal(object)

    def __init__(self, store: Any = None, parent: Optional[QWidget] = None) -> None:
        """Build the picker, the list and the labels. UI thread; `refresh` reads on a
        worker.
        """
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
        # The other way of looking at the same entries: a table, with the View menu every list page has. The two
        # share the pages the timeline fetches; which one shows is the person's, and is remembered.
        self.table = BrowsePane()
        self.table.near_end.connect(self._fetch_more)
        self.table.opened.connect(self.opened)
        self.table.menu_requested.connect(self._show_menu)
        self.table.error.connect(self.error)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.list)
        self.stack.addWidget(self.table)
        self.shape = SegmentedControl()
        self.shape.addItem("Thumbnails", "timeline")
        self.shape.addItem("Table", "table")
        self.shape.setToolTip("Thumbnails: photographs as pictures under the day they are from. "
                              "Table: one line each, with columns you choose.")
        self.shape.setAccessibleName("How Browse is shown")
        self.view_button = self.table.bind_store(store, self.picker.top_row) if store is not None else None
        self.picker.top_row.insertWidget(self.picker.top_row.count() - 1, self.shape)
        self.shape.currentIndexChanged.connect(self._shape_changed)
        self.status = QLabel("")
        self.status.setObjectName("resultsSummary")
        self.status.setWordWrap(True)

        self.picker.add_summary(self.summary)               # the count line shares the picker's first row
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        for item in (self.notes, self.picker, self.heading):
            layout.addWidget(item)
        layout.addWidget(self.stack, 1)
        layout.addWidget(self.status)
        self._restore_shape()
        from app.ui.widgets.side_panel import follow_index

        follow_index(self, self.refresh)           # the years and months follow the index while it is read

    # -- thumbnails or table ----------------------------------------------------

    def _restore_shape(self) -> None:
        try:
            saved = str(self._store.get_state("ui:browse_shape") or "") if self._store is not None else ""
        except Exception:                                  # noqa: BLE001 - a default is fine
            saved = ""
        self.shape.blockSignals(True)
        self.shape.setCurrentIndex(1 if saved == "table" else 0)
        self.shape.blockSignals(False)
        self.stack.setCurrentIndex(1 if saved == "table" else 0)

    def _shape_changed(self, index: int) -> None:
        self.stack.setCurrentIndex(1 if index == 1 else 0)
        if index == 1:
            self.table.set_folds(self._items)
        try:
            from app.ui.state_writes import save_state

            save_state(self._store, "ui:browse_shape", "table" if index == 1 else "timeline")
        except Exception:                                  # noqa: BLE001 - not remembering is not an error
            pass

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
        """UI thread: the counts landed, unless unchanged or for another kind."""
        if overview is None or overview.kind != self.picker.kind():
            return                                   # unchanged, or for a kind no longer chosen
        self._overview = overview
        self.summary.setText(summary_sentence(overview))
        self.notes.setText("  ".join(thin_data_notes(overview)))
        self.notes.setVisible(bool(self.notes.text()))
        self.picker.set_overview(overview)
        if self._period is None and overview.years:
            # Open on the newest year that has anything, so the page is never a blank list waiting for a
            # choice ("nothing is coming up there" - the owner, 2026-10-11).
            newest = max((int(y) for y, n in overview.years if n), default=None)
            if newest is not None:
                self.picker.show_year(newest)
                self.picker.month_buttons[0].setChecked(True)
                self.browse(Period.year(newest))

    def _kind_changed(self, _kind: str) -> None:
        """The picker's kind changed: recount and reload the period."""
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
        self.table.clear()
        self.heading.setText(period_words(period))
        self.status.setText(loading_sentence(period_words(period)))
        self._fetch_more()

    def _fetch_more(self) -> None:
        """Ask for the next page on a worker, tagged so a late page is dropped."""
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
        """UI thread: the page failed; said through `error` if still current."""
        if generation == self._generation:
            self._loading = False
            self.error.emit(error)

    def _page_ready(self, generation: int, page: Any) -> None:
        """UI thread: append the page's blocks and update the status line."""
        if generation != self._generation:
            return                                   # another month was picked meanwhile
        self._loading = False
        self._connected = page.connected or self._connected
        self._cursor, self._done = page.cursor, page.done
        blocks, self._last_day = blocks_for(page.items, self._last_day)
        self._items.extend(page.items)
        self.list.append_blocks(blocks)
        if self.stack.currentIndex() == 1:
            self.table.set_folds(self._items)
        shown = sum(1 + len(fold.older) for fold in self._items)
        self.status.setText(shown_sentence(shown, self._done) if shown
                            else empty_period_sentence(self._period))

    # -- right-click ------------------------------------------------------------

    def _show_menu(self, fold: Any, where: Any) -> None:
        """The right-click menu for one entry, with Show all for a folded group."""
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
        open_menu(menu, where)

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
        if self.stack.currentIndex() == 1:
            self.table.set_folds(self._items)
