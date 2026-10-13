"""'From my photos': choose a contact's photo from the faces of the person they are linked to.

Layer: L5

Order 1k 2c and 6d. The contact is linked to a named person on the photo side
(`app/contacts/people.py`, accepted by the person); this lists that person's best faces as
small pictures and hands back the one chosen. The faces are found and cropped on a worker
(rule 5); the dialog opens at once and fills in. Nothing is changed in the photo.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLabel, QListWidget, QListWidgetItem)

from app.contacts.card import Photo
from app.ui.contacts import loader
from app.ui.widgets.dialog_frame import frame

__all__ = ["pick_photo_from_person", "candidate_faces"]


def candidate_faces(hub: Any, contact_id: int, limit: int = 12) -> tuple[str, list[tuple[str, Photo]]]:
    """Worker: (the person's name or a sentence saying why there is none, [(label, photo)])."""
    from app.contacts import people  # noqa: PLC0415
    from app.contacts.enrich.faces import best_faces, face_photo  # noqa: PLC0415

    link = people.pile_link(hub, contact_id)
    if link is None or link.state != "accepted":
        return ("This contact is not linked to a named person yet. Link them on the Photos page "
                "(or accept the suggested link) and try again.", [])
    out: list[tuple[str, Photo]] = []
    for _face_id, path, bbox in best_faces(hub.store, link.pile_id, limit * 3):
        photo = face_photo(path, bbox)
        if photo is not None:
            out.append((path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1], photo))
        if len(out) >= limit:
            break
    return (link.pile_name, out)


def pick_photo_from_person(parent: Any, hub: Any, contact_id: Optional[int]) -> Optional[Photo]:
    """Modal. The chosen `Photo`, or None when cancelled or there is nothing to choose."""
    dialog = QDialog(parent)
    dialog.setWindowTitle("Photo from my photos")
    dialog.setMinimumSize(460, 360)
    layout = frame(dialog)                        # the one dialog frame (UI phase B)
    note = QLabel("Looking for this person's photos...")
    note.setWordWrap(True)
    note.setAccessibleName("Photo picker status")
    layout.addWidget(note)
    faces = QListWidget()
    faces.setViewMode(QListWidget.ViewMode.IconMode)
    faces.setIconSize(QSize(96, 96))
    faces.setResizeMode(QListWidget.ResizeMode.Adjust)
    faces.setMovement(QListWidget.Movement.Static)
    faces.setAccessibleName("Faces to choose from")
    faces.setToolTip("Choose the face to use as this contact's picture, then press OK.")
    layout.addWidget(faces, 1)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    chosen: dict[str, Photo] = {}

    def filled(result: Any) -> None:
        title, items = result
        if not items:
            note.setText(title if title.startswith("This contact") else f"No usable faces of {title} were found.")
            return
        note.setText(f"Faces of {title}. Choose one.")
        for label, photo in items:
            pix = QPixmap()
            pix.loadFromData(photo.data)
            item = QListWidgetItem(QIcon(pix), "")
            item.setToolTip(label)
            faces.addItem(item)
            chosen[str(faces.count() - 1)] = photo

    def selection() -> None:
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(faces.selectedItems()))

    faces.itemSelectionChanged.connect(selection)
    cid = -1 if contact_id is None else int(contact_id)
    loader.run_task(dialog, lambda: candidate_faces(hub, cid), filled,
                    lambda err: note.setText(getattr(err, "message", str(err))))
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    rows = faces.selectedItems()
    return chosen.get(str(faces.row(rows[0]))) if rows else None
