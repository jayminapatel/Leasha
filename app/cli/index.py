"""`index` and `reembed`: building the index and rebuilding its vectors."""

from __future__ import annotations

import argparse
import json
import os
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


def cmd_index(args: argparse.Namespace) -> int:
    """Build or update the index. Layer 3's entry point.

    Unlike `extract`, this one writes - so it takes the single-instance lock.
    Two copies indexing into one SQLite file is exactly the corruption the
    mutex exists to prevent.
    """
    from app.index.clip_embedder import ClipImageEmbedder
    from app.index.embedder import Embedder
    from app.extract.media import MediaConfig
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.walker import WalkConfig, own_paths
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import ImageVectorStore, VectorStore

    settings = _load(args)
    setup_logging(settings.log_path)
    log = logger.bind(component="cli.index")

    roots = [Path(root).expanduser() for root in (args.roots or [])]
    from_settings = False
    if not roots:
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

    if not roots:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.index",
            key="roots", reason="no folders to index",
            suggestion=r'Name them here - app.cli index "D:\SearchData" - or '
                       r'set them once on the Settings page, under "Folders to '
                       r'index", and run this with no arguments.',
        ), args.json)

    if from_settings and not args.json:
        # **Said out loud.** A command that silently uses a setting is a command
        # whose output cannot be attributed to anything.
        print("Indexing the folders saved in Settings:")
        for root in roots:
            print(f"  {root}")

    missing = [root for root in roots if not root.exists()]
    if missing:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.index",
            key="roots", reason=f"does not exist: {', '.join(str(m) for m in missing)}",
            suggestion="Check the path and the drive. A folder on a disconnected drive looks "
                       "exactly like a folder that was deleted.",
        ), args.json)

    limits = limits_from_settings(settings)

    # **The tuning mode reaches the run, not only the screen.** Before this the
    # panel resolved `0` to `Auto (4)` for display and the run read the literal
    # `0`, so switching modes changed what was shown and nothing about what
    # happened. `resolve_for_run` is the same arithmetic the screen uses - one
    # function, so the two cannot drift.
    from app.index.resolve import resolve_for_run

    with SqliteStore(settings.fts_db) as _store:
        tuned = resolve_for_run(settings, _store)
    limits = replace(limits, workers=tuned.workers)
    _tuning_log = logger.bind(component="cli.index")
    for key, why in tuned.why.items():
        _tuning_log.debug("{}: {}", key, why)

    # `--workers` still wins: a flag typed on this command is a decision about
    # this run, and a tuning mode is a standing preference.
    if args.workers:
        limits = replace(limits, workers=args.workers)
    if args.memory_mb:
        limits = replace(limits, memory_mb=args.memory_mb)
    if args.cpu_percent is not None:
        limits = replace(limits, cpu_percent=args.cpu_percent)
    if args.full_speed:
        # An explicit opt-out for a machine nobody is using. Named for what it
        # costs rather than what it gives: this is the setting that makes the
        # computer unusable while it runs.
        limits = replace(
            limits, cpu_percent=0, pause_on_battery=False, low_priority=False,
            workers=args.workers or max(1, (os.cpu_count() or 2) - 1),
        )

    config = PipelineConfig(
        walk=WalkConfig(
            roots=roots,
            priority_roots=[Path(p).expanduser() for p in (args.first or [])],
            include_cloud=args.include_cloud,
            cloud_content_roots=frozenset(
                str(Path(p).expanduser()).rstrip("\\/").lower()
                for p in (args.allow_cloud_content or [])
            ),
            cloud_content_cap_bytes=(
                args.cloud_content_cap_mb or settings.cloud_content_cap_mb
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
        verify_hash=not args.fast,
        prune_missing=not args.no_prune,
        force=bool(getattr(args, "force", False)),
        retry_skipped=bool(getattr(args, "retry_skipped", False)),
        # A folder marked as an archive is walked once and then checked
        # cheaply - see `app/index/archives.py`. `--all-roots` is the escape
        # hatch that ignores the modes entirely without touching the records.
        ocr_mode=_ocr_mode(args, settings),
        archives=not bool(getattr(args, "all_roots", False)),
        recheck_archives=bool(getattr(args, "recheck_archives", False)),
        recheck_days=settings.archive_recheck_days,
        # Resolved for this machine and this mode, above.
        embed_batch=tuned.embed_batch,
        dedup_chunks=settings.embed_dedup,
        two_phase=settings.index_two_phase,
        bulk_fts=settings.index_bulk_fts,
        caption_trickle_enabled=settings.caption_trickle_enabled,
        ollama_url=settings.ollama_url,
        ollama_vision_model=settings.ollama_vision_model,
        people_recognition_enabled=settings.people_recognition_enabled,
        # Work order 202626270515. Off unless VIDEO_INDEXING_ENABLED and/or
        # AUDIO_TRANSCRIPTION_ENABLED are on in `.env`.
        media=MediaConfig.from_settings(settings),
        # Work order 202626130120 (0t) section 6: resolved once, above, by
        # the same resolve_for_run call the window uses before it builds a
        # Pipeline.
        gpu_regression_notice=tuned.gpu_regression_notice,
    )

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

    progress = ProgressLine(enabled=not args.quiet and not args.json)

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

    def show(stats) -> None:
        if args.json:
            return
        line = (f"  {stats.indexed:>7,} docs  {stats.unchanged:>6,} unchanged  "
                f"{stats.unchanged_documents:>7,} already current  "
                f"{stats.chunks:>8,} chunks")

        if getattr(stats, "paused", False):
            # **A pause with nothing said is a hang, as far as anyone watching
            # is concerned.** The window was given this earlier today; the
            # command line builds its own line and was not, so a real run sat
            # on an unchanging line for minutes while the governor waited for
            # memory to settle - and was reported as stuck. It was working.
            reason = getattr(stats, "pause_reason", "") or "waiting for resources"
            progress.update(f"{line}  | PAUSED - {reason[:70]}")
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
    with SqliteStore(settings.fts_db) as store, \
            IndexRunLock(store, owner=COMMAND_LINE), \
            VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors, \
            ImageVectorStore(settings.vector_path) as image_vectors:
        pipeline = Pipeline(
            store, vectors, embedder, config,
            image_embedder=image_embedder, image_vectors=image_vectors,
        )
        stats = pipeline.run(on_progress=None if args.quiet else show)

    payload = stats.as_dict()
    if args.json:
        print(json.dumps(payload, indent=2))
        return EXIT_ERROR if stats.stopped_early else EXIT_OK

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
    for notice in getattr(stats, "notices", ()):
        print()
        print(f"Note      {notice}")
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
        print(f"Waited    {stats.paused_seconds / 60:,.1f} min across {stats.pauses} "
              f"pause(s) to stay out of the way")
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
    if _images_pass_follows(settings) and _ocr_mode(args, settings) == "text":
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
    p_index.add_argument("--quiet", action="store_true", help="no progress lines")
    p_index.set_defaults(func=cmd_index)


def add_reembed_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_reembed = sub.add_parser(
        "reembed", parents=[common],
        help="rebuild the vector store from SQLite - no documents are re-read")
    p_reembed.add_argument("--all", action="store_true",
                           help="drop every vector and start over, not just the missing ones")
    p_reembed.add_argument("--quiet", action="store_true", help="no progress lines")
    p_reembed.set_defaults(func=cmd_reembed)
