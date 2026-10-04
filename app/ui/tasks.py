"""Worker bodies: the presenter's functions that read a store, the disk or a
subprocess.

Layer: L5

Every function here is handed to a `CallableWorker` (or called from a function
that is) and never runs on the interface thread. `test_ui_never_blocks` reads
this module as the one place a store call or blocking I/O is allowed in UI code,
and refuses a view that calls anything from it inline.

Nothing here imports Qt. They are still importable from `app.ui.presenter`.
"""

from __future__ import annotations

import time as _time
from pathlib import Path
from typing import Any, Optional

from app.core.logging import logger
# §5b's attachment-path rule lives in `app.search.marks` since 2026-10-04.
from app.search.marks import ATTACHMENT_MARKER as _ATTACHMENT_MARKER  # noqa: F401
from app.search.marks import attachment_parent_path as _attachment_parent_path  # noqa: F401
from app.ui.presenter.code import REPO_FILE_LIMIT, code_type_filter, git_rows_matching
from app.ui.presenter.formatting import format_size, format_when
from app.ui.presenter.indexing import pictures_not_read_counts, warned_counts
from app.ui.presenter.repos import RepoRow
from app.ui.presenter.rows import LIST_TOTAL_CAP, MAIL_TOTAL_CAP  # how far each total is counted

_log = logger.bind(component="ui.presenter")


def first_chunk_id(store: Any, path: str) -> int:
    """The first passage of a file, so "find similar" from a chat source has
    something to start from - a source built from a receipt may carry no chunk
    id of its own. 0 when the file or its passages are not there."""
    record = store.get_file(str(path))
    if record is None:
        return 0
    chunks = store.chunks_for_file(record.id)
    return int(chunks[0].id) if chunks else 0


def settings_labels(store: Any) -> tuple:
    r"""`(searches, direct_pst)` for the two slow Settings labels. **Worker.**

    Both used to be computed inside `SettingsView.__init__`, which is inside
    `MainWindow.__init__`: a `COUNT(*)` against the store and an import probe
    for `pst_libpff`, on the UI thread, before the first frame was drawn. The
    count is cheap on an idle database and not on one an index run is writing
    to, and an import is never free the first time.

    `-1` means the count could not be read, which `history_label_text` renders
    as a sentence rather than as a number nobody should trust.
    """
    from app.extract import pst_libpff

    searches = -1
    if store is not None:
        try:
            searches = int(store.count_searches())
        except Exception:                        # noqa: BLE001 - a label, not a search
            searches = -1
    try:
        direct = bool(pst_libpff.available())
    except Exception:                            # noqa: BLE001
        direct = False
    return searches, direct


def filter_offer_notices(store: Any, sentence: str, preferences: Any = None,
                         applied: Any = ()) -> list:
    r"""Offers for the filters the rules read out of a sentence. **Worker only.**

    `translate_rules.read` asks the store for its known senders and file types,
    which is a query - so this is here, off the interface thread, and the view
    only ever receives the finished notices. Never raises.

    `applied` is what the search already applied (`presenter.auto_filters`):
    a filter already in force is not offered again beside its own chip.
    """
    try:
        from app.search.policy import from_settings
        from app.ui.presenter.search import chips_for, filter_offers, unanswered

        chips = chips_for(store, sentence, from_settings("search", preferences))
        return filter_offers(unanswered(chips, applied), sentence)
    except Exception as exc:                     # noqa: BLE001 - an offer, not a search
        _log.debug("no filter offers for this query: {}", exc)
        return []


def decorate_results(store: Any, results: Any) -> dict:
    """Mail subtitles, missing-file marks, Offline Media status and cloud
    placeholder status for one page. **Worker only.**

    Four halves were running, or would have run, on the UI thread - a
    SQLite query, one filesystem stat per row (missing files, then again
    for §3d's placeholder check), and (§3a) a live Windows volume check.
    Together here so there is one worker rather than four, and one place
    that says which of this work is off-thread.
    """
    volumes = offline_volume_marks(store, results)
    # A drive's name rides in with the mail details (2026-10-04): a row on a
    # catalogued drive is never mail, so the two never share a key.
    details = {**volume_labels(store, results), **mail_details(store, results)}
    return {
        "details": details,
        "missing": missing_paths(
            getattr(row, "path", "") for row in results or ()
            if getattr(row, "volume_id", None) is None
        ),
        "volumes": volumes,
        "placeholders": placeholder_marks(results),
        # The Status word per row - one batched read, and the offline answer
        # the line above already paid for rather than a second volume check.
        "statuses": result_statuses(store, results, offline=volumes),
    }


def result_statuses(store: Any, results: Any, *, offline: Any = ()) -> dict[int, str]:
    """`{file_id: word}` for one page of search results. **Worker only.**

    The Search list's Status column (see `app.core.file_state`). A search
    result carries its file's id and nothing about its index state, so this is
    one `store.file_states` read for the whole page; `offline` is the set (or
    dict) of file ids `offline_volume_marks` found on a disconnected volume.
    Never raises: a missing word is a blank cell, not a failed search.
    """
    from app.core.file_state import derive

    ids = {int(getattr(row, "file_id", 0) or 0) for row in results or ()}
    ids.discard(0)
    if not ids or store is None:
        return {}
    try:
        states = store.file_states(sorted(ids))
    except Exception as exc:                     # noqa: BLE001 - a column, not the search
        _log.debug("no status words for this page: {}", exc)
        return {}
    away = set(offline or ())
    return {
        file_id: derive(state.get("status"), state.get("skip_code"),
                        offline=file_id in away)
        for file_id, state in states.items()
    }


def offline_volume_ids(store: Any, rows: Any) -> set[int]:
    """Which of these rows' volumes are not connected right now. **Worker only**
    - the same live check `offline_volume_marks` makes, and only when a row is
    on a catalogued volume at all. `rows` are store dicts or row objects."""
    def volume_of(row: Any) -> Any:
        return row.get("volume_id") if isinstance(row, dict) else getattr(row, "volume_id", None)

    wanted = {int(v) for v in (volume_of(row) for row in rows or ()) if v is not None}
    if not wanted:
        return set()
    from app.index.offline_media import connected_volumes

    try:
        online = connected_volumes(store)
    except Exception:                            # noqa: BLE001 - a column, not the list
        online = {}
    return {volume for volume in wanted if volume not in online}


def _bounded_count(count: Any, *args: Any, **kwargs: Any) -> Optional[int]:
    """A list's total, or None when the store cannot say. Never raises: the
    rows are the answer, the total is the sentence under them."""
    if count is None:
        return None
    try:
        total = count(*args, **kwargs)
    except Exception as exc:                     # noqa: BLE001 - a sentence, not the list
        _log.debug("no total for this list: {}", exc)
        return None
    return None if total is None else int(total)


def read_box(store: Any, raw: str, *, surface: str, preferences: Any = None,
             declined: Any = ()) -> tuple:
    r"""`(parsed, applied)` for what was typed in any tab's box. **Worker.**

    **One reading for every tab** (owner, 1 October 2026): *"the natural
    language search should be available on all search items and should behave
    exactly same across the application"*. Slash commands, then the plain-English
    rules, then the parser - `app.search.run.read_typed` since 2026-10-04, where
    `app.cli files` and the MCP server read a line the same way.
    """
    from app.search.run import read_typed

    return read_typed(store, raw, surface=surface, preferences=preferences,
                      declined=declined)


