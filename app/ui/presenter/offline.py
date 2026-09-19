"""Catalogued drives and online-only files, in words.

Layer: L5. Part of the presenter package; imports no Qt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

from app.ui.presenter.formatting import format_count, format_size, format_when


def offline_volume_note(mark: Optional[dict]) -> str:
    r"""§3a's exact sentence for a row `offline_volume_marks` found
    offline. `""` for everything else - an online row, or an ordinary
    file not on a catalogued volume at all.
    """
    if not mark:
        return ""
    name = mark.get("name") or "that drive"
    scanned = mark.get("scanned")
    if scanned:
        return f"on {name} (offline, scanned {scanned}) - plug it in to open"
    return f"on {name} (offline) - plug it in to open"


def online_only_note(is_placeholder: bool) -> str:
    r"""202626270514 3d's exact words: "online-only - opening will
    download." `""` for anything already on this machine.
    """
    return "online-only - opening will download" if is_placeholder else ""


@dataclass(frozen=True, slots=True)
class VolumeRow:
    """One Offline Media source, for the tab's list. 2a: name, status, size,
    counts, snapshot date."""

    volume_id: int
    name: str
    kind: str            # "drive" | "network" | "cloud" | "phone" | "archived"
    status: str           # the sentence the row shows - see `volume_rows`
    status_code: str      # ONLINE | OFFLINE | LOCKED, raw - for an icon or colour
    size: str             # "128.4 GB"
    files: str             # "48,301"
    scanned: str           # "12 Nov 2025", or "never" before a first Scan finishes
    description: str
    #: Unformatted, for a table that wants to sort on the real value.
    file_count: int = 0
    size_bytes: int = 0
    last_scanned_at: int = 0
    last_seen: int = 0


def volume_rows(rows: Iterable[Mapping[str, Any]],
                online: Optional[Mapping[int, Any]] = None, *,
                now: Optional[float] = None) -> list[VolumeRow]:
    r"""Store rows to display rows for the Offline Media tab.

    `online` is `app.index.offline_media.connected_volumes(store)`'s result -
    fetched on a worker, never here, because it is a live Windows volume
    enumeration and this function must stay a pure formatter like every
    other `*_rows` in this module. `status_code` is read from the row's own
    `status` column, which `refresh_volume_statuses` already wrote before
    this is called - this function only turns a code into the sentence a
    person reads; it never decides ONLINE/OFFLINE/LOCKED itself.

    **"online as F:" - the letter shown as a transient fact only (2a).** It
    is read from `online`, not stored anywhere, and a rescan a moment later
    at a different letter simply reads differently next time this runs.
    """
    online = online or {}
    out: list[VolumeRow] = []
    for row in rows:
        volume_id = int(row.get("id", 0) or 0)
        status_code = str(row.get("status") or "OFFLINE").upper()
        root = online.get(volume_id)
        seen = int(row.get("last_seen") or 0)
        if status_code == "ONLINE" and root is not None:
            letter = str(root).rstrip("\\/") or str(root)
            status = f"Online as {letter}"
        elif status_code == "LOCKED":
            status = "Locked (BitLocker)"
        elif seen:
            status = f"Offline - last seen {format_when(seen * 1_000_000_000, now=now)}"
        else:
            status = "Offline"
        scanned_at = int(row.get("last_scanned_at") or 0)
        count = int(row.get("indexed_files", row.get("file_count", 0)) or 0)
        out.append(VolumeRow(
            volume_id=volume_id,
            name=str(row.get("name", "")),
            kind=str(row.get("kind", "")),
            status=status,
            status_code=status_code,
            size=format_size(int(row.get("size_bytes") or 0)),
            files=format_count(count),
            scanned=format_when(scanned_at * 1_000_000_000, now=now) if scanned_at else "never",
            description=str(row.get("description") or ""),
            file_count=count,
            size_bytes=int(row.get("size_bytes") or 0),
            last_scanned_at=scanned_at,
            last_seen=seen,
        ))
    return out


def offline_media_empty_state() -> str:
    r"""2a's empty list: what to type and press, before there is anything to show."""
    return (
        "No drives catalogued yet. Press “Scan a drive…” and choose "
        "the drive or folder to catalogue - nothing happens to any drive until "
        "you do."
    )


