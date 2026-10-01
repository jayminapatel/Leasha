"""`index` and `reembed`: building the index and rebuilding its vectors."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

from app.cli._common import EXIT_ERROR, EXIT_OK, _load, _report, _saved_roots
from app.cli._progress import ProgressLine, _console_sink
from app.core.config import Settings
from app.core.errors import make_error
from app.core.logging import logger, setup_logging
from app.core.run_lock import COMMAND_LINE, IndexRunLock
from app.index.resources import limits_from_settings

_ENRICHMENT_LABELS = dict(
    unembedded_chunk="vector(s) repaired",
    ocr_pending="held file(s) retried",
    image_tag="photo(s) tagged",
    media_transcript="video/recording file(s) read in the background",
)


def _print_enrichment_counts(stats) -> None:
    """Work order 0i section 2a: per-kind backlog counts, plain words."""
    counts = getattr(stats, "enrichment_counts", None) or dict()
    nonzero = list((k, v) for k, v in counts.items() if v)
    if not nonzero:
        return
    bits = list(
        str(v) + " " + _ENRICHMENT_LABELS.get(k, k) for k, v in nonzero)
    print("Backlog   " + ", ".join(bits))


def _print_vector_coverage(stats) -> None:
    r"""How many of the passages this run wrote actually got a vector.

    **The number whose absence let the embedding gap run for weeks.** A run that
    wrote 3,355 chunks and 0 vectors printed `-> 3,355 chunks` and stopped
    there, which reads as success. The two stores were only ever compared
    afterwards, by `stats` or `doctor`, and nobody runs a diagnostic against a
    run that told them it worked.

    Silent when they agree, deliberately. A line reading "3,355 of 3,355" on
    every healthy run is the noise that teaches people to skim the summary,
    which is how the real one would be missed.
    """
    chunks = int(getattr(stats, "chunks", 0) or 0)
    vectors = int(getattr(stats, "vectors", 0) or 0)
    if not chunks or vectors >= chunks:
        return
    share = vectors / chunks * 100
    print()
    print(f"Vectors   {vectors:,} of {chunks:,} passages embedded ({share:.0f}%)")
    if getattr(stats, "embed_failures", 0):
        print(f"          {stats.embed_failures} embedding batch(es) failed. Those "
              f"files stay PENDING and the next run retries them.")
    print("          Meaning-based search covers only that much of this run; "
          "keyword search is unaffected.")
    print("          `app.cli reembed` fills the gap without re-reading anything.")


class _ActivityPrinter:
    r"""Work order 0w §2d: the run's log, printed as it arrives.

    The same entries, the same `HH:MM:SS` and the same words as the Indexing
    page's log (non-negotiable #8), because both come from
    `presenter.activity`. **Printed on the progress callback**, which runs on
    the run's own thread, so there is no second thread writing to the
    console; an entry recorded between two ticks is printed at the next one
    with the time it was recorded, not the time it was printed. Each line
    goes above the progress line - `clear`, print, `repaint` - the way the
    log sink already does it, so neither mangles the other. ASCII-safe for a
    console left at a legacy code page, as the phase words already were.
    """

    def __init__(self, progress: ProgressLine) -> None:
        self._progress = progress
        self._run: object = None
        self._seq = 0

    def __call__(self, stats: object) -> None:
        from app.ui.presenter.activity import activity_lines, console_safe

        log = getattr(stats, "activity", None)
        if log is None:
            return
        # A notice appended without `add_notice` gets its time here too.
        stamp = getattr(stats, "stamp_notices", None)
        if callable(stamp):
            stamp()
        if log.run != self._run:
            self._run, self._seq = log.run, 0
        fresh = log.since(self._seq)
        if not fresh:
            return
        self._seq = fresh[-1].seq
        self._progress.clear()
        for line in activity_lines(fresh):
            print(console_safe(line, sys.stdout.encoding), flush=True)
        self._progress.repaint()


def _saved_first_folders(store) -> list[str]:
    """The window's "Index this folder first" list, in order. Never raises:
    a list that cannot be read means none is marked, which loses nothing."""
    from app.index.read_order import FIRST_FOLDERS_STATE_KEY, load_first_folders

    try:
        return load_first_folders(store.get_state(FIRST_FOLDERS_STATE_KEY, "") or "")
    except Exception as exc:                     # noqa: BLE001 - see docstring
        logger.bind(component="cli.index").debug(
            "could not read the folders to index first: {}", exc)
        return []


def build_pipeline_config(settings: Settings, roots: list[Path], *, tuned: object,
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
                          read_order: str | None = None):
    r"""The `PipelineConfig` a command-line run uses, from settings and flags.

    **One construction, shared, so two callers cannot drift apart.** It was
    written inline in `cmd_index`, and `app/index/pipeline_bench.py` kept a
    copy of it with a comment asking to be kept in step - which is the kind of
    promise that holds until the first busy day. Work order 0x §2 made it
    matter more: the window's child-process runs go through `cmd_index` too,
    and the benchmark that decides whether they land has to measure the same
    configuration they use.

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
    * `ocr_mode` - `both`, `text` or `images`; None asks the settings, the
      way `_ocr_mode` does with no flag.
    * `read_order` - `newest` or `found` (`--order`); None asks the settings
      (`INDEX_ORDER`). See `app/index/read_order.py`.
    """
    from app.extract.media import MediaConfig
    from app.index.pipeline import PipelineConfig
    from app.index.read_order import normalise_order
    from app.index.walker import WalkConfig, own_paths

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
        ocr_mode = _ocr_mode(argparse.Namespace(), settings)

    return PipelineConfig(
        walk=WalkConfig(
            roots=list(roots),
            priority_roots=[Path(p).expanduser() for p in (first or ())],
            include_cloud=include_cloud,
            cloud_content_roots=frozenset(cloud_content_roots),
            cloud_content_cap_bytes=(
                cloud_content_cap_mb or settings.cloud_content_cap_mb
            ) * 1024 * 1024,
            # Never index our own index, logs, cache or models. Indexing the
            # project folder had the run reading the log file it was writing.
            exclude_paths=own_paths(settings),
            # Every file gets a row, whether or not anything can read it - see
            # `WalkConfig.name_only`. Off makes the walk behave as it did.
            name_only=settings.index_name_only,
        ),
        limits=limits,
        min_free_gb=settings.min_free_gb,
        required_free_gb=settings.required_free_gb,
        verify_hash=verify_hash,
        prune_missing=prune,
        force=force,
        retry_skipped=retry_skipped,
        # A folder marked as an archive is walked once and then checked
        # cheaply - see `app/index/archives.py`. `--all-roots` is the escape
        # hatch that ignores the modes entirely without touching the records.
        ocr_mode=ocr_mode,
        junk_images=bool(getattr(settings, "index_junk_image_filter", True)),
        mail_attachments=str(getattr(settings, "mail_attachments", "documents")),
        archives=archives,
        recheck_archives=recheck_archives,
        recheck_days=settings.archive_recheck_days,
        # Resolved for this machine and this mode, by the caller.
        embed_batch=tuned.embed_batch,
        dedup_chunks=settings.embed_dedup,
        two_phase=settings.index_two_phase,
        bulk_fts=settings.index_bulk_fts,
        # 0x §5b: "Read files in separate processes". The window's child
        # indexer runs through here too, so it honours the same switch.
        read_processes=bool(getattr(settings, "index_read_processes", False)),
        # 0z lane B: the time limits, from the same settings - the window's
        # child indexer runs through here, so it honours them too.
        file_time_limit_s=int(getattr(settings, "index_file_time_limit_s", 120)),
        stall_limit_s=int(getattr(settings, "index_stall_limit_s", 600)),
        caption_trickle_enabled=settings.caption_trickle_enabled,
        ollama_url=settings.ollama_url,
        ollama_vision_model=settings.ollama_vision_model,
        chat_engine=getattr(settings, "chat_engine", "onnx"),
        people_recognition_enabled=settings.people_recognition_enabled,
        # Work order 202626270515. Off unless VIDEO_INDEXING_ENABLED and/or
        # AUDIO_TRANSCRIPTION_ENABLED are on in `.env`.
        media=MediaConfig.from_settings(settings),
        # Work order 202626130120 (0t) section 6: resolved once, by the same
        # resolve_for_run call the window uses before it builds a Pipeline.
        gpu_regression_notice=tuned.gpu_regression_notice,
        # 2026-09-20. The command line's half of the Indexing page's Pause
        # button - see `add_index_parser` for why it is a file and not a verb.
        pause_file=pause_file,
        # 2026-09-29. Newest first unless the settings or `--order` say not.
        read_order=normalise_order(
            read_order if read_order else getattr(settings, "index_order", "")),
    )


class _EventSession:
    r"""`app.cli index --events jsonl`: the child-process side of work order 0x §2.

    The window starts this command as a child (`app/index/child_run.py`) and
    talks to it over two pipes. This class is the child's half:

    * **Out**, on standard output: the `EventWriter` from
      `app/index/run_events.py` - hello, progress, heartbeats, finished.
      Standard output carries events and nothing else (see `_redirect_stdout`).
    * **In**, on standard input: one command a line - `pause`, `resume`,
      `stop` - read by a thread of its own, so a command is acted on at once
      whatever the run is doing.
    * 0z lane B: and `skip <reader>`, the Indexing page's Force skip for that
      reader's current file (`Pipeline.force_skip`). Not kept for later like
      the other three: before the run is live there is no file to skip.

    **Commands can arrive before there is a run to give them to** - the
    person presses Pause while the settings are still loading. They are kept,
    and applied at the run's first progress tick (`progress`): not at
    `attach`, because `Pipeline.run` starts by clearing any stop and letting
    go of any pause left from before it began (a pause "belongs to the run it
    was asked for"), so one applied earlier would be silently undone.

    **The window going away means stop.** When the window closes its end of
    the pipe - or dies, killed from Task Manager, so the operating system
    closes it - this process's standard input reaches its end. That is taken
    as Stop, the clean kind that keeps everything done so far; and if the
    stop has not finished `ORPHAN_GRACE_S` later, the process ends itself.
    An indexer with no window, running for hours, is the "closed but still
    running" incident HANDOFF describes, and it must not be possible here.
    """

    def __init__(self, out) -> None:
        import threading

        from app.index.run_events import EventWriter

        self.writer = EventWriter(out, on_broken=self.window_gone)
        self._lock = threading.Lock()
        self._pipeline = None
        self._live = False           # True from the run's first progress tick
        self._stop = False
        self._paused = False
        self._gone = False

    def attach(self, pipeline) -> None:
        """Name the run that commands will act on, once it is under way."""
        with self._lock:
            self._pipeline = pipeline

    def progress(self, stats) -> None:
        """The pipeline's `on_progress`: apply early commands once, then report.

        The first tick comes from inside `Pipeline.run`, after it has reset
        its stop and pause - so this is the first moment a command sticks.
        Applying one twice (it also arrived directly a moment ago) is
        harmless: pausing a paused run and stopping a stopping one do nothing.
        """
        apply = None
        with self._lock:
            if not self._live and self._pipeline is not None:
                self._live = True
                apply = (self._pipeline, self._stop, self._paused)
        if apply is not None:
            pipeline, stop, paused = apply
            if stop:
                pipeline.request_stop()
            elif paused:
                pipeline.pause()
        self.writer.offer(stats)

    def command(self, word: str) -> None:
        """Act on one command from the window. Unknown words are ignored."""
        from app.index.run_events import (
            COMMAND_PAUSE,
            COMMAND_RESUME,
            COMMAND_SKIP,
            COMMAND_STOP,
        )

        if word.startswith(COMMAND_SKIP + " "):
            with self._lock:
                pipeline = self._pipeline if self._live else None
            force_skip = getattr(pipeline, "force_skip", None)
            if force_skip is not None:
                force_skip(word.split()[1])
            return
        with self._lock:
            pipeline = self._pipeline if self._live else None
            if word == COMMAND_STOP:
                self._stop, self._paused = True, False
            elif word == COMMAND_PAUSE:
                self._paused = True
            elif word == COMMAND_RESUME:
                self._paused = False
        if pipeline is None:
            return
        if word == COMMAND_STOP:
            pipeline.request_stop()
        elif word == COMMAND_PAUSE:
            pipeline.pause()
        elif word == COMMAND_RESUME:
            pipeline.resume()

    def listen(self, stream) -> None:
        """Read commands from `stream` on a daemon thread until it ends."""
        import threading

        from app.index.run_events import parse_command

        if stream is None:
            # No standard input at all (a `pythonw` started with none): there
            # is nobody to take commands from, and no pipe whose end could
            # mean "the window has gone". The run goes on as a plain run.
            return

        def read() -> None:
            try:
                for line in stream:
                    word = parse_command(line)
                    if word:
                        self.command(word)
            except (OSError, ValueError):
                pass
            self.window_gone()

        threading.Thread(target=read, name="index-commands", daemon=True).start()

    def window_gone(self) -> None:
        """Stop cleanly now; end the process if that has not worked in time."""
        import threading

        from app.index.child_run import ORPHAN_GRACE_S

        with self._lock:
            if self._gone:
                return
            self._gone = True
        logger.bind(component="cli.index").warning(
            "the window that started this run has gone; stopping")
        self.command("stop")

        def end() -> None:
            logger.bind(component="cli.index").error(
                "the run did not stop within {:.0f}s of its window going; ending it",
                ORPHAN_GRACE_S)
            os._exit(EXIT_ERROR)

        timer = threading.Timer(ORPHAN_GRACE_S, end)
        timer.daemon = True
        timer.start()


def _redirect_stdout():
    r"""Keep standard output for events only, and return the stream to write them to.

    **Anything else printed would corrupt the conversation.** A library that
    prints a warning, a stray `print` - on standard output, either would land
    in the middle of the event stream. So the real standard output is kept
    aside (a duplicate of file descriptor 1) for the events, and descriptor 1
    itself is pointed at standard error, where the window sends everything to
    a log file. This catches output from C libraries too, which write to the
    descriptor rather than to Python's `sys.stdout`. Where that cannot be
    done, `sys.stdout` alone is swapped, which catches everything written from
    Python. The window's reader ignores any line that is not an event either
    way, so a leak costs a log line, never a misread.
    """
    sys.stdout.flush()
    try:
        saved = os.dup(1)
        os.dup2(2, 1)
        return open(saved, "w", encoding="ascii", errors="replace",  # noqa: SIM115
                    newline="\n", buffering=1)
    except (OSError, ValueError, AttributeError):
        events = sys.stdout
        sys.stdout = sys.stderr if sys.stderr is not None else open(  # noqa: SIM115
            os.devnull, "w")
        return events


def _private_stdin():
    r"""Take the command pipe off standard input, and return a stream to read it.

    2026-09-29, found on Windows CI: every child run sat still until the
    window's first command arrived - five minutes, in the tests, until "stop".
    **On Windows, I/O on a synchronous pipe is one-at-a-time per pipe.** While
    the command thread waits in a read on standard input, anything else that
    touches that same pipe waits behind it: asking what kind of file it is
    (`os.fstat`, `isatty`, which libraries do on import), or starting a
    process that inherits it (OCR, a reader process, LibreOffice), whose own
    start-up asks the same question. The run froze at the first such touch.

    So the pipe is kept aside for the commands - a duplicate of descriptor 0,
    not inherited by anything started later - and descriptor 0 and the
    process's standard input handle are pointed at the null device, which
    anybody may ask about or inherit. Where that cannot be done, the plain
    `sys.stdin` is returned and nothing is worse than before.
    """
    stream = sys.stdin
    if stream is None:
        return None
    try:
        saved = os.dup(0)                        # not inheritable (PEP 446)
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)
        os.close(null)
        # The C runtime points the Win32 handle at the new descriptor only in
        # a console program; `pythonw` is not one, so it is done here.
        from app.core.osbridge.stdio import follow_descriptor_zero

        follow_descriptor_zero()
        sys.stdin = open(os.devnull, encoding="utf-8")  # noqa: SIM115 - for life
        return open(saved, encoding="utf-8", errors="replace")  # noqa: SIM115
    except (OSError, ValueError, AttributeError):
        return stream


def _cmd_index_events(args: argparse.Namespace) -> int:
    """`cmd_index` as a child: events out, commands in, errors as events."""
    from app.core.errors import AppErrorException, to_app_error

    session = _EventSession(_redirect_stdout())
    session.writer.start()
    session.listen(_private_stdin())
    try:
        return cmd_index(args, session)
    except AppErrorException as exc:
        session.writer.finish(error=exc.error, exit_code=EXIT_ERROR)
        return EXIT_ERROR
    except Exception as exc:                     # noqa: BLE001 - said, as an event
        error = to_app_error(exc, "cli.index")
        logger.bind(component="cli.index").error("{}", error.render())
        session.writer.finish(error=error, exit_code=EXIT_ERROR)
        return EXIT_ERROR


def cmd_index(args: argparse.Namespace, events: "_EventSession | None" = None) -> int:
    """Build or update the index. Layer 3's entry point.

    Unlike `extract`, this one writes - so it takes the single-instance lock.
    Two copies indexing into one SQLite file is exactly the corruption the
    mutex exists to prevent.

    With `--events jsonl` it is the window's child process (work order 0x §2):
    machine-readable events on standard output, commands on standard input,
    and every error reported as a `finished` event rather than printed.
    `_cmd_index_events` sets that up and calls back in with `events`; `None`
    is the ordinary command.
    """
    if events is None and getattr(args, "events", None):
        return _cmd_index_events(args)

    from app.core.errors import AppErrorException
    from app.core.run_lock import GUI
    from app.index.clip_embedder import ClipImageEmbedder
    from app.index.embedder import Embedder
    from app.index.pipeline import Pipeline
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import ImageVectorStore, VectorStore
    from app.ui.presenter import phase_words, unfinished_run_line
    from app.ui.presenter.activity import console_safe, timed_notices

    #: Nothing for a person to read: machine output of one kind or the other.
    machine = bool(args.json or events is not None)

    def refuse(error) -> int:
        """A refusal: printed for a person, a `finished` event for the window."""
        if events is not None:
            raise AppErrorException(error)
        return _report(error, args.json)

    settings = _load(args)
    setup_logging(settings.log_path)
    log = logger.bind(component="cli.index")

    # Order 0z F3: `--retry-timed-out`. None for an ordinary run.
    retry, problem = _retry_asked(args)
    if problem is not None:
        return refuse(problem)
    if retry is not None and not machine and not _say_retry(settings, retry):
        return EXIT_OK

    roots = [Path(root).expanduser() for root in (args.roots or [])]
    from_settings = False
    # A retry walks nothing - its files come from the ledger - so it neither
    # needs folders nor falls back to the saved ones.
    if not roots and retry is None:
        # **Falls back to what the window is configured to index.**
        #
        # The same setting had two sources of truth: the window saves "Folders
        # to index" under `ui:roots`, and this command only ever read its own
        # arguments. So a command-line run - including the one somebody uses to
        # verify a migration - indexed whatever folder was typed rather than
        # what the application is actually set up to index, and there was no
        # way to tell the two apart afterwards. Verifying the wrong thing and
        # believing it was the right thing is the expensive kind of wrong.
        #
        # Explicit arguments still win: naming a folder is an instruction, and
        # a command that quietly ignored it in favour of a saved setting would
        # be the same bug pointing the other way.
        saved = _saved_roots(settings)
        roots = [Path(root).expanduser() for root in saved]
        from_settings = bool(roots)

    if not roots and retry is None:
        return refuse(make_error(
            "ERR_CONFIG_INVALID", "cli.index",
            key="roots", reason="no folders to index",
            suggestion=r'Name them here - app.cli index "D:\SearchData" - or '
                       r'set them once on the Settings page, under "Folders to '
                       r'index", and run this with no arguments.',
        ))

    if from_settings and not machine:
        # **Said out loud.** A command that silently uses a setting is a command
        # whose output cannot be attributed to anything.
        print("Indexing the folders saved in Settings:")
        for root in roots:
            print(f"  {root}")

    missing = [root for root in roots if not root.exists()]
    if missing:
        return refuse(make_error(
            "ERR_CONFIG_INVALID", "cli.index",
            key="roots", reason=f"does not exist: {', '.join(str(m) for m in missing)}",
            suggestion="Check the path and the drive. A folder on a disconnected drive looks "
                       "exactly like a folder that was deleted.",
        ))

    # **The tuning mode reaches the run, not only the screen.** Before this the
    # panel resolved `0` to `Auto (4)` for display and the run read the literal
    # `0`, so switching modes changed what was shown and nothing about what
    # happened. `resolve_for_run` is the same arithmetic the screen uses - one
    # function, so the two cannot drift.
    from app.index.resolve import resolve_for_run

    from app.index.interrupted import read_unfinished_run

    with SqliteStore(settings.fts_db) as _store:
        tuned = resolve_for_run(settings, _store)
        # Work order `dates-live-log-and-interrupted-runs` 3a. **Read before
        # this run takes the lock**, because taking it replaces the record a
        # run that died left behind - the only evidence that one did.
        unfinished = read_unfinished_run(_store)
        # 2026-09-29. "Index this folder first", as the window saved it, when
        # the command line named none. `--first` still wins: it is a decision
        # about this run.
        first = tuple(args.first or ()) or tuple(_saved_first_folders(_store))
    if unfinished and not machine:
        print(unfinished_run_line(unfinished, carrying_on=True))
    _tuning_log = logger.bind(component="cli.index")
    for key, why in tuned.why.items():
        _tuning_log.debug("{}: {}", key, why)

    # Folders whose cloud-only files may be downloaded. `--allow-cloud-content`
    # is typed by a person and normalised here; `--cloud-content-key` is the
    # window's child process passing the keys exactly as the window stores
    # them (`archives.normalise`), so the two runs agree on every platform.
    cloud_keys = frozenset(
        str(Path(p).expanduser()).rstrip("\\/").lower()
        for p in (args.allow_cloud_content or [])
    ) | frozenset(getattr(args, "cloud_content_key", None) or ())

    config = build_pipeline_config(
        settings, roots, tuned=tuned,
        workers=args.workers, memory_mb=args.memory_mb,
        cpu_percent=args.cpu_percent, full_speed=args.full_speed,
        first=first, include_cloud=args.include_cloud,
        cloud_content_roots=cloud_keys,
        cloud_content_cap_mb=args.cloud_content_cap_mb,
        verify_hash=not args.fast,
        prune=not args.no_prune,
        force=bool(getattr(args, "force", False)),
        retry_skipped=bool(getattr(args, "retry_skipped", False)),
        ocr_mode=_ocr_mode(args, settings),
        archives=not bool(getattr(args, "all_roots", False)),
        recheck_archives=bool(getattr(args, "recheck_archives", False)),
        pause_file=(Path(args.pause_file).expanduser()
                    if getattr(args, "pause_file", None) else None),
        read_order=getattr(args, "order", None),
    )

    if getattr(args, "fake_embedder_for_bench", False):
        # **For `app.cli bench-pipeline --child-process` only** (hidden from
        # `--help`). The benchmark must be able to run where the real model
        # has never been downloaded, and a fake run in the child has to be
        # the same fake the in-process benchmark uses, or the two numbers
        # would not compare. Its model name says what it is wherever it lands.
        from app.index.pipeline_bench import FAKE_MODEL_NAME, _fake_encoder

        embedder = Embedder(FAKE_MODEL_NAME, dim=settings.embed_dim,
                            encoder=_fake_encoder(settings.embed_dim))
    else:
        embedder = Embedder.from_settings(settings, threads=tuned.onnx_threads)
    # Work order 0h §1c item 3. **The same construction, at the same site
    # that already builds `embedder`**, so indexing from the command line
    # writes the CLIP vectors the search side (`cmd_search`, `cmd_shell`,
    # `cmd_evaluate`, the window) can now query - verified with `grep -rn
    # "image_embedder=\|image_vectors=" app/` before this change, which
    # returned only test call sites: no real run had ever written one.
    # `ClipImageEmbedder.from_settings` is already lazy (nothing loads until
    # the first image is embedded), so building it unconditionally here
    # costs nothing on a run that never reaches an image file.
    image_embedder = ClipImageEmbedder.from_settings(settings)

    progress = ProgressLine(enabled=not args.quiet and not machine)

    # Route console logging through the progress line, so a warning about one
    # unreadable file cannot leave the heartbeat mangled and apparently frozen.
    if progress.enabled:
        # Order matters: `setup_logging` clears every handler, so the progress
        # sink has to be added *after* it, not before. Doing it the other way
        # round silently removes the sink and the warnings vanish entirely -
        # which is worse than the mangled line it was meant to fix.
        # `force=True` matters. `setup_logging` is idempotent by design - both
        # the CLI and the UI call it - so without it this second call returns
        # immediately, the original INFO console sink survives, and every line
        # gets printed twice: once by the sink that respects the progress line
        # and once by the sink that walks straight over it.
        setup_logging(settings.log_path, console_level="CRITICAL", force=True)
        logger.add(
            _console_sink(progress), level="INFO",
            format="{time:HH:mm:ss} {level: <7} {message}",
        )

    say = _ActivityPrinter(progress)

    def show(stats) -> None:
        if args.json:
            return
        say(stats)
        line = (f"  {stats.indexed:>7,} docs  {stats.unchanged:>6,} unchanged  "
                f"{stats.unchanged_documents:>7,} already current  "
                f"{stats.chunks:>8,} chunks")

        if getattr(stats, "paused", False):
            # **A pause with nothing said is a hang, as far as anyone watching
            # is concerned.** The window was given this earlier today; the
            # command line builds its own line and was not, so a real run sat
            # on an unchanging line for minutes while the governor waited for
            # memory to settle - and was reported as stuck. It was working.
            #
            # 2026-09-20: and *whose* pause it is decides what to do about it.
            # A run the machine paused carries on by itself; a run the person
            # paused waits for them, and the line says which, in the same
            # words the button on the Indexing page uses.
            if getattr(stats, "paused_by_person", False):
                progress.update(
                    f"{line}  | PAUSED by you - delete the pause file to carry on")
                return
            reason = getattr(stats, "pause_reason", "") or "waiting for resources"
            progress.update(f"{line}  | PAUSED - {reason[:70]}")
            return

        # The stretches with nothing to count - loading the model, building
        # the vector index at the end - in the same words the Indexing page
        # uses. Without them this line sat unchanged for minutes, which is
        # what a hang looks like. ASCII dots: this is a Windows console.
        doing = phase_words(stats).replace("…", "...")
        if doing:
            progress.update(f"{line}  | {doing}")
            return

        recent = getattr(stats, "recent_files_per_minute", None)
        if recent is not None:
            # **The last fifteen minutes, not the lifetime average.** On a run
            # of days the average stops moving, so a run that has slowed to a
            # crawl reports the rate it managed on the first morning.
            line += f"  | {recent:,.0f}/min"

        if stats.current:
            # Naming the file being read is what separates "working on a big
            # archive" from "hung". A 100MB .pst is one file and can hold the
            # line for minutes.
            waited = time.monotonic() - (stats.current_since or time.monotonic())
            line += f"  | {stats.current[:34]}"
            if stats.current_item:
                line += f" [{stats.current_item:,}]"
            if waited > 5:
                line += f" {waited:,.0f}s"
        progress.update(line)

    # **The run lock, not the process lock.** This used to take
    # `SingleInstance`, which the window holds for its whole lifetime - so
    # `app.cli index` could not run at all while Leasha was open, even though
    # the window was only reading. What must not overlap is two *writers*, and
    # that hazard lasts exactly as long as this block. See `core/run_lock.py`.
    #
    # **Whose run it is**, for the sentence another reader shows ("the window,
    # since 14:02"). The window's own child process says it is the window's,
    # because to the person it is: they pressed Start on the Indexing page.
    owner = GUI if getattr(args, "run_owner", "") == "window" else COMMAND_LINE
    with SqliteStore(settings.fts_db) as store, \
            IndexRunLock(store, owner=owner), \
            VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors, \
            ImageVectorStore(settings.vector_path) as image_vectors:
        build = Pipeline
        if retry is not None:
            # The same construction and the same config; only what is read,
            # and for how long, differs. See `app/index/timed_out_retry.py`.
            from app.index.timed_out_retry import retry_pipeline

            build = retry_pipeline(retry)
        pipeline = build(
            store, vectors, embedder, config,
            image_embedder=image_embedder, image_vectors=image_vectors,
        )
        pipeline.run_owner = owner
        if events is not None:
            # Commands from the window (and any that arrived while the model
            # was loading) now have a run to act on.
            events.attach(pipeline)
            stats = pipeline.run(on_progress=events.progress)
        else:
            stats = pipeline.run(on_progress=None if args.quiet else show)

    if events is not None:
        # The last line the window reads. Everything a person would be told
        # below is in the stats, and the page words it itself.
        code = EXIT_ERROR if stats.stopped_early else EXIT_OK
        events.writer.finish(stats=stats, exit_code=code)
        return code

    payload = stats.as_dict()
    if args.json:
        if unfinished:
            payload["interrupted_before"] = unfinished
        print(json.dumps(payload, indent=2))
        return EXIT_ERROR if stats.stopped_early else EXIT_OK

    # What arrived after the last tick - the vector index, the word index,
    # the last line - before the summary, not lost behind it.
    if not args.quiet:
        say(stats)
    progress.finish()
    print()
    # Documents, not files. A .pst is one file and thousands of messages, and
    # calling them all "files" produced summaries like "seen 8, indexed 17"
    # where the two numbers were different units.
    print(f"Indexed   {stats.indexed:,} document(s) -> {stats.chunks:,} chunks")
    _print_vector_coverage(stats)
    _print_enrichment_counts(stats)
    print(f"Files     {stats.seen:,} seen, {stats.unchanged:,} unchanged")
    if stats.unchanged_documents:
        print(f"          {stats.unchanged_documents:,} document(s) inside them were "
              f"already up to date")
    # Work order 0w §2c: with the time each was said, as the page shows them.
    for notice in timed_notices(stats):
        print()
        print(console_safe(f"Note      {notice}", sys.stdout.encoding))
    if stats.skipped_roots:
        # **Said before the totals, not after.** A run that indexed 40 files
        # because three of its four folders were skipped needs to say so where
        # somebody reading the numbers cannot miss it.
        print()
        print(f"Archives  {len(stats.skipped_roots)} folder(s) were not walked at all:")
        for row in stats.skipped_roots:
            when = (
                time.strftime("%Y-%m-%d", time.localtime(row["archived_at"]))
                if row.get("archived_at") else "an unknown date"
            )
            print(f"  {row['root']}")
            print(f"    {row['reason']} - {row.get('files', 0):,} file(s), "
                  f"fully indexed on {when}")
        print("    Use --recheck-archives to walk them in full now.")
        print()
    print(f"Skipped   {stats.skipped:,}   Deleted {stats.deleted:,}")
    print(f"Read      {stats.bytes_read / 1_048_576:,.1f} MB in {stats.elapsed_s:,.1f}s")
    if stats.pauses:
        # Said plainly, because a four-hour run that was mostly waiting looks
        # identical to a four-hour run that was slow - and the fix is opposite.
        mine = getattr(stats, "manual_paused_seconds", 0.0) or 0.0
        machine = max(0.0, stats.paused_seconds - mine)
        print(f"Waited    {machine / 60:,.1f} min across {stats.pauses} "
              f"pause(s) to stay out of the way")
        if mine:
            # Kept out of the line above on purpose: time the person asked for
            # is not the indexer being polite, and reading it as such would
            # argue for raising a ceiling that was never the reason.
            print(f"Paused    {mine / 60:,.1f} min held at your request")
    print(f"          {stats.files_per_minute:,.0f} files/min, {stats.mb_per_minute:,.1f} MB/min")
    if stats.chunks_deduped:
        # §6e's number, said every run. Whether repeated text is worth
        # avoiding is a question about somebody's corpus, and this is the only
        # place the answer ever appears.
        share = stats.chunks_deduped / max(1, stats.chunks + stats.chunks_deduped)
        print(f"Repeated  {stats.chunks_deduped:,} passage(s) were already "
              f"embedded this run ({share:.0%}) and were not sent again")
    if stats.stages:
        # §6a, in the shape §4f shows: proportions, because the question this
        # answers is "what should I change" and that is about shares.
        total = sum(stats.stages.values()) or 1.0
        shares = " · ".join(f"{name} {seconds / total:.0%}"
                            for name, seconds in stats.stages.items())
        print(f"Time      {shares}")
        from app.index.stages import advice

        said = advice(stats.stages, on_gpu=settings.embed_device == "gpu")
        if said:
            print(f"          {said}")
    if (retry is None and _images_pass_follows(settings)
            and _ocr_mode(args, settings) == "text"):
        # **Said, not started.** A second pass over a scanned corpus is hours;
        # launching it without asking, from a command somebody ran to index
        # their documents, is the kind of surprise that gets an application
        # uninstalled. The window schedules it; the command line names it.
        print()
        print("Images    Set to be read after the run. Start the second pass "
              "with:  leasha index --only-ocr")
    if stats.name_only:
        # **Not "skipped".** Nothing went wrong: there is no reader for a
        # `.mp4`. Reported with the types, because that is the number that
        # tells somebody their corpus is 30% `.dwg`.
        top = sorted(stats.name_only_by_ext.items(),
                     key=lambda row: row[1], reverse=True)[:6]
        kinds = ", ".join(f".{ext} x{count:,}" for ext, count in top)
        print()
        print(f"By name   {stats.name_only:,} file(s) indexed by name only - "
              f"nothing can read them")
        print(f"          {kinds}")
        print("          They are findable by name; their contents are not "
              "searchable.")

    pictures = stats.warned_by_code.get("ERR_MOSTLY_PICTURES", 0)
    if pictures:
        # **The evidence for a decision, not a complaint.** From
        # `WORKORDER-202626081052-ocr-strategy.md` §5: list the affected
        # documents rather than reading them, then decide with the number in
        # hand. Twenty decks: open them. Two thousand: no OCR strategy was ever
        # going to help, and the honest answer is that they are findable by
        # name and title only.
        print()
        print(f"Pictures  {pictures:,} document(s) are mostly images rather than text")
        print("          Their titles and headings are searchable; the pictures are not.")
        print("          Reading them would mean OCR per image - see the OCR work order.")

    partial = stats.warned_by_code.get("ERR_PST_PARTIAL", 0)
    if partial:
        # **Said, because "indexed" alone reads as "complete".** An archive with
        # a few unreadable messages still indexes everything else, and without
        # this line the run looked identical to one that read every message.
        print()
        print(f"Partial   {partial:,} mail archive(s) were only partly readable")
        print("          Everything readable is searchable. The log names what was missed;")
        print("          scanpst.exe repairs a damaged archive, then re-run the index.")

    held = stats.skipped_by_code.get("ERR_OCR_HELD", 0)
    if held:
        # **Named separately from the failures, because it is not one.** A
        # queue of 40,000 images reported inside "skipped by cause" reads as
        # 40,000 things that went wrong.
        print()
        print(f"Held      {held:,} image(s) are queued for the images pass -")
        print("          nothing is wrong with them and nothing was lost.")
        print("          Run: app.cli index --only-ocr")
    if stats.skipped_by_code:
        print(f"Skipped by cause: {stats.skipped_by_code}")
        print("  Run `app.cli stats` to see the totals, or check logs\\errors for the detail.")
    if stats.stopped_early is not None:
        print()
        print(stats.stopped_early.render())
        print("  Everything indexed so far is saved. Re-run to carry on.")
        return EXIT_ERROR

    log.info("index complete: {}", payload)
    return EXIT_OK


# ---------------------------------------------------------------------------
# Order 0z F3: timed-out files - listing them, and reading a group again
# ---------------------------------------------------------------------------

def _retry_asked(args: argparse.Namespace) -> tuple:
    """`(RetryTimedOut, None)` for `--retry-timed-out`, `(None, None)` for an
    ordinary run, or `(None, AppError)` when what was typed cannot be meant."""
    from app.index.timed_out_retry import (
        DEFAULT_FACTOR, MAX_FACTOR, MIN_FACTOR, RetryTimedOut, normalise_group,
    )

    typed = getattr(args, "retry_timed_out", None)
    factor = getattr(args, "time_limit_factor", None)
    if typed is None:
        if factor is not None:
            return None, make_error(
                "ERR_CONFIG_INVALID", "cli.index", key="--time-limit-factor",
                reason="it only applies to a retry of timed-out files",
                suggestion="Add --retry-timed-out, or leave --time-limit-factor "
                           "out. The limit for ordinary runs is 'Time limit per "
                           "file' on the Indexing page's Tuning shelf.")
        return None, None
    if any(mark in str(typed) for mark in ("\\", "/", ":")) or getattr(args, "roots", None):
        return None, make_error(
            "ERR_CONFIG_INVALID", "cli.index", key="--retry-timed-out",
            reason="a retry reads the timed-out files wherever they are, so it "
                   "takes a file type, not a folder",
            suggestion="Run `app.cli timed-out` to see the types, then for "
                       "example: app.cli index --retry-timed-out pdf")
    if factor is None:
        factor = DEFAULT_FACTOR
    if not MIN_FACTOR <= factor <= MAX_FACTOR:
        return None, make_error(
            "ERR_CONFIG_INVALID", "cli.index", key="--time-limit-factor",
            reason=f"{factor:g} is not between {MIN_FACTOR} and {MAX_FACTOR}",
            suggestion=f"Give a number of times the usual limit, such as "
                       f"{DEFAULT_FACTOR}. For no limit at all, set 'Time limit "
                       f"per file' to 0 on the Indexing page's Tuning shelf.")
    return RetryTimedOut(group=normalise_group(typed), factor=float(factor)), None


def _timed_out_groups(settings: Settings) -> list:
    """The store's timed-out groups, or none for an index not yet made. Opens
    the index only if it exists: a listing must not create one."""
    from app.storage.sqlite_store import SqliteStore

    if not Path(settings.fts_db).is_file():
        return []
    with SqliteStore(settings.fts_db) as store:
        return store.timed_out_groups()


def _type_words(ext: str) -> str:
    return f".{ext}" if ext else "(no extension)"


def _say_retry(settings: Settings, retry) -> bool:
    """Say what a retry is about to read. False when there is nothing to read,
    having said so - before the model is loaded or the lock is taken."""
    from app.index.timed_out_retry import group_words

    groups = _timed_out_groups(settings)
    count = sum(g["count"] for g in groups
                if retry.group is None or g["ext"] == retry.group)
    kind = group_words(retry.group)
    kind = f"{kind} " if kind else ""
    if not count:
        print(f"Nothing to read again: no timed-out {kind}files.")
        if groups:
            print("  Timed out: " + ", ".join(
                f"{_type_words(g['ext'])} x{g['count']:,}" for g in groups))
        return False
    print(f"Reading {count:,} timed-out {kind}file(s) again, each with "
          f"{retry.factor:g} times its usual time limit.")
    print("  For this run only: the saved limits are not changed, and no other "
          "file is read.")
    return True


def cmd_timed_out(args: argparse.Namespace) -> int:
    """List the timed-out files, by type. Read-only: no lock, nothing written.

    The command-line half of the Indexing page's "Timed-out files" panel
    (non-negotiable 8): the same groups from the same query, and the command
    that reads one again.
    """
    from app.index.timed_out_retry import DEFAULT_FACTOR, normalise_group
    from app.storage.sqlite_store import SqliteStore

    settings = _load(args)
    setup_logging(settings.log_path)
    groups = _timed_out_groups(settings)
    total = sum(g["count"] for g in groups)
    wanted = getattr(args, "type", None)
    group = normalise_group(wanted)              # None: every type
    files: list = []
    if wanted is not None and groups:
        with SqliteStore(settings.fts_db) as store:
            files = store.timed_out_files(group)

    if args.json:
        payload: dict = {"total": total, "groups": groups}
        if wanted is not None:
            payload["files"] = files
        print(json.dumps(payload, indent=2))
        return EXIT_OK

    if not groups:
        print("No files are timed out.")
        return EXIT_OK
    if wanted is not None:
        kind = "all types" if group is None else _type_words(group)
        print(f"Timed out  {len(files):,} file(s), {kind}")
        for row in files:
            print(f"  {row['path']}")
            if row["skip_detail"]:
                print(f"    {row['skip_detail']}")
        return EXIT_OK

    print(f"Timed out  {total:,} file(s), by type")
    for group in groups:
        print(f"  {_type_words(group['ext']):<16}{group['count']:>8,}   "
              f"e.g. {group['example']}")
    print()
    print("Read a type again with a longer time limit, for that run only:")
    example = groups[0]["ext"] or '""'
    print(f"  app.cli index --retry-timed-out {example} "
          f"--time-limit-factor {DEFAULT_FACTOR}")
    print("Leave the type out to read them all again. `app.cli timed-out TYPE` "
          "lists the files.")
    return EXIT_OK


def _ocr_mode(args: argparse.Namespace, settings: Settings) -> str:
    """Which pass this run is: `both`, `text` or `images`.

    A flag on the command line wins over the setting, because naming one is an
    instruction. Without a flag the setting decides, so the choice made once in
    Settings applies to the scheduled runs as well - which is the whole reason
    it is a setting and not only a flag.
    """
    if getattr(args, "only_ocr", False):
        return "images"
    if getattr(args, "skip_ocr", False):
        return "text"

    from app.index.pipeline import OCR_MODES

    # **`INDEX_OCR_PASS` decides *when*, `INDEX_OCR_MODE` decides *what*.**
    # They meet here because a run is only ever one pass: asking for the images
    # to be done after the run means this run is the text one, and the images
    # pass is a second `--only-ocr` run. Neither setting can express that
    # alone, which is why the schedule half is its own control rather than a
    # fourth value squeezed into the mode.
    schedule = str(getattr(settings, "index_ocr_pass", "with-run")
                   or "with-run").strip().lower()
    if schedule in ("after-run", "manual"):
        return "text"

    stored = str(getattr(settings, "index_ocr_mode", "both") or "both").strip().lower()
    return stored if stored in OCR_MODES else "both"


def _images_pass_follows(settings: Settings) -> bool:
    """Should an images pass be started once the text pass finishes?

    `after-run` yes, `manual` no. The difference is the whole point of having
    two words for it: somebody with a scanned corpus wants the text usable
    today *and* the images eventually, and somebody on a laptop wants to choose
    the evening it happens.
    """
    return str(getattr(settings, "index_ocr_pass", "with-run")
               or "with-run").strip().lower() == "after-run"


def cmd_reembed(args: argparse.Namespace) -> int:
    """Rebuild the vector store from SQLite. No re-reading of any document.

    **This is why SQLite is the authority and LanceDB is derived.** Every chunk's
    text is already in the metadata store, so the vectors can always be rebuilt
    without touching the corpus - which turns "the semantic half of search is
    broken" from a 100GB re-index into a job measured in minutes.

    Deliberately its own command rather than a flag on `index`. It answers a
    different question ("the vectors are wrong") and must not walk the disk,
    hash anything, or prune a file that happens to be on a disconnected drive.
    """
    from app.index.embedder import Embedder
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore

    settings = _load(args)
    setup_logging(settings.log_path)

    # A writer, so it takes the run lock - `reembed` and `index` must exclude
    # each other as firmly as two `index` runs do.
    with SqliteStore(settings.fts_db) as store, \
            IndexRunLock(store, owner=COMMAND_LINE), \
            VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors:
        stats = store.stats()
        total = int(stats["chunks_total"])
        if total == 0:
            print("Nothing is indexed yet, so there is nothing to embed.")
            print(r'  venv\Scripts\python.exe -m app.cli index "D:\YourFolder"')
            return EXIT_OK

        if args.all:
            # Every chunk goes back in the queue. The table is dropped rather
            # than written over: leaving stale rows behind is how a rebuild ends
            # up with more vectors than there are passages.
            print(f"Clearing the vector store and re-embedding all {total:,} passages.")
            vectors.drop()
            store.mark_all_unembedded()

        # **Say what is about to happen, before the silence starts.**
        #
        # Nothing was printed until the first batch of 256 finished. At the 4.4
        # passages/second `embed-bench` measures on a real machine that is
        # nearly a minute of a completely silent terminal, and the correct
        # response to a silent terminal is to assume it has hung and kill it -
        # which loses the work. Reported as exactly that: "this seems stuck".
        #
        # The standing rule is that nothing fails silently. A long operation
        # that says nothing is the same fault wearing a different hat: there is
        # no way to tell it from one that has died.
        outstanding = max(0, total - int(stats["chunks_embedded"]))
        if not args.quiet:
            print(f"Embedding {outstanding:,} of {total:,} passages "
                  f"({total - outstanding:,} already done).")
            print("  Loading the model, then the first batch of 256 - "
                  "the first line takes a minute or so.", flush=True)

        embedder = Embedder.from_settings(settings)
        done = 0
        started = time.time()
        # Split three ways, because the totals lie. `embed-bench` measured 4.4
        # passages/second on this machine and the loop ran at 1.5 - so roughly
        # two thirds of the time was going somewhere other than the model, and
        # no amount of choosing a faster model would have touched it. A rate
        # without a breakdown behind it sends people optimising the wrong thing.
        spent = {"read": 0.0, "embed": 0.0, "write": 0.0}

        mark = time.perf_counter()
        for batch in store.iter_unembedded(batch_size=256):
            spent["read"] += time.perf_counter() - mark

            mark = time.perf_counter()
            embedded = embedder.embed([chunk.text for chunk in batch])
            spent["embed"] += time.perf_counter() - mark

            mark = time.perf_counter()
            written = vectors.add(
                chunk_ids=[chunk.id for chunk in batch],
                file_ids=[chunk.file_id for chunk in batch],
                vectors=embedded,
            )
            # Marked only after the vectors are safely written. The other order
            # loses passages silently: a crash between the two would leave rows
            # flagged embedded with nothing in LanceDB, and nothing would ever
            # pick them up again.
            #
            # **The count, not its truthiness.** `if written:` marked the whole
            # 256-chunk batch embedded when one vector was written - which is
            # exactly the bug `78aa392` fixed in the pipeline, still standing
            # here on the path people run *to repair* that bug. A short write
            # leaves the batch unmarked so the next `reembed` retries it.
            if written is not None and written < len(batch):
                logger.bind(component="cli.reembed").error(
                    "wrote {} vectors for {} passages - the rest stay unembedded "
                    "and a later `reembed` will retry them.", written, len(batch))
            else:
                store.mark_embedded([chunk.id for chunk in batch])
            spent["write"] += time.perf_counter() - mark

            done += len(batch)
            if not args.quiet:
                elapsed = max(time.time() - started, 0.001)
                share = " ".join(
                    f"{name} {value / elapsed:.0%}" for name, value in spent.items()
                )
                print(f"  embedded {done:,}  ({done / elapsed * 60:,.0f}/min)   {share}",
                      flush=True)
            mark = time.perf_counter()

        rows = vectors.count()
        elapsed = max(time.time() - started, 0.001)
        print()
        print(f"Done. {rows:,} vectors for {total:,} passages in {elapsed/60:.1f} min.")
        if done:
            print(f"  reading SQLite   {spent['read']:>7.1f}s  {spent['read']/elapsed:>5.0%}")
            print(f"  embedding        {spent['embed']:>7.1f}s  {spent['embed']/elapsed:>5.0%}"
                  f"   ({done/max(spent['embed'], 0.001):.1f}/sec while running)")
            print(f"  writing vectors  {spent['write']:>7.1f}s  {spent['write']/elapsed:>5.0%}")
            slowest = max(spent, key=spent.get)
            if slowest != "embed":
                print()
                print(f"  Most of the time is going to {slowest}, not the model.")
                print("  A faster or smaller model would not help this run.")
        if rows < total:
            print(f"  {total - rows:,} passages still have no vector - see the log.")
        return EXIT_OK


def add_index_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_index = sub.add_parser("index", parents=[common], help="build or update the index")
    p_index.add_argument("roots", nargs="*", help="folders to index")
    p_index.add_argument("--first", action="append", metavar="PATH",
                         help="index this folder before the others; repeatable, in order")
    p_index.add_argument("--order", choices=("newest", "found"), default=None,
                         help="newest: find every file first, then read the --first "
                              "folders, then the rest newest first (the default, "
                              "from INDEX_ORDER); found: read in the order the scan "
                              "finds them")
    p_index.add_argument("--workers", type=int, metavar="N",
                         help="files read at once (default: half your cores, capped at 4)")
    p_index.add_argument("--fast", action="store_true",
                         help="trust mtime and size without re-hashing changed files")
    p_index.add_argument("--no-prune", action="store_true",
                         help="keep rows for files that have disappeared")
    p_index.add_argument("--include-cloud", action="store_true",
                         help="index OneDrive placeholders too, downloading them - "
                              "every root in this run, at the default cap; for one "
                              "folder at a time use --allow-cloud-content instead")
    p_index.add_argument("--allow-cloud-content", action="append", metavar="PATH",
                         default=[],
                         help="download and index cloud-only files under this folder "
                              "(202626270514 §2b), up to --cloud-content-cap-mb; "
                              "repeatable, one per folder")
    p_index.add_argument("--cloud-content-cap-mb", type=int, metavar="MB",
                         help="cloud content download budget for this run, shared "
                              "across every --allow-cloud-content folder together, "
                              "not one each (default from .env, 1024)")
    p_index.add_argument("--memory-mb", type=int, metavar="MB",
                         help="pause above this much memory (default from .env, 1500)")
    p_index.add_argument("--cpu-percent", type=int, metavar="PCT",
                         help="pause while the machine is busier than this; 0 disables")
    p_index.add_argument("--full-speed", action="store_true",
                         help="no CPU, battery or priority limits - for a machine "
                              "nobody is using. Will make this one feel slow.")
    p_index.add_argument(
        "--force", action="store_true",
        help="index every file found, ignoring change detection. Use when the\nindex says a file is up to date but its content is missing.")
    # **The two passes.** Mutually exclusive so `--skip-ocr --only-ocr` is
    # refused with a sentence rather than silently resolved to one of them.
    ocr_group = p_index.add_mutually_exclusive_group()
    ocr_group.add_argument(
        "--skip-ocr", "--no-ocr", dest="skip_ocr", action="store_true",
        help="index everything readable without OCR, and queue the images.\n"
             "Search becomes useful in a day or two instead of a fortnight;\n"
             "the queued files are held, not failed.")
    ocr_group.add_argument(
        "--only-ocr", dest="only_ocr", action="store_true",
        help="read only the images, and only walk the image types. This is\n"
             "the second pass - run it behind the first.")
    p_index.add_argument(
        "--recheck-archives", action="store_true",
        help="walk every folder marked as an archive in full, and record a new\n"
             "pass. Use when something has plainly changed inside one.")
    p_index.add_argument(
        "--all-roots", action="store_true",
        help="ignore the Live/Archive modes for this run only, without\n"
             "updating any archive's record")
    p_index.add_argument(
        "--retry-skipped", action="store_true",
        help="re-read files an earlier run skipped, even unchanged ones.\n"
             "Normally a skip is settled: an unchanged file cannot produce a\n"
             "different answer, and re-parsing thousands of known failures every\n"
             "run costs hours. Use this after changing what the machine can do -\n"
             "installing LibreOffice, adding a library, raising a size ceiling.\n"
             "Far cheaper than --force, which re-indexes everything.")
    # Order 0z F3. `nargs="?"`: the flag alone is every timed-out file, and
    # the window's child process writes `--retry-timed-out=TYPE` (with `=`, so
    # an empty TYPE - files with no extension - survives).
    p_index.add_argument(
        "--retry-timed-out", nargs="?", const="*", default=None, metavar="TYPE",
        help="read the timed-out files again, with a longer time limit for\n"
             "this run only, and read nothing else. TYPE is a file type from\n"
             "`app.cli timed-out` (pdf, pst, ...); leave it out for all of\n"
             "them. A file that times out again stays timed out; one that\n"
             "has changed since is read with the usual limit.")
    p_index.add_argument(
        "--time-limit-factor", type=float, metavar="N", default=None,
        help="with --retry-timed-out: how many times the usual limit each\n"
             "file is given (default 4; 1 to 100). The saved limits are not\n"
             "changed.")
    # 2026-09-20. **The pause, as a file rather than a verb.**
    #
    # Pausing is something you do to a run that is already going, and one
    # command cannot reach into another command's memory. The only channel
    # two Leasha processes share is the index database, and its stop flag
    # lives in `app/core/run_lock.py`, outside this change. A verb -
    # `app.cli index --pause` - would therefore have had nothing to act on,
    # and a switch that cannot work is worse than no switch.
    #
    # So the run watches a path it was told about: the file appears, the run
    # holds; the file goes, the run carries on. Anything can make it - the
    # person, a script, a scheduled task before a meeting - and nothing has
    # to be installed or listening for it to work.
    p_index.add_argument(
        "--pause-file", metavar="PATH",
        help="hold this run whenever this file exists, and carry on when it\n"
             "is deleted. Nothing is lost either way: a paused run keeps its\n"
             "place and picks up where it left off. Use it to get the machine\n"
             "back for an hour without ending the run:\n"
             "  type nul > pause.flag   holds it\n"
             "  del pause.flag          carries on")
    p_index.add_argument("--quiet", action="store_true", help="no progress lines")
    # Work order 0x §2a. **The machine-readable mode the window's child process
    # uses** - see `_EventSession` and `app/index/run_events.py`. Shown in
    # `--help`, because a person can usefully watch it: one JSON object per
    # line on standard output; `pause`, `resume` or `stop` typed on standard
    # input act on the run; closing standard input stops it.
    p_index.add_argument(
        "--events", choices=("jsonl",), metavar="jsonl",
        help="machine-readable progress: one JSON object per line on stdout\n"
             "(progress, heartbeat, finished); reads pause / resume / stop on\n"
             "stdin, and stops when stdin closes. For the window's own use.")
    # The rest are for the window and the benchmark only, so hidden from help.
    p_index.add_argument("--run-owner", choices=("command-line", "window"),
                         default="command-line", help=argparse.SUPPRESS)
    p_index.add_argument("--cloud-content-key", action="append", default=[],
                         help=argparse.SUPPRESS)
    p_index.add_argument("--fake-embedder-for-bench", action="store_true",
                         help=argparse.SUPPRESS)
    p_index.set_defaults(func=cmd_index)
    # Order 0z F3: `timed-out`, the listing that goes with `--retry-timed-out`.
    # Registered from here, beside the flag it serves.
    add_timed_out_parser(sub, common)


def add_timed_out_parser(sub: argparse._SubParsersAction,
                         common: argparse.ArgumentParser) -> None:
    p_timed = sub.add_parser(
        "timed-out", parents=[common],
        help="list the files that ran out of time, by type")
    p_timed.add_argument(
        "type", nargs="?", default=None, metavar="TYPE",
        help="list each file of this type (pdf, pst, ...; * for all) with "
             "what was recorded, in place of the counts")
    p_timed.set_defaults(func=cmd_timed_out)


def add_reembed_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_reembed = sub.add_parser(
        "reembed", parents=[common],
        help="rebuild the vector store from SQLite - no documents are re-read")
    p_reembed.add_argument("--all", action="store_true",
                           help="drop every vector and start over, not just the missing ones")
    p_reembed.add_argument("--quiet", action="store_true", help="no progress lines")
    p_reembed.set_defaults(func=cmd_reembed)