def _words_of(parsed: Any) -> str:
    """The words left to match once filters are taken out (`run.words_of`)."""
    from app.search.run import words_of

    return words_of(parsed)


def _in_index(count: Any) -> Optional[int]:
    """A whole-index total for the summary, or None. Never raises."""
    try:
        return int(count()) if callable(count) else None
    except Exception as exc:                     # noqa: BLE001 - a sentence, not the list
        _log.debug("no index total: {}", exc)
        return None


def _respelt(store: Any, words: Any, *, surface: str, preferences: Any = None) -> Any:
    """The Search tab's spelling help for an empty list, or None (`run.respell`)."""
    from app.search.run import respell

    return respell(store, words, surface=surface, preferences=preferences)


def browse_files_typed(store: Any, raw: str, *, limit: int, preferences: Any = None,
                       declined: Any = ()) -> dict:
    """`browse_files_page` for a typed line, read the shared way. **Worker.**

    The search is `app.search.run.find_files` (2026-10-04), the one `app.cli
    files` and the MCP `find_files` tool run; this adds the tab's page - its
    bounded total and offline volumes - and the whole-index count.
    """
    from app.search.run import find_files

    page = find_files(store, raw, limit=limit, preferences=preferences, declined=declined,
                      page=lambda parsed: browse_files_page(store, parsed, limit=limit))
    page.update(in_index=_in_index(getattr(store, "count_listed_files", None)))
    return page


def browse_messages_typed(store: Any, raw: str, *, limit: int, preferences: Any = None,
                          declined: Any = ()) -> dict:
    """`browse_messages_page` for a typed line, read the shared way. **Worker.**

    The words left once the filters are taken out narrow the list by what the
    messages say - "holiday" in "mail about holiday from maya" - where this tab
    used to ignore them and say so.
    """
    from app.ui.presenter.rows import mail_filters

    parsed, applied = read_box(store, raw, surface="mail", preferences=preferences,
                               declined=declined)
    filters = mail_filters(parsed)
    words = _words_of(parsed)
    if words:
        filters["words"] = words
    page = browse_messages_page(store, limit=limit, **filters)
    found = None if page["rows"] else _respelt(
        store, words.split(), surface="mail", preferences=preferences)
    if found is not None:
        respelt = " ".join(found.suggestion if word.lower() == found.typed else word
                           for word in words.split())
        again = browse_messages_page(store, limit=limit, **{**filters, "words": respelt})
        if again["rows"]:
            page, words = again, respelt
            page["spelling"] = found.sentence()
    page.update(parsed=parsed, applied=applied, words=words,
                in_index=_in_index(getattr(store, "count_messages", None)))
    return page


def browse_files_page(store: Any, parsed: Any, *, limit: int) -> dict:
    """One page of the Files list, its total and its offline volumes. **Worker.**

    `rows` is exactly `store.browse_files` - its errors still reach the view,
    because a failed search must say so. `total` is the bounded count behind
    the page (`None` when unknown or when the page was not full), so the
    summary can say "Showing 200 of 12,431" instead of stopping at 200 in
    silence. `offline` feeds the Status column's Offline word.
    """
    # 2026-10-04: an attachment's message and a drive's name, read here for
    # the page, so the list shows them the Search list's way.
    rows = file_row_context(store, store.browse_files(parsed, limit=limit))
    total = None
    if len(rows) >= limit:
        total = _bounded_count(getattr(store, "count_browse_files", None),
                               parsed, cap=LIST_TOTAL_CAP)
    return {"rows": rows, "total": total, "offline": offline_volume_ids(store, rows)}


def browse_messages_page(store: Any, *, limit: int, **filters: Any) -> dict:
    """One page of the Mail list and its total. **Worker.**

    Asked for directly: *"when searching for mails the search displays maximum
    500 but does not tell how much total"*. Counted only when the page is full
    - a page with room left in it is already the whole answer.
    """
    rows = store.browse_messages(limit=limit, **filters)
    total = None
    if len(rows) >= limit:
        total = _bounded_count(getattr(store, "count_messages_matching", None),
                               cap=MAIL_TOTAL_CAP, **filters)
    return {"rows": rows, "total": total}


def status_funnel_counts(store: Any, stats: Any = None) -> dict[str, int]:
    """Counts per status word for the Indexing page's funnel. **Worker only.**

    The store's grouped counts (`store.status_counts`, three indexed
    statements - see there) plus the two numbers only a live run knows:
    `Reading`, the readers with a file open, and `Discovered`, what the walk
    has found that no reader has finished with. `stats` is the latest progress
    snapshot, or None when nothing is running.
    """
    from app.core.file_state import funnel_counts

    grouped = store.status_counts(store.offline_volume_ids())
    reading = discovered = 0
    if stats is not None:
        workers = getattr(stats, "workers", None) or {}
        reading = sum(1 for slot in workers.values()
                      if isinstance(slot, dict) and slot.get("file"))
        done = sum(int(getattr(stats, name, 0) or 0)
                   for name in ("indexed", "unchanged", "skipped"))
        # `seen` is roughly `done` plus the bounded queue (see
        # `presenter.progress_for`); for an archive `indexed` counts messages,
        # which can overtake `seen` - hence the floor at zero.
        discovered = max(0, int(getattr(stats, "seen", 0) or 0) - done - reading)
        # 2026-10-04: a file the scan has listed by name (a `PENDING` row) is
        # already counted as Queued by the store - not twice.
        discovered = max(0, discovered - int(grouped["by_status"].get("PENDING", 0) or 0))
    return funnel_counts(grouped["by_status"], grouped["coded"], grouped["offline"],
                         reading=reading, discovered=discovered)


def offline_volume_marks(store: Any, results: Any) -> dict[int, dict]:
    r"""Which of this page's rows are on a catalogued Offline Media volume
    that is not connected right now, and what to say about it. §3a: "on
    **<name>** (offline, scanned <date>) - plug it in to open".

    **Online rows are absent from the returned dict entirely.** **Worker
    only.** The check is `app.search.marks.offline_volumes` since 2026-10-04,
    so the command line and the MCP server mark the same rows; this adds the
    date as the list says it.
    """
    from app.search.marks import offline_volumes

    return {
        file_id: {"name": mark["name"],
                  "scanned": (format_when(mark["scanned_at"] * 1_000_000_000)
                              if mark["scanned_at"] else "")}
        for file_id, mark in offline_volumes(store, results).items()
    }


def placeholder_marks(results: Any) -> set[str]:
    r"""202626270514 3d: which of this page's rows are cloud placeholders
    *right now* - a OneDrive/SharePoint file with Files On-Demand set,
    never hydrated (or dehydrated again since). "3a, the per-file
    placeholder model... applies to NORMAL roots too" is 3a/3b/3c's own
    words: this is not about a catalogued Offline Media volume at all,
    only about an ordinary indexed file whose bytes are not on this
    machine right now.

    **Worker only** - `winfs.is_cloud_placeholder` is a filesystem stat,
    the same cost class `missing_paths` already pays for the identical
    reason. Never raises: a decoration that fails to compute costs a
    missing badge, not the search that found the row.

    Volume-backed rows are never checked here - an offline catalogued
    drive already has its own decoration (`offline_volume_marks`), and a
    letter-free synthetic path would not `Path()` into anything real
    anyway.
    """
    from app.core import winfs

    marks: set[str] = set()
    for row in results or ():
        if getattr(row, "volume_id", None) is not None:
            continue
        text = str(getattr(row, "path", "") or "")
        if not text or text.startswith("pst://"):
            continue
        try:
            if winfs.is_cloud_placeholder(Path(text)):
                marks.add(text)
        except Exception:                         # noqa: BLE001 - see docstring
            continue
    return marks


