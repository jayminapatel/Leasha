r"""What the Tidy page says: its left-hand list and every sentence on it. Qt-free.

Layer: L5 presenter. Order 1q D8 (2026-10-11).

The list is the 1p `SideList`: Overview, Clean up, Keep safe, Report, History. Each
entry's `value` is the pane it opens (`overview`, `copies`, `similar`, `large`,
`leftovers`, `only`, `report`, `history`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from app.core.row_facts import format_size
from app.ui.presenter.side_entry import SideEntry

__all__ = ["TidyCounts", "side_entries", "queue_words", "plan_question", "big_question",
           "outcome_words", "set_heading", "overview_document", "only_copy_document",
           "batch_line", "put_back_words", "set_sentence", "KIND_WORDS"]

KIND_WORDS = {
    "copies": "Exact copies",
    "similar": "Similar photos",
    "large": "Large files",
    "leftovers": "Leftovers",
}


@dataclass(frozen=True)
class TidyCounts:
    """The numbers the list shows, read on a worker."""

    files_here: int = 0
    drives: int = 0
    copy_sets: int = 0
    copy_spare: int = 0
    similar_sets: int = 0
    large: int = 0
    leftovers: int = 0
    only_here: int = 0
    batches: int = 0


def side_entries(c: TidyCounts) -> list[SideEntry]:
    """The Tidy page's list (D8)."""
    return [
        SideEntry("overview", "What's indexed", "Overview", "database", count=c.files_here or None,
                  tip="Everything Leasha has read on this computer, by kind and by folder, and the drives it has catalogued.",
                  value="overview"),
        SideEntry("copies", "Exact copies", "Clean up", "copy", count=c.copy_sets or None,
                  tip=f"Files on this computer stored more than once, byte for byte. Keeping one of each gives back {format_size(c.copy_spare)}.",
                  value="copies"),
        SideEntry("similar", "Similar photos", "Clean up", "image", count=c.similar_sets or None,
                  tip="Photos that are the same picture saved more than once - a resize, a re-save, a burst. Compare them side by side.",
                  value="similar"),
        SideEntry("large", "Large files", "Clean up", "archive", count=c.large or None,
                  tip="The biggest files on this computer, 100 MB and over. Nothing is marked for you - each may be the only copy.",
                  value="large"),
        SideEntry("leftovers", "Leftovers", "Clean up", "package", count=c.leftovers or None,
                  tip="Unfinished downloads, temporary files and installers left in Downloads. Nothing is marked for you.",
                  value="leftovers"),
        SideEntry("only", "Only one copy", "Keep safe", "shield-check", count=c.only_here or None,
                  tip="Files that exist in one place only - nothing else in the index has the same contents. Tidy never offers these as copies.",
                  value="only"),
        SideEntry("report", "The Space Report", "Report", "chart-column",
                  tip="Which files exist in more than one place, how much room the copies take, and what exists nowhere else.",
                  value="report"),
        SideEntry("history", "Removed by Leasha", "History", "history", count=c.batches or None,
                  tip="Every time Tidy moved files to the Recycle Bin, and the way to put them back.",
                  value="history"),
    ]


def queue_words(files: int, size_bytes: int) -> str:
    """The marked queue, shown beside Review and remove."""
    if not files:
        return "Nothing marked"
    return f"Marked: {files:,} - {format_size(size_bytes)}"


def plan_question(files: int, size_bytes: int, refused: int) -> tuple[str, str]:
    """The confirmation before anything moves (D1, D2)."""
    text = (f"Move {files:,} file{'s' if files != 1 else ''} ({format_size(size_bytes)}) to the "
            "Recycle Bin?\n\nEach is checked again first: anything that changed since Leasha read "
            "it, or whose kept copy cannot be checked, stays where it is. You can put them back "
            "from History, or from the Recycle Bin.")
    if refused:
        text += (f"\n\n{refused:,} set{'s were' if refused != 1 else ' was'} left out because every "
                 "file in it was marked - one copy always stays.")
    return "Move to the Recycle Bin", text


def big_question(files: int, size_bytes: int) -> tuple[str, str]:
    """D7: the second question for a big batch."""
    return ("This is a big batch",
            f"That is {files:,} files and {format_size(size_bytes)}. Move them all?")


