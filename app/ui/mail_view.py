"""Mail, as a table you can scan.

Layer: L5, driving L1

**Why mail is a tab and not a filter.** The search box answers "which document
says this", and it answers it by relevance. That is the wrong question and the
wrong order for mail. Somebody looking through a mailbox is scanning columns -
who sent it, who it went to, when, what it was called - and wants them newest
first. Ranking a mailbox by BM25 puts an eight-year-old thread above this
morning's, which no amount of good ranking makes useful.

So this reads `messages` directly and never touches a chunk of text. It is a
browser, not a search: fast enough to filter on every keystroke because every
filter is an indexed column, and honest about the fact that it cannot find a
message by what it *says*. That is still the search tab's job, and the view says
so rather than returning an empty table.

**The same `/` commands as everywhere else.** `/from`, `/to`, `/subject`,
`/has`, `/after`, `/before` all parse through `app/search/query.py`, so a filter
learned in the search box works here with the same spelling. Nothing new was
invented for this tab, which is the point.

Thin, like every view: the formatting is in `presenter.py`, the query is in
`sqlite_store.py`, and the menu is in `widgets/file_menu.py`.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import Qt, QThreadPool, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.logging import logger
from app.search.commands import expand_slashes
from app.search.query import parse_query
from app.ui.presenter import MAIL_COMMANDS, mail_filters, mail_rows
from app.ui.preview_loader import stored_text
from app.ui.view_options import (
    apply_to_table, available_columns, button as view_button,
)
from app.ui.widgets.command_popup import attach_to
from app.ui.widgets.file_menu import FileActions, show_for, viewport_point
from app.ui.widgets.preview import attach_preview
from app.ui.widgets.result_table import ResultTable
from app.ui.widgets.sortable_item import SORT_ROLE, SortableItem
from app.ui.workers import CallableWorker, run, stop_timers

__all__ = ["MailView", "MAIL_DEBOUNCE_MS", "COLUMNS", "PREFS_KEY"]

#: Namespace for this list's view preferences in `index_state`.
PREFS_KEY = "ui:mail"

_log = logger.bind(component="ui.mail")

#: No model to load and no vectors to probe - one indexed lookup - so this only
#: needs to be long enough to avoid a query per keystroke on a fast typist.
MAIL_DEBOUNCE_MS = 120

#: (heading, attribute on MailRow, right-aligned?). The order people scan in:
#: who, to whom, when, what, whether anything came with it, how big.
#: (key, heading, attribute, right-aligned?)
COLUMNS: tuple[tuple[str, str, str, bool], ...] = (
    ("from", "From", "sender", False),
    ("to", "To", "recipients", False),
    ("date", "Date", "sent", False),
    ("subject", "Subject", "subject", False),
    ("attach", "Attach", "attachment", False),
    ("size", "Size", "size", True),
)

#: Offered whatever the rows say. A mail list with no sender and no subject is
#: not a mail list, and a mailbox filtered down to one blank-subject message
#: should not take the column away from the next filter.
ALWAYS_OFFERED = ("from", "subject", "date")

#: A mailbox can hold two hundred thousand messages. The table is populated
#: row by row on the UI thread, so this is the number that decides whether
#: typing stays smooth - not the query, which is indexed and fast either way.
PAGE_SIZE = 500


class MailView(QWidget):
    """A filterable, sortable table of every indexed message."""

    error = pyqtSignal(object)
    #: Search the *contents* of one message. The bridge to the search tab, for
    #: the question this tab deliberately cannot answer.
    search_inside_requested = pyqtSignal(str)
    opened = pyqtSignal(int)                 # file_id

    def __init__(self, store: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._generation = 0
        self._rows: list[Any] = []
        self._available: tuple[str, ...] = tuple(key for key, *_ in COLUMNS)

        self.input = QLineEdit()
        self.input.setPlaceholderText(
            "Filter with / commands — /from dave  /to priya  /subject invoice  "
            "/has attachment  /after 2024-01-01"
        )
        self.input.setClearButtonEnabled(True)
        self.input.textChanged.connect(lambda _t: self._timer.start())
        # Only what this tab honours - see `command_popup.MAIL_COMMANDS`.
        self._popup = attach_to(self.input, only=MAIL_COMMANDS)

        self.summary = QLabel("")
        self.summary.setObjectName("resultsSummary")

        # Sortable, unlike the search results: these rows have no rank to
        # destroy, and "biggest attachment" and "oldest thread" are real
        # questions that a click on a header answers for free.
        self.results = ResultTable([h for _k, h, _a, _r in COLUMNS],
                                   sortable=True, alternating=True)
        header = self.results.horizontalHeader()
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)   # Subject
        header.setSectionsMovable(True)

        self.results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.results.customContextMenuRequested.connect(self._on_context_menu)
        self.results.itemDoubleClicked.connect(lambda _item: self._open_selected())
        # Right-click the header for the column, density and text-size menu.
        # On the header rather than in Settings: it is a preference about this
        # table, and the place people look for it is the table.
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(
            lambda point: self.view_button.show_menu(header.mapToGlobal(point)))

        self.view_button = view_button(
            self, store, PREFS_KEY,
            columns=[(key, heading) for key, heading, _a, _r in COLUMNS],
            on_change=self._prefs_changed,
        )

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(MAIL_DEBOUNCE_MS)
        self._timer.timeout.connect(self._run)

        # Off until asked for - `Ctrl+P` or the View menu. The body comes from
        # `_message_body` rather than from the file, because a PST is a hundred
        # thousand messages in one file and there is nothing on disk to open
        # for any one of them.
        self.preview, self.split = attach_preview(
            self.results, lambda _row: self._open_selected(), self.error.emit)
        self.preview.body_provider = lambda row: stored_text(store, row.file_id)

        top = QHBoxLayout()
        top.addWidget(self.input, stretch=1)
        top.addWidget(self.view_button)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.summary)
        layout.addWidget(self.split, 1)
        self._apply_prefs()

    def shutdown(self) -> None:
        """Stop the debounce timers - see `workers.stop_timers`."""
        stop_timers(self)
        self.preview.shutdown()

    def focus(self) -> None:
        self.input.setFocus()
        self.input.selectAll()

    def refresh(self) -> None:
        """Re-run the current filter. Called when an index run finishes."""
        self._run()

    # -- querying -------------------------------------------------------------

    def _run(self) -> None:
        text = expand_slashes(self.input.text().strip())
        parsed = parse_query(text)
        filters = mail_filters(parsed)

        # Free text cannot be honoured here - this reads `messages` and never
        # touches chunk text. Saying so beats returning an empty table for a
        # query that looks perfectly reasonable.
        leftover = (parsed.text or "").strip()

        self._generation += 1
        generation = self._generation

        worker = CallableWorker(
            self._store.browse_messages, limit=PAGE_SIZE, component="ui.mail", **filters
        )
        worker.signals.finished.connect(
            lambda rows, g=generation: self._show(rows, g, leftover)
        )
        worker.signals.failed.connect(self.error.emit)
        run(QThreadPool.globalInstance(), worker)

    def _show(self, rows: Any, generation: int, leftover: str) -> None:
        if generation != self._generation:
            return                          # newer typing has overtaken this

        display = mail_rows(rows)
        self._rows = display

        # Off while filling, on afterwards. Qt re-sorts after every `setItem`
        # otherwise, which is O(n log n) per cell and turns five hundred rows
        # into a visible freeze.
        self.results.setSortingEnabled(False)
        self.results.setRowCount(len(display))
        for index, row in enumerate(display):
            for column, (_key, _heading, attribute, right) in enumerate(COLUMNS):
                item = SortableItem(getattr(row, attribute))
                if right:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                # Sort on the real value, not the formatted string: "3 KB" and
                # "10 KB" sort the wrong way as text, and a date column sorted
                # alphabetically is worse than one that does not sort at all.
                if attribute == "sent":
                    item.setData(SORT_ROLE, row.sent_at)
                elif attribute == "size":
                    item.setData(SORT_ROLE, row.size_bytes)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row.file_id)
                    item.setToolTip(row.path)
                self.results.setItem(index, column, item)
        # Before sorting is re-enabled: the objects are attached in table order
        # and a sort would already have moved the cells out from under them.
        self.results.set_row_objects(display)
        self.results.setSortingEnabled(True)

        # Recomputed from the rows on screen, so a column is offered when the
        # data can fill it and disabled when it cannot - see `view_options`.
        self.view_button.available = self._available = available_columns(
            display, [(key, attribute) for key, _h, attribute, _r in COLUMNS],
            always=ALWAYS_OFFERED,
        )
        self._apply_prefs()

        self.summary.setText(self._summary_text(len(display), leftover))

    def _summary_text(self, shown: int, leftover: str) -> str:
        if not shown:
            # No count here: `COUNT(*)` over `messages` is instant on a test
            # corpus and is not on two hundred thousand of them, and this runs
            # inside the handler that paints results. The wording covers both
            # cases rather than paying a query to tell them apart.
            return ("No message matches those filters. If no mail is indexed "
                    "yet, add a .pst in Settings and run an index.")

        parts = [f"{shown:,} message{'s' if shown != 1 else ''}"]
        if shown >= PAGE_SIZE:
            parts.append(f"showing the newest {PAGE_SIZE:,} — narrow the filters to see more")
        if leftover:
            parts.append(
                f"'{leftover}' was ignored — this tab filters on the header fields only. "
                "Use Search to look inside messages."
            )
        return "  ·  ".join(parts)

    # -- how it looks ----------------------------------------------------------

    def _apply_prefs(self) -> None:
        apply_to_table(
            self.results, self.view_button.prefs,
            columns=[(key, heading) for key, heading, _a, _r in COLUMNS],
            available=self._available,
        )
        self.preview.apply_preference(
            self.view_button.prefs, self.results.current_row())

    def _prefs_changed(self, _prefs: Any) -> None:
        """The button owns the preferences and has already saved them."""
        self._apply_prefs()

    def selected_row(self) -> Optional[Any]:
        """The MailRow under the selection, by file_id.

        By id rather than by position, because the table is sortable: after a
        click on a header, visual row 3 is not `self._rows[3]`, and acting on
        the wrong message is the kind of bug nobody reports because they assume
        they misclicked.
        """
        items = self.results.selectedItems()
        if not items:
            return None
        file_id = self.results.item(items[0].row(), 0)
        if file_id is None:
            return None
        wanted = file_id.data(Qt.ItemDataRole.UserRole)
        return next((row for row in self._rows if row.file_id == wanted), None)

    def keyPressEvent(self, event: Any) -> None:           # noqa: N802 - Qt's naming
        """Enter opens the selected message.

        **Mail had no keyboard route at all**: double-click was the only way in,
        which in a project whose specification requires keyboard-only operation
        end to end is a plain failure rather than a rough edge. Both Return and
        Enter, because the numeric keypad sends the other one and somebody using
        the keypad to move through a list is exactly the person affected.
        """
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            row = self.selected_row()
            if row is not None:
                self._open_selected()
                event.accept()
                return
        super().keyPressEvent(event)

    def _open_selected(self) -> None:
        """Show what is inside the selected message.

        **Not "open the file", because there is no file.** A message's path is
        synthetic - `pst://archive.pst/E12` - so handing it to Explorer opens
        nothing and reports that nothing is there. Searching inside it is the
        only way to read a message in this application, so that is what opening
        one means.

        `opened` is emitted as well, for anything that wants the id rather than
        the content.
        """
        row = self.selected_row()
        if row is None:
            return
        self.opened.emit(row.file_id)
        if row.path:
            self.search_inside_requested.emit(row.path)

    def _on_context_menu(self, point: Any) -> None:
        """The same menu as the other two lists - see widgets/file_menu.py."""
        # Viewport coordinates: the signal gives a point relative to the table,
        # `rowAt` wants one relative to the viewport, and the header sits
        # between them. Getting this wrong is why right-click looked broken.
        index = self.results.rowAt(viewport_point(self.results, point).y())
        if index >= 0:
            self.results.selectRow(index)
        row = self.selected_row()
        if row is None:
            return

        # No "Open" or "Show in folder": a message lives inside a .pst and has
        # no file on disk to open. Offering either would be offering something
        # that fails, which is worse than not offering it.
        show_for(self.results, point, row.path, FileActions(
            search_inside=lambda: self.search_inside_requested.emit(row.path),
            copy=[("Copy subject", row.subject), ("Copy sender", row.sender)],
        ))
