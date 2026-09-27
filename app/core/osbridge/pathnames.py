r"""How paths are joined and compared: the separator, and whether letter case counts.

Layer: L0 (part of `app.core.osbridge`)

Work order 0x section 7. Two questions every file-search program has to answer,
which Leasha used to answer the Windows way everywhere, without asking.

**1. Which character divides the folders in a path?** Windows writes
`D:\Photos\2019\beach.jpg` with backslashes; a Mac (and Linux) writes
`/Users/jaymin/Photos/2019/beach.jpg` with forward slashes. Code that glues a
backslash between a Mac folder and a file name produces
`/Users/jaymin/repo\src\main.py` - one file name with a backslash inside it,
which nothing can open. `separator_for` and `join_under` give the right
character for the path in hand.

**2. Are `Report.docx` and `report.docx` the same file?** On Windows, always:
its file system (NTFS) ignores letter case when it looks a name up, so the two
spellings open one file, and a folder cannot hold both. That is why Leasha
lower-cases paths before comparing them in about thirty places. On a Mac the
answer depends on how the disk was formatted: Apple's file system (APFS) is
case-*insensitive* by default, like Windows, but can be formatted
case-*sensitive*; on Linux (ext4) names are case-sensitive, so a folder really
can hold both `Report.docx` and `report.docx` - two different files. Lower-casing
there makes the second one look like a duplicate of the first, and it is never
indexed.

**The answer is decided per indexed folder, by asking the disk.** Nothing in a
path says how its drive was formatted, and one Mac can have both kinds of disk
plugged in at once. So `case_sensitive(root)` *probes*: it takes a name that is
really inside the folder, swaps the case of its letters, and asks the file
system whether that spelling exists too. If it finds the same file, the folder
ignores case; if nothing is there, the folder respects case. The answer is kept
(cached) for the life of the process, so each folder is probed once, when the
walk reaches it.

**Windows never probes and never changes.** On Windows every function here
returns exactly what the old code produced, byte for byte - `path_key` is
`str(path).lower()`, the joiner uses `\` - with no disk access at all. The index
on the owner's machine was written with those strings, and they must keep
matching. The same holds anywhere for a path that is *shaped* like a Windows
path (`D:\...` or `\\server\share`): tests on Linux use such paths all the time,
and an index copied from a Windows machine is full of them.

**What stays as it was, on purpose.** Some lower-cased strings are *stored*: a
setting keyed by folder (`archives.normalise`, the cloud opt-in list), a hash
used as a resume key. Those keep their Windows format on every system, because
changing a stored key on Windows would orphan the owner's settings. Each such
place says so where it is written.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any, Optional

from app.core.osbridge._platform import is_macos, is_windows

__all__ = [
    "is_windows_shaped",
    "separator_for",
    "join_under",
    "case_sensitive",
    "path_key",
    "same_path",
    "forget_probed_roots",
]

#: `D:` (a drive letter and a colon) at the start, or `\\` (a network share,
#: written `\\server\share`). Either one means "this path was written by
#: Windows", whatever system is reading it now.
_WINDOWS_SHAPE = re.compile(r"^(?:[A-Za-z]:|\\\\)")


def is_windows_shaped(path: Any) -> bool:
    r"""True for `D:\Docs`, `d:/docs`, `C:` or `\\server\share`.

    Used to keep Windows rules for Windows paths even when the code is running
    somewhere else - in the Linux test runs, or reading an index that was built
    on Windows. A Mac or Linux path always starts with `/` (or is relative), so
    it can never match by accident.
    """
    return bool(_WINDOWS_SHAPE.match(str(path)))


def separator_for(path: Any) -> str:
    r"""The character that divides folders in `path`: `\` or `/`.

    - **Windows**: always `\`, exactly as before - the rest of the application
      stores backslashes, and a path built here must match them.
    - **Anywhere else**: `\` if the path is Windows-shaped (see
      `is_windows_shaped`), so a `D:\...` path keeps looking like one;
      otherwise this system's own separator, `os.sep`, which is `/` on a Mac
      and on Linux.
    """
    if is_windows() or is_windows_shaped(path):
        return "\\"
    return os.sep


def join_under(root: Any, relative: str) -> str:
    r"""`root` + separator + `relative`, as text, whether or not the file exists.

    `relative` comes from git, which always writes forward slashes
    (`src/app/main.py`). The result uses one separator throughout - the one
    `separator_for(root)` picks - so the path can be shown and opened.

    **Joined as text, not through `Path`.** `Path(r"D:\a") / "src/b.cs"` on
    Linux gives `D:\a/src/b.cs`, two separators in one path, because pathlib
    there does not know `\` divides anything.

    **On Windows this is character for character the code it replaced**
    (`app/search/federate.py`, before order 0x section 7):
    `base + "\\" + relative.replace("/", "\\").lstrip("\\")`.
    """
    base = str(root).rstrip("\\/")
    if not relative:
        return base
    separator = separator_for(root)
    if separator == "\\":
        return base + "\\" + relative.replace("/", "\\").lstrip("\\")
    # A Mac or Linux root: git's forward slashes are already right. Only a
    # leading slash is removed, so `root` + `/` + `/src` cannot become `//src`.
    return base + separator + relative.lstrip("/")