def outcome_words(moved: int, size_moved: int, left: list[tuple[str, str]]) -> str:
    """What happened, said once."""
    words = f"{moved:,} moved to the Recycle Bin ({format_size(size_moved)})."
    if left:
        first_path, first_reason = left[0]
        words += f" {len(left):,} stayed where they were - for example {first_path}: {first_reason}"
    return words


def set_heading(kind: str, index: int, total: int, size_bytes: int, found: int = 0) -> str:
    """"Exact copies - set 12 of 200 (3,214 in all) - 2.1 MB"; a list kind has no set number."""
    if kind in ("large", "leftovers"):
        return f"{KIND_WORDS[kind]} - {total:,} file{'s' if total != 1 else ''} - {format_size(size_bytes)}"
    if not total:
        return f"{KIND_WORDS.get(kind, kind)} - none found"
    more = f" ({found:,} in all; the ones that give back most room first)" if found > total else ""
    return f"{KIND_WORDS.get(kind, kind)} - set {index + 1:,} of {total:,}{more} - {format_size(size_bytes)}"


def set_sentence(tset: Any, marked: set) -> str:
    """Under a set: what is marked and what stays, said as it is - never why a rule chose."""
    if tset is None:
        return "Nothing to tidy here."
    going = [f for f in tset.files if f.path in marked]
    if tset.kind in ("large", "leftovers"):
        lead = "Nothing here is marked for you - each of these may be the only copy."
        return f"{lead} {len(going):,} ticked." if going else f"{lead} Tick what can go."
    stays = [f for f in tset.files if f.path not in marked]
    if not stays:
        return "Every file here is marked, so none of them will go - one must stay."
    if not going:
        return ("Nothing is marked. Tick the files that can go, or right-click the one to keep "
                "and choose Keep this one.")
    kept = ", ".join(f.name for f in stays[:3]) + (" and others" if len(stays) > 3 else "")
    return (f"{len(going):,} marked for the Recycle Bin ({format_size(sum(f.size_bytes for f in going))}); "
            f"keeping {kept}. Double-click another to keep it instead.")


def overview_document(o: Any, coverage: Optional[str] = None) -> str:
    """The Overview pane, in Markdown, from `find.Overview`."""
    lines = ["## What's indexed", "",
             f"**{o.files_here:,} files on this computer**, {format_size(o.bytes_here)} - "
             f"{o.photos_here:,} of them photos.",
             f"{o.messages:,} mail messages, and {o.files_on_drives:,} files on catalogued drives.", ""]
    if coverage:
        lines += [coverage, ""]
    if o.places:
        lines += ["### By folder", ""]
        lines += [f"- {root} - {n:,} files, {format_size(size)}" for root, n, size in o.places]
        lines.append("")
    if o.kinds:
        lines += ["### By kind (most room first)", ""]
        lines += [f"- .{ext} - {n:,} files, {format_size(size)}" for ext, n, size in o.kinds]
        lines.append("")
    if o.drives:
        lines += ["### Catalogued drives", ""]
        lines += [f"- {name} - {n:,} files" for name, n in o.drives]
    return "\n".join(lines)


def only_copy_document(uniqueness: list) -> str:
    """Keep safe: what exists in one place only, by source (`space.find_source_uniqueness`)."""
    if not uniqueness:
        return "## Only one copy\n\nNothing found yet - Leasha compares files by their contents once it has read them."
    lines = ["## Only one copy", "",
             "These exist in one place only. Tidy never offers them as copies; if a drive "
             "below fails, they go with it.", ""]
    for u in uniqueness:
        seen = ""
        if u.last_seen and u.status in ("offline", "locked"):
            seen = ", last seen " + time.strftime("%d %b %Y", time.localtime(int(u.last_seen)))
        lines.append(f"- **{u.name}**{seen} - {u.file_count:,} files nowhere else")
    return "\n".join(lines)


def batch_line(b: Any) -> str:
    """One History row."""
    when = time.strftime("%d %b %Y %H:%M", time.localtime(int(b.at)))
    waiting = (f"{b.waiting:,} still in the Recycle Bin" if b.waiting else "all put back")
    return f"{when} - {b.files:,} file{'s' if b.files != 1 else ''}, {format_size(b.size_bytes)} - {waiting}"


def put_back_words(returned: int, left: list[tuple[str, str]]) -> str:
    words = f"{returned:,} put back where they were."
    if left:
        words += f" {len(left):,} could not be - for example {left[0][0]}: {left[0][1]}"
    return words