def record_open(engine: Any, search_id: Any, chunk_id: Any) -> None:
    """A click is worth recording and never worth blocking on. **Worker only.**

    Everything in Layer 10 is derived from these, but this is a database
    *write*, and it was running between the double-click and the file opening -
    so a busy index made opening a result feel slow for a reason that has
    nothing to do with opening it.
    """
    try:
        engine.record_open(search_id, chunk_id)
    except Exception:                            # noqa: BLE001 - never block an open
        pass


def missing_paths(paths: Any) -> set[str]:
    """Which of these no longer exist on disk. **Worker thread only.**

    One filesystem stat per path, so never on the interface thread - see
    `app.search.marks.missing_paths`, where the rule lives since 2026-10-04
    so every search surface marks the same files.
    """
    from app.search.marks import missing_paths as _missing

    return _missing(paths)


def file_row_context(store: Any, rows: Any) -> Any:
    """What a page of Files rows is shown with that the row does not carry.
    **Worker only.** Returns the same row dicts, added to:

    * an attachment's message - `message_sender`, `message_subject`,
      `message_sent_at` - so its folder says who sent it and about what, and
      its date is the message's (the Search list's way, 2026-10-04);
    * a catalogued drive's name, `volume_label`, for "<drive> > folder".

    **One statement for the attachments and one per distinct drive on the
    page** - never one per row, and nothing on the thread that paints. Never
    raises: a missing folder or date is a cosmetic loss, not the list.
    """
    from app.search.marks import parent_messages

    rows = list(rows or ())
    if store is None or not rows:
        return rows
    try:
        parents = parent_messages(store, [(row.get("id"), row.get("path")) for row in rows])
        for row in rows:
            parent = (parents or {}).get(int(row.get("id") or 0))
            if parent is not None:
                row["message_sender"] = parent.get("sender")
                row["message_subject"] = parent.get("subject")
                row["message_sent_at"] = parent.get("sent_at")
        labels: dict[int, str] = {}
        for volume_id in {int(row["volume_id"]) for row in rows
                          if row.get("volume_id") is not None}:
            record = store.get_volume(volume_id) if hasattr(store, "get_volume") else None
            labels[volume_id] = str(getattr(record, "name", "") or "") if record else ""
        for row in rows:
            if row.get("volume_id") is not None:
                row["volume_label"] = labels.get(int(row["volume_id"]), "")
    except Exception as exc:                     # noqa: BLE001 - see docstring
        _log.debug("no attachment or drive context for this page: {}", exc)
    return rows


def volume_labels(store: Any, results: Any) -> dict[int, dict]:
    """`{file_id: {"volume_label": name}}` for a page of search results on a
    catalogued drive - one read per distinct drive. **Worker only.** Merged
    into the page's details so a group's folder reads "<drive> > Photos"
    (`facts.volume_folder`), as the Files list's does."""
    by_volume: dict[int, str] = {}
    out: dict[int, dict] = {}
    for row in results or ():
        volume_id = getattr(row, "volume_id", None)
        if volume_id is None:
            continue
        volume_id = int(volume_id)
        if volume_id not in by_volume:
            try:
                record = store.get_volume(volume_id)
            except Exception:                    # noqa: BLE001 - a folder, not the search
                record = None
            by_volume[volume_id] = str(getattr(record, "name", "") or "") if record else ""
        out[int(getattr(row, "file_id", 0) or 0)] = {"volume_label": by_volume[volume_id]}
    return out


def save_attachment_copy(store: Any, path: str, cache_path: Any, *,
                         reader: Any = None) -> Path:
    """A read-only copy of the attachment or zip member `path` names. 2026-10-04.

    Raises `AppErrorException` (`ERR_ATTACHMENT_OPEN`) with the way out. A
    zip member is read from the zip on disk; an attachment from its archive,
    found by the message's row (`messages.store_path`, `entry_id` and, from
    schema 32, `folder_path` / `folder_index`). `reader` is the seam for
    tests; left out, `pst_attachment.read_attachment`.
    """
    from app.ui.attachment_open import bytes_of, shown_name, write_copy, zip_member_of
    from app.ui.presenter.mail import attachment_of

    message = None
    if not zip_member_of(path)[0]:
        record = store.get_file(attachment_of(path)[0])
        message = store.get_message(record.id) if record is not None else None
    data = bytes_of(path, message, reader=reader)
    return write_copy(path, shown_name(path), data, cache_path)


def archive_of(store: Any, path: str) -> str:
    """The `.pst` an attachment was read from, for Show in folder. 2026-10-04.

    Raises `AppErrorException` (`ERR_ATTACHMENT_OPEN`) when its message is not
    in the index or names no archive.
    """
    from app.core.errors import AppErrorException, make_error
    from app.ui.presenter.mail import attachment_of

    parent, name = attachment_of(path)
    # 2026-10-04: a message's own key too - Mail's Show in folder, like the pane's.
    parent = parent or str(path or "")
    record = store.get_file(parent) if parent else None
    message = store.get_message(record.id) if record is not None else None
    archive = str((message or {}).get("store_path") or "").strip()
    if not archive:
        raise AppErrorException(make_error(
            "ERR_ATTACHMENT_OPEN", "ui.tasks", path=parent or path,
            name=name or "this message",
            details="the index does not say which archive it is in"))
    return archive


def code_line(path: str, char_start: Any) -> int:
    """The line a code hit's passage starts on, read from the file. **Worker.**

    0 when the file cannot be read or the offset is past its end - the hit
    then opens as a file does, which is what it did before (2026-10-04).
    """
    from app.extract.base import line_in_text
    from app.extract.plaintext import read_text

    try:
        text, _degraded, _truncated = read_text(Path(path))
        return line_in_text(text, int(char_start or 0))
    except Exception as exc:                     # noqa: BLE001 - opens at the top instead
        _log.debug("no line for {}: {}", path, exc)
        return 0


def _message_of(store: Any, row: Any, path: str) -> Optional[dict]:
    """The `messages` row a result stands for, or None. Two lookups by key."""
    try:
        file_id = int(getattr(row, "file_id", 0) or 0)
        if file_id <= 0 and "://" in path:
            record = store.get_file(path)
            file_id = int(record.id) if record is not None else 0
        return store.get_message(file_id) if file_id > 0 else None
    except Exception as exc:                     # noqa: BLE001 - opened as a file instead
        _log.debug("no message row for {}: {}", path, exc)
        return None


