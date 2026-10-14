r"""The counts the left-hand lists show, read in one grouped query each. Read-only.

Layer: L2

Order 1p (2026-10-11). Kept out of `sqlite_store.py`, which every order touches: each
function takes a store and reads `store.conn`. Called on a worker, never on the
window's thread.
"""

from __future__ import annotations

from typing import Any, Iterable

__all__ = ["folder_counts", "drive_counts", "kind_counts", "repo_counts", "language_counts",
           "saved_roots", "KINDS_SHOWN"]

#: How many file types a Kinds or Languages section lists.
KINDS_SHOWN = 12


def saved_roots(store: Any) -> list[str]:
    """The folders the owner chose to index (`ui:roots`), as saved."""
    try:
        raw = str(store.get_state("ui:roots", "") or "")
    except Exception:                                      # noqa: BLE001 - no roots, no Places
        return []
    return [root for root in raw.split("|") if root]


def folder_counts(store: Any) -> list[tuple[str, int]]:
    """`(folder, files directly in it)` for every folder of plain files on this computer."""
    return [(str(folder or ""), int(count)) for folder, count in store.conn.execute(
        "SELECT parent_dir, COUNT(*) FROM files "
        "WHERE source_kind = 'file' AND volume_id IS NULL AND parent_dir IS NOT NULL "
        "GROUP BY parent_dir")]


def drive_counts(store: Any) -> list[tuple[str, str, int]]:
    """`(name, status, files)` for every catalogued drive, most files first."""
    try:
        rows = store.conn.execute(
            "SELECT v.name, COALESCE(v.status, ''), COUNT(f.id) FROM volumes v "
            "LEFT JOIN files f ON f.volume_id = v.id GROUP BY v.id ORDER BY 3 DESC, 1").fetchall()
    except Exception:                                      # noqa: BLE001 - an index without drives
        return []
    return [(str(name or ""), str(status), int(count)) for name, status, count in rows]


def kind_counts(store: Any, *, limit: int = KINDS_SHOWN, always: Iterable[str] = ()) -> list[tuple[str, int]]:
    """`(type, files)` for the commonest types of plain files, most first - and then, after them, each type in
    `always` that the index holds and the commonest did not already include.

    The commonest twelve are all a Kinds list has room for, and a type with a few thousand files (Visio drawings,
    Project plans) never makes it: "visio and microsoft project file types missing" (the owner, 2026-10-12). The
    types to always list are a setting (`FILE_KINDS_ALWAYS`), and each is listed with its count even when that
    is 0 (2026-10-14): a type that silently is not there reads as "Leasha does not know this type", which is the
    complaint itself. Only plain files on disk are counted (`source_kind = 'file'`), as for every other kind - a
    `.vsd` inside a zip or attached to a message is not one."""
    rows = [(str(ext or ""), int(count)) for ext, count in store.conn.execute(
        "SELECT ext, COUNT(*) FROM files WHERE source_kind = 'file' AND ext IS NOT NULL "
        "AND ext != '' GROUP BY ext ORDER BY 2 DESC, 1 LIMIT ?", (int(limit),))]
    have = {ext for ext, _n in rows}
    extra = sorted({e.strip().lower().lstrip(".") for e in always if e and e.strip()} - have)
    for ext in extra:
        n = int(store.conn.execute("SELECT COUNT(*) FROM files WHERE source_kind = 'file' AND ext = ?",
                                   (ext,)).fetchone()[0])
        rows.append((ext, n))
    return rows[:int(limit)] + sorted(rows[int(limit):], key=lambda r: (-r[1], r[0]))


def repo_counts(store: Any) -> list[tuple[str, str, int]]:
    """`(name, root, files)` for every repository, most files first."""
    try:
        rows = store.conn.execute(
            "SELECT r.name, r.root_path, COUNT(f.id) FROM repos r "
            "LEFT JOIN files f ON f.repo_id = r.id GROUP BY r.id ORDER BY 3 DESC, 1").fetchall()
    except Exception:                                      # noqa: BLE001 - an index without repos
        return []
    return [(str(name or ""), str(root or ""), int(count)) for name, root, count in rows]


def language_counts(store: Any, *, limit: int = KINDS_SHOWN) -> list[tuple[str, int]]:
    """`(type, files)` for the commonest types inside repositories, most first."""
    return [(str(ext or ""), int(count)) for ext, count in store.conn.execute(
        "SELECT ext, COUNT(*) FROM files WHERE repo_id IS NOT NULL AND ext IS NOT NULL "
        "AND ext != '' GROUP BY ext ORDER BY 2 DESC, 1 LIMIT ?", (int(limit),))]
