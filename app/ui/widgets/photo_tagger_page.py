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

from typing import Any, Optional

from PyQt6.QtCore import QMimeData, QSize, Qt, QThreadPool, pyqtSignal
from PyQt6.QtGui import QDrag, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFileDialog, QGridLayout,
    QHBoxLayout, QInputDialog, QLabel, QListView, QListWidget, QListWidgetItem,
    QMenu, QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from app.core.logging import logger
from app.ui.widgets.buttons import style_all

__all__ = ["PhotoTaggerPage"]

_log = logger.bind(component="ui.photo_tagger")

#: Same cell shape as `thumbnail_grid.THUMB_CELL` - one house style for every
#: photo-grid surface in the application, so a person's eye does not have to
#: re-learn a new layout switching between the results grid and this page.
CELL = 240
ROLE_PILE_ID = int(Qt.ItemDataRole.UserRole)
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

    combine_requested = pyqtSignal(int, int)          # source_pile_id, target_pile_id

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

    decided = pyqtSignal(int, bool)          # face_id, accept

    def __init__(self, suggestion: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._face_id = int(suggestion.face_id)

        self._picture = QLabel()
        self._picture.setFixedSize(72, 72)
        self._picture.setAlignment(Qt.AlignmentFlag.AlignCenter)

        question = QLabel(f"Is this {suggestion.pile_name}?")
        question.setWordWrap(True)
        question.setFixedWidth(96)

        yes = QPushButton("Yes")
        yes.setToolTip(f"Adds this photo to {suggestion.pile_name}.")
        yes.clicked.connect(lambda: self.decided.emit(self._face_id, True))
        no = QPushButton("No")
        no.setToolTip(
            "Leasha will not guess this one on its own again - you can "
            "still place it by hand from the pile it belongs to.")
        no.clicked.connect(lambda: self.decided.emit(self._face_id, False))

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

    def set_picture(self, image: Any) -> None:
        if image is None:
            return
        pixmap = QPixmap.fromImage(image)
        if not pixmap.isNull():
            self._picture.setPixmap(pixmap.scaled(
                72, 72, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))


class PhotoTaggerPage(QWidget):
    """Grid of piles, biggest first. Click to name, drag to combine."""

    #: A photo was opened from the "manage faces" dialog - the page has no
    #: own preview; the opener (wherever this widget is placed) decides what
    #: "open" means, the same delegation `ThumbnailGrid.opened` already uses.
    opened = pyqtSignal(str)

    def __init__(self, store: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._store = store
        self._pool = QThreadPool.globalInstance()
        self._generation = 0

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
        self._suggestions_holder = QWidget()
        self._suggestions_holder.setLayout(self._suggestions_row)
        self._suggestions_holder.setVisible(False)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addLayout(top_bar)
        layout.addWidget(self._suggestions_holder)
        layout.addWidget(self._list, stretch=1)
        layout.addWidget(self._empty_note)

        self.reload()
        # The button system (widgets/buttons.py): every action button in
        # here gets its icon, its kind and its natural width.
        style_all(self)

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

    def _piles_ready(self, piles: Any, generation: int) -> None:
        if generation != self._generation:
            return                               # a later reload won
        self._list.clear()
        piles = list(piles or [])
        self._empty_note.setVisible(not piles)
        self._list.setVisible(bool(piles))

        placeholder = self.style().standardIcon(
            self.style().StandardPixmap.SP_DirIcon)
        for rank, pile in enumerate(piles, start=1):
            item = QListWidgetItem(placeholder, _pile_label(pile, rank))
            item.setData(ROLE_PILE_ID, pile.id)
            item.setToolTip(
                "Click to name this person. Drag onto another pile if it "
                "is the same person." if not pile.name else
                f"{pile.name}. Right-click for more options.")
            self._list.addItem(item)

        self._load_crops(piles, generation)

    def _load_crops(self, piles: Any, generation: int) -> None:
        from app.ui.thumbnail_loader import decode_face_crop
        from app.ui.workers import CallableWorker, run

        for index, pile in enumerate(piles):
            if not pile.samples:
                continue
            sample = pile.samples[0]
            worker = CallableWorker(
                decode_face_crop, sample.path, sample.bbox,
                component="ui.photo_tagger")
            worker.signals.finished.connect(
                lambda image, i=index, g=generation: self._crop_ready(i, image, g))
            worker.signals.failed.connect(
                lambda _error, i=index, g=generation: None)
            run(self._pool, worker)

    def _crop_ready(self, index: int, image: Any, generation: int) -> None:
        if generation != self._generation:
            return
        if index < 0 or index >= self._list.count() or image is None:
            return
        pixmap = QPixmap.fromImage(image)
        if pixmap.isNull():
            return
        self._list.item(index).setIcon(QIcon(pixmap))

    # -- suggestions: "Is this Daddy?" (section 2c) --------------------------

    def _suggestions_ready(self, suggestions: Any, generation: int) -> None:
        if generation != self._generation:
            return                               # a later reload won
        while self._suggestions_row.count() > 1:      # keep the trailing stretch
            item = self._suggestions_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        suggestions = list(suggestions or [])
        self._suggestions_holder.setVisible(bool(suggestions))
        if not suggestions:
            return

        from app.ui.thumbnail_loader import decode_face_crop
        from app.ui.workers import CallableWorker, run

        for suggestion in suggestions:
            chip = _SuggestionChip(suggestion, self)
            chip.decided.connect(self._on_suggestion_decided)
            self._suggestions_row.insertWidget(
                self._suggestions_row.count() - 1, chip)

            worker = CallableWorker(
                decode_face_crop, suggestion.path, suggestion.bbox,
                component="ui.photo_tagger.suggestions")
            worker.signals.finished.connect(
                lambda image, c=chip, g=generation:
                c.set_picture(image) if g == self._generation else None)
            worker.signals.failed.connect(lambda _error: None)
            run(self._pool, worker)

    def _on_suggestion_decided(self, face_id: int, accept: bool) -> None:
        """The chip's Yes/No. Declining does not delete anything - see
        `SqliteStore.confirm_suggestion`'s own docstring: the face just
        returns to the unclustered pool."""
        from app.ui.workers import CallableWorker, run

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

        worker = CallableWorker(
            self._store.rename_pile, pile_id, name, component="ui.photo_tagger")
        worker.signals.finished.connect(lambda _r: self.reload())
        worker.signals.failed.connect(
            lambda error: _warn_write_failed(self, "Could not rename", error))
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

        worker = CallableWorker(
            self._store.combine_piles, source_id, target_id,
            component="ui.photo_tagger")
        worker.signals.finished.connect(lambda _r: self.reload())
        worker.signals.failed.connect(
            lambda error: _warn_write_failed(self, "Could not combine", error))
        run(self._pool, worker)

    # -- context menu: forget, manage faces ----------------------------------

    def _on_context_menu(self, point: Any) -> None:
        item = self._list.itemAt(point)
        if item is None:
            return
        pile_id = int(item.data(ROLE_PILE_ID))
        name = item.text().split(" — ", 1)[0]

        menu = QMenu(self)
        rename_action = menu.addAction("Name this person…")
        manage_action = menu.addAction("Manage the faces in this pile…")
        forget_action = menu.addAction("Forget this person…")
        chosen = menu.exec(self._list.mapToGlobal(point))

        if chosen is rename_action:
            self._rename(pile_id, item.text())
        elif chosen is manage_action:
            self._manage_faces(pile_id)
        elif chosen is forget_action:
            self._forget(pile_id, name)

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
        dialog = _ManageFacesDialog(self._store, pile_id, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.reload()

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

    def chosen_year(self) -> int:
        low, high = sorted((self._from.value(), self._to.value()))
        return (low + high) // 2


class _ManageFacesDialog(QDialog):
    """Every face in one pile, each with Remove and a multi-select Split.
    Work order 0j section 2b."""

    def __init__(self, store: Any, pile_id: int, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Manage faces")
        self._store = store
        self._pile_id = pile_id
        self._checks: list[Any] = []

        self._grid_holder = QWidget()
        self._grid = QGridLayout(self._grid_holder)

        remove_button = QPushButton("Remove selected from this pile")
        remove_button.setToolTip(
            "The selected faces go back to being unsorted - they are not "
            "deleted, and Leasha may group them again later.")
        remove_button.clicked.connect(self._remove_selected)

        split_button = QPushButton("Move selected to a new pile")
        split_button.setToolTip(
            "The selected faces become their own, unnamed pile - use this "
            "when a pile has more than one person mixed together.")
        split_button.clicked.connect(self._split_selected)

        close_button = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close_button.rejected.connect(self.reject)
        close_button.accepted.connect(self.accept)

        buttons = QHBoxLayout()
        buttons.addWidget(remove_button)
        buttons.addWidget(split_button)
        buttons.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self._grid_holder)
        layout.addLayout(buttons)
        layout.addWidget(close_button)

        self._load()
        # The button system (widgets/buttons.py): every action button in
        # here gets its icon, its kind and its natural width.
        style_all(self)

    def _load(self) -> None:
        from app.ui.thumbnail_loader import decode_face_crop
        from app.ui.workers import CallableWorker, run

        faces = []
        try:
            faces = self._store.conn.execute(
                "SELECT id, file_id, bbox_x, bbox_y, bbox_w, bbox_h "
                "FROM faces WHERE pile_id = ?", (self._pile_id,)).fetchall()
        except Exception as exc:                     # noqa: BLE001
            _log.warning("could not list faces for pile {}: {}", self._pile_id, exc)
            return

        pool = QThreadPool.globalInstance()
        from PyQt6.QtWidgets import QCheckBox

        for index, row in enumerate(faces):
            box = QCheckBox()
            box.setAccessibleName(f"Select face {row['id']}")
            box.setToolTip(
                "Tick this face, then Remove or Move above to correct a "
                "pile that has the wrong person mixed into it.")
            box.setProperty("face_id", int(row["id"]))
            self._checks.append(box)
            self._grid.addWidget(box, index, 0)

            picture = QLabel()
            picture.setFixedSize(96, 96)
            self._grid.addWidget(picture, index, 1)

            file_row = self._store.conn.execute(
                "SELECT path FROM files WHERE id = ?", (row["file_id"],)).fetchone()
            path = file_row["path"] if file_row else ""
            bbox = (row["bbox_x"], row["bbox_y"], row["bbox_w"], row["bbox_h"])
            worker = CallableWorker(
                decode_face_crop, path, bbox, component="ui.photo_tagger.manage")
            worker.signals.finished.connect(
                lambda image, lbl=picture: self._show(lbl, image))
            run(pool, worker)

    @staticmethod
    def _show(label: QLabel, image: Any) -> None:
        if image is None:
            return
        pixmap = QPixmap.fromImage(image)
        if not pixmap.isNull():
            label.setPixmap(pixmap.scaled(
                96, 96, Qt.AspectRatioMode.KeepAspectRatio))

    def _selected_face_ids(self) -> list[int]:
        return [int(box.property("face_id")) for box in self._checks if box.isChecked()]

    def _remove_selected(self) -> None:
        from app.ui.workers import CallableWorker, run

        face_ids = self._selected_face_ids()

        def _remove_all() -> None:
            # One worker for the whole selection, not one per face - a
            # dialog's worth of ticked boxes is a handful of rows, and this
            # keeps the same one-bad-item-does-not-lose-the-rest tolerance
            # the direct-call version had.
            for face_id in face_ids:
                try:
                    self._store.remove_face_from_pile(face_id)
                except Exception as exc:              # noqa: BLE001
                    _log.warning("could not remove face {}: {}", face_id, exc)

        worker = CallableWorker(_remove_all, component="ui.photo_tagger.manage")
        worker.signals.finished.connect(lambda _r: self.accept())
        worker.signals.failed.connect(
            lambda error: _warn_write_failed(self, "Could not remove", error))
        run(QThreadPool.globalInstance(), worker)

    def _split_selected(self) -> None:
        selected = self._selected_face_ids()
        if not selected:
            return
        from app.ui.workers import CallableWorker, run

        worker = CallableWorker(
            self._store.split_pile, selected, component="ui.photo_tagger.manage")
        worker.signals.finished.connect(lambda _r: self.accept())
        worker.signals.failed.connect(
            lambda error: _warn_write_failed(self, "Could not split", error))
        run(QThreadPool.globalInstance(), worker)