def open_target(store: Any, row: Any, *, reveal: bool = False, cache_path: Any = None,
                editor: Any = ("auto", ""), engine: Any = None, search_id: Any = None,
                outlook: Any = None) -> Any:
    r"""Open or reveal one row: every page's Open and Show in folder. **Worker.**

    2026-10-04, the owner: "the same code should run for functions so they
    are all consistent". The one body behind `workers.open_row_async`, acting
    on `presenter.opening.plan_for`. Returns an `AppError`, a sentence for the
    toast, a `SearchInside` (a message nothing can open), or None.

    An opened indexed result is recorded for ranking (`record_open`) here,
    whichever page it was opened from - it used to be the Search list only.
    """
    from app.core.errors import AppError, AppErrorException

    from app.ui.presenter.opening import plan_for

    plan = plan_for(row, reveal=reveal)
    try:
        outcome = _carry_out(store, row, plan, cache_path=cache_path,
                             editor=editor, outlook=outlook)
    except AppErrorException as exc:
        outcome = exc.error
    chunk_id = int(getattr(row, "chunk_id", 0) or 0)
    if not reveal and engine is not None and chunk_id > 0 and not isinstance(outcome, AppError):
        record_open(engine, search_id, chunk_id)
    return outcome


def _carry_out(store: Any, row: Any, plan: Any, *, cache_path: Any, editor: Any,
               outlook: Any) -> Any:
    """`open_target`'s steps, for one plan. Raises `AppErrorException`."""
    from app.ui.attachment_open import zip_member_of
    from app.ui.presenter.mail import original_target
    from app.ui.presenter.opening import SearchInside, on_a_volume
    from app.ui.widgets.mail_open import open_original
    from app.ui.workers import open_at_line, open_in_explorer, open_media_at

    how, path = plan.how, plan.path
    if plan.volume:
        # The drive's current mount point first, then the moment or the line
        # on the real file - a volume row used to skip both.
        path = resolve_open_path(store, on_a_volume(row))
    if how == "reveal":
        return open_in_explorer(path, select=True)
    if how == "archive":
        return open_in_explorer(archive_of(store, path), select=True)
    if how == "copy":
        target = save_attachment_copy(store, path, cache_path)
        error = open_in_explorer(str(target), select=False)
        if error is not None:
            return error
        where = "the zip" if zip_member_of(path)[0] else "the email"
        return (f"Opened a copy of '{target.name}' from {where}. "
                f"Changes to it are not saved back to {where}.")
    if how in ("message", "file") and store is not None and not plan.volume:
        # The owner's decision (2026-10-04): Open on a message opens it in
        # Outlook, everywhere - what the preview's "Open in Outlook" does. A
        # `.eml` or `.msg` on disk is a file and opens as one.
        message = _message_of(store, row, path)
        if message:
            target = original_target(message, path)
            return (open_original(target, outlook=outlook) if target is not None
                    else SearchInside(path))
    if how == "message":
        return SearchInside(path)
    if how == "media":
        return open_media_at(path, plan.seconds)
    if how == "code":
        line = plan.line or code_line(path, plan.char_start)
        if line:
            choice, custom = editor or ("auto", "")
            return open_at_line(path, line, choice=choice or "auto", custom=custom or "")
    return open_in_explorer(path, select=False)


def mail_details(store: Any, results: Any) -> dict:
    """Subjects and senders for the messages on one page of results.

    **One query for the page, never one per row**, and an attachment carries
    its parent message's details. **Worker only.** The rule lives in
    `app.search.marks.mail_details` since 2026-10-04, so the MCP server reads
    mail the same batched way rather than one result at a time.
    """
    from app.search.marks import mail_details as _details

    return _details(store, results)


# ---------------------------------------------------------------------------
# Worker bodies
#
# Here rather than in `shell.py` for the reason `read_index_summary` is here:
# `test_ui_never_blocks` reads the window's source and refuses any store call it
# cannot prove is inside a worker, and it cannot prove that about a module-level
# function - correctly, since nothing in the file says so. Keeping the bodies in
# this module makes the rule mechanical rather than a matter of trust.
# ---------------------------------------------------------------------------

def _read_external_run(store: Any) -> dict:
    r"""Is another process indexing, and what does it say about itself?

    **Two questions, and only one of them is authoritative.** `is_indexing`
    asks the mutex, which an operating system releases when a process dies.
    `active_run` reads the row that process last wrote, which survives a crash.
    A record without a lock is a crash rather than a run, and treating it as a
    run would refuse Start until somebody edited a database by hand.

    On a worker, because it runs on a timer for as long as the window is open
    and both halves touch something outside this process.
    """
    from app.core.deeplink import take_pending
    from app.core.run_lock import active_run, is_indexing, take_front_request

    found: dict = {"locked": False, "record": None, "link": None, "front_requested": False}
    try:
        found["locked"] = is_indexing(store)
        found["record"] = active_run(store)
    except Exception as exc:                     # noqa: BLE001 - a watcher, not a run
        _log.debug("could not read the external run state: {}", exc)

    # **Adoptions §7a rides this watcher rather than bringing a timer.** A
    # `leasha://` link is delivered by a second process writing one
    # `index_state` row and exiting - the channel `run_lock` already uses to
    # say "please stop" - and something has to notice. Polling the database
    # every second for the life of every session, so that a link somebody
    # clicks once a week arrives instantly, is not a trade this codebase
    # would make anywhere else. The cost is that a link takes up to the
    # watcher's interval to land, which is said in the order's note.
    try:
        found["link"] = take_pending(store)
    except Exception as exc:                     # noqa: BLE001 - a link, not a run
        _log.debug("could not read the pending link: {}", exc)

    # A second launch that found the window already open writes the same
    # flag `request_stop` uses, then exits. This watcher is the only thing
    # polling `index_state` for as long as the window is open, so fronting
    # rides it rather than adding a timer of its own - see the note above.
    try:
        found["front_requested"] = take_front_request(store)
    except Exception as exc:                     # noqa: BLE001 - a front request, not a run
        _log.debug("could not read the front request: {}", exc)

    # 2026-10-04: whether the images pass is due, which a run in another
    # process (`app.cli index`) records by the same rule as this window's own
    # runs (`run_setup.record_pass`). One row, on the same tick; the window
    # takes it up in `IndexController._adopt_images_due`. Left out when it
    # cannot be read: "unknown" must not overwrite what the window knows.
    from app.index.run_setup import IMAGES_DUE_STATE

    try:
        found["images_due"] = (store.get_state(IMAGES_DUE_STATE, "") or "") == "1"
    except Exception as exc:                     # noqa: BLE001 - a status, not a run
        _log.debug("could not read whether the images pass is due: {}", exc)
    return found


def _scan_and_save(store: Any, roots: list[str]) -> dict:
    """Count the corpus and save the total, so the bar has a denominator.

    The same work `app.cli scan` does, which until now was the only way to get
    one - and the reason a GUI-started index never had a percentage.
    """
    import json

    from app.index.scan import SCAN_STATE_KEY, ScanConfig, scan

    result = scan(ScanConfig(roots=[Path(root) for root in roots]))
    store.set_states({SCAN_STATE_KEY: json.dumps({
        "at": int(_time.time()),
        "roots": list(result.roots),
        "files": result.indexable.files,
        "bytes": result.indexable.bytes,
    })})
    return {"files": result.indexable.files, "bytes": result.indexable.bytes}


# ---------------------------------------------------------------------------
# doctor.py, run and rendered
#
# Here rather than in `settings_view.py` for the reason this module exists: a
# subprocess call and a text formatter are logic, and logic in a Qt widget can
# only be checked by a person clicking a button. The view is left with two
# lines - start a worker, put the result in a text box.
# ---------------------------------------------------------------------------

