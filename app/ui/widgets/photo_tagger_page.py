r"""The Photo Tagger page - naming people, the Google Photos way. Work order
0j (`202626270512`) section 2.

Layer: L5

**Designed for an eight-year-old** (section 2a's own words) - this is the one
indexing chore a child does voluntarily, so every label on this page follows
the tab-one plain-words rules and every control states its effect in its
tooltip (the standing §6a rule this whole codebase holds every surface to).

**Automatic and identity stay separate here too.** This page only ever shows
what clustering already found (`SqliteStore.piles_with_counts`) and only ever
calls `SqliteStore.rename_pile`/`combine_piles`/`split_pile`/
`remove_face_from_pile`/`forget_person` in direct response to something a
person clicked - nothing here infers or suggests a name on its own; the
learning loop's own suggestions (section 2c) surface as explicit "Is this
Daddy?" yes/no chips, never a silent auto-rename.

**Every decode is a worker**, the same M11 pattern `thumbnail_grid.py`
already established for exactly this reason: a page painting a hundred face
crops must never block the interface thread doing it.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QMimeData, QSize, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QDrag, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFrame,
    QGridLayout,
    QHBoxLayout, QInputDialog, QLabel, QListView, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from app.core.logging import logger
from app.ui.widgets.buttons import style_all
from app.ui.widgets.face_crops import (blank_tile, cached_face_crop, face_key, remember,
                                       remembered, round_pixmap)
from app.ui.widgets.number_field import fit_all as fit_number_fields
from app.ui.qtsip import open_menu

#: How often the page looks for faces grouped since it last read them, while
#: it is on screen. 2026-10-04.
FOLLOW_MS = 30_000

__all__ = ["PhotoTaggerPage"]

_log = logger.bind(component="ui.photo_tagger")

#: Same cell shape as `thumbnail_grid.THUMB_CELL` - one house style for every
#: photo-grid surface in the application, so a person's eye does not have to
#: re-learn a new layout switching between the results grid and this page.
CELL = 240
#: The face id on an item in the manage dialog.
ROLE_FACE_ID = int(Qt.ItemDataRole.UserRole) + 7
#: 2026-10-05: a suggestion chip's text width, and the least a Yes or No may
#: shrink to - wide enough for a name on two lines and the words on the buttons.
CHIP_WIDTH = 128
CHIP_BUTTON_MIN = 56
ROLE_PILE_ID = int(Qt.ItemDataRole.UserRole)
#: The face a pile's tile shows, or has asked for (`face_crops.face_key`).
ROLE_FACE_KEY = int(Qt.ItemDataRole.UserRole) + 8
ROLE_MIME = "application/x-leasha-pile-id"


def _warn_write_failed(widget: QWidget, title: str, error: Any) -> None:
    """The shared failure path for every store write on this page and its
    dialog - logs, then tells the person plainly rather than leaving the
    click looking like it did nothing."""
    _log.warning("{}: {}", title, error)
    QMessageBox.warning(widget, title, str(error))


def _pile_label(pile: Any, rank: int) -> str:
    """Section 2a: "Person 1 — 47 photos" for an unnamed pile, the name
    itself for a named one. `rank` is 1-based position in the biggest-first
    list `piles_with_counts` already returns - not stored anywhere, because
    an unnamed pile has no identity of its own to remember a number for."""
    if pile.name:
        return f"{pile.name} — {pile.face_count} photo(s)"
    return f"Person {rank} — {pile.face_count} photo(s)"


class _PileList(QListWidget):
    """A `QListWidget` whose drop means "combine onto", not "reorder".

    Work order 0j section 2b: "Combine (drag pile onto pile)". Qt's own
    `InternalMove` drag-drop mode reorders rows in place, which is a
    different feature; this overrides the drop itself so a drag ending on
    another pile's cell asks the page to combine the two rather than
    rearranging the grid.
    """

    combine_requested = Signal(int, int)          # source_pile_id, target_pile_id

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def startDrag(self, actions: Any) -> None:        # noqa: N802 - Qt override
        item = self.currentItem()
        if item is None:
            return
        pile_id = item.data(ROLE_PILE_ID)
        mime = QMimeData()
        mime.setData(ROLE_MIME, str(int(pile_id)).encode("ascii"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        icon = item.icon()
        if not icon.isNull():
            drag.setPixmap(icon.pixmap(QSize(64, 64)))
        drag.exec(Qt.DropAction.MoveAction)

    def dragEnterEvent(self, event: Any) -> None:      # noqa: N802 - Qt override
        if event.mimeData().hasFormat(ROLE_MIME):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event: Any) -> None:        # noqa: N802 - Qt override
        if event.mimeData().hasFormat(ROLE_MIME):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event: Any) -> None:            # noqa: N802 - Qt override
        mime = event.mimeData()
        if not mime.hasFormat(ROLE_MIME):
            super().dropEvent(event)
            return
        target_item = self.itemAt(event.position().toPoint()
                                   if hasattr(event, "position") else event.pos())
        source_id = int(bytes(mime.data(ROLE_MIME)).decode("ascii"))
        if target_item is None:
            event.ignore()
            return
        target_id = int(target_item.data(ROLE_PILE_ID))
        event.acceptProposedAction()
        if source_id != target_id:
            self.combine_requested.emit(source_id, target_id)


class _SuggestionChip(QWidget):
    """One "Is this <name>?" chip - section 2c's learning-loop queue. A
    face crop, the question in the pile's own name, and a plain Yes/No -
    the "explicit... never a silent auto-rename" promise this module's own
    docstring makes, given a face."""

    decided = Signal(int, bool)          # face_id, accept

    def __init__(self, suggestion: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._face_id = int(suggestion.face_id)

        self._picture = QLabel()
        self._picture.setFixedSize(72, 72)
        self._picture.setAlignment(Qt.AlignmentFlag.AlignCenter)

        question = QLabel(f"Is this {suggestion.pile_name}?")
        question.setWordWrap(True)
        # 2026-10-05, the owner's screenshot: at 96 the name was cut ("Is
        # this Sarit") and Yes and No were squeezed to blobs with no words.
        question.setFixedWidth(CHIP_WIDTH)
        question.setAlignment(Qt.AlignmentFlag.AlignHCenter)

        yes = QPushButton("Yes")
        yes.setToolTip(f"Adds this photo to {suggestion.pile_name}.")
        yes.clicked.connect(lambda: self.decided.emit(self._face_id, True))
        no = QPushButton("No")
        no.setToolTip(
            "Leasha will not guess this one on its own again - you can "
            "still place it by hand from the pile it belongs to.")
        no.clicked.connect(lambda: self.decided.emit(self._face_id, False))

        for button in (yes, no):
            button.setMinimumWidth(CHIP_BUTTON_MIN)
        buttons = QHBoxLayout()
        buttons.addWidget(yes)
        buttons.addWidget(no)

        layout = QVBoxLayout(self)
        layout.addWidget(self._picture, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(question, alignment=Qt.AlignmentFlag.AlignHCenter)
        layout.addLayout(buttons)
        # The button system (widgets/buttons.py): every action button in
        # here gets its icon, its kind and its natural width.
        style_all(self)

    @property
    def face_id(self) -> int:
        return self._face_id

    def set_picture(self, image: Any) -> None:
        if image is None:
            return
        self.set_pixmap(QPixmap.fromImage(image))

    def set_pixmap(self, pixmap: QPixmap) -> None:
        if not pixmap.isNull():
            self._picture.setPixmap(round_pixmap(pixmap).scaled(
                72, 72, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))


class PhotoTaggerPage(QWidget):
    """Grid of piles, biggest first. Click to name, drag to combine."""

    #: A photo was opened from the "manage faces" dialog - the page has no
    #: own preview; the opener (wherever this widget is placed) decides what
    #: "open" means, the same delegation `ThumbnailGrid.opened` already uses.
    opened = Signal(str)

    def __init__(self, store: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._pool = QThreadPool.globalInstance()
        self._generation = 0
        self._pile_names: dict[int, str] = {}
        #: The piles as last read, by id - what an instant rename or combine
        #: works out its new label from before the store has answered.
        self._piles: dict[int, Any] = {}
        #: 2026-10-05: face crops kept beside the photo thumbnails, made once.
        data = getattr(store, "db_path", None)
        self._faces_dir = Path(data).parent / "thumbs" / "faces" if data else None
        # 2026-10-04, the owner: "the pictures for naming should be updated
        # periodically if not live". Grouping now happens during a run
        # (`Pipeline._maybe_detect_faces`), so the page looks every
        # `FOLLOW_MS` while it is on screen - four counts, on a worker - and
        # re-reads the piles only when they changed, so it does nothing while
        # nothing happens and never resets a list somebody is working in.
        self._stamp: Optional[tuple] = None
        self._follow = QTimer(self)
        self._follow.setInterval(FOLLOW_MS)
        self._follow.timeout.connect(self._check_for_new_faces)

        title = QLabel("Name the people in your photos")
        title.setObjectName("photoTaggerTitle")
        subtitle = QLabel(
            "Leasha has grouped similar faces together automatically - "
            "nothing is named until you name it. Click a pile to name it; "
            "drag one pile onto another if they are the same person.")
        subtitle.setWordWrap(True)

        self._list = _PileList(self)
        self._list.setViewMode(QListView.ViewMode.IconMode)
        self._list.setIconSize(QSize(CELL - 20, CELL - 20))
        self._list.setGridSize(QSize(CELL, CELL))
        self._list.setResizeMode(QListView.ResizeMode.Adjust)
        self._list.setMovement(QListView.Movement.Static)
        self._list.setSpacing(6)
        self._list.setWordWrap(True)
        self._list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._list.itemActivated.connect(self._on_activated)
        self._list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._list.customContextMenuRequested.connect(self._on_context_menu)
        self._list.combine_requested.connect(self._combine)

        self._empty_note = QLabel(
            "No piles yet. Turn on \"Recognise people in photos on this "
            "computer\" in Settings, then index or re-index your photos.")
        self._empty_note.setWordWrap(True)
        self._empty_note.setVisible(False)

        self._batch_era_button = QPushButton("Set roughly when a folder of scans is from…")
        self._batch_era_button.setToolTip(
            "For old scanned photos with no camera date - pick a folder and "
            "roughly which years, and every photo in it that has no better "
            "date gets one. A camera's own date is never overridden.")
        self._batch_era_button.clicked.connect(self._open_batch_era_dialog)

        top_bar = QHBoxLayout()
        top_bar.addWidget(self._batch_era_button)
        top_bar.addStretch(1)

        #: Section 2c: "Is this Daddy?" - a row of chips above the grid,
        #: nothing here until `reload()` finds a suggestion to ask about.
        self._suggestions_row = QHBoxLayout()
        self._suggestions_row.addStretch(1)
        strip = QWidget()
        strip.setLayout(self._suggestions_row)
        self._suggestions_strip = strip
        # 2026-10-05, the owner: "that window is not maximizing or scaling
        # properly it is not scrolling on the bottom too". The chips sat in a
        # plain row, so twenty of them made the window at least ~1,900 px
        # wide - wider than the screen - and its bottom, the grid's last row
        # and scroll bar with it, fell off the screen. They scroll sideways now.
        self._suggestions_holder = QScrollArea()
        self._suggestions_holder.setWidget(strip)
        self._suggestions_holder.setWidgetResizable(True)
        self._suggestions_holder.setFrameShape(QFrame.Shape.NoFrame)
        self._suggestions_holder.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._suggestions_holder.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._suggestions_holder.setVisible(False)

        # 2026-10-05, the owner: "need to mass accept names as most cases the
        # system was right". One button answers Yes to every waiting question -
        # for everyone, or for one person - after saying how many.
        self._waiting = QLabel("")
        self._accept_all = QPushButton("Accept all")
        self._accept_all.setToolTip(
            "Says Yes to every waiting \"Is this ...?\" - for everyone, or for one "
            "person. You are asked first, with the numbers.")
        self._accept_menu = QMenu(self._accept_all)
        self._accept_all.setMenu(self._accept_menu)
        self._suggestion_counts: list[tuple[int, str, int]] = []
        accept_bar = QHBoxLayout()
        accept_bar.addWidget(self._waiting)
        accept_bar.addWidget(self._accept_all)
        accept_bar.addStretch(1)
        self._accept_bar = QWidget()
        self._accept_bar.setLayout(accept_bar)
        self._accept_bar.setVisible(False)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addLayout(top_bar)
        layout.addWidget(self._accept_bar)
        layout.addWidget(self._suggestions_holder)
        layout.addWidget(self._list, stretch=1)
        layout.addWidget(self._empty_note)
        # 2026-10-05, the UI review: with no piles the grid is hidden, and the
        # room it left was shared out between the title, the two sentences and
        # the button - four lines scattered down an empty window. This takes
        # that room instead; while the grid shows, the grid's stretch wins.
        layout.addStretch(0)

        self.reload()
        # The button system (widgets/buttons.py): every action button in
        # here gets its icon, its kind and its natural width.
        style_all(self)

    # -- following a run ---------------------------------------------------

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt's name
        super().showEvent(event)
        self._follow.start()
        self._check_for_new_faces()

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt's name
        self._follow.stop()
        super().hideEvent(event)

    def _check_for_new_faces(self) -> None:
        from app.ui.workers import CallableWorker, run

        worker = CallableWorker(self._store.faces_stamp, component="ui.photo_tagger.follow")
        worker.signals.finished.connect(self._stamp_ready)
        run(self._pool, worker)

    def _stamp_ready(self, stamp: Any) -> None:
        stamp = tuple(stamp or ())
        if stamp == self._stamp:
            return
        first = self._stamp is None
        self._stamp = stamp
        # The constructor has just read the piles; the first stamp only
        # records what that read saw.
        if not first and not self._busy_naming():
            self.reload()

    def _busy_naming(self) -> bool:
        """Whether somebody is mid-edit in the list - then the next tick reloads."""
        state = self._list.state()
        return state == self._list.State.EditingState or (
            self._list.hasFocus() and bool(self._list.selectedItems()))

    # -- loading ----------------------------------------------------------

    def reload(self) -> None:
        """Re-read every pile - and every pending suggestion - from the
        store. Worker, always - these are real queries over
        `faces`/`piles`, not free."""
        from app.ui.workers import CallableWorker, run

        self._generation += 1
        generation = self._generation
        worker = CallableWorker(
            self._store.piles_with_counts, component="ui.photo_tagger")
        worker.signals.finished.connect(
            lambda piles, g=generation: self._piles_ready(piles, g))
        worker.signals.failed.connect(
            lambda _error, g=generation: self._piles_ready([], g))
        run(self._pool, worker)

        suggestions_worker = CallableWorker(
            self._store.pending_suggestions, component="ui.photo_tagger.suggestions")
        suggestions_worker.signals.finished.connect(
            lambda suggestions, g=generation: self._suggestions_ready(suggestions, g))
        suggestions_worker.signals.failed.connect(
            lambda _error, g=generation: self._suggestions_ready([], g))
        run(self._pool, suggestions_worker)

        counts_worker = CallableWorker(
            self._store.suggestion_counts, component="ui.photo_tagger.suggestions")
        counts_worker.signals.finished.connect(
            lambda counts, g=generation: self._counts_ready(counts, g))
        counts_worker.signals.failed.connect(
            lambda _error, g=generation: self._counts_ready([], g))
        run(self._pool, counts_worker)

    def _piles_ready(self, piles: Any, generation: int) -> None:
        r"""Bring the grid up to date **in place**.

        2026-10-05, the owner: "the photos flash folder then the picture etc
        even when tagging". This used to clear the list and add every pile
        again with a folder icon, then cut every face out of its photo again.
        Now a pile still there keeps its tile, its picture and its place on
        screen; only its words change. A pile gone is taken out, a new one put
        in, and a face already seen this session shows at once.
        """
        if generation != self._generation:
            return                               # a later reload won
        piles = list(piles or [])
        self._piles = {pile.id: pile for pile in piles}
        self._pile_names = {pile.id: pile.name or "" for pile in piles}
        self._empty_note.setVisible(not piles)
        self._list.setVisible(bool(piles))

        wanted = {pile.id for pile in piles}
        scroll = self._list.verticalScrollBar().value()
        current = self._list.currentItem()
        current_id = current.data(ROLE_PILE_ID) if current is not None else None
        self._list.setUpdatesEnabled(False)
        try:
            kept: dict[int, QListWidgetItem] = {}
            for row in range(self._list.count() - 1, -1, -1):
                pile_id = self._list.item(row).data(ROLE_PILE_ID)
                if pile_id in wanted and pile_id not in kept:
                    kept[pile_id] = self._list.item(row)
                else:
                    self._list.takeItem(row)
            for rank, pile in enumerate(piles, start=1):
                item = kept.get(pile.id)
                if item is None:
                    item = QListWidgetItem(blank_tile(CELL - 20, round_=True), "")
                    item.setData(ROLE_PILE_ID, pile.id)
                    self._list.insertItem(rank - 1, item)
                elif self._list.row(item) != rank - 1:
                    self._list.insertItem(rank - 1, self._list.takeItem(self._list.row(item)))
                self._dress(item, pile, rank)
            if current_id in wanted:
                self._list.setCurrentItem(self._item_for(current_id))
        finally:
            self._list.setUpdatesEnabled(True)
        self._list.verticalScrollBar().setValue(scroll)

        self._load_crops(piles)

    def _dress(self, item: QListWidgetItem, pile: Any, rank: int) -> None:
        """A pile's words - set only when they changed, so nothing repaints for nothing."""
        label = _pile_label(pile, rank)
        if item.text() != label:
            item.setText(label)
        tip = ("Click to name this person. Drag onto another pile if it "
               "is the same person." if not pile.name else
               f"{pile.name}. Right-click for more options.")
        if item.toolTip() != tip:
            item.setToolTip(tip)

    def _item_for(self, pile_id: int) -> Optional[QListWidgetItem]:
        for row in range(self._list.count()):
            item = self._list.item(row)
            if item.data(ROLE_PILE_ID) == pile_id:
                return item
        return None

    def _load_crops(self, piles: Any) -> None:
        r"""Each pile's face: from memory at once when it was seen this session,
        otherwise on a worker (from disk when it was made before). A tile
        whose face is unchanged is not touched; one whose face changed keeps
        its old picture until the new one is ready - never a blank between."""
        from app.ui.workers import CallableWorker, run

        for pile in piles:
            if not pile.samples:
                continue
            sample = pile.samples[0]
            key = face_key(sample.path, sample.bbox)
            item = self._item_for(pile.id)
            if item is None or item.data(ROLE_FACE_KEY) == key:
                continue                         # showing it, or already asked for it
            item.setData(ROLE_FACE_KEY, key)
            pixmap = remembered(key)
            if pixmap is not None:
                item.setIcon(QIcon(round_pixmap(pixmap)))
                continue
            worker = CallableWorker(
                cached_face_crop, sample.path, sample.bbox, self._faces_dir,
                component="ui.photo_tagger")
            worker.signals.finished.connect(
                lambda image, p=pile.id, k=key: self._crop_ready(p, k, image))
            worker.signals.failed.connect(lambda _error: None)
            run(self._pool, worker)

    def _crop_ready(self, pile_id: int, key: str, image: Any) -> None:
        """A face is ready - shown on its pile if that pile still wants it,
        and kept either way for the next time it is asked for."""
        pixmap = remember(key, image)
        item = self._item_for(pile_id)
        if pixmap is not None and item is not None and item.data(ROLE_FACE_KEY) == key:
            item.setIcon(QIcon(round_pixmap(pixmap)))

    # -- suggestions: "Is this Daddy?" (section 2c) --------------------------

    def _suggestions_ready(self, suggestions: Any, generation: int) -> None:
        r"""2026-10-05: a chip still being asked stays as it is, picture and
        all; only the answered ones go and only the new ones come. It used to
        tear the whole strip down and cut every face again on each Yes or No."""
        if generation != self._generation:
            return                               # a later reload won
        suggestions = list(suggestions or [])
        wanted = {int(s.face_id) for s in suggestions}
        chips: dict[int, _SuggestionChip] = {}
        while self._suggestions_row.count() > 1:      # keep the trailing stretch
            widget = self._suggestions_row.takeAt(0).widget()
            if widget is None:
                continue
            face_id = getattr(widget, "face_id", None)
            if face_id in wanted and face_id not in chips:
                chips[face_id] = widget
            else:
                widget.hide()
                widget.deleteLater()

        self._suggestions_holder.setVisible(bool(suggestions))
        if not suggestions:
            return

        from app.ui.workers import CallableWorker, run

        for suggestion in suggestions:
            chip = chips.get(int(suggestion.face_id))
            if chip is not None:
                self._suggestions_row.insertWidget(self._suggestions_row.count() - 1, chip)
                continue
            chip = _SuggestionChip(suggestion, self)
            chip.decided.connect(self._on_suggestion_decided)
            self._suggestions_row.insertWidget(
                self._suggestions_row.count() - 1, chip)

            key = face_key(suggestion.path, suggestion.bbox)
            pixmap = remembered(key)
            if pixmap is not None:
                chip.set_pixmap(pixmap)
                continue
            worker = CallableWorker(
                cached_face_crop, suggestion.path, suggestion.bbox, self._faces_dir,
                component="ui.photo_tagger.suggestions")
            worker.signals.finished.connect(
                lambda image, c=chip, k=key: self._chip_picture(c, k, image))
            worker.signals.failed.connect(lambda _error: None)
            run(self._pool, worker)

        # As tall as the tallest chip and its own scroll bar, so none is cut
        # off - asked of the chips, which know their height before they are
        # laid out (the strip does not, and came out a sliver).
        tallest = max((chip.sizeHint().height()
                       for chip in self._suggestions_strip.findChildren(_SuggestionChip)),
                      default=0)
        margins = self._suggestions_row.contentsMargins()
        bar = self._suggestions_holder.horizontalScrollBar().sizeHint().height()
        self._suggestions_holder.setFixedHeight(
            tallest + margins.top() + margins.bottom() + bar + 4)

    def _chip_picture(self, chip: _SuggestionChip, key: str, image: Any) -> None:
        pixmap = remember(key, image)
        if pixmap is None:
            return
        try:
            chip.set_pixmap(pixmap)
        except RuntimeError:
            pass                                 # answered and gone meanwhile

    # -- accept all (2026-10-05) ------------------------------------------------

    def _counts_ready(self, counts: Any, generation: int) -> None:
        if generation != self._generation:
            return
        self._suggestion_counts = [tuple(c) for c in counts or []]
        total = sum(c[2] for c in self._suggestion_counts)
        self._accept_bar.setVisible(total > 0)
        self._waiting.setText(f"{total:,} face(s) waiting for a Yes or No")
        self._accept_menu.clear()
        if not total:
            return
        everyone = self._accept_menu.addAction(f"Everyone ({total:,})")
        everyone.triggered.connect(lambda: self.accept_all(None))
        self._accept_menu.addSeparator()
        for pile_id, name, waiting in self._suggestion_counts:
            action = self._accept_menu.addAction(f"{name} ({waiting:,})")
            action.triggered.connect(lambda _c=False, p=pile_id: self.accept_all(p))

    def accept_all(self, pile_id: Optional[int]) -> bool:
        """Yes to every waiting suggestion - one person's, or everyone's -
        after the person has seen the numbers. False when they said no."""
        from app.ui.workers import CallableWorker, run

        chosen = [c for c in self._suggestion_counts if pile_id is None or c[0] == pile_id]
        total = sum(c[2] for c in chosen)
        if not total:
            return False
        lines = "\n".join(f"  {name}: {waiting:,}" for _p, name, waiting in chosen[:12])
        if len(chosen) > 12:
            lines += f"\n  and {len(chosen) - 12} more"
        answer = QMessageBox.question(
            self, "Accept all",
            f"File {total:,} face(s) under the person Leasha suggested?\n\n{lines}\n\n"
            f"A face filed wrongly can be taken out later - right-click the person, "
            f"then \"Manage the faces in this pile\".")
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self._accept_all.setEnabled(False)
        worker = CallableWorker(self._store.accept_all_suggestions, pile_id,
                                component="ui.photo_tagger")
        worker.signals.finished.connect(lambda _n: self._accepted())
        worker.signals.failed.connect(
            lambda error: (self._accept_all.setEnabled(True),
                           _warn_write_failed(self, "Could not accept them", error)))
        run(self._pool, worker)
        return True

    def _accepted(self) -> None:
        self._accept_all.setEnabled(True)
        self.reload()

    def _on_suggestion_decided(self, face_id: int, accept: bool) -> None:
        """The chip's Yes/No. Declining does not delete anything - see
        `SqliteStore.confirm_suggestion`'s own docstring: the face just
        returns to the unclustered pool."""
        from app.ui.workers import CallableWorker, run

        # Answered: the chip goes now, not when the store has caught up.
        for chip in self._suggestions_strip.findChildren(_SuggestionChip):
            if chip.face_id == face_id:
                self._suggestions_row.removeWidget(chip)
                chip.hide()
                chip.deleteLater()
        if all(c.isHidden() for c in self._suggestions_strip.findChildren(_SuggestionChip)):
            self._suggestions_holder.setVisible(False)
        worker = CallableWorker(
            self._store.confirm_suggestion, face_id, accept,
            component="ui.photo_tagger")
        worker.signals.finished.connect(lambda _r: self.reload())
        worker.signals.failed.connect(
            lambda error: _warn_write_failed(self, "Could not record that", error))
        run(self._pool, worker)

    # -- naming -------------------------------------------------------------

    def _on_activated(self, item: QListWidgetItem) -> None:
        self._rename(int(item.data(ROLE_PILE_ID)), item.text())

    def _rename(self, pile_id: int, current_label: str) -> None:
        current_name = current_label.split(" — ", 1)[0]
        if current_name.startswith("Person "):
            current_name = ""
        name, ok = QInputDialog.getText(
            self, "Name this person",
            "What is this person's name? Leave blank to keep it unnamed.",
            text=current_name)
        if not ok:
            return
        from app.ui.workers import CallableWorker, run

        # 2026-10-05, the owner: two groups both named "Jason" should be one.
        # Whether the name is taken is asked on a worker, and a taken name is
        # offered as a combine - confirmed, as dragging one group onto another is.
        check = CallableWorker(self._store.pile_id_named, name, exclude=pile_id,
                               component="ui.photo_tagger")
        check.signals.finished.connect(
            lambda taken, n=name: self._name_checked(pile_id, n, taken))
        check.signals.failed.connect(
            lambda error: _warn_write_failed(self, "Could not rename", error))
        run(self._pool, check)

    def _name_checked(self, pile_id: int, name: str, taken: Any) -> None:
        from app.ui.workers import CallableWorker, run

        if taken is not None:
            answer = QMessageBox.question(
                self, "Put these faces with them?",
                f"There is already a group called {name.strip()}. Put these faces "
                f"with {name.strip()}? The two groups become one.")
            if answer != QMessageBox.StandardButton.Yes:
                return
            self._show_combined(pile_id, int(taken))
            worker = CallableWorker(self._store.combine_piles, pile_id, int(taken),
                                    component="ui.photo_tagger")
            worker.signals.failed.connect(
                lambda error: _warn_write_failed(self, "Could not combine", error))
        else:
            self._show_renamed(pile_id, name)
            worker = CallableWorker(self._store.rename_pile, pile_id, name,
                                    component="ui.photo_tagger")
            worker.signals.failed.connect(
                lambda error: _warn_write_failed(self, "Could not rename", error))
        worker.signals.finished.connect(lambda _r: self.reload())
        run(self._pool, worker)

    def _combine(self, source_id: int, target_id: int) -> None:
        """Section 2b. Confirmed - a merge cannot be undone by dragging back,
        so this is exactly the kind of action non-negotiable #11's
        "destructive... state the cost and confirm" pattern applies to."""
        confirmed = QMessageBox.question(
            self, "Combine these piles?",
            "Every photo in the pile you dragged will move into the one "
            "you dropped it on, and the pile you dragged will be gone. "
            "Use this when they are the same person.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirmed != QMessageBox.StandardButton.Yes:
            return
        from app.ui.workers import CallableWorker, run

        self._show_combined(source_id, target_id)
        worker = CallableWorker(
            self._store.combine_piles, source_id, target_id,
            component="ui.photo_tagger")
        worker.signals.finished.connect(lambda _r: self.reload())
        worker.signals.failed.connect(
            lambda error: _warn_write_failed(self, "Could not combine", error))
        run(self._pool, worker)

    # -- showing a change before the store has answered (2026-10-05) -----------
    #
    # The owner wanted naming to feel like Google Photos: the label changes as
    # the name is given, and the reload that follows only confirms it in place.
    # A write that fails says so (`_warn_write_failed`) and the reload puts the
    # grid back to what the store holds.

    def _show_renamed(self, pile_id: int, name: str) -> None:
        pile, item = self._piles.get(pile_id), self._item_for(pile_id)
        if pile is None or item is None:
            return
        pile = replace(pile, name=name.strip() or None)
        self._piles[pile_id] = pile
        self._dress(item, pile, self._list.row(item) + 1)

    def _show_combined(self, source_id: int, target_id: int) -> None:
        source, target = self._piles.get(source_id), self._piles.get(target_id)
        item = self._item_for(source_id)
        if item is not None:
            self._list.takeItem(self._list.row(item))
        if source is None or target is None:
            return
        self._piles.pop(source_id, None)
        target = replace(target, face_count=target.face_count + source.face_count)
        self._piles[target_id] = target
        kept = self._item_for(target_id)
        if kept is not None:
            self._dress(kept, target, self._list.row(kept) + 1)

    # -- context menu: forget, manage faces ----------------------------------

    def _on_context_menu(self, point: Any) -> None:
        item = self._list.itemAt(point)
        if item is None:
            return
        pile_id = int(item.data(ROLE_PILE_ID))
        name = item.text().split(" — ", 1)[0]

        menu = QMenu(self)
        rename_action = menu.addAction("Name this person…")
        # 2026-10-07, the owner: "combine for sure". Dragging one group onto
        # another already did this; the menu is where it is looked for.
        combine_action = menu.addAction("Combine with another person…")
        manage_action = menu.addAction("Manage the faces in this pile…")
        forget_action = menu.addAction("Forget this person…")
        chosen = open_menu(menu, self._list.mapToGlobal(point))

        if chosen is rename_action:
            self._rename(pile_id, item.text())
        elif chosen is combine_action:
            self._combine_with(pile_id, name)
        elif chosen is manage_action:
            self._manage_faces(pile_id)
        elif chosen is forget_action:
            self._forget(pile_id, name)

    def _combine_with(self, pile_id: int, name: str) -> None:
        """Pick the group these faces belong with, confirm, combine. 2026-10-07.

        Named people first, by name - that is who somebody is looking for -
        then the groups still unnamed, in the order the page shows them."""
        others: list[tuple[str, int]] = []
        for row in range(self._list.count()):
            other = self._list.item(row)
            other_id = int(other.data(ROLE_PILE_ID))
            if other_id != pile_id:
                others.append((other.text(), other_id))
        if not others:
            QMessageBox.information(
                self, "Combine with another person",
                "There is no other group to combine this one with yet.")
            return
        unnamed = lambda label: label.startswith("Person ")       # noqa: E731
        others.sort(key=lambda entry: (unnamed(entry[0]),
                                       "" if unnamed(entry[0]) else entry[0].casefold()))
        labels = [label for label, _id in others]
        picked, ok = QInputDialog.getItem(
            self, "Combine with another person",
            f"The faces in {name} are the same person as:", labels, 0, False)
        if not ok or picked not in labels:
            return
        target_label, target_id = others[labels.index(picked)]
        target_name = target_label.split(" — ", 1)[0]
        answer = QMessageBox.question(
            self, "Combine these two?",
            f"Put the faces in {name} with {target_name}? The two groups become "
            f"one, called {target_name}. This cannot be undone from here.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        from app.ui.workers import CallableWorker, run

        self._show_combined(pile_id, target_id)
        worker = CallableWorker(self._store.combine_piles, pile_id, target_id,
                                component="ui.photo_tagger")
        worker.signals.finished.connect(lambda _r: self.reload())
        worker.signals.failed.connect(
            lambda error: _warn_write_failed(self, "Could not combine", error))
        run(self._pool, worker)

    def _forget(self, pile_id: int, name: str) -> None:
        r"""Section 2e. Guardrails: "Forget this person" deletes the name
        and, if asked, the face data. Two explicit choices, never one
        button that silently does the more destructive thing."""
        box = QMessageBox(self)
        box.setWindowTitle("Forget this person")
        box.setText(
            f"This removes the name \"{name}\" - Leasha will not call "
            "this pile that any more. What about the faces themselves?")
        keep_button = box.addButton(
            "Keep the faces, just unnamed", QMessageBox.ButtonRole.YesRole)
        delete_button = box.addButton(
            "Also delete the face data", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in (keep_button, delete_button):
            return
        from app.ui.workers import CallableWorker, run

        worker = CallableWorker(
            self._store.forget_person, pile_id,
            delete_faces=(clicked is delete_button), component="ui.photo_tagger")
        worker.signals.finished.connect(lambda _r: self.reload())
        worker.signals.failed.connect(
            lambda error: _warn_write_failed(self, "Could not forget", error))
        run(self._pool, worker)

    def _manage_faces(self, pile_id: int) -> None:
        """Section 2b's remove-from-pile and split, together - a small
        dialog listing each face in the pile with a checkbox, so a mixed
        pile can be corrected without leaving this page."""
        dialog = _ManageFacesDialog(self._store, pile_id, self,
                                    pile_name=self._pile_names.get(pile_id, ""))
        dialog.exec()
        self.reload()                            # whatever was changed in there

    # -- batch-era control (0511 section 4b's override) -----------------------

    def _open_batch_era_dialog(self) -> None:
        """Section 2d. A folder, and roughly which years - see
        `SqliteStore.apply_batch_era` for why this only ever overrides a
        hint or a blank date, never a camera's own EXIF fact."""
        folder = QFileDialog.getExistingDirectory(
            self, "Folder of scans to date")
        if not folder:
            return
        dialog = _BatchEraDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        year = dialog.chosen_year()
        from app.extract.era_hints import year_to_epoch_ns
        from app.ui.workers import CallableWorker, run

        def _dated(changed: int) -> None:
            QMessageBox.information(
                self, "Dates set",
                f"{changed} photo(s) in that folder now show as roughly {year}.")

        worker = CallableWorker(
            self._store.apply_batch_era, folder, year_to_epoch_ns(year),
            component="ui.photo_tagger")
        worker.signals.finished.connect(_dated)
        worker.signals.failed.connect(
            lambda error: _warn_write_failed(self, "Could not set the date", error))
        run(self._pool, worker)


class _BatchEraDialog(QDialog):
    """From-year / to-year, collapsed to their midpoint. Section 2d."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Roughly which years?")

        self._from = QSpinBox()
        self._from.setRange(1826, 2100)
        self._from.setValue(1998)
        self._from.setToolTip(
            "The earliest year this folder of scans is roughly from.")
        self._to = QSpinBox()
        self._to.setRange(1826, 2100)
        self._to.setValue(2002)
        self._to.setToolTip(
            "The latest year this folder of scans is roughly from.")

        row = QHBoxLayout()
        row.addWidget(QLabel("From"))
        row.addWidget(self._from)
        row.addWidget(QLabel("to"))
        row.addWidget(self._to)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Never overrides a photo's own camera date - only photos with "
            "no date, or an earlier guess, are affected."))
        layout.addLayout(row)
        layout.addWidget(buttons)
        # The button system (widgets/buttons.py): every action button in
        # here gets its icon, its kind and its natural width.
        style_all(self)
        # Number fields: typed, no arrows, a back-to-default button.
        fit_number_fields(self)

    def chosen_year(self) -> int:
        low, high = sorted((self._from.value(), self._to.value()))
        return (low + high) // 2


class _ManageFacesDialog(QDialog):
    """Every face in one group: select some, then say what they are.

    *2026-10-05, the owner: "manage the faces in the pile also the window is
    not right".* The first version put one face per row with no scrolling - a
    group of 178 faces was a dialog about 17,000 pixels tall, most of it off
    the screen - and it read the database on the window's thread, once per
    face. Now: a grid that scrolls and wraps, a window that resizes and
    maximizes, everything read on workers, and three answers for a selection -
    not this person (and never again), a new person, or another named person.
    """

    FACE = 112

    def __init__(self, store: Any, pile_id: int, parent: Optional[QWidget] = None,
                 *, pile_name: str = "") -> None:
        super().__init__(parent, Qt.WindowType.Window
                         | Qt.WindowType.WindowMaximizeButtonHint
                         | Qt.WindowType.WindowCloseButtonHint)
        who = pile_name or "this person"
        self.setWindowTitle(f"Faces of {who}" if pile_name else "Manage faces")
        self._store = store
        self._pile_id = pile_id
        self._pool = QThreadPool.globalInstance()
        self._changed = False

        intro = QLabel(
            f"Select the faces that are not {who} - click, Ctrl-click or Shift-click "
            f"for several - then choose what they are instead.")
        intro.setWordWrap(True)
        self._count = QLabel("")

        self._list = QListWidget()
        self._list.setViewMode(QListView.ViewMode.IconMode)
        self._list.setIconSize(QSize(self.FACE, self.FACE))
        self._list.setGridSize(QSize(self.FACE + 16, self.FACE + 16))
        self._list.setResizeMode(QListView.ResizeMode.Adjust)
        self._list.setMovement(QListView.Movement.Static)
        self._list.setUniformItemSizes(True)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._list.itemSelectionChanged.connect(self._selection_changed)

        select_all = QPushButton("Select all")
        select_all.setToolTip("Select every face in this group")
        select_all.clicked.connect(self._list.selectAll)
        self._not_this = QPushButton(f"Not {who}")
        self._not_this.setToolTip(
            f"The selected faces leave {who} and go back to the unsorted faces - "
            f"and Leasha will not put them with {who} again.")
        self._not_this.clicked.connect(self._not_this_person)
        self._new_person = QPushButton("Make a new person")
        self._new_person.setToolTip(
            "The selected faces become a group of their own, ready to be named - "
            "for when two people were mixed together.")
        self._new_person.clicked.connect(self._split_selected)
        self._move_to = QComboBox()
        self._move_to.setToolTip("Another person you have named")
        self._move = QPushButton("Move to")
        self._move.setToolTip("The selected faces go to the person chosen beside it.")
        self._move.clicked.connect(self._move_selected)

        actions = QHBoxLayout()
        for widget in (select_all, self._not_this, self._new_person, self._move,
                       self._move_to):
            actions.addWidget(widget)
        actions.addStretch(1)

        close_button = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_button.rejected.connect(self._close)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self._count)
        layout.addWidget(self._list, 1)
        layout.addLayout(actions)
        layout.addWidget(close_button)

        screen = self.screen().availableGeometry() if self.screen() else None
        if screen is not None:
            self.resize(int(screen.width() * 0.7), int(screen.height() * 0.75))
        self._selection_changed()
        self._load()
        # The button system (widgets/buttons.py): every action button in
        # here gets its icon, its kind and its natural width.
        style_all(self)

    # -- reading, on workers --------------------------------------------------------

    def _load(self) -> None:
        from app.ui.later import when_done
        from app.ui.workers import CallableWorker, run

        # Where the person was in the grid, so a move or a split does not
        # throw them back to the top.
        self._scroll = self._list.verticalScrollBar().value()
        self._list.clear()
        self._count.setText("Reading the faces...")
        faces = CallableWorker(self._store.faces_in_pile, self._pile_id,
                               component="ui.photo_tagger.manage")
        when_done(self, faces, finished=self._faces_ready)
        run(self._pool, faces)
        people = CallableWorker(self._store.piles_with_counts,
                                component="ui.photo_tagger.manage")
        when_done(self, people, finished=self._people_ready)
        run(self._pool, people)

    def _faces_ready(self, faces: Any) -> None:
        from app.ui.later import when_done
        from app.ui.workers import CallableWorker, run

        faces = list(faces or [])
        self._count.setText(f"{len(faces):,} face(s)")
        faces_dir = getattr(self.parent(), "_faces_dir", None)
        for face_id, path, bbox in faces:
            key = face_key(path, bbox)
            pixmap = remembered(key)
            item = QListWidgetItem(blank_tile(self.FACE) if pixmap is None else
                                   self._icon(pixmap), "")
            item.setData(ROLE_FACE_ID, int(face_id))
            item.setToolTip(Path(path).name)
            item.setSizeHint(QSize(self.FACE + 12, self.FACE + 12))
            self._list.addItem(item)
            if pixmap is not None:
                continue
            crop = CallableWorker(cached_face_crop, path, bbox, faces_dir,
                                  component="ui.photo_tagger.manage")
            when_done(self, crop,
                      finished=lambda image, it=item, k=key: self._show(it, remember(k, image)))
            run(self._pool, crop)
        self._list.doItemsLayout()
        self._list.verticalScrollBar().setValue(getattr(self, "_scroll", 0))

    def _people_ready(self, piles: Any) -> None:
        self._move_to.clear()
        for pile in piles or []:
            if pile.id != self._pile_id and pile.name:
                self._move_to.addItem(pile.name, pile.id)
        self._selection_changed()

    def _icon(self, pixmap: QPixmap) -> QIcon:
        return QIcon(pixmap.scaled(
            self.FACE, self.FACE, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))

    def _show(self, item: QListWidgetItem, pixmap: Optional[QPixmap]) -> None:
        if pixmap is None or pixmap.isNull():
            return
        try:
            item.setIcon(self._icon(pixmap))
        except RuntimeError:
            pass                                 # the list was re-read meanwhile

    # -- what the selection is ----------------------------------------------------------

    def _selected_face_ids(self) -> list[int]:
        return [int(item.data(ROLE_FACE_ID)) for item in self._list.selectedItems()]

    def _selection_changed(self) -> None:
        some = bool(self._list.selectedItems())
        self._not_this.setEnabled(some)
        self._new_person.setEnabled(some)
        self._move.setEnabled(some and self._move_to.count() > 0)

    def _apply(self, work: Any, failure: str) -> None:
        from app.ui.later import when_done
        from app.ui.workers import CallableWorker, run

        worker = CallableWorker(work, component="ui.photo_tagger.manage")
        when_done(self, worker, finished=lambda _r: self._applied(),
                  failed=lambda error: _warn_write_failed(self, failure, error))
        run(self._pool, worker)

    def _applied(self) -> None:
        self._changed = True
        self._load()

    def _not_this_person(self) -> None:
        face_ids = self._selected_face_ids()

        def work() -> None:
            for face_id in face_ids:
                try:
                    self._store.not_this_person(face_id)
                except Exception as exc:              # noqa: BLE001 - one face, not all
                    _log.warning("could not take face {} out: {}", face_id, exc)

        if face_ids:
            self._apply(work, "Could not take them out")

    def _split_selected(self) -> None:
        face_ids = self._selected_face_ids()
        if face_ids:
            self._apply(lambda: self._store.split_pile(face_ids), "Could not split")

    def _move_selected(self) -> None:
        face_ids = self._selected_face_ids()
        target = self._move_to.currentData()
        if not face_ids or target is None:
            return

        def work() -> None:
            for face_id in face_ids:
                self._store.assign_face(face_id, int(target))

        self._apply(work, "Could not move them")

    def _close(self) -> None:
        if self._changed:
            self.accept()
        else:
            self.reject()

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt's name
        if self._changed:
            self.setResult(QDialog.DialogCode.Accepted)
        super().closeEvent(event)
