"""Find a file by its name. The other kind of search.

Layer: L4 (UI), driving L1

**Why this is a tab and not a filter.** `chunks_fts` indexes what documents
*say*. Until schema v4 nothing indexed what they were *called*, so a file named
`Invoice 2024.pdf` whose contents never used those words could not be found at
all - which is how most people look for most files most of the time.

That is a different question, answered from a different index, and it deserves
its own place rather than being a mode of the search box. Everything about it is
different: no embedding, no reranking, no snippets, results on every keystroke,
and a result row that shows size and age rather than a passage of text.

**Substring, not word-prefix.** The index uses FTS5's trigram tokeniser, so
"voice" finds "Invoice". People type the middle of a name and expect a hit; a
word tokeniser cannot do that and the feature feels broken.

**Messages are deliberately absent.** A PST message's "path" is a synthetic key
nobody typed and nobody would recognise, and mail would outnumber documents ten
to one here. Mail is searched from the search tab.
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
from app.ui.presenter import FILES_COMMANDS, file_query, file_rows, file_summary
from app.ui.view_options import (
    apply_to_table, available_columns, button as view_button,
)
from app.ui.widgets.command_popup import attach_to
from app.ui.widgets.file_menu import FileActions, show_for, viewport_point
from app.ui.widgets.preview import attach_preview
from app.ui.widgets.result_table import ResultTable
from app.ui.widgets.sortable_item import SORT_ROLE, SortableItem
from app.ui.workers import CallableWorker, open_async, run, stop_timers

__all__ = ["FilesView", "NAME_DEBOUNCE_MS", "COLUMNS", "PREFS_KEY"]

#: (key, heading, attribute on FileRow, right-aligned?)
#:
#: **The fourth field is now read by the table rather than by this file.** It
#: says the same thing it always said; `ResultTable` turns it into an
#: alignment for the cells *and for the heading above them*, which is what
#: §2a is about - a right-aligned number under a centred heading reads as a
#: table somebody stopped caring about halfway through.
COLUMNS: tuple[tuple[str, str, str, bool], ...] = (
    ("name", "Name", "name", False),
    ("size", "Size", "size", True),
    ("modified", "Modified", "modified", True),
    ("type", "Type", "kind", False),
    ("folder", "Folder", "folder", False),
)

#: What each column sorts on, when it is not the text in it. Attribute on
#: `FileRow`, or None to sort by what is shown.
#:
#: `size` and `modified` are formatted for reading - "10 KB", "3 weeks ago" -
#: and both orders are wrong: 10 KB sorts before 3 KB and "3 weeks ago" before
#: "yesterday". This is the U4 lesson, and it is why the raw values are now
#: carried on the row.
SORT_KEYS: dict[str, str] = {"size": "size_bytes", "modified": "mtime_ns"}

#: A file list without a name is not a file list.
ALWAYS_OFFERED = ("name",)

#: Namespace for this list's view preferences in `index_state`.
PREFS_KEY = "ui:files"

_log = logger.bind(component="ui.files")

#: Much shorter than the search box's debounce. There is no model to load and
#: no vectors to probe - one FTS5 lookup over a table of filenames - so the only
#: reason to wait at all is to avoid a query per keystroke on a fast typist.
NAME_DEBOUNCE_MS = 80

#: Below this, a name query matches nearly everything and the answer is not
#: useful. The *list* is not cleared while typing towards it - see `_run`.
MIN_NAME_CHARS = 2


class FilesView(QWidget):
    """A filename browser: type, get files, double-click to open."""

    error = pyqtSignal(object)

    #: A path to search the *contents* of. The bridge between the two lists:
    #: found it by name, now find what is in it. Without it the filename
    #: browser is a dead end - you can see the file and do nothing with it.
    search_inside_requested = pyqtSignal(str)

    def __init__(self, store: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._generation = 0
        self._available: tuple[str, ...] = tuple(key for key, *_ in COLUMNS)

        self.input = QLineEdit()
        self.input.setPlaceholderText(
            "Part of a file name — 'voice' finds 'Invoice 2024.pdf'.  "
            "Or / for filters: /type pdf"
        )
        self.input.setClearButtonEnabled(True)
        self.input.textChanged.connect(self._on_typed)
        # The same dropdown as the search box. It is wired to a parser here too
        # - see `_run`. Offering a filter the tab then ignores would be worse
        # than not offering it at all.
        # Only what this tab honours - see `command_popup.FILES_COMMANDS`.
        self._popup = attach_to(self.input, only=FILES_COMMANDS, store=store)

        self.summary = QLabel("")
        self.summary.setObjectName("resultsSummary")

        # Not sortable: ranked by match quality, and a header click would throw
        # that away silently. See widgets/result_table.py.
        #
        # **2026-08-28, tables order:** superseded, and the reasoning above is
        # honoured rather than dropped. The ranking is no longer discarded by
        # a click - `ranked=True` keeps it in a hidden column, and a third
        # click on the same header puts the list back into best-match order.
        # A list you cannot reorder is a list that cannot answer "which of
        # these is the biggest", and that was the cost of being right about
        # the ranking.
        self.results = ResultTable(
            [h for _k, h, _a, _r in COLUMNS], ranked=True,
            aligns=["right" if right else "left" for *_rest, right in COLUMNS])
        header = self.results.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        # Double-click opens the file; right-click offers everything else; Enter
        # does what double-click does, because a keyboard user should never have
        # to reach for the mouse to act on a result they have already selected.
        self.results.itemDoubleClicked.connect(lambda _item: self._open_selected())
        self.results.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.results.customContextMenuRequested.connect(self._on_context_menu)
        self.results.installEventFilter(self)
        # Right-click the header for columns, density and text size. On the
        # header rather than in Settings, because it is a preference about this
        # table and the table is where people look for it.
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(
            lambda point: self.view_button.show_menu(header.mapToGlobal(point)))

        self.view_button = view_button(
            self, store, PREFS_KEY,
            columns=[(key, heading) for key, heading, _a, _r in COLUMNS],
            on_change=self._prefs_changed,
            # A dragged width is remembered; an automatic fit is not - see
            # `view_options.remember_widths` for why that needs a guard.
            table=self.results,
        )

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(NAME_DEBOUNCE_MS)
        self._timer.timeout.connect(self._run)

        # Off until asked for - `Ctrl+P` or the View menu. Same pane, same
        # shortcut and same behaviour as the search tab: the owner's rule is
        # that a feature helping one search area is applied to the others.
        self.preview, self.split = attach_preview(
            self.results, lambda _row: self._open_selected(), self.error.emit)

        top = QHBoxLayout()
        top.addWidget(self.input, stretch=1)
        top.addWidget(self.view_button)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.summary)
        layout.addWidget(self.split, 1)

        self._apply_prefs()
        self.refresh_summary()
        # **Filled on open, not on the first keystroke.** A blank table cannot
        # be browsed and is indistinguishable from an empty index - which is the
        # complaint this answers: *"at startup the code window has a list others
        # dont"*. The query is the same one typing runs, with an empty name.
        self._run()

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

    def shutdown(self) -> None:
        """Stop the debounce timers - see `workers.stop_timers`."""
        stop_timers(self)
        self.preview.shutdown()

    def focus(self) -> None:
        self.input.setFocus()
        self.input.selectAll()

    def refresh_summary(self) -> None:
        """Count the indexed filenames, on a worker.

        `COUNT(*)` over `files_fts` is instant on a test corpus and is not on a
        real one - and this runs on every tab switch and after every index run.
        A label is never worth blocking the window for.
        """
        worker = CallableWorker(
            self._store.count_named_files, component="ui.files.count")
        worker.signals.finished.connect(self._show_summary)
        worker.signals.failed.connect(lambda _e: None)   # a label, not a search
        run(QThreadPool.globalInstance(), worker)

    def _show_summary(self, total: int) -> None:
        self.summary.setText(file_summary(total))

    # -- searching -----------------------------------------------------------

    def _on_typed(self, _text: str) -> None:
        self._timer.start()

    def _run(self) -> None:
        parsed = file_query(self.input.text())
        # The free text, as the store will see it. Kept here only to decide
        # whether the box is too short to answer and what to put in the summary
        # - the *filtering* is entirely the store's, through the one definition
        # every tab now shares.
        text = " ".join(parsed.terms).strip() or (parsed.text or "").strip()
        filtered = any((
            parsed.ext, parsed.paths, parsed.names, parsed.sizes, parsed.repos,
            parsed.senders, parsed.recipients, parsed.subjects,
            parsed.after, parsed.before, parsed.has_attachment is not None,
        ))

        # **Only an empty box clears the list.**
        #
        # Reported as "the mail tab searches as you type, this one is not the
        # same". Mail never blanks: it keeps what it has until something
        # replaces it. This wiped the table on the way *to* a query - typing
        # `in` on the way to `invoice` emptied the screen and then refilled it,
        # which reads as the tab losing your results rather than working.
        #
        # A filter on its own is also a complete request: `/type pdf` means
        # "every PDF", and demanding characters of name as well would refuse it.
        # **An empty box lists everything, newest first.** It used to blank the
        # table, which cannot be browsed, shows nothing of what is in the index,
        # and looks exactly like an index that is empty. Asked for: *"initially
        # should display everything and it filters as you type"*. The store
        # reads an empty name as "everything" - see `search_files_by_name` - so
        # this falls through to the same query the rest of the tab uses rather
        # than becoming a second path that can drift from it.
        if len(text) < MIN_NAME_CHARS and text and not filtered:
            # Too short to be meaningful - one character matches nearly every
            # file - but the previous results stay on screen rather than the
            # table going blank mid-word.
            self.summary.setText(
                f"Keep typing — {MIN_NAME_CHARS} characters or more, "
                f"or use / for a filter."
            )
            # Returns without querying and without clearing: the rows from the
            # last complete query stay put, which is the whole point.
            return

        # Generation-tagged, like the main search box: a lookup that lands after
        # newer typing must be dropped rather than overwrite fresher results.
        # These are fast enough that it rarely happens and cheap enough to be
        # certain about.
        self._generation += 1
        generation = self._generation

        worker = CallableWorker(
            self._store.browse_files, parsed, limit=200, component="ui.files",
        )
        worker.signals.finished.connect(
            lambda rows, g=generation: self._show(rows, g, text)
        )
        worker.signals.failed.connect(self.error.emit)
        run(QThreadPool.globalInstance(), worker)

    def _show(self, rows: Any, generation: int, text: str) -> None:
        if generation != self._generation:
            return                                  # a newer query has been sent

        display = file_rows(rows)
        self.results.setRowCount(len(display))
        for index, row in enumerate(display):
            for column, (key, _heading, attribute, _right) in enumerate(COLUMNS):
                # `SortableItem`, and the alignment comes from the column spec
                # the table was built with - see COLUMNS.
                item = SortableItem(getattr(row, attribute))
                sort_by = SORT_KEYS.get(key)
                if sort_by:
                    item.setData(SORT_ROLE, getattr(row, sort_by, 0))
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row.file_id)
                    if row.note:
                        item.setToolTip(row.note)
                self.results.setItem(index, column, item)
        # What the preview pane draws from. After filling, so the order here is
        # the order the rows went in.
        self.results.set_row_objects(display)

        # Offered when the data can fill it, disabled when it cannot.
        self.view_button.available = self._available = available_columns(
            display, [(key, attribute) for key, _h, attribute, _r in COLUMNS],
            always=ALWAYS_OFFERED,
        )
        self._apply_prefs()

        self.summary.setText(file_summary(0, shown=len(display), text=text))

    # -- opening -------------------------------------------------------------

    def eventFilter(self, watched: Any, event: Any) -> bool:   # noqa: N802 - Qt's naming
        """Enter opens the selected row.

        A table that can only be acted on with a mouse fails the keyboard-only
        requirement, and this one could not even be opened without one.
        """
        from PyQt6.QtCore import QEvent

        if watched is self.results and event.type() == QEvent.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._open_selected()
                return True
        return super().eventFilter(watched, event)

    def selected_path(self) -> Optional[str]:
        """The path of the highlighted row, or None. Never raises.

        **Read from the row, not from the database.** This did a synchronous
        `get_file_by_id` on the UI thread for every double-click and every
        right-click - to fetch a path the row object beside it already carried.
        `show_rows` stores those objects with `set_row_objects` precisely so the
        table can answer questions about itself, and the preview pane has always
        used them.

        One indexed lookup is fast until the database is being written by an
        index run, at which point it waits on the write lock, on the thread
        that paints.
        """
        row = self.results.current_row()
        path = str(getattr(row, "path", "") or "") if row is not None else ""
        return path or None

    def _open_selected(self) -> None:
        """Open the file itself.

        This used to *reveal* it in Explorer instead, on the reasoning that a
        filename search is usually the first half of doing something in the
        folder. That is sometimes true and always surprising: everywhere else,
        double-clicking a file opens it. Both are available from the right-click
        menu, and the unsurprising one is now the default.
        """
        self._open(self.selected_path(), reveal=False)

    def _reveal_selected(self) -> None:
        self._open(self.selected_path(), reveal=True)

    def _open(self, path: Optional[str], *, reveal: bool) -> None:
        """See `workers.open_async` - never on the UI thread."""
        open_async(path or "", reveal=reveal, on_error=self.error.emit,
                   component="ui.files.open")

    def _on_context_menu(self, point: Any) -> None:
        """The same menu the search results use - see widgets/file_menu.py."""
        # **Viewport coordinates, not widget coordinates.** The signal gives a
        # point relative to the table; `rowAt` wants one relative to the
        # viewport, and the header sits between them. Without this the menu
        # acted on the row below the one clicked, and on the last row found no
        # row at all and silently did nothing.
        row = self.results.rowAt(viewport_point(self.results, point).y())
        if row >= 0:
            self.results.selectRow(row)
        # Right-clicking below the last row, or pressing the Menu key, lands on
        # no row. Falling back to the selection means both still work, rather
        # than the menu appearing to be broken in exactly the places people
        # reach for it.
        path = self.selected_path()
        if path is None:
            return

        show_for(self.results, point, path, FileActions(
            open_file=self._open_selected,
            reveal=self._reveal_selected,
            search_inside=lambda: self.search_inside_requested.emit(path),
        ))
