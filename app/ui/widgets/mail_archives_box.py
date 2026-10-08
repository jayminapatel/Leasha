r"""Every mail archive in the index, how each is read, and reading one again.

Layer: L5

2026-10-07, the owner: "need a way for each pst file it can be configured how
to index outlook or direct ... there should be a reindex button on those
files ... dont forget we need icons everywhere".

**Why per archive.** "How to read archives" above is one answer for all of
them, and a collection is rarely that tidy: one old archive Outlook refuses to
open beside ten the direct reader handles, or the other way round. Each line
follows the setting above until it is given a choice of its own; the reader
applies the choices at the start of every run (`run_setup.load_pst_backends`).
An `.ost` is Outlook's own copy of a mailbox and only Outlook reads it, so its
line says so and cannot be changed.

**Two ways to read one again, because they answer two different worries.**
"Read again" reads the archive from the start over the top of what is there -
nothing is taken away, so its mail stays findable throughout. "Clear and read
again" first takes its messages and attachments out of the index (the window
asks, with the count), for when what is there is wrong and should not stay.
Both are icon buttons on the line they act on, as "Index now" is on a folder's
line in `roots_box.py`: nothing to select first, and the column's heading says
the words once.

**The first choice names the way it reads** (the owner, 2026-10-08: "the text
use the setting above makes no sense it should display the actual option"):
"Direct file reading (as set above)" and so on, following "How to read
archives" as it changes (`set_default_backend`).

**Columns and lines fit their contents** (the same day: "the columns on this
page are not sizeable and should autofit by default.. also the buttons size is
big they are getting clipped"): every column can be dragged, the Archive column
takes the spare width and elides in the middle, and each line is as tall as its
controls (`fitted_tree.py`).

Widgets only: the list is read by the controller on a worker, and every word is
in `app/ui/presenter/mail_archives.py`.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QGroupBox, QLabel, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from app.index.archives import normalise
from app.ui.presenter.mail_archives import (
    ARCHIVE_CHOICE_TIP, OUTLOOK_ONLY_TIP, archive_choices, archive_default_label,
    archive_status_words, is_outlook_only, mail_archives_empty_text, messages_words,
)
from app.ui.widgets.buttons import icon_button, put_on_row
from app.ui.widgets.fitted_tree import FittedColumns
from app.ui.widgets.result_table import align_headers

__all__ = ["MailArchivesBox"]

_BACKENDS = ("auto", "libpff", "outlook")


def _backend(value: Any) -> str:
    """One of "auto", "libpff", "outlook"; anything else is "auto"."""
    text = str(value or "auto").strip().lower()
    return text if text in _BACKENDS else "auto"

#: Column numbers, named so the controller and the tests need not count.
COL_ARCHIVE, COL_MESSAGES, COL_STATUS, COL_HOW, COL_READ, COL_CLEAR = range(6)

_PATH_ROLE = Qt.ItemDataRole.UserRole


class MailArchivesBox(QGroupBox):
    """One line per `.pst`/`.ost` in the index."""

    #: `(archive path, "auto"|"libpff"|"outlook")` - a line's drop-down changed.
    #: Fired on the person's action only, never by `set_archives`.
    choice_changed = Signal(str, str)
    #: "Read again" on a line: that archive, from the start, over the top.
    read_again = Signal(str)
    #: "Clear and read again" on a line: ask, clear its items, read it again.
    clear_and_read = Signal(str)

    def __init__(self, parent: Optional[Any] = None) -> None:
        super().__init__("Mail archives", parent)
        self._rows: list[dict[str, Any]] = []
        #: "How to read archives" above (`ui:pst_backend`): what each line's
        #: first choice is named after.
        self._default_backend = "auto"

        self.empty = QLabel(mail_archives_empty_text())
        self.empty.setWordWrap(True)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(6)
        # The two button columns' headings on two lines (2026-10-08): on one,
        # "Clear and read again" made its column four times the width of its
        # button, and on the window's default size the row ran off the side.
        # The same words, broken where a person would break them.
        self.tree.setHeaderLabels(["Archive", "Messages", "Status", "How it is read",
                                   "Read\nagain", "Clear and\nread again"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        align_headers(self.tree)
        # Every column draggable and fitted to its contents; Archive takes the
        # spare width. A skipped archive's reason is a whole sentence: sized to
        # its contents it pushed the drop-down and both buttons off the side of
        # the page, so Status is the column that gives way first, and its
        # tooltip carries all of it.
        self.columns = FittedColumns(self.tree, stretch=COL_ARCHIVE,
                                     squeeze=(COL_STATUS,))

        self.note = QLabel(
            "Each archive is read the way \"How to read archives\" says, unless it "
            "is given a way of its own here. Read again reads it from the start "
            "and keeps what is in the index meanwhile; Clear and read again takes "
            "it out of the index first.")
        self.note.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self.empty)
        layout.addWidget(self.tree)
        layout.addWidget(self.note)
        self._sync_empty()

    # -- filling it in --------------------------------------------------------

    def set_archives(self, rows: Sequence[Mapping[str, Any]],
                     choices: Optional[Mapping[str, str]] = None,
                     default_backend: Optional[str] = None) -> None:
        """Replace the list, **without emitting**. `rows` as
        `SqliteStore.mail_archives` returns them; `choices` is
        `{normalised path: backend}`, the saved per-archive setting;
        `default_backend` is "How to read archives" above, left as it was when
        omitted."""
        choices = dict(choices or {})
        if default_backend is not None:
            self._default_backend = _backend(default_backend)
        self._rows = [dict(row) for row in rows]
        self.tree.clear()
        for row in self._rows:
            self._append(row, choices.get(normalise(row["path"]), "auto"))
        self._sync_empty()

    def _append(self, row: Mapping[str, Any], choice: str) -> QTreeWidgetItem:
        path = str(row["path"])
        messages = int(row.get("messages") or 0)
        status = archive_status_words(row.get("status"), row.get("skip_code"), messages)
        item = QTreeWidgetItem([path, messages_words(messages), status, "", "", ""])
        item.setData(COL_ARCHIVE, _PATH_ROLE, path)
        item.setToolTip(COL_ARCHIVE, path)
        item.setToolTip(COL_STATUS, status)
        item.setTextAlignment(COL_MESSAGES, Qt.AlignmentFlag.AlignRight
                              | Qt.AlignmentFlag.AlignVCenter)
        self.tree.addTopLevelItem(item)

        combo = QComboBox()
        combo.setAccessibleName(f"How this archive is read: {path}")
        for label, value in archive_choices(self._default_backend):
            combo.addItem(label, value)
        # As on the folder list: a wheel over a combo in a scrolling page
        # changes it while the person is trying to scroll past.
        combo.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        # As wide as its longest choice: cut short, "Direct file reading (no
        # Outlook needed)" lost its last word on the page.
        combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        if is_outlook_only(path):
            combo.setCurrentIndex(combo.findData("outlook"))
            combo.setEnabled(False)
            combo.setToolTip(OUTLOOK_ONLY_TIP)
        else:
            index = combo.findData(str(choice or "auto"))
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.setToolTip(ARCHIVE_CHOICE_TIP)
            # Connected after the value is set, so filling the list says nothing.
            combo.currentIndexChanged.connect(
                lambda _i, box=combo, where=path: self.choice_changed.emit(
                    where, str(box.currentData())))
        self.tree.setItemWidget(item, COL_HOW, combo)

        again = icon_button(
            "Read again", name=f"Read this archive again: {path}",
            tooltip=("Read again - this archive only.\n\n"
                     "It is read from the start, over the top of what is in the "
                     "index. Nothing is taken out first, so its mail can still be "
                     "found while it is read."))
        again.clicked.connect(
            lambda _checked=False, where=path: self.read_again.emit(where))
        put_on_row(self.tree, item, COL_READ, again)

        clear = icon_button(
            "Clear and read again", name=f"Clear this archive and read it again: {path}",
            tooltip=("Clear and read again - this archive only.\n\n"
                     "Its messages and their attachments are taken out of the "
                     "index, then it is read again from the start. Leasha says "
                     "how many items first and asks. Your file is not touched."))
        clear.clicked.connect(
            lambda _checked=False, where=path: self.clear_and_read.emit(where))
        put_on_row(self.tree, item, COL_CLEAR, clear)
        return item

    def set_default_backend(self, backend: str) -> None:
        """"How to read archives" above changed (or was read): every line's
        first choice is renamed after it. **Emits nothing** - a line that
        follows the setting still follows it; only its words change."""
        self._default_backend = _backend(backend)
        label = archive_default_label(self._default_backend)
        for index in range(self.tree.topLevelItemCount()):
            combo = self.tree.itemWidget(self.tree.topLevelItem(index), COL_HOW)
            if combo is None:
                continue
            first = combo.findData("auto")
            if first >= 0:
                combo.setItemText(first, label)
        # The first choice may now be longer or shorter than it was.
        self.columns.schedule(force=True)

    def default_backend(self) -> str:
        """The setting above, as this box last heard it."""
        return self._default_backend

    def _sync_empty(self) -> None:
        """One line saying there are none, instead of an empty table."""
        empty = self.tree.topLevelItemCount() == 0
        self.empty.setVisible(empty)
        self.tree.setVisible(not empty)
        self.note.setVisible(not empty)

    # -- what the controller reads --------------------------------------------

    def archives(self) -> list[str]:
        """Every line's path, in the order shown."""
        return [str(self.tree.topLevelItem(i).data(COL_ARCHIVE, _PATH_ROLE))
                for i in range(self.tree.topLevelItemCount())]

    def messages_in(self, path: str) -> int:
        """How many messages the list says this archive gave, 0 if unlisted."""
        wanted = normalise(path)
        for row in self._rows:
            if normalise(row["path"]) == wanted:
                return int(row.get("messages") or 0)
        return 0

    def item_for(self, path: str) -> Optional[QTreeWidgetItem]:
        wanted = normalise(path)
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            if normalise(str(item.data(COL_ARCHIVE, _PATH_ROLE))) == wanted:
                return item
        return None

    def current_choices(self) -> dict[str, str]:
        """`{normalised path: backend}` for every line with a way of its own -
        a line following the setting above and an `.ost`'s fixed line are left
        out."""
        found: dict[str, str] = {}
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            combo = self.tree.itemWidget(item, COL_HOW)
            if combo is None or not combo.isEnabled():
                continue
            value = str(combo.currentData() or "auto")
            if value != "auto":
                found[normalise(str(item.data(COL_ARCHIVE, _PATH_ROLE)))] = value
        return found
