"""7c: a photo for a contact from the faces of the person it is linked to.

Layer: L7

Order 1k section 7c and 2c. Local only. A contact is linked to a named person
(`contact_pile_links`, state `accepted`); the person's faces are `faces` rows
whose `pile_id` is that pile. The best face is the largest box that can still be
cut out of its photo, so a photo moved or deleted since is skipped and the next
one is tried. The crop is padded like the Photo Tagger's (`decode_face_crop`),
square, 256 pixels, JPEG; Pillow does it so the module needs no Qt.

Faces from mail pictures have synthetic paths (`pst://...`); those cannot be opened
and are passed over.
"""

from __future__ import annotations

import io
import os
from typing import Any, Optional

from app.contacts.card import Card, Photo
from app.contacts.enrich.base import Proposal

__all__ = ["pile_of", "best_faces", "face_photo", "face_proposals"]

MARGIN = 0.35
EDGE = 256
TRIES = 5


def pile_of(store: Any, cid: int) -> Optional[int]:
    row = store.conn.execute("SELECT pile_id FROM contact_pile_links WHERE contact_id = ? AND state = 'accepted'",
                             (int(cid),)).fetchone()
    return int(row[0]) if row else None


def best_faces(store: Any, pile_id: int, limit: int = TRIES) -> list[tuple[int, str, tuple[float, float, float, float]]]:
    """`(face_id, photo path, (x, y, w, h))`, the largest boxes first."""
    rows = store.conn.execute(
        "SELECT f.id, files.path, f.bbox_x, f.bbox_y, f.bbox_w, f.bbox_h FROM faces f "
        "JOIN files ON files.id = f.file_id WHERE f.pile_id = ? "
        "ORDER BY f.bbox_w * f.bbox_h DESC, COALESCE(f.confidence, 0) DESC, f.id LIMIT ?",
        (int(pile_id), int(limit))).fetchall()
    return [(int(r[0]), str(r[1]), (r[2], r[3], r[4], r[5])) for r in rows]


def face_photo(path: str, bbox: tuple[float, float, float, float]) -> Optional[Photo]:
    """A square JPEG of one face, or None when the photo cannot be opened."""
    if not path or "://" in path or not os.path.isfile(path):
        return None
    try:
        from PIL import Image, ImageOps  # noqa: PLC0415
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            x, y, w, h = bbox
            left, top = max(0, int(x - w * MARGIN)), max(0, int(y - h * MARGIN))
            right, bottom = min(im.width, int(x + w + w * MARGIN)), min(im.height, int(y + h + h * MARGIN))
            if right <= left or bottom <= top:
                return None
            crop = im.crop((left, top, right, bottom))
            edge = min(crop.size)
            crop = crop.crop(((crop.width - edge) // 2, (crop.height - edge) // 2,
                              (crop.width - edge) // 2 + edge, (crop.height - edge) // 2 + edge))
            crop = crop.resize((EDGE, EDGE))
            out = io.BytesIO()
            crop.save(out, "JPEG", quality=85)
    except Exception:                                    # noqa: BLE001 - a bad photo is a skipped face
        return None
    return Photo("image/jpeg", out.getvalue())


def face_proposals(store: Any, cid: int, card: Card) -> list[Proposal]:
    """The best face of the linked person as a photo - only for a card with no photo."""
    if card.photo is not None:
        return []
    pile = pile_of(store, cid)
    if pile is None:
        return []
    name = store.conn.execute("SELECT name FROM piles WHERE id = ?", (pile,)).fetchone()
    label = (name[0] if name and name[0] else "a named person")
    for _fid, path, bbox in best_faces(store, pile):
        photo = face_photo(path, bbox)
        if photo is not None:
            return [Proposal(cid, "photo", "", "", photo, "face",
                             f"face of {label} in {os.path.basename(path)}", 0.7, opens=path)]
    return []