#: doctor.py probes Outlook over COM and opens LanceDB, so it is slow by nature
#: rather than by accident. Generous, because this now runs in a worker and a
#: timeout that fires early turns a slow answer into no answer.
DOCTOR_TIMEOUT_S = 180


def doctor_report(timeout_s: int = DOCTOR_TIMEOUT_S) -> dict:
    """Run `doctor.py --json --quick` and parse the result.

    **Never call this on the UI thread.** It was called there, and a check that
    can take two minutes with the event loop stopped is a window Windows paints
    "Not Responding" over - indistinguishable from a crash, and unkillable with
    Ctrl+C because Qt never lets the interpreter run to see the signal.

    `CREATE_NO_WINDOW` stops a console flashing up on Windows when the app was
    launched from a shortcut: `pythonw.exe` has no console, so the child would
    otherwise create one of its own.
    """
    import json
    import subprocess
    import sys

    from app.core.config import project_root
    from app.core.errors import AppErrorException, make_error

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    finished = subprocess.run(
        [sys.executable, str(project_root() / "doctor.py"), "--json", "--quick"],
        capture_output=True, text=True, timeout=timeout_s, check=False,
        creationflags=flags,
    )
    try:
        return json.loads(finished.stdout)
    except ValueError:
        # Doctor's diagnostics are worth most at exactly the moment it fails to
        # produce JSON, so stderr is surfaced rather than swallowed.
        detail = (finished.stderr or finished.stdout or "").strip()[:2000]
        raise AppErrorException(make_error(
            "ERR_UNEXPECTED", "ui.doctor",
            details=f"doctor.py exited {finished.returncode} without valid JSON:\n{detail}",
            suggestion=(
                "Run it yourself to see the whole output: "
                r"venv\Scripts\python.exe doctor.py"
            ),
        )) from None


def install_package(package: str, version: str = "", timeout_s: int = 600) -> dict:
    """pip install into this venv. **Blocking - call it from a worker.**

    Lives here rather than in the wizard that wants it, because this module is
    the one allowed to block: `test_ui_never_blocks` skips it by name and
    enforces the guarantee at the call site instead. A pip install can take a
    minute on a cold cache, which on the UI thread is a white window.

    Never raises. The result says what happened and, on failure, the exact
    command to run by hand - an install that fails quietly leaves a reader that
    can never work and nobody knowing why.
    """
    # **`sys` as well as `subprocess`.** This module imports neither at the
    # top, and the reference to `sys.executable` below was a `NameError`
    # waiting on the one path nobody runs in a test - the button that installs
    # a missing reader. "Never raises" was written above it and was not true.
    import subprocess
    import sys

    target = f"{package}=={version}" if version else package
    fix = f"venv\\Scripts\\pip install {target}"

    try:
        finished = subprocess.run(
            [sys.executable, "-m", "pip", "install", target],
            capture_output=True, text=True, timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "package": target,
                "detail": f"pip did not finish within {timeout_s}s",
                "fix": fix}
    except Exception as exc:                     # noqa: BLE001 - boundary
        return {"ok": False, "package": target,
                "detail": f"{type(exc).__name__}: {exc}", "fix": fix}

    if finished.returncode == 0:
        return {"ok": True, "package": target, "detail": "", "fix": ""}

    output = (finished.stderr or finished.stdout or "").strip().splitlines()
    return {
        "ok": False,
        "package": target,
        # The last few lines carry the reason; the rest is resolver noise.
        "detail": " ".join(output[-3:]) if output else f"pip exited {finished.returncode}",
        "fix": fix,
    }


def read_index_summary(store: Any, settings: Any = None) -> dict[str, Any]:
    """Gather everything `index_summary` needs. **Runs in a worker, never on the
    UI thread** - it opens the vector store and walks a folder.

    Returns a payload rather than rows so the failure is data too: a locked
    database produces `{"error": ...}`, which `index_summary` renders as a row
    like any other. The previous version of this returned early on any
    exception, leaving the page blank with nothing to explain it.
    """
    payload: dict[str, Any] = {"error": ""}
    try:
        payload["stats"] = store.stats()
        payload["last_run"] = store.get_state("index:last_run") or ""
        last_run_stats = store.get_state("last_run_stats")
        payload["warned"] = warned_counts(last_run_stats)
        # Order 0z lane D: the junk-image filter's count, from the same record.
        payload["pictures_not_read"] = pictures_not_read_counts(last_run_stats)
    except Exception as exc:                     # noqa: BLE001 - reported, not swallowed
        payload["error"] = f"{type(exc).__name__}: {exc}"
        return payload

    # Work order `dates-live-log-and-interrupted-runs` 3a. Here, on the worker,
    # because it probes the run mutex as well as reading a row. Never raises.
    from app.index.interrupted import read_part_read_archives, read_unfinished_run

    payload["unfinished"] = read_unfinished_run(store)
    # The Indexing page's funnel - counts per status word. Its own guard: a
    # funnel that cannot be read leaves the rest of the summary standing.
    try:
        payload["funnel"] = status_funnel_counts(store)
    except Exception as exc:                     # noqa: BLE001 - one line of the page
        _log.debug("no status funnel: {}", exc)
    # 3c: archives a run stopped inside. Stats each one, so on the worker too.
    payload["part_read"] = read_part_read_archives(store)
    # Order 0z F3: the timed-out files by type, for the panel that offers to
    # read a type again. Its own guard, as the funnel has: absent means "not
    # read", and the panel then leaves what it shows alone.
    try:
        payload["timed_out"] = store.timed_out_groups()
    except Exception as exc:                     # noqa: BLE001 - one panel of the page
        _log.debug("no timed-out groups: {}", exc)

    if settings is None:
        return payload

    payload["data_path"] = str(getattr(settings, "data_path", ""))
    payload["disk_bytes"] = folder_size(getattr(settings, "data_path", None))
    try:
        from app.storage.vector_store import VectorStore

        with VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors:
            payload["vectors"] = vectors.stats() if vectors.exists else {}
    except Exception:                            # noqa: BLE001 - one missing number
        payload["vectors"] = {}
    return payload


def folder_size(path: Any) -> Optional[int]:
    """Bytes under a folder, or None. Never raises and never takes long.

    Capped at a few thousand files: the index is a handful of large files plus a
    model cache, so a full walk is unnecessary, and a diagnostic that stalls on
    a network drive is worse than one that says nothing.
    """
    if not path:
        return None
    from pathlib import Path as _Path

    total = 0
    seen = 0
    try:
        for item in _Path(path).rglob("*"):
            if seen > 5000:
                break
            try:
                if item.is_file():
                    total += item.stat().st_size
                    seen += 1
            except OSError:
                continue
    except OSError:
        return None
    return total or None


#: Suffixes a log folder may contain. **An allow-list, not a deny-list**, and
#: that direction is the whole safety of `clear_logs`: LOG_PATH defaults to a
#: folder inside the project, an owner may well point it somewhere sharing space
#: with something else, and a routine that deletes "everything here" would one
#: day be pointed at a folder that was not only logs. Anything not named here
#: survives, including `README.txt`, which describes the folder structure and is
#: regenerated rather than discarded.
LOG_SUFFIXES: frozenset[str] = frozenset({".log", ".jsonl", ".zip", ".json"})