def offline_media_help_text() -> str:
    r"""The tab's own help line - two sentences the order names
    verbatim, neither of them obvious from the buttons alone.

    202626270514 1d, the decommission case: scanning a share is worth
    doing *before* the server goes away, not only for a share still
    live - and what gets caught is exactly what this account could see
    that day, nothing more.

    202626270514 3b-3, the doctrine: a proprietary backup format is out
    of scope by design - the backup product is the generating system,
    and 3b-1's \"mark as archived\" is the supported path once a tape is
    written.
    """
    return (
        "Scan a network share before a server is switched off or access is "
        "lost - it stays searchable forever after. The catalogue holds only "
        "what this account could read on the day of the scan.\n\n"
        "Leasha does not read backup software's own formats (Veeam, "
        "NetBackup, tar-on-tape) directly. Catalogue the folder before it is "
        "backed up, then mark the finished tape or disc as archived once it "
        "is written."
    )


def rename_suggestion_text(suggested_name: str) -> tuple[str, str]:
    r"""202626270514 1a's offer, worded once: "is this *Old NAS* at a new
    address?" - assist, never assume. `(title, body)`, the same shape
    `delete_volume_confirmation` uses, for the same reason: the tab's
    `RenameSuggestionDialog` and any headless caller read the identical
    words, and a test can check them without opening a window.

    Shown only when `app.index.offline_media.suggest_renamed_source` found
    a structure match against a *different*, already-catalogued identity -
    never for an ordinary rescan of a source already known at this one.
    """
    title = f"Is this {suggested_name!r} at a new address?"
    body = (
        f"This location's top-level folders match a source already "
        f"catalogued as {suggested_name!r}. If it is the same drive or "
        f"share - moved, renamed, or reconnected differently - choosing "
        f"\u201cYes\u201d keeps its existing catalogue and everything "
        f"already indexed from it, instead of starting a new one.\n\n"
        f"Nothing is changed until you choose."
    )
    return title, body


def delete_volume_confirmation(name: str, file_count: int) -> tuple[str, str]:
    r"""2c's confirmation text: the count, and the crucial sentence, verbatim.

    A `(title, body)` pair so the dialog and any headless caller (a test, a
    future CLI `--yes` prompt) render the identical words - the wording
    lives here once, the way every notice and error message in this project
    does, rather than typed again at each call site.
    """
    title = f"Forget {name!r}?"
    body = (
        f"This removes {file_count:,} file(s) catalogued under {name!r} "
        f"from Leasha's index.\n\n"
        "This removes the catalogue from Leasha's index. Nothing on the "
        "drive itself is touched."
    )
    return title, body


def offline_media_run_summary(result: Any) -> str:
    r"""The status-bar sentence after a Scan, Rescan or Delete completes -
    2a's whole "what happened" story, since the tab shows no progress bar
    while one runs. **Always names an amount**, the same rule
    `cleared_message` follows: a silent success is indistinguishable from
    nothing having happened.
    """
    if not isinstance(result, dict):
        return "Done."
    if "deleted" in result:
        name = result.get("name") or "that source"
        files = int(result.get("files") or 0)
        return (f"Forgot {name!r}: {files:,} file(s) removed from the index. "
                "Nothing on the drive itself was touched.")
    stats = result.get("stats")
    indexed = int(getattr(stats, "indexed", 0) or 0)
    seen = int(getattr(stats, "seen", 0) or 0)
    deleted = int(getattr(stats, "deleted", 0) or 0)
    moved = int(result.get("moved") or 0)
    pieces = [f"{indexed:,} new/changed document(s)", f"{seen:,} file(s) seen"]
    if moved:
        pieces.append(f"{moved:,} moved on disk and repaired without re-extraction")
    if deleted:
        pieces.append(f"{deleted:,} row(s) removed for files genuinely gone")
    return "Scanned: " + ", ".join(pieces) + "."
