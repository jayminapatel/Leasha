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
from app.ui.presenter import (
    MAIL_COMMANDS, mail_summary, with_date_problems,
)
from app.ui.presenter.mail import THREAD_COLUMN, mail_list
from app.ui.presenter.rows import understood_line
from app.ui.tasks import browse_messages_typed
from app.ui.view_options import (
    apply_to_table, available_columns, button as view_button, weak_slot,
)
from app.ui.widgets.chips import list_chips, show_page
from app.ui.widgets.command_popup import attach_to
from app.ui.widgets.file_menu import FileActions, show_for, viewport_point
from app.ui.widgets.mail_card import attach_mail
from app.ui.widgets.preview import attach_preview
from app.ui.widgets.result_table import ResultTable
from app.ui.widgets.status_column import STATUS_COLUMN, fill_rows
from app.ui.workers import CallableWorker, open_row_async, run, stop_timers

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
    THREAD_COLUMN,              # 0z F2: how many messages a folded row stands for
    ("attach", "Attach", "attachment", False),
    ("size", "Size", "size", True),
    STATUS_COLUMN,              # 2026-09-29: the one-word Status - see file_state
)

#: Sort on the real value, not the formatted string: "3 KB" and "10 KB" sort
#: the wrong way as text, and a date column sorted alphabetically is worse
#: than one that does not sort at all. Key -> attribute on `MailRow`.
SORT_KEYS: dict[str, str] = {"date": "sent_at", "size": "size_bytes", "messages": "thread_count"}

#: Offered whatever the rows say. A mail list with no sender and no subject is
#: not a mail list, and a mailbox filtered down to one blank-subject message
#: should not take the column away from the next filter.
ALWAYS_OFFERED = ("from", "subject", "date")

#: A mailbox can hold two hundred thousand messages. The table is populated
#: row by row on the UI thread, so this is the number that decides whether
#: typing stays smooth - not the query, which is indexed and fast either way.
PAGE_SIZE = 500


def _first_cell(item: Any, row: Any) -> None:
    """The message's id, and its synthetic path as the tooltip."""
    item.setData(Qt.ItemDataRole.UserRole, row.file_id)
    item.setToolTip(row.path)


