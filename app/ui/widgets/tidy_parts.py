r"""The Tidy page's worker reads and its History pane.

Layer: L5. Order 1q (2026-10-11). Everything here that touches the store runs on a
worker (non-negotiable 5); `TidyView` only draws what comes back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)

__all__ = ["TidySnapshot", "read_snapshot", "read_quality", "read_file_sets", "HistoryPane"]

_BATCH = int(Qt.ItemDataRole.UserRole) + 31


@dataclass
class TidySnapshot:
    """One load of the page: the counts, the overview and every list."""

    counts: Any = None
    overview: str = ""
    only: str = ""
    sets: dict[str, list] = field(default_factory=dict)       # kind -> sets
    batches: list = field(default_factory=list)
    #: What the snapshot was read from: the index's data stamp and the journal's size.
    stamp: tuple = ()


def read_snapshot(store: Any, last_stamp: tuple = (), on_progress: Any = None) -> Optional[TidySnapshot]:
    """Worker body: everything the page shows, read once - or None when nothing moved.

    The Space Report pane's rule (`reports_view._report_snapshot`): the index's data
    stamp (`MAX(indexed_at)`) only moves when a run touches a file, so a visit that
    finds it unchanged - and no batch moved or put back since - reads nothing more.
    """
    from app.reports.common import report_generated_at

    journal = store.conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(put_back_at), 0) FROM tidy_journal").fetchone()
    stamp = (report_generated_at(store), int(journal[0]), int(journal[1]))
    if last_stamp and stamp == last_stamp:
        return None
    from app.reports.space import coverage_sentence, find_source_uniqueness, hash_coverage
    from app.storage.side_counts import saved_roots
    from app.tidy import find, remove
    from app.ui.presenter.tidy import TidyCounts, only_copy_document, overview_document

    def stage(text: str) -> None:
        if on_progress is not None:
            on_progress(text)

    stage("Reading what is indexed...")
    overview = find.overview(store, saved_roots(store))
    coverage = hash_coverage(store)
    stage("Finding exact copies...")
    copy_sets, copy_spare = find.copy_totals(store)
    copies = find.copy_sets(store)
    stage("Finding similar photos...")
    similar = find.similar_sets(store, limit=10 ** 9)
    stage("Finding large files and leftovers...")
    large = find.large_files(store)
    left = find.leftovers(store)
    uniqueness = find_source_uniqueness(store)
    batches = remove.batches(store)
    here = next((u.file_count for u in uniqueness if u.kind == "local"), 0)
    counts = TidyCounts(
        files_here=overview.files_here, drives=len(overview.drives), copy_sets=copy_sets,
        copy_spare=copy_spare, similar_sets=len(similar), large=len(large.files),
        leftovers=len(left.files), only_here=here, batches=len(batches))
    return TidySnapshot(
        stamp=stamp,
        counts=counts,
        overview=overview_document(overview, coverage_sentence(coverage["files_total"],
                                                               coverage["files_compared"])),
        only=only_copy_document(uniqueness),
        sets={"copies": copies, "similar": similar[:find.SETS_SHOWN], "large": [large],
              "leftovers": [left]},
        batches=batches)


def read_quality(tset: Any) -> tuple[str, dict]:
    """Worker body: the photos' pixels and sharpness, for "Leasha picks" on a similar set."""
    from app.tidy.keep import qualities

    return tset.key, qualities(tset)


def read_file_sets(store: Any, path: str) -> tuple[str, list]:
    """Worker body: D5, the sets "Find copies of this..." opens."""
    from app.tidy.find import sets_for_file

    return path, sets_for_file(store, path)


class HistoryPane(QWidget):
    """Every batch Tidy moved, newest first, with Put back."""

    put_back = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.batches = QListWidget()
        self.batches.setAccessibleName("Batches moved to the Recycle Bin")
        self.empty = QLabel("Tidy has not moved anything yet.")
        self.empty.setObjectName("resultsSummary")
        self.button = QPushButton("Put back")
        self.button.setToolTip("Return every file of the chosen batch to where it was. A file "
                               "whose old place is taken by another file is left in the Recycle Bin.")
        self.button.clicked.connect(self._put_back)
        self.batches.currentItemChanged.connect(lambda *_: self._enable())
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.empty)
        layout.addWidget(self.batches, 1)
        layout.addLayout(row)
        self._enable()

    def set_batches(self, batches: list) -> None:
        from app.ui.presenter.tidy import batch_line

        self.batches.clear()
        for b in batches:
            item = QListWidgetItem(batch_line(b))
            item.setData(_BATCH, b.batch_id)
            item.setData(_BATCH + 1, b.waiting)
            self.batches.addItem(item)
        self.empty.setVisible(not batches)
        self._enable()

    def _enable(self) -> None:
        item = self.batches.currentItem()
        self.button.setEnabled(item is not None and bool(item.data(_BATCH + 1)))

    def _put_back(self) -> None:
        item = self.batches.currentItem()
        if item is not None:
            self.put_back.emit(str(item.data(_BATCH)))
