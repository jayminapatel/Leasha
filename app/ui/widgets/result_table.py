r"""A results table that knows which object each row came from.

Layer: L5

**Written because the preview pane could not be attached to anything else.**
`attach_preview` needs two things from a results widget: a `selected` signal
carrying the row object, and `current_row()` so the pane can draw what is
already highlighted the moment it is switched on. `ResultsView` on the search
tab has both. Files, Mail and Code used a bare `QTableWidget`, which has neither
- it knows about cells and strings, and the row object with the path on it was
thrown away as soon as the text had been read out of it.

So the preview shipped on one tab out of four. The owner's instruction is
explicit and is a standing rule, not a request about this feature: *"preview
pane should be in every search type"*, and *"any feature added which helps the
other search areas has to be applied to others for consistency"*.

**It also absorbs the table boilerplate**, which was copied between views and
had already drifted - one hid its vertical header before setting the labels and
one after, one enabled sorting and one did not, for reasons that were real but
were nowhere written down. Both reasons now live here, next to the switch that
selects between them.

Filling stays in the views. What a Mail row looks like is not something this
should know, and the alternative was a widget taking a formatter, a sort-key
extractor and a tooltip callback - a configuration language for one table.
`set_row_objects` is the whole contract: fill the cells however you like, then
say which object each row came from.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QAbstractItemView, QTableWidget, QWidget

__all__ = [
    "ResultTable", "ROLE_ROW", "ROLE_RANK", "RELEVANCE", "ALIGNMENTS",
    "alignment_for", "redraw_with_details",
]

#: The alignment words a view declares its columns with.
#:
#: **Words, not `Qt.AlignmentFlag` bit arithmetic.** A view says what a column
#: *is* - text, or a number - and this widget knows what that looks like. The
#: alternative was every population loop repeating
#: `AlignRight | AlignVCenter`, which is how the header ended up aligned one
#: way and its column the other in the first place.
ALIGNMENTS: dict[str, Any] = {}


def alignment_for(word: Any) -> Any:
    """The Qt flag for `"left"`, `"right"` or `"centre"`. Left for anything else.

    Forgiving on purpose: this decides where text sits, and an unrecognised
    word should cost a column its alignment rather than fail to build a view.
    """
    if not ALIGNMENTS:                                   # first call: build it
        ALIGNMENTS.update({
            "left": Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            "right": Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            "centre": Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
            "center": Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
        })
    return ALIGNMENTS.get(str(word or "left").strip().lower(), ALIGNMENTS["left"])

#: Where the row object is kept. **On the item, not in a list beside the table.**
#: Mail's table is sortable, and after a header click visual row 3 is not
#: `rows[3]` - a positional list would preview a different message from the one
#: highlighted, which is the kind of bug nobody reports because they assume they
#: misclicked. Qt moves item data when it sorts; it does not move a list.
#:
#: **`+ 100`, and the number matters.** `UserRole` itself is taken - both tables
#: keep `file_id` there - and `UserRole + 1` is `sortable_item.SORT_ROLE`, which
#: is where `SortableItem.__lt__` looks for the value a column sorts on. Landing
#: on it meant Mail's "From" column sorted by comparing whole row *objects*:
#: reported as *"on the mail results when i clicked on top of a column to sort
#: it it crashed"*.
#:
#: Two roles that must not be equal, defined in two files, is exactly the sort
#: of thing that goes wrong silently - so `test_result_table.py` asserts they
#: differ, and `SortableItem` now ignores a sort value it cannot compare.
ROLE_ROW = Qt.ItemDataRole.UserRole + 100

#: Where a ranked table keeps the position the engine put a row in.
#:
#: A number apart from `ROLE_ROW` and from `SORT_ROLE`, for the reason the
#: note above gives at length: roles are integers chosen in different files,
#: and two that must differ are exactly the thing that goes wrong silently.
#: `test_result_table.py` asserts all three differ.
ROLE_RANK = Qt.ItemDataRole.UserRole + 101

#: What the third click on a header means, and what a ranked table starts in.
RELEVANCE = "relevance"

#: What the header says about the third click. **The affordance has to be
#: stated somewhere**: a cycle nobody knows about is a cycle nobody uses, and
#: this is the one interaction in the application that is not either obvious
#: or in a menu.
RELEVANCE_HINT = (
    "Click to sort. Click again to reverse. Click a third time to go back to "
    "best match first."
)


class ResultTable(QTableWidget):
    """A table whose rows remember the objects they were built from."""

    #: The row under the cursor changed. The preview pane listens; nothing else
    #: does, and this widget does not know the preview exists.
    selected = pyqtSignal(object)

    def __init__(self, headings: Sequence[str], *, sortable: bool = True,
                 alternating: bool = False, ranked: bool = False,
                 aligns: Optional[Sequence[str]] = None,
                 parent: Optional[QWidget] = None) -> None:
        #: Ranked tables carry one extra, hidden column holding the order the
        #: engine returned. **A column rather than a role on column zero**,
        #: because "put it back in relevance order" then costs `sortItems` -
        #: the machinery Qt already has - instead of a hand-written model
        #: reshuffle that would have to agree with Qt's about ties.
        self._rank_column = len(headings) if ranked else -1
        super().__init__(0, len(headings) + (1 if ranked else 0), parent)
        self.setHorizontalHeaderLabels(
            [*headings, ""] if ranked else list(headings))
        if ranked:
            self.setColumnHidden(self._rank_column, True)
        self.verticalHeader().hide()
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)

        #: `"left"` / `"right"` / `"centre"` per column, as the view declared
        #: them. Applied to the cells **and to the header sections**, which is
        #: the whole of §2a: a right-aligned number under a centred heading
        #: reads as a table somebody stopped caring about halfway through.
        self._aligns = tuple(aligns or ())
        #: The same, as Qt flags, worked out once: `setItem` asks for one per
        #: cell, and a two-thousand-row list is twenty thousand cells.
        self._align_flags = tuple(alignment_for(word) for word in self._aligns)
        self._apply_header_alignment()

        # **Sorting is now on by default, and that is a reversal.** It used to
        # be off for ranked lists because a header click discarded the match
        # ordering irrecoverably. That reasoning was right and is honoured
        # rather than dropped: the ranking is *kept*, in a hidden column, and
        # a third click on the same header puts it back. See `RELEVANCE_HINT`.
        self.setSortingEnabled(bool(sortable))
        self.setAlternatingRowColors(alternating)

        #: The sort the person chose, re-applied after every repopulation so a
        #: refresh does not silently throw it away. `None` means relevance for
        #: a ranked table, and insertion order for any other.
        self._sort: Optional[tuple[int, Qt.SortOrder]] = None
        #: How many times the current section has been clicked, for the cycle.
        self._clicks = 0
        self._clicked_section = -1

        header = self.horizontalHeader()
        if ranked:
            header.setToolTip(RELEVANCE_HINT)
        if sortable:
            header.sectionClicked.connect(self._on_section_clicked)

        self.currentCellChanged.connect(
            lambda row, _c, _pr, _pc: self.selected.emit(self.row_object(row)))

    # -- alignment (§2a) -----------------------------------------------------

    def _apply_header_alignment(self) -> None:
        """Point each heading the way its column reads.

        Per *section*, which needs a header item: `QHeaderView` has one
        `defaultAlignment` for the whole row of them, so a table mixing text
        and numbers cannot be corrected with it. Measured by looking rather
        than assumed - the default is centred, and every column in this
        application except two is left-aligned text.
        """
        for column in range(self.columnCount()):
            item = self.horizontalHeaderItem(column)
            if item is not None:
                item.setTextAlignment(self.alignment_of(column))

    def alignment_of(self, column: int) -> Any:
        """The Qt flag for a column, from the words the view declared."""
        if 0 <= column < len(self._align_flags):
            return self._align_flags[column]
        return alignment_for("left")

    def setItem(self, row: int, column: int, item: Any) -> None:   # noqa: N802 - Qt's naming
        """Qt's own hook, so **no population loop can forget the alignment**.

        The order's words: views declare alignment once instead of per-cell
        flags scattered through population loops. Doing it here rather than in
        a helper each view must remember to call is what makes that true - one
        of the three tables had already dropped its right-aligned flag on the
        floor while nobody noticed.

        A view that set its own alignment is left alone, so migrating one at a
        time never changes what is on screen.
        """
        if item is not None and self._aligns and not item.textAlignment():
            item.setTextAlignment(self.alignment_of(column))
        super().setItem(row, column, item)

    # -- sorting (§1b, §1d) --------------------------------------------------

    @property
    def ranked(self) -> bool:
        """Does this table have a relevance order to return to?"""
        return self._rank_column >= 0

    def _on_section_clicked(self, section: int) -> None:
        """Cycle: ascending, descending, then back to relevance.

        Qt has already done the sort by the time this runs - it is a header
        click - so the third one is an override rather than an interception,
        which is why the counter is kept here instead of the sort being
        blocked.
        """
        if section != self._clicked_section:
            self._clicked_section = section
            self._clicks = 1
        else:
            self._clicks += 1

        if self.ranked and self._clicks >= 3:
            self.sort_by_relevance()
            return
        self._sort = (section, self.horizontalHeader().sortIndicatorOrder())

    def sort_by_relevance(self) -> None:
        """Put a ranked table back in the order the engine returned.

        **The reconciliation §1b asks for.** `files_view` refused sorting
        because a header click discarded the ranking irrecoverably; the
        ranking is now recoverable, so the refusal is not needed.
        """
        if not self.ranked:
            return
        self._sort = None
        self._clicks = 0
        self._clicked_section = -1
        was = self.isSortingEnabled()
        self.setSortingEnabled(True)
        self.sortItems(self._rank_column, Qt.SortOrder.AscendingOrder)
        self.horizontalHeader().setSortIndicatorShown(False)
        self.setSortingEnabled(was)

    @property
    def sort_order(self) -> Any:
        """`(column, order)`, or `"relevance"` for a ranked table in its own
        order, or `None` for an unranked table nobody has sorted."""
        if self._sort is not None:
            return self._sort
        return RELEVANCE if self.ranked else None

    def setRowCount(self, rows: int) -> None:              # noqa: N802 - Qt's naming
        """Qt's own hook again, and the other half of §1d.

        Every population starts here, so this is where sorting goes off - not
        in each view, where one of four would eventually forget. Leaving it on
        makes Qt re-sort after **every** `setItem`, which is `O(n log n)` per
        cell and turns five hundred rows into a visible freeze; `mail_view`
        had worked that out and bracketed it by hand, and nothing carried the
        lesson to the other three.
        """
        self._filling = self.isSortingEnabled()
        if self._filling:
            self.setSortingEnabled(False)
        super().setRowCount(rows)

    # -- the contract the preview pane needs ---------------------------------

    def set_row_objects(self, rows: Sequence[Any]) -> None:
        """Say which object each table row was built from.

        Call it after filling: the data is attached in table order, and a sort
        that happened first would have moved the cells out from under it. This
        is also where sorting comes back on and **the person's sort is
        re-applied** - §1d - so a refresh mid-search does not quietly return
        the list to relevance under their pointer.
        """
        for index, row in enumerate(rows):
            item = self.item(index, 0)
            if item is not None:
                item.setData(ROLE_ROW, row)
            if self.ranked:
                self._stamp_rank(index)

        if getattr(self, "_filling", False):
            self._filling = False
            self._sorting_back_on()

        # A fresh result set with a row still highlighted from the last one
        # would otherwise preview whatever is now at that index. Qt keeps the
        # current index across a `setRowCount`, so this is not hypothetical.
        self.selected.emit(self.row_object(self.currentRow()))

    def _stamp_rank(self, index: int) -> None:
        """Record where the engine put this row, in the hidden column."""
        from app.ui.widgets.sortable_item import SORT_ROLE, SortableItem

        item = SortableItem("")
        item.setData(SORT_ROLE, int(index))
        item.setData(Qt.ItemDataRole.DisplayRole, "")
        super().setItem(index, self._rank_column, item)

    def _sorting_back_on(self) -> None:
        r"""Sorting on again after a fill, in the order that was on screen -
        **for one sort, or none.**

        Measured 2026-09-30 on a 2,000-row Code list, offscreen: attaching the
        rows and switching sorting back on was 290-365 ms of a draw of about a
        second, nearly all of it Qt calling `SortableItem.__lt__` in Python.
        It sorted twice. `setSortingEnabled(True)` sorts at once by whatever
        the header's indicator points at - Qt's own behaviour, counted in
        `test_table_sorting.py` - and then `sortItems` sorted again, by the
        column somebody chose or by the hidden rank column. For a ranked list
        this method now takes about 3 ms.

        * **A sort somebody chose**: the indicator is pointed at it first (no
          sort happens while sorting is off), so switching sorting on *is* the
          sort, and the second one has gone.
        * **A ranked list in its own order**: the rows went in in the engine's
          order and `_stamp_rank` numbered them as they stand, so they are
          already in relevance order. The indicator is pointed at no column,
          which gives Qt nothing to compare, and no sort happens at all.
        * **An unranked list nobody has sorted** (Mail): the same, and for it
          this was a fault rather than a cost. Qt's header starts out pointing
          at column 0, descending, so switching sorting on put the Mail list
          in order of sender, Z to A, with an arrow on "From" - when the list
          had been asked for newest first, and `sort_order` said nobody had
          sorted it. It now stays in the order it was filled in, with no arrow
          until somebody clicks a heading.
        """
        header = self.horizontalHeader()
        if self._sort is not None:
            column, order = self._sort
            header.setSortIndicator(column, order)
            self.setSortingEnabled(True)
        else:
            header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
            self.setSortingEnabled(True)
            header.setSortIndicatorShown(False)

    def row_object(self, row: int) -> Any:
        """The object behind a row index, or None.

        Never raises: this is on the selection path, and an index left over from
        a longer result set must cost a preview, never the window.
        """
        item = self.item(row, 0) if row >= 0 else None
        return item.data(ROLE_ROW) if item is not None else None

    def current_row(self) -> Any:
        """The highlighted row's object, or None.

        Exists so the pane can draw what is *already* selected when it is
        switched on. Without it the pane opens empty beside a highlighted row,
        which reads as a broken preview rather than as nothing having been
        selected since it appeared.
        """
        return self.row_object(self.currentRow())


def align_headers(view: Any, aligns: Optional[Sequence[str]] = None) -> None:
    r"""Point a plain table's or tree's headings the way their columns read.

    §2b. **Qt centres header text by default and left-aligns cell text**, so
    every table in this application that was not built by `ResultTable` had a
    centred heading over a left-aligned column - which is the owner's report,
    exactly: *headers are centre-aligned while their columns are not*.

    `aligns` is the same `"left"`/`"right"`/`"centre"` vocabulary the widget
    uses; omitted, every column is left, which is what all of these are.

    Works on `QTableWidget` and `QTreeWidget` alike, because the two share no
    base for this and writing it twice is how they would drift. **Never
    raises**: this is where text sits.
    """
    try:
        count = (view.columnCount() if hasattr(view, "columnCount")
                 else view.headerItem().columnCount())
        for column in range(count):
            flag = alignment_for(aligns[column] if aligns
                                 and column < len(aligns) else "left")
            item = (view.horizontalHeaderItem(column)
                    if hasattr(view, "horizontalHeaderItem") else view.headerItem())
            if item is None:
                continue
            if hasattr(view, "horizontalHeaderItem"):
                item.setTextAlignment(flag)
            else:
                item.setTextAlignment(column, flag)
    except Exception:                            # noqa: BLE001 - see docstring
        return


def offer_filters(view: Any, notices: Any, generation: int, response: Any = None) -> None:
    """Add the filters the typed sentence contains to the notice bar, as offers.

    The read is a store query, so it runs on a worker (`filter_offers_async`)
    and lands here later; it is dropped if another search has landed since.
    Here, beside `redraw_with_details`, because it is the same kind of thing -
    a second paint of a page that was drawn a moment ago - and `search_view.py`
    is at its line guard.

    **The filters the search already applied are drawn first, as chips**
    (`response.applied`, see `presenter.auto_filters`), and are not offered
    again: "only 2017?" beside a page already limited to 2017 is noise.
    """
    from app.ui.workers import filter_offers_async

    applied = tuple(getattr(response, "applied", ()) or ())
    chips = getattr(view, "chips", None)
    if chips is not None and hasattr(chips, "show_applied"):
        chips.show_applied(applied)

    def landed(offers: Any) -> None:
        if offers and generation == view._shown_generation:
            view.notices.show_notices([*notices, *offers])

    filter_offers_async(getattr(view._engine, "store", None), view.input.text(),
                        view._search_preferences, landed, applied)


def redraw_with_details(results: Any, response: Any, terms: Any, summary: str,
                        extra: Any) -> None:
    """Draw the same results again, now carrying their mail subtitles and
    missing-file marks.

    **`keep_scroll`, because this is not a new search.** The rows were painted
    a moment ago from what was already known, and the metadata arrives from a
    worker afterwards - so somebody who has started reading must not be moved
    back to the top by the second paint.

    Here rather than in `search_view.py` because it is painting, and that view
    is at the 250-line guard: the rule that keeps views short is the rule that
    keeps decisions out of them.
    """
    found = extra if isinstance(extra, dict) else {}
    results.show_results(
        response.results, terms, summary=summary, keep_scroll=True,
        details=found.get("details", {}), missing=found.get("missing", set()),
        volumes=found.get("volumes", {}),
        placeholders=found.get("placeholders", set()),
        statuses=found.get("statuses", {}))
