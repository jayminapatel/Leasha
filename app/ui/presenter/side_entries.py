r"""What each page's left-hand list holds, as `SideEntry` lines. Qt-free.

Layer: L5 presenter

Order 1p (2026-10-11), D3. The counts come from `app.storage.side_counts` on a worker;
these functions only arrange them. An entry's `value` is what a click means:
`(switch, value)` written into the page's box (Files, Code - D2), or a period or a
kind (Browse). Ticks (`checked`) say what the box holds now.
"""

from __future__ import annotations

from pathlib import PureWindowsPath
from typing import Any, Iterable, Optional

from app.ui.presenter.side_entry import SideEntry

__all__ = ["files_entries", "code_entries", "browse_entries", "folder_tree", "box_holds",
           "clear_from_box", "PLACES_DEPTH", "MONTHS"]

#: How many folder levels below an indexed folder Places shows.
PLACES_DEPTH = 3

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December")

#: Icons for the commonest kinds; anything else is a plain file.
_KIND_ICONS = {
    "pdf": "file-text", "doc": "file-text", "docx": "file-text", "txt": "file-text",
    "md": "file-text", "rtf": "file-text", "odt": "file-text",
    "xls": "sheet", "xlsx": "sheet", "xlsm": "sheet", "csv": "sheet",
    "ppt": "presentation", "pptx": "presentation",
    "jpg": "image", "jpeg": "image", "png": "image", "heic": "image", "gif": "image",
    "zip": "archive", "7z": "archive", "rar": "archive",
    "msg": "mail", "eml": "mail", "pst": "mail",
    "mp4": "video", "mov": "video", "avi": "video", "mkv": "video",
}


def _icon_for(ext: str) -> str:
    return _KIND_ICONS.get(ext.lower(), "file")


def box_holds(text: str, switch: str, value: Any) -> bool:
    """Whether the box holds `switch:value` now (the Photos rule, shared)."""
    from app.ui.presenter.photos import box_has

    return box_has(text or "", switch, value)


def _norm(path: str) -> str:
    return str(path or "").replace("/", "\\").rstrip("\\")


def folder_tree(roots: Iterable[str], counts: Iterable[tuple[str, int]],
                depth: int = PLACES_DEPTH) -> list[tuple[str, str, str, int]]:
    """`(key, parent key, folder, files under it)` for each root and the folders up to
    `depth` levels below it, parents before children, largest first at each level."""
    totals: dict[str, int] = {}
    #: Each path's children, collected as the paths are made. (Looking for them afterwards, in every
    #: path, for every path, was 7 seconds for 18,000 folders - on the window's thread, at every click.)
    kids: dict[str, list[str]] = {}
    roots = [_norm(r) for r in roots if _norm(r)]
    lowered = [r.lower() for r in roots]
    for folder, count in counts:
        folder = _norm(folder)
        low = folder.lower()
        for root, low_root in zip(roots, lowered):
            if low == low_root or low.startswith(low_root + "\\"):
                parts = [p for p in folder[len(root):].split("\\") if p][:depth]
                path = root
                totals[path] = totals.get(path, 0) + int(count)
                for part in parts:
                    parent, path = path, f"{path}\\{part}"
                    if path not in totals:
                        kids.setdefault(parent, []).append(path)
                    totals[path] = totals.get(path, 0) + int(count)
                break
    out: list[tuple[str, str, str, int]] = []

    def walk(parent: str) -> None:
        for kid in sorted(kids.get(parent, ()), key=lambda p: (-totals[p], p.lower())):
            out.append((f"place:{kid}", f"place:{parent}", kid, totals[kid]))
            walk(kid)

    for root in roots:
        if root in totals:
            out.append((f"place:{root}", "", root, totals[root]))
            walk(root)
    return out


