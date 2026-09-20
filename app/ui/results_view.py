r"""The results list.

Layer: L5

**One row per document, not one per matching chunk** (`presenter.group_results`
does the grouping; this shows it). **Rows are painted, not built**: a
`QListView` over a plain model with `ResultDelegate` painting only what is on
screen, so the cost stops scaling with the result count - the item construction
and the right-click menu are in `widgets/results_items.py`.

A row reads the way a browser result reads: **name first**, location small and
grey underneath, date to the right. `explain` and the score live in the tooltip
and the right-click menu, and return inline for anyone who turns them on. **A
result whose file has vanished is marked, not hidden**: a stale index entry is a
genuine finding, and dropping it silently would make the count disagree with the
list for reasons nobody could see.
"""

from __future__ import annotations

from typing import Any, Optional, Sequence

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtWidgets import QAbstractItemView, QLabel, QListView, QVBoxLayout, QWidget

from app.ui.presenter import (
    KIND_LABELS, ResultGroup, ResultRow, group_results, results_terminator,
    row_identity, to_rows,
)
from app.ui.result_delegate import ROLE_PAYLOAD, ResultDelegate
from app.ui.view_options import ViewPreferences, apply_font
from app.ui.widgets.file_menu import show_for
from app.ui.widgets.result_drag_model import DraggableResultsModel
from app.ui.widgets.results_items import result_item, show_result_menu, terminator_item
from app.ui.widgets.skeleton import disarm as disarm_skeleton

__all__ = ["ResultsView", "KIND_LABELS"]


