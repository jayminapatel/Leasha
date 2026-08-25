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
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.logging import logger
from app.search.commands import expand_slashes
from app.search.query import parse_query
from app.ui.presenter import file_rows
from app.ui.view_options import (
    apply_to_table, available_columns, button as view_button,
)
from app.ui.widgets.command_popup import attach_to
from app.ui.widgets.file_menu import FileActions, show_for, viewport_point
from app.ui.workers import CallableWorker, open_in_explorer, run

__all__ = ["FilesView", "NAME_DEBOUNCE_MS", "COLUMNS", "PREFS_KEY"]

#: (key, heading, attribute on FileRow, right-aligned?)
COLUMNS: tuple[tuple[str, str, str, bool], ...] = (
    ("name", "Name", "name", False),
    ("size", "Size", "size", True),
    ("modified", "Modified", "modified", True),
    ("type", "Type", "kind", False),
    ("folder", "Folder", "folder", False),
)

#: A file list without a name is not a file list.
ALWAYS_OFFERED = ("name",)

#: Namespace for this list's view preferences in `index_state`.
PREFS_KEY = "ui:files"

_log = logger.bind(component="ui.files")

#: Much shorter than the search box's debounce. There is no model to load and
#: no vectors to probe - one FTS5 lookup over a table of filenames - so the only
#: reason to wait at all is to avoid a query per keystroke on a fast typist.
NAME_DEBOUNCE_MS = 80


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
        self._popup = attach_to(self.input)

        self.summary = QLabel("")
        self.summary.setObjectName("resultsSummary")

        self.results = QTableWidget(0, len(COLUMNS))
        self.results.setHorizontalHeaderLabels([h for _k, h, _a, _r in COLUMNS])
        self.results.verticalHeader().hide()
        self.results.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.results.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.results.setSortingEnabled(False)      # ranked by match quality, not by column
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
        )

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(NAME_DEBOUNCE_MS)
        self._timer.timeout.connect(self._run)

        top = QHBoxLayout()
        top.addWidget(self.input, stretch=1)
        top.addWidget(self.view_button)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self.summary)
        layout.addWidget(self.results, 1)

        self._apply_prefs()
        self.refresh_summary()

    # -- how it looks ----------------------------------------------------------

    def _apply_prefs(self) -> None:
        apply_to_table(
            self.results, self.view_button.prefs,
            columns=[(key, heading) for key, heading, _a, _r in COLUMNS],
            available=self._available,
        )

    def _prefs_changed(self, _prefs: Any) -> None:
        """The button owns the preferences and has already saved them."""
        self._apply_prefs()

    def shutdown(self) -> None:
        """Stop the debounce timers, so no new query starts while closing.

        A timer that fires during teardown starts a search against a store that
        is being closed, which arrives as a traceback telling the owner to send
        the log file. Nothing is wrong; the work simply should not have begun.
        """
        self._generation += 1        # anything still in flight is now stale
        timer = getattr(self, "_timer", None)
        if timer is not None:
            timer.stop()

    def focus(self) -> None:
        self.input.setFocus()
        self.input.selectAll()

    def refresh_summary(self) -> None:
        try:
            total = self._store.count_named_files()
        except Exception:                          # noqa: BLE001 - a label is not worth crashing over
            return
        self.summary.setText(
            f"{total:,} file names indexed."
            if total
            else "No file names indexed yet — run an index first."
        )

    # -- searching -----------------------------------------------------------

    def _on_typed(self, _text: str) -> None:
        self._timer.start()

    def _run(self) -> None:
        # **The `/` commands are parsed, not merely offered.**
        #
        # The dropdown existed here first as a copy of the search box's, which
        # meant `/type pdf` was inserted as `type:pdf` and then handed to a
        # trigram index as a literal string - matching nothing, with a dropdown
        # cheerfully suggesting it. An offer the application does not honour is
        # worse than no offer at all.
        #
        # Only the filters this tab can actually apply are used. `type:` becomes
        # the `ext` argument the store already takes; everything else in the
        # parse is about document *contents*, which this tab does not read.
        parsed = parse_query(expand_slashes(self.input.text().strip()))
        text = " ".join((*parsed.terms, *parsed.names)).strip() or (parsed.text or "").strip()
        ext = list(parsed.ext)

        # A filter on its own is a complete request: `/type pdf` means "every
        # PDF", and demanding two characters of name as well would refuse it.
        if len(text) < 2 and not ext:
            self.results.setRowCount(0)
            self.refresh_summary()
            return

        # Generation-tagged, like the main search box: a lookup that lands after
        # newer typing must be dropped rather than overwrite fresher results.
        # These are fast enough that it rarely happens and cheap enough to be
        # certain about.
        self._generation += 1
        generation = self._generation

        worker = CallableWorker(
            self._store.search_files_by_name, text, limit=200, ext=ext or None,
            component="ui.files",
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
            for column, (_key, _heading, attribute, right) in enumerate(COLUMNS):
                item = QTableWidgetItem(getattr(row, attribute))
                if right:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                    )
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, row.file_id)
                    if row.note:
                        item.setToolTip(row.note)
                self.results.setItem(index, column, item)

        # Offered when the data can fill it, disabled when it cannot.
        self.view_button.available = self._available = available_columns(
            display, [(key, attribute) for key, _h, attribute, _r in COLUMNS],
            always=ALWAYS_OFFERED,
        )
        self._apply_prefs()

        self.summary.setText(
            f"{len(display):,} file name{'s' if len(display) != 1 else ''} contain '{text}'"
            if display
            else f"No file name contains '{text}'."
        )

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
        """The path of the highlighted row, or None. Never raises."""
        items = self.results.selectedItems()
        if not items:
            return None
        cell = self.results.item(items[0].row(), 0)
        if cell is None:
            return None
        try:
            record = self._store.get_file_by_id(int(cell.data(Qt.ItemDataRole.UserRole)))
        except Exception as exc:                   # noqa: BLE001
            _log.debug("could not read the selected file: {}", exc)
            return None
        return record.path if record is not None else None

    def _open_selected(self) -> None:
        """Open the file itself.

        This used to *reveal* it in Explorer instead, on the reasoning that a
        filename search is usually the first half of doing something in the
        folder. That is sometimes true and always surprising: everywhere else,
        double-clicking a file opens it. Both are available from the right-click
        menu, and the unsurprising one is now the default.
        """
        path = self.selected_path()
        if path is None:
            return
        error = open_in_explorer(path, select=False)
        if error is not None:
            self.error.emit(error)

    def _reveal_selected(self) -> None:
        path = self.selected_path()
        if path is None:
            return
        error = open_in_explorer(path, select=True)
        if error is not None:
            self.error.emit(error)

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