def log_files(log_path: Any) -> list:
    """Every log file under `log_path`, at any depth. Never raises.

    Returns paths rather than a count, because both callers - the summary and
    the deletion - must agree exactly about what counts as a log, and the only
    way to guarantee that is for them to ask the same function.
    """
    from pathlib import Path as _Path

    if not log_path:
        return []
    found = []
    try:
        for item in _Path(log_path).rglob("*"):
            try:
                if item.is_file() and item.suffix.lower() in LOG_SUFFIXES:
                    found.append(item)
            except OSError:
                continue
    except OSError:
        return []
    return found


def logs_summary(log_path: Any) -> str:
    """The sentence beside the Clear button: how much is there, and where.

    Named before it is deleted. "Clear logs" with no indication of what that
    means is a button people either never press or press and regret, and the
    number is also the answer to *"is this what is filling my disk"*, which is
    the question that makes somebody look for the button at all.
    """
    files = log_files(log_path)
    if not files:
        return f"No log files in {log_path}."
    total = 0
    for item in files:
        try:
            total += item.stat().st_size
        except OSError:
            continue
    return (f"{len(files):,} log file{'' if len(files) == 1 else 's'}, "
            f"{format_size(total)}, in {log_path}")


def clear_logs(log_path: Any, *, keep: Any = ()) -> dict[str, Any]:
    """Delete the log files under `log_path`. Returns what happened.

    **Today's files are kept, and not out of caution.** The application is
    writing to them at the moment the button is pressed: on Windows an open file
    cannot be unlinked, so deleting the current run log fails, and deleting the
    current *day's* application log would succeed on POSIX and leave loguru
    writing into a file with no directory entry - the session's own logging
    silently going nowhere. `keep` is how the caller names those.

    Empty folders are left in place. `ensure_log_dirs` would recreate them on
    the next start, so removing them buys nothing and risks racing a writer.

    Never raises. A file that will not delete is counted and named; the rest are
    still removed, because "clear the logs" failing wholesale over one locked
    file is the least useful outcome available.
    """
    from pathlib import Path as _Path

    protected = {_Path(one).resolve() for one in (keep or ()) if one}
    removed = 0
    freed = 0
    failed: list[str] = []

    kept = 0
    for item in log_files(log_path):
        try:
            if item.resolve() in protected:
                # Counted where it is skipped, not from `len(keep)`: a caller
                # may name a file that does not exist yet - the errors sink
                # writes nothing until the first warning - and claiming to have
                # kept a file that was never there is a small lie in the one
                # sentence somebody reads to check what happened.
                kept += 1
                continue
            size = item.stat().st_size
            item.unlink()
        except OSError as exc:
            failed.append(f"{item.name} ({exc.strerror or exc})")
            continue
        removed += 1
        freed += size

    return {"removed": removed, "freed": freed, "failed": failed, "kept": kept}


def index_bytes(store: Any, settings: Any) -> int:
    """What the index itself weighs: the database, its journal, the vectors.

    **Not `folder_size(data_path)`, and that difference is the whole point.**
    The Indexing page shows the size of the entire data folder, which includes
    the ~130MB embedding-model cache - and the model cache correctly survives a
    reset, since throwing it away would turn the next start into a silent
    download. So on a modest index the headline number barely moves after a
    reset, which is exactly what was reported: *"i reset the index the index
    size remained the same"*.

    This weighs only the two things a reset removes, so the difference across
    one is a true statement about what was given back. Never raises: it exists
    to make a sentence, and no sentence is better than a failed reset.
    """
    total = 0
    try:
        total += int(store.file_bytes())
    except Exception:                            # noqa: BLE001 - one number
        pass
    # Uncapped, unlike `folder_size`: a LanceDB table is a handful of large
    # files, not the thousands that cap exists to bound.
    try:
        from pathlib import Path as _Path

        for item in _Path(str(getattr(settings, "vector_path", "") or ".")).rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def index_counts(store: Any) -> str:
    """The status bar's sentence. **Runs on a worker; see `_refresh_status`.**

    Here rather than in the window because it is two numbers and a sentence,
    and because a view that is under a 250-line guard should not be the place
    the wording lives - `test_every_qt_view_keeps_its_logic_in_the_presenter`.
    """
    stats = store.stats()
    return (f"{stats['files_total']:,} files  ·  "
            f"{stats['chunks_total']:,} chunks indexed")


def read_repo_files(store: Any, row: "RepoRow", *, limit: int = REPO_FILE_LIMIT) -> list[Any]:
    """Every indexed file in one repository. **Runs on a worker, never inline.**

    Prefers `store.repo_files`, which reads the indexed `files.repo_id` column
    directly. Falls back to walking `iter_files` and matching the repository
    root as a path prefix, so the tab works against a store that predates that
    accessor - correct either way, and only the speed differs.

    The fallback asks for `source_kind="file"` so an archive of 200,000 emails
    is excluded in SQL rather than turned into 200,000 objects and discarded.
    """
    dedicated = getattr(store, "repo_files", None)
    if callable(dedicated) and row.repo_id:
        return list(dedicated(row.repo_id, limit=limit + 1))

    root = _comparable(row.root)
    if not root:
        return []
    found: list[Any] = []
    for record in store.iter_files(source_kind="file"):
        if _comparable(getattr(record, "path", "")).startswith(root):
            found.append(record)
            if len(found) > limit:
                break
    return found


def _comparable(path: str) -> str:
    """A path in the one shape prefix comparisons can trust.

    Windows gives back `D:\\Code\\Leasha` and `d:/code/leasha` for the same
    folder, and a prefix test on the raw strings answers no to both.
    """
    return str(path or "").replace("\\", "/").rstrip("/").lower()


def code_rows_for(store: Any, scope: Any, route: Any,
                  *, cached: Any = None, limit: int = 500) -> list[dict]:
    r"""The Code list, for the current scope and the current query.

    **Two engines, one shape**, so the table, the preview and the row menu do
    not know which one ran. Both end as the mappings `repo_file_rows` reads.

    **But only one of them may run behind a keystroke.** A branch scope is
    answered by `git ls-tree`, a subprocess: fetching it per keystroke is the
    first non-negotiable broken, and `test_nothing_that_runs_on_a_keystroke_
    imports_this` caught exactly that in the first version of this function.
    So the listing is fetched **once per selection**, by the pane that owns the
    tree, and handed in as `cached`; typing then filters it in memory. The
    listing does not change while somebody types, so this is both correct and
    far faster than the version the guard rejected.

    The index path has no such problem - it is an indexed query that applies
    the text in SQL - so it still runs per keystroke.

    **The switches compose with the scope** - *"dont forget the code switches
    apply there too"*. The tree says where to look, the box says what to look
    for. A typed `/type` beats the configured code types on either path, so a
    branch and the working tree filter identically.
    """
    types = code_type_filter(store)
    text = str(getattr(route, "text", "") or "")
    typed = tuple(getattr(route, "extensions", ()) or ())
    parsed = getattr(route, "parsed", None)

    if cached is not None:
        return git_rows_matching(
            cached, text=text, extensions=typed, types=types,
            paths=tuple(getattr(parsed, "paths", ()) or ()),
            names=tuple(getattr(parsed, "names", ()) or ()),
        )[:limit]

    repo = (str(getattr(scope, "repo", "") or "")
            or str(getattr(route, "repo", "") or ""))
    if parsed is None:                   # a git route: no parse to apply
        return list(store.code_files(
            text, repo=repo, ext=list(typed) or types, limit=limit))

    # **One query, and that is the point.** `code_files` understood `repo`,
    # `text` and `ext`; `browse_files` understands every switch, through the
    # same definition Files and the search box use. Keeping both would have
    # meant two answers to "what does `/path` mean here", which is the split
    # this whole change exists to close.
    #
    # The tree's selection is injected as `repos` rather than passed beside the
    # parse, so a repository chosen on the left and one typed as `/repo` reach
    # the filter by the same route and cannot disagree.
    #
    # **Scoped to `code`, which is what keeps this the Code tab.**
    # `file_filter_sql` reads that scope as `f.repo_id IS NOT NULL` - "in a
    # repository", the same narrowing the search box's Code chip applies.
    # Without it this would list the whole index, which is the one thing a tab
    # about repositories must not do.
    if repo:
        from dataclasses import replace as _replace

        parsed = _replace(parsed, repos=(repo,))
    return list(store.browse_files(
        parsed.scoped("code"), limit=limit,
        extra_ext=None if typed else types))