class ResultsView(QWidget):
    """A painted list of search results, grouped by document."""

    opened = pyqtSignal(object)          # ResultRow
    reveal_requested = pyqtSignal(object)
    reindex_requested = pyqtSignal(object)
    #: The row under the cursor changed. The preview pane listens; nothing else
    #: does, and nothing here knows the preview exists.
    selected = pyqtSignal(object)
    #: "Pin" chosen from the right-click menu - workspace §3c.
    pin_requested = pyqtSignal(object)
    #: "More like this" chosen from the right-click menu - work order 0h §2d.
    similar_requested = pyqtSignal(object)
    #: "See everything from this month" chosen from the right-click menu -
    #: order 0n section 4b. The shell opens the Life Timeline at the month.
    period_requested = pyqtSignal(object)
    #: The rows on screen changed - a new search or a federated append. The
    #: timeline strip listens; nothing here knows it exists either. The
    #: thumbnail grid (work order 0h §3a) listens too, and filters it down to
    #: the photos on its own.
    rows_changed = pyqtSignal(list)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._rows: list[ResultRow] = []
        self._details: dict[int, Any] = {}
        self._prefs = ViewPreferences()
        #: file_ids currently showing their chunks. Deliberately not persisted:
        #: it is about the query on screen, and restoring it against a different
        #: result set would expand arbitrary rows.
        self._expanded: set[int] = set()
        #: Paths that no longer exist, decided on a worker - never statted here.
        self._missing: set[str] = set()
        #: file_id -> {"name", "scanned"} for a row on an Offline Media volume
        #: that is not connected right now - §3a, decided on the same worker
        #: as `_missing`. Absent entirely for an online volume or an ordinary
        #: file; see `presenter.offline_volume_marks`.
        self._volumes: dict[int, Any] = {}
        #: Paths that are cloud placeholders right now - 202626270514 §3d,
        #: decided on the same worker as `_missing`/`_volumes`. See
        #: `presenter.placeholder_marks`.
        self._placeholders: set = set()
        #: "plain" or "technical" - item 4b's date register, from the tab.
        self._register = "plain"
        #: Adoptions section 1: `() -> (typed terms, search preferences)`,
        #: set by the search tab - read at click time, so the menu always
        #: reflects the switch as it is now, never as it was when built.
        self.explain_context: Any = None

        self._summary = QLabel("")
        self._summary.setObjectName("resultsSummary")
        #: UI Redesign (202626160950 §3f): the label the toolbar's summary line
        #: is folded against. When both would say the same thing, this one
        #: hides; when this carries different information it stays.
        self._fold_with: Optional[QLabel] = None

        # §3b: the same model, wherever it drags to - see `result_drag_model`.
        self._model = DraggableResultsModel(self, missing=lambda: self._missing)
        self._delegate = ResultDelegate(self)

        self._list = QListView()
        self._list.setObjectName("resultsList")       # §1b: borderless rows
        self._list.setModel(self._model)
        self._list.setItemDelegate(self._delegate)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.setDragEnabled(True)          # §3b; see `set_drag_enabled`
        self._list.setUniformItemSizes(False)
        # **Rows follow the list's width, both ways.** `QListView` lays out once
        # (`ResizeMode.Fixed`) and keeps that width, so opening the preview and
        # inspector - which narrows the list from 269 to 207 - left every row
        # 62px wider than its viewport: the date, the path and the snippet were
        # clipped at the edge and a horizontal scrollbar appeared. Measured on
        # the real window on 2026-09-20. Text is elided, so nothing ever needs
        # to scroll sideways.
        self._list.setResizeMode(QListView.ResizeMode.Adjust)
        self._list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self._list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._list.setMouseTracking(True)          # item 5a: hover needs it
        self._list.activated.connect(self._on_activated)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        self._list.viewport().installEventFilter(self)  # item 2a: chevron click
        self._list.selectionModel().currentChanged.connect(
            lambda current, _prev: self.selected.emit(self._row_for(
                current.data(ROLE_PAYLOAD) if current.isValid() else None)))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._summary)
        layout.addWidget(self._list, stretch=1)

    def current_row(self) -> Any:
        """The selected row, or None.

        Exists so the preview pane can draw what is *already* selected the
        moment it is switched on. Without it the pane opens empty beside a
        highlighted row, which reads as a broken preview rather than as nothing
        having been selected since.
        """
        index = self._list.currentIndex()
        if not index.isValid():
            return None
        return self._row_for(index.data(ROLE_PAYLOAD))

    def set_view_preferences(self, prefs: Any) -> None:
        """Text size, spacing, grouping and whether scores show inline."""
        self._prefs = prefs if isinstance(prefs, ViewPreferences) else ViewPreferences()
        self._delegate.prefs = self._prefs
        # The delegate paints from `option.font`, i.e. the list's own font, and
        # nothing else set it: the text-size preference reached only the
        # tables. Same stylesheet route as there (see `apply_font`).
        apply_font(self._list, self._prefs.font_pt)
        if self._rows:
            self._rebuild()

    def set_drag_enabled(self, enabled: bool) -> None:
        """§6's off switch for §3b. On by default."""
        self._list.setDragEnabled(bool(enabled))

    # -- populating ---------------------------------------------------------

    def show_results(self, results: Sequence[Any], terms: Sequence[str], summary: str = "",
                     details: Optional[dict[int, Any]] = None, missing: Optional[set[str]] = None,
                     volumes: Optional[dict[int, Any]] = None,
                     placeholders: Optional[set] = None,
                     keep_scroll: bool = False, register: Optional[str] = None) -> None:
        """`details` maps file_id to mail metadata - see `store.messages_for`.

        `keep_scroll` is False here and True in `_rebuild`, and the difference
        is the whole point. **A new search starts at the top**; carrying the old
        position over lands somebody mid-list with the best hits scrolled off
        the screen, looking like the search returned something worse than it
        did. Toggling a preference or expanding a row is the opposite case -
        those must not jump - which is why the position is kept there.

        The redraw that adds mail subtitles and missing-file marks arrives a
        moment after the rows and passes `keep_scroll=True`: it is the same
        results, so it is not a new search and must not move anybody.
        """
        disarm_skeleton(self)                     # §6d

        # `missing` is the set of paths that no longer exist, computed **on the
        # worker** by `presenter.missing_paths`. It used to be a `Path.exists()`
        # per row here, on the UI thread - twenty stats for a normal page, five
        # hundred for a full one, each of which can block for seconds on a
        # network share. In the virtualisation work, of all places.
        self._rows = to_rows(results, terms)
        self._details = dict(details or {})
        self._missing = set(missing or ())
        if volumes is not None:
            # **Only when told**, the same rule `_register` already follows
            # just below: `redraw_with_details`'s follow-up paint of the same
            # results always carries this, but nothing else does, and a
            # missing argument must not be read as "nothing is offline".
            self._volumes = dict(volumes)
            # §3a's remaining half: the delegate paints the same note inline,
            # on the group's subtitle line - see `ResultDelegate.volumes`.
            # Kept in step here rather than read by the delegate from this
            # view directly, the same split `prefs` already draws.
            self._delegate.volumes = self._volumes
        if placeholders is not None:
            # Same rule as `volumes` just above - only when told, and kept
            # in step with the delegate the identical way.
            self._placeholders = set(placeholders)
            self._delegate.placeholders = self._placeholders
        if register is not None:
            # **Only when told.** `redraw_with_details`'s follow-up paint of
            # the same results omits this - item 4b's register must not reset
            # to "plain" on every metadata redraw that happens to follow.
            self._register = str(register)
        self._expanded.clear()
        self._summary.setText(summary)
        self._fold_summary(summary)
        self._rebuild(keep_scroll=keep_scroll)

    def append_results(self, results: Sequence[Any], terms: Sequence[str]) -> int:
        r"""Add rows beneath what is already shown. Returns the new total.

        **For a second source answering later**, which today means a repository
        history search: it takes seconds where the index takes milliseconds, so
        its rows arrive after somebody has started reading. Appending is the
        only honest way to show that - replacing would blank a list mid-read,
        and waiting for both would make every history search look like a hang.

        Here rather than in the view, because this widget already owns "what is
        on screen": keeping a second copy of the rows in `search_view.py` so it
        could concatenate them would be two answers to one question, and it is
        the question this class exists to answer.

        `keep_scroll` is not offered. Rows arriving below the fold must never
        move somebody who is reading the ones above it.
        """
        if not results:
            return len(self._rows)
        self._rows = list(self._rows) + to_rows(results, terms)
        self._rebuild(keep_scroll=True)
        return len(self._rows)

    def _rebuild(self, *, keep_scroll: bool = True) -> None:
        """Refill the model. Rows are data now, so this is cheap.

        Keeps the scroll position by default: toggling a preference or expanding
        a row otherwise jumps the list to the top, losing the place of somebody
        who had scrolled to the eighth result to read it. A new search passes
        False - see `show_results`.
        """
        bar = self._list.verticalScrollBar()
        position = bar.value() if bar is not None and keep_scroll else 0
        # Item 5d: *what* was current, not *where* - a rebuild that adds or
        # re-ranks rows changes every screen position, never the payload.
        #
        # **Kept whether or not the scroll position is.** It was taken only when
        # `keep_scroll` was True, so a second paint for a *different* query text
        # - the interim tier of "barn" replaced by the full tier of "barnsley",
        # which is what a slow typist or a busy machine produces - silently
        # dropped the selection the person had just made. The pane then still
        # showed that row, but `current_row()` was None, so opening the preview
        # a moment later said "Nothing selected" beside a list they had clicked.
        # A row that is still in the new results stays selected; scrolling to the
        # top for a new search is a separate decision and is unchanged.
        anchor = row_identity(self._list.currentIndex().data(ROLE_PAYLOAD))
        if anchor == ("chunk", None):                # nothing was current
            anchor = None

        self._model.clear()
        if self._prefs.group_by_document:
            for group in group_results(self._rows, details=self._details, register=self._register):
                expanded = group.file_id in self._expanded
                self._append(group, expanded=expanded, anchor=anchor)
                if expanded and group.match_count > 1:
                    for row in group.rows:
                        self._append(row, anchor=anchor)
        else:
            for row in self._rows:
                self._append(row, anchor=anchor)
        # Item 5c: a quiet, unselectable row of its own, so reaching it by
        # scrolling is what answers "are there more" - see `Terminator`.
        if self._rows:
            self._append_terminator(results_terminator(len(self._rows)))

        if bar is not None:
            bar.setValue(min(position, bar.maximum()))
        self.rows_changed.emit(self._rows)          # the timeline strip listens

    def _append(self, payload: Any, *, expanded: bool = False, anchor: Any = None) -> None:
        self._model.appendRow(result_item(
            payload, expanded=expanded, missing=self._missing,
            volumes=self._volumes, placeholders=self._placeholders))
        if anchor is not None and row_identity(payload) == anchor:      # item 5d
            self._list.setCurrentIndex(self._model.index(self._model.rowCount() - 1, 0))

    def _append_terminator(self, text: str) -> None:
        if text:
            self._model.appendRow(terminator_item(text))

    def clear(self, message: str = "") -> None:
        disarm_skeleton(self)                     # §6d
        self._rows = []
        self._expanded.clear()
        self._model.clear()
        self._summary.setText(message)
        self._fold_summary(message)

    def fold_summary_with(self, label: Any) -> None:
        """§3f: hide the summary when `label` already says it."""
        self._fold_with = label

    def _fold_summary(self, text: str) -> None:
        other = self._fold_with.text() if self._fold_with is not None else None
        self._summary.setVisible(bool(text) and text != other)

    # Item 6a: the search box's ↓/↑ forwards here - QListView's own key
    # handling already moves the selection, scrolls it into view, and skips
    # a disabled row (the terminator) correctly, which is why this delegates
    # to it rather than re-deriving the same arithmetic.
    def forward_key(self, event: Any) -> None:
        self._list.keyPressEvent(event)

    def open_current(self, *, reveal: bool = False) -> bool:
        row = self.current_row()          # Enter/Ctrl+Enter - item 6a
        if row is None:
            return False
        (self.reveal_requested if reveal else self.opened).emit(row)
        return True

    def selected_rows(self) -> list[ResultRow]:
        rows = (self._row_for(i.data(ROLE_PAYLOAD)) for i in self._list.selectedIndexes())
        return [row for row in rows if row is not None]

    # -- interaction --------------------------------------------------------

    def _row_for(self, payload: Any) -> Optional[ResultRow]:
        """A group stands in for its own best chunk when acted on."""
        if isinstance(payload, ResultRow):
            return payload
        return getattr(payload, "best", None)

    def _on_activated(self, index: Any) -> None:
        payload = index.data(ROLE_PAYLOAD)
        # Enter or double-click on a multi-match group expands it rather than
        # opening: the matches are the reason the row says "3 matches", and
        # opening the first silently discards the other two.
        if isinstance(payload, ResultGroup) and payload.match_count > 1:
            self._toggle(payload.file_id)
            return
        row = self._row_for(payload)
        if row is not None:
            self.opened.emit(row)

    def _toggle(self, file_id: int) -> None:
        self._expanded.symmetric_difference_update({file_id})
        self._rebuild()

    def eventFilter(self, obj: Any, event: Any) -> bool:      # noqa: N802 - Qt's naming
        """Item 2a: a click on the chevron toggles too, via `ResultDelegate.chevron_hit`."""
        click = obj is self._list.viewport() and event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton
        file_id = self._delegate.chevron_hit(self._list, event.pos()) if click else None
        if file_id is None:
            return super().eventFilter(obj, event)
        self._toggle(file_id)
        return True

    def _on_context_menu(self, point: Any) -> None:
        # `show_for` is passed in, not imported over there, so it stays
        # replaceable here - the menu is modal and a test cannot click it.
        show_result_menu(self, point, show_for)

    def image_rows(self) -> list[ResultRow]:
        """The photo rows currently on screen, in list order.

        Work order 0h §3b: the lightbox's sibling list when a pop-out is
        opened for a photo found through the list rather than the grid - the
        two surfaces show the same result set, so arrow-key navigation
        should walk the same photos whichever one somebody opened it from.
        """
        from app.ui.thumbnail_loader import is_image_result

        return [row for row in self._rows if is_image_result(getattr(row, "ext", ""))]