# ---------------------------------------------------------------------------
# Letter case, per indexed folder
# ---------------------------------------------------------------------------

#: `folder as written (no trailing slash) -> True if it respects letter case`.
#: Filled by `case_sensitive`, read by `path_key` for every path it is given.
_CASE_BY_ROOT: dict[str, bool] = {}
#: The same folders, longest first, so a folder inside another (a disk mounted
#: inside a folder that is also indexed) is found before the one around it.
#: Rebuilt only when a folder is added - a handful of times per run.
_ROOTS_LONGEST_FIRST: list[str] = []
#: Held while the two above are changed, because the walker and the pipeline
#: run on different threads and a list half-rebuilt is not a list.
_LOCK = threading.Lock()


def _default_case_sensitive() -> bool:
    """The answer for a path under no probed folder, when the disk cannot say.

    - macOS: **False** - APFS as formatted by default ignores case
      (UNCONFIRMED on macOS: the default is Apple's documented one; a Mac whose
      startup disk was formatted case-sensitive would get the wrong default
      here, but every folder that is *walked* is probed and does not use this).
    - Linux and others: **True** - ext4, btrfs and xfs all respect case.
    - Windows never reaches this function.
    """
    return not is_macos()


def _swap_ascii_case(name: str) -> str:
    """`Report2019.docx` -> `rEPORT2019.DOCX`, touching only the letters A-Z.

    Only plain English letters are swapped. For letters outside that range the
    rules differ between file systems (`ß` becomes `SS` in Python, which no file
    system treats as the same name), and a probe must never depend on them.
    """
    return "".join(
        ch.swapcase() if ("a" <= ch <= "z" or "A" <= ch <= "Z") else ch
        for ch in name)


def _same_entry(first: os.stat_result, second: os.stat_result) -> bool:
    """Do two `stat` results describe one and the same file on disk?

    A file is identified by the disk it is on (`st_dev`) and its number on that
    disk (`st_ino`). Two spellings that find the same pair found one file.
    """
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _probe_pair(real: str, swapped: str) -> Optional[bool]:
    """Compare a real entry with its case-swapped spelling.

    Returns True (case counts: the swapped spelling is not there, or is a
    different file), False (case is ignored: it is the same file), or None when
    the disk would not answer (no permission, the entry vanished meanwhile).
    """
    try:
        real_stat = os.lstat(real)
    except OSError:
        return None
    try:
        other_stat = os.lstat(swapped)
    except FileNotFoundError:
        return True
    except OSError:
        return None
    # Found something under the swapped spelling. The same file means the disk
    # ignored the case; a different file means the folder genuinely holds two
    # names that differ only by case - which only a case-respecting disk allows.
    return not _same_entry(real_stat, other_stat)


def _probe(root: str) -> Optional[bool]:
    """Ask the disk whether the folder `root` respects letter case.

    **A name inside the folder is tried first**, because that entry lives on the
    folder's own disk. Probing the folder's *own* name would ask the disk above
    it instead - for `/Volumes/Work` that is the Mac's startup disk, not the
    `Work` disk the files are on. Up to 200 entries are looked at, to find one
    with a letter in its name; one is enough.

    Only if the folder is empty (or has no letters in any name) is the folder's
    own name tried. None means neither worked, and the caller uses the default.
    """
    try:
        with os.scandir(root) as entries:
            for count, entry in enumerate(entries):
                if count >= 200:
                    break
                swapped = _swap_ascii_case(entry.name)
                if swapped == entry.name:
                    continue                  # no letters to swap: try the next
                answer = _probe_pair(entry.path, os.path.join(root, swapped))
                if answer is not None:
                    return answer
    except OSError:
        pass                                  # unreadable: fall through
    here = Path(root)
    swapped_name = _swap_ascii_case(here.name)
    if here.name and swapped_name != here.name:
        return _probe_pair(str(here), str(here.parent / swapped_name))
    return None


