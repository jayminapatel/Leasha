r"""Which file of a set stays, by the keep rule the owner chose (order 1q D4). Qt-free.

Layer: L4 (tidy) - pure, except `photo_quality`, which opens a photo to read it.

**Leasha picks** - for photos, the most pixels, then the sharpest; for copies, the
one not in a Downloads, temp or recycle folder and not named like a copy
("Report (1).docx", "Copy of Report.docx"), then the oldest. **Oldest** and
**Newest** go by the modified time. **I'll choose** (`manual`) keeps nothing
and marks nothing: the owner clicks (the owner's "a method to do it manually").

Large files and leftovers are never marked by any rule (D10) - they usually exist
once, so there is nothing for a rule to keep instead.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from app.tidy.find import TidyFile, TidySet

__all__ = ["RULES", "RULE_WORDS", "DEFAULT_RULE", "keeper", "marked_by_rule", "photo_quality",
           "looks_spare", "qualities", "describe"]

RULES = ("leasha", "oldest", "newest", "manual")
RULE_WORDS = {
    "leasha": "Leasha picks",
    "oldest": "Keep the oldest",
    "newest": "Keep the newest",
    "manual": "I'll choose",
}
DEFAULT_RULE = "leasha"

_SPARE_FOLDERS = ("\\downloads\\", "\\temp\\", "\\tmp\\", "\\$recycle.bin\\", "\\appdata\\local\\temp\\")
_SPARE_NAME = re.compile(r"(^copy of )|( - copy( \(\d+\))?$)|( \(\d+\)$)", re.IGNORECASE)


def looks_spare(path: str) -> bool:
    """True for a copy in a Downloads/temp/recycle folder, or one named like a copy."""
    low = "\\" + path.replace("/", "\\").lower()
    if any(folder in low for folder in _SPARE_FOLDERS):
        return True
    stem = low.rsplit("\\", 1)[-1].rsplit(".", 1)[0]
    return bool(_SPARE_NAME.search(stem))


def keeper(tset: TidySet, rule: str,
           quality: Optional[Mapping[str, tuple[int, float]]] = None) -> Optional[TidyFile]:
    """The file the rule keeps, or None (`manual`, a list kind, or an empty set)."""
    if rule == "manual" or tset.kind not in ("copies", "similar") or not tset.files:
        return None
    files = list(tset.files)
    if rule == "oldest":
        return min(files, key=lambda f: (f.mtime_ns, f.path))
    if rule == "newest":
        return max(files, key=lambda f: (f.mtime_ns, f.path))
    if tset.kind == "similar":
        q = quality or {}
        return max(files, key=lambda f: (q.get(f.path, (0, 0.0))[0], q.get(f.path, (0, 0.0))[1],
                                         f.size_bytes, -f.mtime_ns))
    return min(files, key=lambda f: (looks_spare(f.path), f.mtime_ns, f.path))


def marked_by_rule(tset: TidySet, rule: str,
                   quality: Optional[Mapping[str, tuple[int, float]]] = None) -> set[str]:
    """The paths the rule marks for the Recycle Bin: every file of the set but the keeper."""
    kept = keeper(tset, rule, quality)
    if kept is None:
        return set()
    return {f.path for f in tset.files if f.path != kept.path}


def photo_quality(path: str) -> tuple[int, float]:
    r"""`(pixels, sharpness)` of a photo, read from the file; `(0, 0.0)` if it cannot be read.

    Sharpness is the variance of an edge filter over a 512-pixel greyscale copy - a
    blurred or shaken shot has soft edges and a low number. Comparable only between
    versions of the same picture, which is all D4 asks of it. Opens the file to read
    it and never writes.
    """
    try:
        from PIL import Image, ImageFilter, ImageStat

        with Image.open(path) as image:
            pixels = int(image.width) * int(image.height)
            grey = image.convert("L")
            grey.thumbnail((512, 512))
            edges = grey.filter(ImageFilter.FIND_EDGES)
            sharpness = float(ImageStat.Stat(edges).var[0])
    except Exception:                             # noqa: BLE001 - an unreadable photo ranks last
        return 0, 0.0
    return pixels, sharpness


def qualities(tset: TidySet) -> dict[str, tuple[int, float]]:
    """`photo_quality` for every photo of a similar set - on a worker, never the window's thread."""
    if tset.kind != "similar":
        return {}
    return {f.path: photo_quality(f.path) for f in tset.files}


def describe(tset: TidySet, rule: str, kept: Optional[TidyFile],
             quality: Optional[Mapping[str, Any]] = None) -> str:
    """One sentence on why that file stays, for the page under the set."""
    if tset.kind in ("large", "leftovers"):
        return "Nothing here is marked for you - each of these may be the only copy. Tick what can go."
    if kept is None:
        return "Nothing is marked. Click the copy to keep, or tick the ones that can go."
    if rule == "oldest":
        return f"Keeping the oldest: {kept.name}."
    if rule == "newest":
        return f"Keeping the newest: {kept.name}."
    if tset.kind == "similar":
        return f"Keeping the one with the most detail: {kept.name}. Click another to keep it instead."
    return (f"Keeping {kept.path}: it is not in Downloads or a temporary folder, and it is the "
            "oldest. Click another to keep it instead.")