def search_check_lines() -> list[str]:
    r"""Run the built-in search check and return its report as lines.
    **Runs on a worker.**

    `app.cli evaluate --builtin` is what the installer tells a person to run to
    "prove search works, in about ten seconds"; a person in the window had no
    button for it. Same measurement, same wording (`Report.lines()`): a small
    corpus with known answers, keyword only, in a throwaway database - it never
    reads the person's own index, so it is safe to press any time. The numbers
    are optimistic by design (the report says so) and only answer "does the
    mechanism work" and "did a change break something".
    """
    import tempfile

    from app.search import keyword
    from app.search.commands import expand_slashes
    from app.search.evaluate import evaluate
    from app.search.query import parse_query
    from app.storage.sqlite_store import SqliteStore

    try:
        from tests.fixtures.evaluation import CORPUS, QUESTIONS, load_into
    except ImportError:
        return ["The built-in check needs the test fixtures, which are not "
                "installed with this copy of Leasha."]

    with tempfile.TemporaryDirectory(prefix="leasha-check-") as folder:
        with SqliteStore(Path(folder) / "check.db") as store:
            load_into(store)

            def search(query: str) -> list[str]:
                parsed = parse_query(expand_slashes(query))
                return [hit["path"] for hit in keyword.search(store, parsed, limit=1)]

            report = evaluate(
                QUESTIONS, search, k=1, mode="built-in corpus, keyword only",
                note=f"{len(CORPUS)} documents, a small clean corpus - every number "
                     "here is optimistic. It shows the mechanism works, not how well "
                     "your own archive is searched.")
    return list(report.lines())


def repo_health_notes(store: Any, *, limit: int = 3) -> list[str]:
    r"""The sentences the Code tab shows when a "repository" is not a checkout.
    **Runs on a worker.**

    Order 202626081149 section 3 built `app.index.repo_health` after a copy of
    this project's own `.git` dragged into a document archive adopted 44% of
    the corpus, and its own docstring says the warning "has to arrive where
    somebody meets it". Nothing ever called it, so the only place the warning
    lived was `app.cli repos`. Judged against the default code extensions, not
    the tab's current filter - a person who has narrowed the tab to one
    language has not made the archive a checkout.
    """
    from app.core.code_types import DEFAULT_PRESET, extensions_for
    from app.index.repo_health import describe, suspicion

    extensions = extensions_for(DEFAULT_PRESET) or frozenset()
    counts = store.repo_code_counts(sorted(extensions))
    notes: list[str] = []
    for row in store.repos_list():
        files = int(row.get("files") or 0)
        reason = suspicion(files=files, code_files=counts.get(int(row["id"]), 0))
        sentence = describe(str(row.get("name") or ""), str(row.get("root_path") or ""), reason)
        if sentence:
            notes.append(sentence)
    if len(notes) > limit:
        extra = len(notes) - limit
        notes = notes[:limit] + [f"...and {extra} more repositories look the same way."]
    return notes


def code_rows_and_repos(store: Any, scope: Any, route: Any, *, cached: Any = None,
                        repos: Any = (), limit: int = 500) -> dict:
    r"""The Code list **and** which repositories hold a match, in one worker.

    Two things the panes need, fetched together on purpose. The order's
    constraint is that *"the count must come from the same query the list ran"* -
    a second, separately-scheduled count is how a tree and a list come to
    disagree, which is the code-tab order's §5.

    It also keeps the aggregate off the UI thread. Narrowing the tree in the
    slot that paints the rows would put a store read on the keystroke path,
    which `test_ui_never_blocks` refuses and is right to.
    """
    rows = code_rows_for(store, scope, route, cached=cached, limit=limit)
    return {"rows": rows,
            "matches": code_content_matches(store, scope, route, cached=cached),
            "matching": matching_repos(store, route, cached=cached, repos=repos)}


def code_rows_typed(store: Any, scope: Any, raw: str, *, cached: Any = None,
                    repos: Any = (), limit: int = 500, preferences: Any = None,
                    declined: Any = ()) -> dict:
    """`code_rows_and_repos` for a typed line, read the shared way. **Worker.**

    The same plain-English reading as every other tab (`read_box`), applied
    before the route is worked out. **A reading never sends a line to git**:
    the view only calls this for an index search, and if the applied filters
    somehow read as a git switch the line is routed as typed instead.
    """
    from app.search.commands import expand_slashes
    from app.search.policy import from_settings
    from app.ui.presenter.code import code_route
    from app.ui.presenter.search import auto_filters

    query, applied = auto_filters(store, expand_slashes(str(raw or "").strip()),
                                  from_settings("code", preferences), tuple(declined or ()))
    route = code_route(query)
    if route.engine != "index":
        route, applied = code_route(raw), ()
    payload = code_rows_and_repos(store, scope, route, cached=cached, repos=repos,
                                  limit=limit)
    payload.update(applied=tuple(applied), parsed=route.parsed)
    return payload


def code_content_matches(store: Any, scope: Any, route: Any, *, cached: Any = None) -> list:
    r"""Order 0y §2: the lines of code that hold what was typed. **On the worker.**

    Index only (`app.search.code_search`), with line numbers read from the files
    inside a small time budget. None of it for a branch scope (`cached`): that
    list comes from `git ls-tree` and describes a tree the index does not hold,
    so a line from the working copy would be a claim about the wrong version.
    """
    from dataclasses import replace as _replace

    from app.search.code_search import code_matches, resolve_lines

    parsed = getattr(route, "parsed", None)
    if cached is not None or parsed is None:
        return []
    repo = (str(getattr(scope, "repo", "") or "")
            or str(getattr(route, "repo", "") or ""))
    if repo:
        parsed = _replace(parsed, repos=(repo,))
    try:
        found = code_matches(store, parsed, types=code_type_filter(store))
    except Exception as exc:                 # noqa: BLE001 - the file list still answers
        _log.debug("code content search failed: {}", exc)
        return []
    return resolve_lines(found)