def files_entries(roots: Iterable[str], folders: Iterable[tuple[str, int]],
                  drives: Iterable[tuple[str, str, int]], kinds: Iterable[tuple[str, int]],
                  box: str = "", tree: Optional[list] = None) -> list[SideEntry]:
    """Files (D3): All files, Places, Drives, Kinds. `tree` is `folder_tree(roots, folders)` when the
    caller has already made it (once, on a worker): only the ticks depend on the box."""
    if tree is None:
        tree = folder_tree(roots, folders)
    total = sum(count for key, parent, _f, count in tree if not parent)
    entries = [SideEntry("all", "All files", icon="folder", count=total or None, kind="all",
                         tip="Take every narrowing from this list away")]
    for key, parent, folder, count in tree:
        name = folder if not parent else PureWindowsPath(folder).name
        entries.append(SideEntry(
            key, name, section="Places", icon="folder", count=count, parent=parent,
            value=("path", folder), tip=folder, checked=box_holds(box, "path", folder)))
    for name, status, count in drives:
        offline = status.lower() != "online"
        entries.append(SideEntry(
            f"drive:{name}", name, section="Drives", icon="hard-drive", count=count,
            value=("on", name),
            tip=f"{name} - {'not plugged in' if offline else 'plugged in'}",
            checked=box_holds(box, "on", name)))
    for ext, count in kinds:
        entries.append(SideEntry(
            f"kind:{ext}", ext.upper(), section="Kinds", icon=_icon_for(ext), count=count,
            value=("type", ext), tip=f"Only .{ext} files", checked=box_holds(box, "type", ext)))
    return entries


def code_entries(repos: Iterable[tuple[str, str, int]], languages: Iterable[tuple[str, int]],
                 box: str = "") -> list[SideEntry]:
    """Code (D3): All code, Repositories, Languages."""
    repos = list(repos)
    entries = [SideEntry("all", "All code", icon="code", count=sum(c for *_x, c in repos) or None,
                         kind="all", tip="Take every narrowing from this list away")]
    for name, root, count in repos:
        entries.append(SideEntry(
            f"repo:{root or name}", name, section="Repositories", icon="git-branch", count=count,
            value=("repo", name), tip=root or name, checked=box_holds(box, "repo", name)))
    for ext, count in languages:
        entries.append(SideEntry(
            f"lang:{ext}", ext.upper(), section="Languages", icon="code", count=count,
            value=("type", ext), tip=f"Only .{ext} files", checked=box_holds(box, "type", ext)))
    return entries


def browse_entries(overview: Any, kind: str = "everything",
                   kinds: Iterable[tuple[str, str]] = ()) -> list[SideEntry]:
    """Browse (D3): Years, each opening to its months, with counts; Show."""
    entries: list[SideEntry] = []
    if overview is not None:
        for year, count in sorted(getattr(overview, "years", ()), reverse=True):
            entries.append(SideEntry(f"year:{year}", str(year), section="Years", icon="calendar",
                                     count=count, value=("year", int(year)),
                                     tip=f"Everything from {year}"))
            for month, inside in sorted(overview.months_of(year).items(), reverse=True):
                if inside:
                    entries.append(SideEntry(
                        f"month:{year}-{month:02d}", MONTHS[month - 1], parent=f"year:{year}",
                        count=inside, value=("month", (int(year), int(month))),
                        tip=f"{MONTHS[month - 1]} {year}"))
    for label, value in kinds:
        entries.append(SideEntry(f"show:{value}", label, section="Show", icon=_show_icon(value),
                                 value=("kind", value), checked=(value == kind),
                                 tip=f"Show {label.lower()} on the timeline"))
    return entries


def _show_icon(value: str) -> str:
    return {"photos": "image", "videos": "video", "documents": "file-text", "mail": "mail",
            "code": "code"}.get(str(value), "calendar")


def clear_from_box(text: str, switches: Iterable[str]) -> str:
    """The box without any of these switches - what "All ..." does (D2)."""
    from app.ui.chips_logic import chips_for, without

    wanted = set(switches)
    for chip in reversed(list(chips_for(text or ""))):
        if chip.op in wanted and not chip.negated:
            text = without(text, chip)
    return (text or "").strip()
