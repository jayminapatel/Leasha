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
from app.ui.presenter.code import REPO_FILE_LIMIT, code_type_filter, git_rows_matching
from app.ui.presenter.formatting import format_size, format_when
from app.ui.presenter.repos import RepoRow

_log = logger.bind(component="ui.presenter")


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


def filter_offer_notices(store: Any, sentence: str, preferences: Any = None) -> list:
    r"""Offers for the filters the rules read out of a sentence. **Worker only.**

    `translate_rules.read` asks the store for its known senders and file types,
    which is a query - so this is here, off the interface thread, and the view
    only ever receives the finished notices. Never raises.
    """
    try:
        from app.search.policy import from_settings
        from app.ui.presenter.search import chips_for, filter_offers

        chips = chips_for(store, sentence, from_settings("search", preferences))
        return filter_offers(chips, sentence)
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
    return {
        "details": mail_details(store, results),
        "missing": missing_paths(
            getattr(row, "path", "") for row in results or ()
            if getattr(row, "volume_id", None) is None
        ),
        "volumes": offline_volume_marks(store, results),
        "placeholders": placeholder_marks(results),
    }


def offline_volume_marks(store: Any, results: Any) -> dict[int, dict]:
    r"""Which of this page's rows are on a catalogued Offline Media volume
    that is not connected right now, and what to say about it. §3a: "on
    **<name>** (offline, scanned <date>) - plug it in to open".

    **Online rows are absent from the returned dict entirely.** They open
    normally through 1b's ordinary resolution and 3a asks for nothing to
    be said about them - a decoration on every row of a drive that is
    plugged in right now would be noise, not information.

    **Worker only** - `connected_volumes` is a live Windows volume check,
    the same reason `missing_paths` is worker-only. Never raises: a
    decoration that fails to compute costs a missing sentence, not the
    search that found the row.
    """
    rows = [row for row in (results or ()) if getattr(row, "volume_id", None) is not None]
    if not rows:
        return {}
    from app.index.offline_media import connected_volumes

    try:
        online = connected_volumes(store)
    except Exception:                            # noqa: BLE001 - a decoration, not the search
        online = {}

    marks: dict[int, dict] = {}
    volumes_seen: dict[int, Any] = {}
    for row in rows:
        volume_id = int(row.volume_id)
        if volume_id in online:
            continue
        if volume_id not in volumes_seen:
            try:
                volumes_seen[volume_id] = store.get_volume(volume_id)
            except Exception:                     # noqa: BLE001
                volumes_seen[volume_id] = None
        record = volumes_seen[volume_id]
        if record is None:
            continue
        scanned_at = int(getattr(record, "last_scanned_at", 0) or 0)
        marks[int(getattr(row, "file_id", 0))] = {
            "name": record.name,
            "scanned": format_when(scanned_at * 1_000_000_000) if scanned_at else "",
        }
    return marks


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

    `Path.exists()` is a filesystem stat: microseconds on a warm local disk,
    *seconds* on a network share or a drive that has spun down. It was being
    called once per row while filling the results model - twenty stats for a
    normal page, five hundred for a full one - on the UI thread, inside the
    virtualisation work whose whole purpose was to make that list cheap.

    Done once per result set, off-thread, and passed in. Never raises: a
    disconnected drive means "cannot open it", not a crash, and a result whose
    file has vanished is a real finding that must still be shown.
    """
    from pathlib import Path as _Path

    missing = set()
    for path in paths or ():
        text = str(path or "")
        # A message lives inside a .pst and has no file of its own; statting a
        # synthetic key would report every message as missing.
        if not text or text.startswith("pst://"):
            continue
        try:
            if not _Path(text).exists():
                missing.add(text)
        except OSError:
            continue
    return missing


#: §5b. The path convention `email_pst.py`'s `_attachment_documents` writes:
#: `f"{message_key}/attachments/{name}"`. Read back here rather than carried
#: as a column, because no schema holds the link - the file's own `path`
#: already says everything needed, and reading it beats a migration nobody
#: asked this order to make.
_ATTACHMENT_MARKER = "/attachments/"


def _attachment_parent_path(path: str) -> str:
    """The message this attachment belongs to, or `""` if `path` is not one.

    **Only the PST-via-Outlook attachment convention produces this shape.**
    A standalone `.eml`/`.msg` or an mbox message never separately indexes
    its attachments - only their *names*, inside the message's own text and
    `has_attach` - so those never reach here at all; `/has attachment` still
    finds the message, just never gets a row of its own for what was
    attached to it.
    """
    text = str(path or "")
    index = text.find(_ATTACHMENT_MARKER)
    return text[:index] if index > 0 else ""


def mail_details(store: Any, results: Any) -> dict:
    """Subjects and senders for the messages on one page of results.

    **One query for the page, never one per row.** At the fetch depth grouping
    needs, a per-row lookup is fifty queries per keystroke - the shape of
    slowness that gets blamed on the search itself.

    **§5b's exception, and it is a real one.** An attachment's own file_id
    has no row in `messages` - it is not itself a message - so its *parent's*
    row is what supplies "its message is the context" (§5b). The parent is
    found by path (`_attachment_parent_path`), which costs one indexed
    `get_file` lookup per *distinct attachment* on the page - never per row,
    and zero when a page holds no attachments at all, which is nearly every
    page. A bulk by-path lookup in `sqlite_store.py` would remove even that,
    and is the natural next step for whoever next has that file open; it is
    outside this order's file scope today.

    Never raises. A missing subtitle is a cosmetic loss; failing the search that
    produced it is not, and a store that has been closed underneath a worker is
    a normal condition during shutdown rather than an error.
    """
    if store is None or not hasattr(store, "messages_for"):
        return {}
    try:
        results = list(results or ())
        file_ids = [getattr(r, "file_id", 0) for r in results]

        parent_id_of: dict[int, int] = {}
        if hasattr(store, "get_file"):
            # **Keyed by path, not by row.** Several attachments can share one
            # parent message - a reply with the same two files re-attached is
            # the ordinary case - and resolving each would be exactly the
            # per-row lookup this function's own docstring exists to avoid.
            resolved: dict[str, Optional[int]] = {}
            for result in results:
                file_id = getattr(result, "file_id", 0)
                parent_path = _attachment_parent_path(getattr(result, "path", ""))
                if not parent_path:
                    continue
                if parent_path not in resolved:
                    try:
                        record = store.get_file(parent_path)
                    except Exception:              # noqa: BLE001 - a subtitle, not the search
                        record = None
                    resolved[parent_path] = record.id if record is not None else None
                parent_id = resolved[parent_path]
                if parent_id is not None:
                    parent_id_of[file_id] = parent_id

        wanted = list(dict.fromkeys([*file_ids, *parent_id_of.values()]))
        details = dict(store.messages_for(wanted))
        for file_id, parent_id in parent_id_of.items():
            parent_detail = details.get(parent_id)
            if parent_detail:
                # Marked so `_build_group` draws this as an attachment whose
                # *parent's* detail this is, never as a message in its own
                # right - the two share every other key.
                details[file_id] = {**parent_detail, "attachment_of": parent_id}
        return details
    except Exception:                            # noqa: BLE001 - see docstring
        return {}


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
    except Exception as exc:                     # noqa: BLE001 - reported, not swallowed
        payload["error"] = f"{type(exc).__name__}: {exc}"
        return payload

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
            "matching": matching_repos(store, route, cached=cached, repos=repos)}


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