def matching_repos(store: Any, route: Any, *, cached: Any = None,
                   repos: Any = ()) -> Optional[set]:
    r"""Which repositories hold a match, or None for "no criteria - show all".

    **The git tree listed every repository whatever was typed.** Type a term
    matching files in one checkout and the other three sat there as though they
    matched too - and a tree is read as *"these are the repositories that have
    what you asked for"*, which made it the most misleading pane here. Same
    fault as `code_type_filter` and the empty-list states in the code-tab order:
    a pane showing something the query did not ask for, with nothing saying why.

    `None` rather than "all of them" is the distinction that matters. An empty
    box asks nothing, so every repository is shown and no count is claimed; a
    box with criteria narrows, and says `2 of 4`. Collapsing the two would put
    a meaningless "4 of 4" on screen for somebody who has typed nothing.

    **Never a subprocess.** A branch scope is answered from the listing the tree
    already fetched for the selection - `cached` - because fetching one per
    keystroke is the first non-negotiable broken, and the guard in
    `test_nothing_that_runs_on_a_keystroke_imports_this` caught exactly that
    once already.
    """
    parsed = getattr(route, "parsed", None)
    text = str(getattr(route, "text", "") or "").strip()
    typed = tuple(getattr(route, "extensions", ()) or ())
    has_criteria = bool(text or typed or (parsed is not None and parsed.has_filters))
    if not has_criteria:
        return None

    if cached is not None:
        # A branch listing, already in memory. Filtering it is the same work the
        # list does, so the two cannot disagree.
        rows = git_rows_matching(
            cached, text=text, extensions=typed,
            types=code_type_filter(store),
            paths=tuple(getattr(parsed, "paths", ()) or ()),
            names=tuple(getattr(parsed, "names", ()) or ()),
        )
        found = {str(row.get("repo") or "") for row in rows}
        return {name for name in found if name}

    if parsed is None:
        return None                      # a git route: the tree is not narrowed

    try:
        ids = store.repos_with_matches(
            parsed.scoped("code"),
            extra_ext=None if typed else code_type_filter(store))
    except Exception as exc:             # noqa: BLE001 - a tree, not a search
        _log.debug("could not narrow the repository tree: {}", exc)
        return None

    def field(row: Any, key: str) -> Any:
        """Rows arrive as `sqlite3.Row` here and as objects in the tests."""
        try:
            return row[key]
        except (TypeError, KeyError, IndexError):
            return getattr(row, key, None)

    by_id = {int(field(repo, "id") or 0): str(field(repo, "name") or "")
             for repo in (repos or ())}
    return {by_id[one] for one in ids if one in by_id and by_id[one]}


def resolve_open_path(store: Any, row: Any) -> str:
    r"""The real path to open for one result row. **Worker only.**

    Straight through for an ordinary file. For one on a catalogued volume
    (Offline Media, order 202626270513), `row.path` is never a real
    filesystem path - it is the letter-free key `volume_synthetic_path`
    builds - and 1b requires resolution through the volume's *current*
    mount point, every time, which means a live Windows volume check and
    therefore never the interface thread.

    Raises `AppErrorException` (`ERR_FILE_CORRUPT`) when a volume-backed
    row's volume is not connected right now - the same code
    `open_in_explorer` already raises for an ordinary missing file, so
    "cannot reach it" reads the same way in the error box regardless of
    which kind of unreachable produced it.
    """
    volume_id = getattr(row, "volume_id", None)
    if volume_id is None:
        return str(getattr(row, "path", ""))

    from app.core.errors import raise_error
    from app.index.offline_media import resolve_file_path

    resolved = resolve_file_path(store, row)
    if resolved is None:
        raise_error(
            "ERR_FILE_CORRUPT", "ui.open",
            path=str(getattr(row, "path", "")),
            suggestion="This file is on a catalogued drive that is not "
                      "plugged in right now. Plug it in and try again.",
            details="Volume not currently connected.",
        )
    return str(resolved)


def answer_model_menu(settings: Any, engine: str = "", ollama_name: str = "", *,
                      ollama: bool = True) -> dict:
    r"""Every model that can answer, and the one Settings would use. **Worker.**

    2026-10-04, the owner: "if multiple models are available they should be listed
    so they can be changed at chat or search time". For the Chat tab's drop-down and
    Interpret's menu on the Search page: the ONNX chat models that are downloaded
    (the catalogue, then the disk) and Ollama's installed text models (`/api/tags`,
    a local request that fails quickly when Ollama is not running).

    `engine` and `ollama_name` say what the caller uses now ("" for Settings'); the
    answer's `"default"` is that model's option. `"free_mb"` is the memory free right
    now, for loading a model ahead of time. `ollama=False` leaves Ollama unasked - the
    start-up preload when nothing in use is Ollama's. Never raises.
    """
    from app.chat.llm import probe_installed
    from app.chat.roles import InstalledModels, answer_options, default_option
    from app.llm.engines import engine_of

    onnx_rows: list[tuple[str, str, int]] = []
    serving = ""
    cache = getattr(settings, "model_cache", None)
    try:
        from app.ort import catalogue, hub

        if cache:
            rows = catalogue.load().for_job("chat")
            for entry in [e for e in rows if e.is_verified] + [e for e in rows if not e.is_verified]:
                if hub.resolve(entry.model(), Path(cache)) is not None:
                    onnx_rows.append((entry.key, entry.label, int(entry.approx_mb)))
            found = catalogue.best("chat", Path(cache))
            serving = found[0].key if found is not None else ""
    except Exception as exc:                     # noqa: BLE001 - Ollama's may still be listed
        _log.debug("the downloaded chat models could not be listed: {}", exc)
    installed = probe_installed(
        str(getattr(settings, "ollama_url", "") or "http://127.0.0.1:11434"),
        timeout=3.0) if ollama else InstalledModels()
    options = answer_options(onnx_rows, installed)
    free_mb = 0
    try:
        import psutil

        free_mb = int(psutil.virtual_memory().available / 1024 ** 2)
    except Exception as exc:                     # noqa: BLE001 - unknown means "do not preload"
        _log.debug("free memory unknown: {}", exc)
    name = ollama_name or str(getattr(settings, "ollama_model", "") or "")
    return {"options": options, "free_mb": free_mb,
            "default": default_option(options, engine or engine_of(settings), serving, name)}


def interpret_client(settings: Any, value: str, *, warm: bool = False) -> Any:
    """The client Interpret talks to for a model picked on the Search page, loaded
    when `warm` (2026-10-04). **Worker** - an ONNX model is looked up in the
    catalogue and may load 1-2 GB. `None` for a value that names no model."""
    from types import SimpleNamespace

    from app.chat.roles import parse_option
    from app.llm.engines import text_model

    engine, name = parse_option(value)
    if not engine:
        return None
    chosen = SimpleNamespace(
        chat_engine=engine, model_cache=getattr(settings, "model_cache", None),
        embed_device=getattr(settings, "embed_device", "auto"),
        ollama_url=getattr(settings, "ollama_url", "http://127.0.0.1:11434"), ollama_model=name)
    client = text_model(chosen, name if engine == "ollama" else "",
                        onnx_model=name if engine == "onnx" else "")
    if warm and hasattr(client, "warm"):
        try:
            client.warm()
        except Exception as exc:                 # noqa: BLE001 - the first press pays instead
            _log.debug("Interpret's model was not warmed: {}", exc)
    return client