class MailView(QWidget):
    """A filterable, sortable table of every indexed message."""

    error = pyqtSignal(object)
    #: Search the *contents* of one message. The bridge to the search tab, for
    #: the question this tab deliberately cannot answer.
    search_inside_requested = pyqtSignal(str)
    period_requested = pyqtSignal(int)       # file_id - "see everything from this month"
    opened = pyqtSignal(int)                 # file_id

    def __init__(self, store: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._generation = 0
        self._folded = False        # 0z F2: is the list on screen one row per conversation
        self._rows: list[Any] = []
        self._available: tuple[str, ...] = tuple(key for key, *_ in COLUMNS)

        self.input = QLineEdit()
        self.input.setPlaceholderText(
            "Filter with / commands — /from dave  /to priya  /subject invoice  "
            "/has attachment  /after 2024-01-01"
        )
        self.input.setClearButtonEnabled(True)
        # **2026-09-30: `weak_slot` and methods, never `lambda: self...`** - a
        # callback closing over `self`, held by this view's own child, kept a
        # view that had been let go alive. See `view_options.weak_slot`.
        self.input.textChanged.connect(weak_slot(self, lambda view, _t: view._timer.start()))
        # Only what this tab honours - see `command_popup.MAIL_COMMANDS`.
        self._popup = attach_to(self.input, only=MAIL_COMMANDS, store=store)

        self.summary = QLabel("")
        self.summary.setObjectName("resultsSummary")

        # Sortable, unlike the search results: these rows have no rank to
        # destroy, and "biggest attachment" and "oldest thread" are real
        # questions that a click on a header answers for free.
        self.results = ResultTable(
            [h for _k, h, _a, _r in COLUMNS], sortable=True, alternating=True,
            aligns=["right" if right else "left" for *_rest, right in COLUMNS])
        header = self.results.horizontalHeader()
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)   # Subject
        header.setSectionsMovable(True)

        self.results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.results.customContextMenuRequested.connect(self._on_context_menu)
        self.results.itemDoubleClicked.connect(self._open_selected)   # the cell is no row
        # Right-click the header for the column, density and text-size menu.
        # On the header rather than in Settings: it is a preference about this
        # table, and the place people look for it is the table.
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(weak_slot(self, lambda view, point: (
            view.view_button.show_menu(view.results.horizontalHeader().mapToGlobal(point)))))

        self.view_button = view_button(
            self, store, PREFS_KEY,
            columns=[(key, heading) for key, heading, _a, _r in COLUMNS],
            on_change=self._prefs_changed, conversations=True,
            # A dragged width is remembered; an automatic fit is not - see
            # `view_options.remember_widths` for why that needs a guard.
            table=self.results,
        )

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(MAIL_DEBOUNCE_MS)
        self._timer.timeout.connect(self._run)

        # Off until asked for - the Preview toggle, `Ctrl+Shift+P` or the View
        # menu (this said `Ctrl+P` for weeks; that is "go to Files"). The body comes from
        # `_message_body` rather than from the file, because a PST is a hundred
        # thousand messages in one file and there is nothing on disk to open
        # for any one of them.
        # `store=` (order 0y section 4): with it the pane reads a message as a
        # message - the header card, its conversation, its original.
        self.preview, self.split = attach_preview(
            self.results, self._open_selected, self.error.emit, store=store)
        # The message as text for a pinned window, the stripped-quote notice,
        # and the words to highlight (0y 4b) - `mail_card.attach_mail`.
        attach_mail(self.preview, store,
                    weak_slot(self, lambda view: getattr(view, "_parsed", None)))

        top = QHBoxLayout()
        top.addWidget(self.input, stretch=1)
        self.view_button.add_to(top)         # 2026-10-04: the Preview toggle, then View

        layout = QVBoxLayout(self)
        self.chips = list_chips(self, layout, top, self._run)
        layout.addWidget(self.summary)
        layout.addWidget(self.split, 1)
        self._apply_prefs()
        # **The list exists on open.** `refresh()` was only reached after an
        # index run, so the tab opened empty and stayed empty until somebody
        # typed - with no filters this is the newest few hundred messages,
        # which is what a mail list should show.
        self.refresh()

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
        # **Read on the worker, the same way as every other tab** (1 October
        # 2026): "mail about holiday from maya" becomes `type:mail from:maya`
        # plus the word "holiday", which now narrows by what messages say.
        self._generation += 1
        generation = self._generation

        worker = CallableWorker(   # the reading, the page, its total and the index's
            browse_messages_typed, self._store, self.input.text(), limit=PAGE_SIZE,
            component="ui.mail", declined=tuple(self.chips.declined),
        )
        # `weak_slot`: a finished worker and its slots wait for the collector.
        worker.signals.finished.connect(weak_slot(
            self, lambda view, rows, g=generation: view._show(rows, g, "")))
        worker.signals.failed.connect(self.error.emit)
        run(QThreadPool.globalInstance(), worker)

    def _show(self, rows: Any, generation: int, leftover: str) -> None:
        if generation != self._generation:
            return                          # newer typing has overtaken this

        page = rows if isinstance(rows, dict) else {"rows": rows}
        # Kept for the summary: an unreadable date is said there (§1d).
        self._parsed = page.get("parsed", getattr(self, "_parsed", None))
        # 0z F2: one row per conversation when the View menu says so. `folded`
        # is what the summary says about it; the message counts stay as they are.
        self._folded = self.view_button.prefs.group_by_conversation
        display, folded = mail_list(page["rows"], fold=self._folded)
        self._rows = display

        # Off while filling, on afterwards. Qt re-sorts after every `setItem`
        # otherwise, which is O(n log n) per cell and turns five hundred rows
        # into a visible freeze.
        #
        # **2026-08-28, tables order:** this is now `ResultTable`'s own job -
        # `setRowCount` turns sorting off and `set_row_objects` turns it back
        # on and re-applies the sort somebody chose. The two lines here were
        # the only place in the application that had worked it out, and three
        # other tables had to be made sortable without inheriting the lesson.
        # The cells, sort values, Status tooltip and row objects (attached in
        # table order, before sorting is re-enabled) - `status_column.fill_rows`.
        fill_rows(self.results, display, COLUMNS, SORT_KEYS, first=_first_cell)

        # Recomputed from the rows on screen, so a column is offered when the
        # data can fill it and disabled when it cannot - see `view_options`.
        self.view_button.available = self._available = available_columns(
            display, [(key, attribute) for key, _h, attribute, _r in COLUMNS],
            # Every column this list has shown stays for the next search (2026-10-04).
            always=ALWAYS_OFFERED, kept=self.__dict__.setdefault("_kept_columns", set()),
        )
        self._apply_prefs()

        said = understood_line((), "", noun="messages", in_index=page.get("in_index"),
                               spelling=page.get("spelling", ""))
        show_page(self, page.get("applied"), folded + self._summary_text(
            len(page["rows"]), leftover, page.get("total")) + (f"  ·  {said}" if said else ""))

    def _summary_text(self, shown: int, leftover: str, total: Optional[int] = None) -> str:
        """The wording lives in `presenter.mail_summary` - this file had one
        line of headroom under the 250-line guard, and three branches of string
        formatting inside a widget can only be checked by a person looking at a
        mail tab at the right moment."""
        return with_date_problems(mail_summary(shown, leftover, page_size=PAGE_SIZE, total=total),
                                  getattr(self, "_parsed", None))

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
        # 0z F2: folding conversations changes the rows themselves - ask again.
        if _prefs.group_by_conversation != self._folded:
            return self._run()
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

    def _open_selected(self, row: Any = None) -> None:
        """Open the selected message - in Outlook (the owner, 2026-10-04: the
        same as the preview's "Open in Outlook"), by the one open route. A
        message Outlook cannot show is searched inside, as Open always did here.

        `opened` is emitted as well, for anything that wants the id.
        """
        # `row` from the pane's Open: the message on show, which after a click
        # in its conversation list (0y 4c) is not the row selected here.
        row = row if hasattr(row, "file_id") else self.selected_row()
        if row is None:
            return
        self.opened.emit(row.file_id)
        open_row_async(self._store, row, on_error=self.error.emit,
                       search_inside=self.search_inside_requested.emit)

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

        # 2026-10-04: Open (Outlook) and Show in folder (the archive), as the
        # preview pane offers them - the open route does both for a message.
        show_for(self.results, point, row.path, FileActions(
            open_file=lambda: self._open_selected(row), row=row,
            reveal=lambda: open_row_async(self._store, row, reveal=True, on_error=self.error.emit),
            search_inside=lambda: self.search_inside_requested.emit(row.path),
            same_period=lambda: self.period_requested.emit(int(row.file_id)),
            copy=[("Copy subject", row.subject), ("Copy sender", row.sender)],
        ))
