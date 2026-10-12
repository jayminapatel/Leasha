"""Settings > Search > Your words: the owner's own words and what they mean.

Layer: L7 (UI)

Order `smarter-answers` 4d (owner, 2026-10-12). "Mum" means Jane Example; "the
house" means 12 Example Road. Chat and the Search page's answers look for the
meaning whenever the word is used (`app/chat/my_words.py`). Words told to Chat
("remember Mum is Jane Example") appear here too, and every one can be changed
or removed (rule 11: what can be set has a control).

Not a `.env` setting: these are the owner's data, kept in the index database
(`my_words`) and through a Reset, like the folders to index. Every read and
write is on a worker; the window's thread only draws.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QThreadPool, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from app.ui.later import when_done
from app.ui.tasks import my_words_read as _read, my_words_remove as _remove
from app.ui.tasks import my_words_set as _write
from app.ui.widgets.buttons import action_button, button_row
from app.ui.workers import CallableWorker, run

__all__ = ["MyWordsBox", "INTRO"]

INTRO = ("Names only you use - “Mum”, “the house”, “my accountant” - "
         "and what they mean in your files. Chat and the answers on the Search page look for "
         "the meaning whenever you use the word. You can also tell Chat: "
         "“remember Mum is Jane Example”.")


class MyWordsBox(QGroupBox):
    """The answers switch, then a table of word -> meaning, a row to add one, and Remove."""

    #: `{"SEARCH_ANSWERS": bool}` - the switch, written to `.env` by the window.
    changed = Signal(dict)

    def __init__(self, settings: Any = None, parent: Optional[QWidget] = None) -> None:
        super().__init__("Answers and your words", parent)
        self.setObjectName("myWordsBox")
        self._store: Any = None
        self._load = 0

        # Order smarter-answers 3b: the Search page's answer card, on or off.
        self.answers = QCheckBox("Answer questions on the Search page")
        self.answers.setObjectName("SEARCH_ANSWERS")
        self.answers.setAccessibleName("Answer questions on the Search page")
        self.answers.setToolTip(
            "When what you type reads as a question, a short answer from your files "
            "appears above the results, each sentence checked against the document it "
            "cites. The results never wait for it.")
        self.answers.setChecked(bool(getattr(settings, "search_answers", True)))
        self.answers.toggled.connect(lambda on: self.changed.emit({"SEARCH_ANSWERS": on}))

        intro = QLabel(INTRO)
        intro.setWordWrap(True)
        self.table = QTableWidget(0, 2)
        self.table.setObjectName("myWordsTable")
        self.table.setAccessibleName("Your words and what they mean")
        self.table.setHorizontalHeaderLabels(["Word", "Means"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMaximumHeight(180)
        self.table.itemSelectionChanged.connect(self._picked)

        self.phrase = QLineEdit()
        self.phrase.setObjectName("myWordsPhrase")
        self.phrase.setPlaceholderText("Mum")
        self.phrase.setAccessibleName("Word")
        self.meaning = QLineEdit()
        self.meaning.setObjectName("myWordsMeaning")
        self.meaning.setPlaceholderText("Jane Example")
        self.meaning.setAccessibleName("What it means")
        self.add = action_button("Add word")
        self.add.setObjectName("myWordsAdd")
        self.add.setToolTip("Save the word and what it means; a word already listed gets the new meaning")
        self.remove = action_button("Remove word")
        self.remove.setObjectName("myWordsRemove")
        self.remove.setToolTip("Forget the word picked in the list")
        self.status = QLabel("")
        self.status.setObjectName("myWordsStatus")
        self.add.clicked.connect(self._add)
        self.meaning.returnPressed.connect(self._add)
        self.remove.clicked.connect(self._remove)

        entry = QHBoxLayout()
        entry.addWidget(self.phrase, 1)
        entry.addWidget(QLabel("means"))
        entry.addWidget(self.meaning, 2)
        layout = QVBoxLayout(self)
        layout.addWidget(self.answers)
        layout.addWidget(intro)
        layout.addWidget(self.table)
        layout.addLayout(entry)
        layout.addLayout(button_row(self.add, self.remove))
        layout.addWidget(self.status)
        self._sync()

    # -- the store, always on a worker ---------------------------------------------

    def set_store(self, store: Any) -> None:
        """The window's index. Read at once, off the window's thread."""
        self._store = store
        self._call(_read, "")

    def _call(self, body: Any, said: str, *args: Any) -> None:
        if self._store is None:
            return
        self._load += 1
        load = self._load
        worker = CallableWorker(body, self._store, *args, component="ui.my_words")
        when_done(self, worker,
                  finished=lambda rows: self._show(load, rows, said),
                  failed=lambda error: self.status.setText(
                      "Could not save that: " + str(getattr(error, "message", error))))
        run(QThreadPool.globalInstance(), worker)

    # -- drawing ----------------------------------------------------------------------

    def _show(self, load: int, rows: Any, said: str) -> None:
        if load != self._load:
            return
        self.table.setRowCount(0)
        for phrase, meaning, _source in rows or ():
            at = self.table.rowCount()
            self.table.insertRow(at)
            for column, text in enumerate((phrase, meaning)):
                item = QTableWidgetItem(text)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(at, column, item)
        self.status.setText(said)
        self._sync()

    def words(self) -> list[tuple[str, str]]:
        """What the table shows, top to bottom."""
        return [(self.table.item(r, 0).text(), self.table.item(r, 1).text())
                for r in range(self.table.rowCount())]

    def _picked(self) -> None:
        """A row picked fills the boxes, so a meaning can be changed by Add."""
        rows = self.table.selectionModel().selectedRows()
        if rows:
            at = rows[0].row()
            self.phrase.setText(self.table.item(at, 0).text())
            self.meaning.setText(self.table.item(at, 1).text())
        self._sync()

    def _sync(self) -> None:
        self.remove.setEnabled(bool(self.table.selectionModel().selectedRows()))

    # -- actions ----------------------------------------------------------------------

    def _add(self) -> None:
        phrase = " ".join(self.phrase.text().split())
        meaning = " ".join(self.meaning.text().split())
        if not phrase or not meaning:
            self.status.setText("Type the word and what it means.")
            return
        self.phrase.clear()
        self.meaning.clear()
        self._call(_write, f"“{phrase}” now means {meaning}.", phrase, meaning)

    def _remove(self) -> None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        phrase = self.table.item(rows[0].row(), 0).text()
        self.phrase.clear()
        self.meaning.clear()
        self._call(_remove, f"“{phrase}” removed.", phrase)