def _root_text(root: Any) -> str:
    """A folder as a lookup key: as written, with no trailing separator."""
    text = str(root).rstrip("/")
    return text if text else "/"


def case_sensitive(root: Any) -> bool:
    r"""Does the folder `root` treat `Report.docx` and `report.docx` as two files?

    - **Windows**: always False, answered without touching the disk. NTFS ignores
      case, and this is the behaviour every index on the owner's machine was
      built with.
    - **A Windows-shaped path anywhere** (`D:\...`): False, for the same reason.
    - **Mac and Linux**: probed on the disk once (see `_probe`) and remembered
      for the life of the process. A folder that does not exist yet is not
      remembered, so it is probed properly once it appears (a drive plugged in
      later). If the disk cannot answer, the system's usual default is used
      (see `_default_case_sensitive`).

    Calling this is also what *registers* the folder, so that `path_key` knows
    which rule to use for every path under it. The walker calls it for each of
    its roots before it lists a single file.
    """
    if is_windows() or is_windows_shaped(root):
        return False
    key = _root_text(root)
    known = _CASE_BY_ROOT.get(key)
    if known is not None:
        return known
    if not os.path.isdir(key):
        return _default_case_sensitive()
    answer = _probe(key)
    if answer is None:
        answer = _default_case_sensitive()
    with _LOCK:
        _CASE_BY_ROOT[key] = answer
        _ROOTS_LONGEST_FIRST[:] = sorted(_CASE_BY_ROOT, key=len, reverse=True)
    return answer


def _rule_for(text: str) -> bool:
    """True if `text` sits under a probed folder that respects case.

    Finds the longest probed folder that `text` is inside. Checked exactly
    first (the walker's own paths always start with the folder as written);
    then, for folders that ignore case, with the case ignored too, because on
    such a disk `/Users/Me/Docs/a.txt` can legitimately be spelled
    `/users/me/docs/a.txt` by somebody typing it.
    """
    roots = _ROOTS_LONGEST_FIRST
    for root in roots:
        if root == "/" or text == root or text.startswith(root + "/"):
            return _CASE_BY_ROOT[root]
    lowered = text.lower()
    for root in roots:
        if _CASE_BY_ROOT[root]:
            continue
        low = root.lower()
        if lowered == low or lowered.startswith(low + "/"):
            return False
    return _default_case_sensitive()


def path_key(path: Any) -> str:
    r"""The string two paths are compared by: "is this the same file?".

    - **Windows**: exactly `str(path).lower()` - the key every "have we seen
      this file?" set in the indexer has always used. Byte for byte, and with
      no lookup, because this runs for every file in a 100GB walk.
    - **A Windows-shaped path anywhere**: the same, lower-cased.
    - **Mac and Linux**: lower-cased if the folder it is in ignores case, left
      exactly as it is if the folder respects case - so `Report.docx` and
      `report.docx` in a case-sensitive folder are two keys, two files, both
      indexed.

    **Every place that fills or reads one shared "seen" set must use this**, not
    `.lower()` of its own: the walker adds keys and the pipeline's clean-up pass
    looks rows up by key, and if the two disagreed the clean-up would delete
    files that are still there.
    """
    text = str(path)
    if is_windows() or is_windows_shaped(text):
        return text.lower()
    return text if _rule_for(text) else text.lower()


def same_path(first: Any, second: Any) -> bool:
    """True if `first` and `second` name the same file, by `path_key`'s rules."""
    return path_key(first) == path_key(second)


def forget_probed_roots() -> None:
    """Clear every remembered answer. **For tests only**: each test that probes
    a folder starts from nothing, so one test's folders cannot decide the rule
    for another's."""
    with _LOCK:
        _CASE_BY_ROOT.clear()
        _ROOTS_LONGEST_FIRST.clear()
