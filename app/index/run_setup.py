r"""What every index run is set up from, whoever starts it.

Layer: L3

*Added 4 October 2026, the owner: "where ever possible the same code should
run for functions so they are all consistent and standard".* An index run is
started from six places - Start in the window, the same run in a separate
process (`app.cli index --events jsonl`), the command line, the folder watch,
an Offline Media Scan or Rescan, and "Retry with a longer time limit" - and
each had grown its own copy of the decisions below. The copies had drifted:
the window's run read Leasha's own folders, the PST reader chosen in Settings
reached only the window's own run, an Offline Media scan read every picture
on a text pass and wrote no picture-search vectors, a retry in a separate
process took a different pass from the same retry in the window, and only the
window knew the images pass was due. So each decision is made here, once, and
every one of those places calls it:

* `build_pipeline_config` - the `PipelineConfig` itself, from the settings
  and the few things that are the caller's to decide (which folders, prune or
  not, the pass);
* `pass_for` / `pass_for_store` - which pass a run is (`both`, `text` or
  `images`), from `INDEX_OCR_MODE` (what), `INDEX_OCR_PASS` (when) and
  whether a finished text pass left the images due;
* `images_due_after` / `record_pass` - what a finished run means for the
  next one's pass;
* `saved_cloud_content_keys` - the cloud-content opt-ins the window saves;
* `apply_saved_pst_backend` - the PST reader chosen in Settings, applied by
  `Pipeline.run` itself so no run can miss it.

Nothing here imports Qt, and nothing here is slow: each store read is one
row of `index_state`, made once per run, never per file.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

from app.core.logging import logger

__all__ = [
    "CLOUD_SWITCH_STATE_KEY",
    "IMAGES_DUE_STATE",
    "LAST_RUN_STATE",
    "NOW",
    "OCR_MODES",
    "PST_BACKEND_STATE_KEY",
    "SCHEDULED",
    "WATCH",
    "apply_saved_pst_backend",
    "build_pipeline_config",
    "covers_saved_folders",
    "images_due_after",
    "ocr_schedule",
    "ocr_what",
    "pass_for",
    "pass_for_store",
    "read_images_due",
    "record_pass",
    "run_kind",
    "saved_cloud_content_keys",
]

log = logger.bind(component="index.run_setup")

#: Store state: a text pass has finished and its images are still to read, so
#: the next run under "after-run" is the images pass (2026-10-04). "1" or "".
IMAGES_DUE_STATE = "index:images_pass_due"
#: Store state: the PST reader chosen in Settings - `auto`, `libpff` or
#: `outlook` (`email_pst.PstBackend`). Written by the Settings page.
PST_BACKEND_STATE_KEY = "ui:pst_backend"
#: Store state: the master switch for reading cloud-only files ("on"/"off").
#: The per-folder opt-ins are `walker.CLOUD_CONTENT_STATE_KEY`.
CLOUD_SWITCH_STATE_KEY = "ui:index_cloud"
#: Store state: when the last whole run over the saved folders finished, as
#: ISO time - what the window's schedule counts its interval from.
LAST_RUN_STATE = "index:last_run"

#: Which kind of run is asking which pass it is.
#:
#: * `SCHEDULED` - Start, a command-line run over the saved folders, the same
#:   run in a separate process: under "after-run" the text and images passes
#:   take turns, and the run records which one it was.
#: * `WATCH` - a folder-watch batch: a handful of files just saved. Never the
#:   images pass (it would leave the saved documents unread) and it records
#:   nothing - a batch is not the corpus's text pass.
#: * `NOW` - a retry of timed-out files, an Offline Media drive, "Index now"
#:   on one folder, a command-line run over folders it names: files asked
#:   for now. They are read as `INDEX_OCR_MODE` says, whatever the schedule,
#:   because the schedule's second pass would not come back for them - a
#:   retry reads nothing else, a drive is unplugged afterwards, and the
#:   images pass reads the whole index's ledger, not one folder's. Records
#:   nothing either.
#:
#: *2026-10-04, code review:* "Index now" on one folder and a command-line
#: run over named folders were `SCHEDULED`, so they read the index-wide
#: "images due" flag and wrote it: one folder's text pass made the next Start
#: over every folder the images pass, and one folder's images pass (which
#: read every held scan in the index) cleared it. `run_kind` decides now:
#: only a run over the saved folder set is `SCHEDULED`.
SCHEDULED = "scheduled"
WATCH = "watch"
NOW = "now"

#: The passes a run can be. *2026-10-04, code review:* declared here, where
#: the pass is decided, and imported by `app.index.pipeline` - so the What
#: gets read page (`presenter/coverage.py`) can ask `ocr_what` without
#: importing the whole pipeline on the window's thread.
OCR_MODES = ("both", "text", "images")

_SCHEDULES = ("with-run", "after-run", "manual")


def _folder_keys(folders: Any) -> frozenset[str]:
    from app.core.osbridge.pathnames import path_key

    return frozenset(path_key(str(Path(str(folder)).expanduser()).rstrip("\\/"))
                     for folder in (folders or ()) if str(folder).strip())


def covers_saved_folders(roots: Any, saved: Any) -> bool:
    """Is a run over `roots` a run over the saved folder set - the same
    folders, by `path_key`, in any order and spelling? False when nothing is
    saved: a run with nothing to compare against is a run over its own
    folders. No I/O."""
    wanted = _folder_keys(saved)
    return bool(wanted) and _folder_keys(roots) == wanted


def run_kind(*, whole: bool, retry: bool = False) -> str:
    """`SCHEDULED` for a run over the saved folder set, `NOW` for any other
    (one folder, named folders, a retry). See `NOW` above."""
    return SCHEDULED if whole and not retry else NOW


# ---------------------------------------------------------------------------
# Which pass
# ---------------------------------------------------------------------------

def ocr_schedule(settings: Any) -> str:
    """`INDEX_OCR_PASS`, normalised: `with-run`, `after-run` or `manual`."""
    value = str(getattr(settings, "index_ocr_pass", "with-run")
                or "with-run").strip().lower()
    return value if value in _SCHEDULES else "with-run"


def ocr_what(settings: Any) -> str:
    """`INDEX_OCR_MODE`, normalised: `both`, `text` or `images`."""
    value = str(getattr(settings, "index_ocr_mode", "both") or "both").strip().lower()
    return value if value in OCR_MODES else "both"


def pass_for(settings: Any, kind: str = SCHEDULED, *, images_due: bool = False) -> str:
    r"""Which pass this run is: `both`, `text` or `images`.

    `INDEX_OCR_MODE` says what; `INDEX_OCR_PASS` says when. They meet here
    because a run is only ever one pass: choosing to do the images after the
    run means *this* run is the text one, and the images are a second run.
    Under "after-run", the run after a finished text pass is that second run
    (`images_due`). `manual` is always the text pass - the images pass is
    asked for by name (`--only-ocr`).
    """
    if kind == NOW:
        return ocr_what(settings)
    schedule = ocr_schedule(settings)
    if schedule == "after-run" and images_due and kind == SCHEDULED:
        return "images"
    if schedule in ("after-run", "manual"):
        return "text"
    what = ocr_what(settings)
    # *2026-10-04, code review:* "with-run" with `INDEX_OCR_MODE=images` made
    # a folder-watch batch the images pass - the file just saved, a document,
    # was walked past unread. A batch is never the images pass (`WATCH`).
    if kind == WATCH and what == "images":
        return "text"
    return what


def read_images_due(store: Any) -> bool:
    """Whether a finished text pass left its images to read. Never raises:
    an unreadable answer is "no", which is a text pass - the old behaviour."""
    try:
        return (store.get_state(IMAGES_DUE_STATE, "") or "") == "1"
    except Exception as exc:                         # noqa: BLE001 - see docstring
        log.debug("could not read whether the images pass is due: {}", exc)
        return False


def pass_for_store(settings: Any, store: Any, kind: str = SCHEDULED) -> str:
    """`pass_for`, asking the store whether the images are due - only when the
    answer could change the pass, so a "with-run" run reads nothing extra."""
    due = (kind == SCHEDULED and ocr_schedule(settings) == "after-run"
           and read_images_due(store))
    return pass_for(settings, kind, images_due=due)


def images_due_after(settings: Any, ocr_mode: str, *, kind: str = SCHEDULED,
                     finished: bool = True) -> Optional[bool]:
    """What a run that has ended means for the next one's pass.

    True: the images are due (a text pass finished). False: they are not (the
    images pass finished). None: nothing changes - not "after-run", not a
    scheduled run, or a run that was stopped or failed part-way, which the
    next run carries on as the same pass.
    """
    if kind != SCHEDULED or not finished or ocr_schedule(settings) != "after-run":
        return None
    return str(ocr_mode or "") != "images"


def record_pass(store: Any, settings: Any, ocr_mode: str, *, kind: str = SCHEDULED,
                finished: bool = True) -> Optional[bool]:
    """`images_due_after`, written to the store. For a run's own thread (the
    command line, the separate process); the window writes the same value
    through its queued writes. Never raises: the run itself has succeeded."""
    due = images_due_after(settings, ocr_mode, kind=kind, finished=finished)
    if due is None:
        return None
    try:
        store.set_state(IMAGES_DUE_STATE, "1" if due else "")
    except Exception as exc:                         # noqa: BLE001 - see docstring
        log.warning("could not record whether the images pass is due: {}", exc)
    return due


# ---------------------------------------------------------------------------
# What the window saved
# ---------------------------------------------------------------------------

def saved_cloud_content_keys(store: Any) -> frozenset[str]:
    """The folders whose cloud-only files may be downloaded, as the window saved
    them - none at all while the master switch is off, as in the window. Never
    raises: unreadable means none, the safe direction."""
    from app.index.walker import CLOUD_CONTENT_STATE_KEY, load_cloud_content_roots

    try:
        if (store.get_state(CLOUD_SWITCH_STATE_KEY, "") or "") != "on":
            return frozenset()
        return load_cloud_content_roots(store.get_state(CLOUD_CONTENT_STATE_KEY, "") or "")
    except Exception as exc:                         # noqa: BLE001 - see docstring
        log.debug("could not read the saved cloud-content folders: {}", exc)
        return frozenset()


def apply_saved_pst_backend(store: Any) -> Optional[str]:
    """Point the PST reader at the route chosen in Settings, if one was saved.

    Called by `Pipeline.run` at the start of every run, on the run's own
    thread, so the window's run, the separate process, the command line, the
    folder watch and Offline Media all read a `.pst` the same way. Before,
    only the window's own process was told, and every other run fell back to
    `auto`. Nothing saved leaves the reader as it is. Never raises.
    """
    from app.extract.email_pst import PstBackend

    try:
        chosen = str(store.get_state(PST_BACKEND_STATE_KEY, "") or "").strip().lower()
    except Exception as exc:                         # noqa: BLE001 - see docstring
        log.debug("could not read the PST reader choice: {}", exc)
        return None
    if chosen not in PstBackend.ALL:
        return None
    try:
        from app.extract.base import extractor_for

        extractor = extractor_for(Path("x.pst"))
        if extractor is not None and getattr(extractor, "backend", chosen) != chosen:
            extractor.backend = chosen
    except Exception as exc:                         # noqa: BLE001 - see docstring
        log.debug("could not apply the PST reader choice: {}", exc)
        return None
    return chosen


# ---------------------------------------------------------------------------
# The configuration
# ---------------------------------------------------------------------------

def build_pipeline_config(settings: Any, roots: list[Path], *, tuned: object,
                          workers: int | None = None, memory_mb: int | None = None,
                          cpu_percent: int | None = None, full_speed: bool = False,
                          first: tuple = (), include_cloud: bool = False,
                          cloud_content_roots: frozenset = frozenset(),
                          cloud_content_cap_mb: int | None = None,
                          verify_hash: bool = True, prune: bool = True,
                          force: bool = False, retry_skipped: bool = False,
                          ocr_mode: str | None = None, archives: bool = True,
                          recheck_archives: bool = False,
                          pause_file: Path | None = None,
                          read_order: str | None = None,
                          whole: bool = False,
                          prune_under: tuple | None = None):
    r"""The `PipelineConfig` every index run uses, from settings and flags.

    **One construction, shared, so the callers cannot drift apart.** It was
    written inline in `cmd_index`, and `app/index/pipeline_bench.py` kept a
    copy of it with a comment asking to be kept in step - which is the kind of
    promise that holds until the first busy day. Work order 0x §2 made it
    matter more: the window's child-process runs go through `cmd_index` too,
    and the benchmark that decides whether they land has to measure the same
    configuration they use. *2026-10-04*: the window's own run and Offline
    Media are built here too now (moved from `app/cli/index.py`, which still
    exports it) - the window's copy was why its runs read Leasha's own folders.

    `tuned` is what `resolve.resolve_for_run` decided for this machine (its
    `workers`, `embed_batch` and `gpu_regression_notice` are read); the
    caller resolves it because doing so needs the store. Every keyword is a
    `cmd_index` flag, with the flag's default, so a caller that passes
    nothing gets exactly what `app.cli index FOLDER` does:

    * `workers`, `memory_mb`, `cpu_percent` - override the settings for this
      run, as `--workers`, `--memory-mb` and `--cpu-percent` do;
    * `full_speed` - no CPU ceiling, no pausing on battery, normal priority;
    * `cloud_content_roots` - normalised folder keys whose cloud-only files
      may be downloaded and read;
    * `ocr_mode` - `both`, `text` or `images`; None asks the settings
      (`pass_for`, with no images pass due).
    * `read_order` - `newest` or `found` (`--order`); None asks the settings
      (`INDEX_ORDER`). See `app/index/read_order.py`.
    * `whole` - this run is over the saved folder set (`covers_saved_folders`).
      Only such a run's clean-up looks at the whole index;
    * `prune_under` - what a run that is not `whole` cleans up under; None
      is `roots` themselves (Offline Media passes its drive's own key).

    *2026-10-04, code review:* `prune=True` with no `whole` pruned the whole
    index. A command-line run over one folder (`app.cli index D:\A`) deleted
    the rows of every other indexed folder whose files were not on disk at
    that moment - a disconnected drive's, a folder moved for a day - and an
    Offline Media Scan did the same from its drive. Now a run that is not
    over the saved folder set prunes only under its own folders.

    Settings are read with their `config.py` defaults, so a partial settings
    object (a test's stand-in) builds the same configuration as a full one.
    """
    from app.extract.media import MediaConfig
    from app.index.pipeline import PipelineConfig
    from app.index.read_order import normalise_order
    from app.index.resources import limits_from_settings
    from app.index.walker import WalkConfig, own_paths

    def setting(name: str, default: Any) -> Any:
        value = getattr(settings, name, default)
        return default if value is None else value

    limits = replace(limits_from_settings(settings), workers=tuned.workers)
    # A flag typed on the command still wins: it is a decision about this
    # run, and a tuning mode is a standing preference.
    if workers:
        limits = replace(limits, workers=workers)
    if memory_mb:
        limits = replace(limits, memory_mb=memory_mb)
    if cpu_percent is not None:
        limits = replace(limits, cpu_percent=cpu_percent)
    if full_speed:
        # An explicit opt-out for a machine nobody is using. Named for what it
        # costs rather than what it gives: this is the setting that makes the
        # computer unusable while it runs.
        limits = replace(
            limits, cpu_percent=0, pause_on_battery=False, low_priority=False,
            workers=workers or max(1, (os.cpu_count() or 2) - 1),
        )
    if ocr_mode is None:
        ocr_mode = pass_for(settings)
    # The clean-up's reach: the whole index only for a run over the saved
    # folders; otherwise this run's own folders (or what the caller names).
    scope = None
    if prune and not whole:
        scope = tuple(str(item) for item in (
            prune_under if prune_under is not None else roots))
        # No folders to clean under is no clean-up - never the whole index
        # (`PipelineConfig.prune_under` empty means "everywhere").
        prune = bool(scope)

    return PipelineConfig(
        walk=WalkConfig(
            roots=list(roots),
            priority_roots=[Path(p).expanduser() for p in (first or ())],
            include_cloud=include_cloud,
            cloud_content_roots=frozenset(cloud_content_roots),
            cloud_content_cap_bytes=int(
                cloud_content_cap_mb or setting("cloud_content_cap_mb", 1024)
            ) * 1024 * 1024,
            # Never index our own index, logs, cache or models. Indexing the
            # project folder had the run reading the log file it was writing.
            exclude_paths=own_paths(settings),
            # Every file gets a row, whether or not anything can read it - see
            # `WalkConfig.name_only`. Off makes the walk behave as it did.
            name_only=bool(setting("index_name_only", True)),
        ),
        limits=limits,
        min_free_gb=setting("min_free_gb", 5),
        required_free_gb=int(setting("required_free_gb", 0)),
        verify_hash=verify_hash,
        prune_missing=prune,
        prune_under=scope,
        force=force,
        retry_skipped=retry_skipped,
        # A folder marked as an archive is walked once and then checked
        # cheaply - see `app/index/archives.py`. `--all-roots` is the escape
        # hatch that ignores the modes entirely without touching the records.
        ocr_mode=ocr_mode,
        junk_images=bool(setting("index_junk_image_filter", True)),
        mail_attachments=str(setting("mail_attachments", "documents")),
        archives=archives,
        recheck_archives=recheck_archives,
        recheck_days=int(setting("archive_recheck_days", 30)),
        # Resolved for this machine and this mode, by the caller.
        embed_batch=tuned.embed_batch,
        dedup_chunks=bool(setting("embed_dedup", True)),
        two_phase=bool(setting("index_two_phase", True)),
        bulk_fts=str(setting("index_bulk_fts", "auto")),
        # 0x §5b: "Read files in separate processes". The window's child
        # indexer runs through here too, so it honours the same switch.
        read_processes=bool(setting("index_read_processes", False)),
        # 0z lane B: the time limits, from the same settings - the window's
        # child indexer runs through here, so it honours them too.
        file_time_limit_s=int(setting("index_file_time_limit_s", 120)),
        stall_limit_s=int(setting("index_stall_limit_s", 600)),
        caption_trickle_enabled=bool(setting("caption_trickle_enabled", False)),
        ollama_url=str(setting("ollama_url", "http://127.0.0.1:11434")),
        ollama_vision_model=str(setting("ollama_vision_model", "llava")),
        chat_engine=str(setting("chat_engine", "onnx")),
        people_recognition_enabled=bool(setting("people_recognition_enabled", False)),
        # Work order 202626270515. Off unless VIDEO_INDEXING_ENABLED and/or
        # AUDIO_TRANSCRIPTION_ENABLED are on in `.env`.
        media=MediaConfig.from_settings(settings),
        # Work order 202626130120 (0t) section 6: resolved once, by the same
        # resolve_for_run call the window uses before it builds a Pipeline.
        gpu_regression_notice=getattr(tuned, "gpu_regression_notice", ""),
        # 2026-09-20. The command line's half of the Indexing page's Pause
        # button - see `add_index_parser` for why it is a file and not a verb.
        pause_file=pause_file,
        # 2026-09-29. Newest first unless the settings or `--order` say not.
        read_order=normalise_order(
            read_order if read_order else setting("index_order", "")),
        # 2026-10-04, code review: attachment scratch folders under the cache,
        # not the system's temporary folder (`pst_libpff.configure_scratch`).
        scratch_dir=getattr(settings, "cache_path", None) or None,
    )
