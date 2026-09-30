"""The index controller: an index run's lifecycle, and the work around it.

Layer: L5

Extracted from `app/ui/shell.py` (work order 202626082352 section 7,
"Structural"). Everything here was a `MainWindow` method: resolving the tuning
numbers and building the `Pipeline` for Start, the schedule, the run another
process may be doing, the idle bench and the tuning evidence, resetting the
index, converting a PST, and the offline-media Scan / Rescan / Delete runs -
which take the same run lock and are the same kind of work.

**The window still owns the state and the views.** `window._resolving_index`,
`window._idle_bench_running`, `window.scheduler`, `window.indexing_view` and the
rest are read and written through `self._w`, exactly as they were through
`self`. Every call from one handler to another also goes back out through the
window (`self._w._start_indexing(...)`, not `self._start_indexing(...)`), so
`MainWindow` keeps a same-named method for each handler below and a test that
replaces one on the window intercepts the calls made from in here too.

**Threading is unchanged, and this is where it matters most.** Each handler
that dispatched a `CallableWorker` still does, with the same function, the same
`run(QThreadPool.globalInstance(), worker)` and the same signals; the moves
add no store or filesystem call to the UI thread. `test_ui_never_blocks` scans
this module with the same rules as the rest of `app/ui`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from PyQt6.QtCore import QObject, QThreadPool
from PyQt6.QtWidgets import QMessageBox

from app.core.logging import logger
from app.core.run_lock import GUI
from app.index.resources import limits_from_settings
from app.index.schedule import SchedulePolicy
from app.ui.scheduler import IndexScheduler
# **Worker bodies live in the presenter**, not here: `test_ui_never_blocks`
# reads these files and refuses any store call it cannot prove is inside a
# worker, and it cannot prove that of a module-level function defined here.
from app.ui.presenter import (
    PREPARING_WORDS, _read_external_run, _scan_and_save, cleared_message,
    index_bytes, offline_media_run_summary,
)
from app.ui.state_writes import save_states
from app.ui.widgets.indexing_layout import repaint_totals
from app.ui.workers import CallableWorker, run

_log = logger.bind(component="ui.shell")


def _on_battery() -> bool:
    """A confirmed "running unplugged" answer, and only that. `None` - no
    battery, or a platform where the question does not apply - is False, so
    an unreadable state never blocks the idle bench (rule 2)."""
    try:
        import psutil

        state = psutil.sensors_battery()
    except Exception:                            # noqa: BLE001 - advisory
        return False
    return state is not None and not state.power_plugged


class IndexController(QObject):
    """The index run's lifecycle for one `MainWindow`.

    A `QObject` parented to the window, not a plain class: a signal connected
    to one of its bound methods is delivered on the GUI thread by the same
    rule that applied when the receiver was the window itself.
    """

    def __init__(self, window: Any) -> None:
        super().__init__(window)
        self._w = window

    # -- the schedule -------------------------------------------------------

    def _start_scheduler(self) -> None:
        """Wire the clock to the indexer.

        `is_running` is the guard that matters. The single-instance lock stops a
        second *process* touching the database; nothing stops this application
        starting a scheduled run on top of one already in progress, and that is
        the mistake a timer makes at 02:00 with nobody watching.
        """
        self._w.scheduler = IndexScheduler(
            SchedulePolicy.from_settings(self._w._settings),
            is_running=lambda: self._w.indexing_view.is_running(),
            load_last_run=self._w._load_last_index_time,
            save_last_run=self._w._save_last_index_time,
            parent=self._w,
        )
        self._w.scheduler.due.connect(lambda: self._w._start_indexing())
        self._w.scheduler.state_changed.connect(
            lambda text: self._w.notify(f"Indexing: {text}", 8_000)
        )
        # **"Next run" was permanently blank.** `set_next_run` existed, said what
        # it was for, and nothing ever called it - so the one line answering "is
        # this thing going to run on its own, and when" showed nothing at all,
        # on a page whose whole job is to answer that.
        self._w.scheduler.state_changed.connect(self._w.indexing_view.set_next_run)
        self._w.indexing_view.finished.connect(lambda _stats: self._w.scheduler.notify_finished())
        self._w.scheduler.start()
        self._w.indexing_view.set_next_run(self._w.scheduler.status())

    def _schedule_changed(self, policy: Any) -> None:
        """Apply a schedule change immediately, and persist it.

        Persisted in `index_state` rather than rewritten into `.env`: the app
        must never edit a file the user maintains by hand, and a settings panel
        that silently rewrites configuration is how hand-written comments and
        overrides disappear. `.env` remains the default; this is the override.
        """
        # Queued (bug 3a): a schedule is most often changed while a run is
        # going, which is exactly when a synchronous write waits on its batch.
        save_states(self._w._store, {
            "ui:index_schedule": policy.mode,
            "ui:index_interval_hours": str(policy.interval_hours),
            "ui:index_daily_at": f"{policy.daily_at[0]:02d}:{policy.daily_at[1]:02d}",
        }, component="ui.schedule")
        self._w.scheduler.set_policy(policy)
        self._w.indexing_view.schedule_box.set_schedule_status(self._w.scheduler.status())

    def _load_last_index_time(self) -> Optional[datetime]:
        raw = self._w._store.get_state("index:last_run")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            # A corrupt timestamp must not stop the app opening. Treating it as
            # "never ran" schedules one run, which is the safe direction.
            return None

    def _save_last_index_time(self, when: datetime) -> None:
        # A worker body already: `IndexScheduler.notify_finished` hands this
        # to a `CallableWorker`, which is why it is named in
        # `test_ui_never_blocks.OFF_THREAD` rather than queued again here.
        self._w._store.set_state("index:last_run", when.isoformat(timespec="seconds"))

    # -- the tuning screen's evidence ---------------------------------------

    def _last_run_record(self) -> Optional[dict]:
        r"""What the last run measured, for the tuning footer.

        **Read from what the run itself wrote**, so the footer cannot disagree
        with the run log about what happened. `stats` are stored as a `repr`
        of a plain dict of numbers and strings, which `literal_eval` reads
        without executing anything - a `pickle` here would be a file on disk
        that runs code, for a progress figure.

        Stage timings are not recorded yet; the footer says so rather than
        inventing a split. §6a is where they start being measured.
        """
        raw = self._w._read_state("last_run_stats", "")
        if not raw:
            return None
        try:
            import ast

            stats = ast.literal_eval(raw)
            if not isinstance(stats, dict):
                return None
        except (ValueError, SyntaxError) as exc:
            _log.debug("the last run's stats could not be read: {}", exc)
            return None

        elapsed = float(stats.get("elapsed_s") or 0.0)
        chunks = float(stats.get("chunks") or 0.0)
        return {
            "stages": stats.get("stages") or {},
            "chunks_per_minute": (chunks / elapsed * 60) if elapsed > 0 else 0,
            # **What the run recorded, not what the settings say now.** The
            # settings are a fallback for records written before §5c existed;
            # reading them for a recent run would describe this moment rather
            # than that one, which is the difference between a measurement and
            # an anecdote.
            "resolved": stats.get("resolved") or {
                "workers": self._w._settings.index_workers,
                "batch": self._w._settings.embed_batch,
                "device": self._w._settings.embed_device,
            },
        }

    def _ocr_mode_for_run(self) -> str:
        r"""Which pass this run is, given *what* to read and *when*.

        `INDEX_OCR_MODE` says what; `INDEX_OCR_PASS` says when. They meet here
        because a run is only ever one pass: choosing to do the images after
        the run means *this* run is the text one, and the images are a second
        run. Neither setting can express that alone, which is why the schedule
        is its own control rather than a fourth value crammed into the mode.
        """
        schedule = str(getattr(self._w._settings, "index_ocr_pass", "with-run")
                       or "with-run")
        if schedule in ("after-run", "manual"):
            return "text"
        return str(getattr(self._w._settings, "index_ocr_mode", "both"))

    def _offer_images_pass(self, _stats: Any) -> None:
        """After a text-only run, say the images are still to do.

        **Offered, never started.** A second pass over a scanned corpus is
        hours; launching it because a text run finished - possibly while
        somebody has gone home - is the kind of surprise that gets an
        application uninstalled. `manual` says nothing at all, which is what
        the word means.
        """
        if str(getattr(self._w._settings, "index_ocr_pass", "")) != "after-run":
            return
        self._w.notify(
            "Text is indexed. Images and scans are still to read - press Start "
            "again to do those.", 30_000)

    def _refresh_tuning_status(self) -> None:
        """§5d's status line, and the rates Auto-tune resolves against.

        Both read the store, so both happen here rather than in the widget -
        the panel is built inside `MainWindow.__init__`, where nothing may
        touch a database.
        """
        try:
            from app.core.compute_profile import cached_profile
            from app.core.measured import for_profile
            from app.index.autotune import status_line

            profile = cached_profile(self._w._store, self._w._settings.data_path)
            self._w.indexing_view.tuning.set_tuned_status(
                status_line(self._w._store, profile))
            self._w.indexing_view.tuning.set_measured(
                {"rates": for_profile(self._w._store, profile)})
        except Exception as exc:                 # noqa: BLE001 - a status line
            _log.debug("the tuning status could not be refreshed: {}", exc)

    def _learn_from_run(self, stats: Any) -> None:
        r"""§5c: what the run just measured, and what it argues for.

        **In Auto the change applies itself and the notice is past tense.** The
        product rule is explicit: a non-technical person must never be handed a
        decision in order to get the benefit. In Manual it is a proposal, and
        the status bar says so.

        Wrapped whole, because none of this may cost somebody the end of an
        index run that otherwise succeeded.
        """
        try:
            from app.core.compute_profile import cached_profile
            from app.index.autotune import learn

            profile = cached_profile(self._w._store, self._w._settings.data_path)
            found = learn(self._w._store, profile, stats,
                          mode=self._w.indexing_view.tuning.current_mode(),
                          device=self._w._settings.embed_device)
            self._w._refresh_tuning_status()
            self._w.indexing_view.tuning.set_last_run(self._w._last_run_record())
            if not found:
                return
            if found.applied:
                self._w._limits_changed({key.lower(): value
                                      for key, value in found.values.items()})
            self._w.notify(found.message, 20_000, level="warning")
        except Exception as exc:                 # noqa: BLE001
            _log.debug("nothing was learned from this run: {}", exc)

    def _benchmark_models(self) -> None:
        """Time the embedding model on this machine, off the UI thread.

        **The one question the specification sheet cannot answer.** Whether 96
        Iris Xe execution units beat this particular processor on a small embed
        model is not knowable from the numbers on the box, and §0 of the
        index-tuning order says so; this is how somebody finds out.
        """
        from app.core.compute_profile import cached_profile
        from app.core.measured import remember
        from app.index.index_bench import run_index_bench

        # **The whole pipeline, not only the model.** This button used to run
        # `embed_bench`, which answers "how fast is the model here" - the
        # smaller half. A machine whose model is quick and whose disk is slow
        # is bounded by the disk, and a screen holding only the model number
        # will confidently recommend a graphics card to somebody who needs a
        # different drive. `bench-index` on the command line does the same
        # work; this is the same function, so the two cannot disagree.
        devices = ("cpu", "gpu") if self._w._can_use_gpu() else None

        def measure() -> Any:
            found = run_index_bench(self._w._settings, devices=devices)
            if not found.error:
                profile = cached_profile(self._w._store, self._w._settings.data_path)
                remember(self._w._store, found.as_measured(profile.fingerprint()))
            return found

        self._w.notify(
            "Timing this computer on a fixed workload - about a minute…",
            120_000)
        worker = CallableWorker(measure, component="ui.tuning")
        worker.signals.finished.connect(self._w._benchmarked)
        worker.signals.failed.connect(self._w._show_error)
        run(QThreadPool.globalInstance(), worker)

    def _can_use_gpu(self) -> bool:
        """Is there a graphics card worth timing against the processor?

        The one question §0 says the specification sheet cannot answer, so it
        is only worth the extra minute when there is something to compare.
        """
        try:
            from app.core.compute_profile import cached_profile
            from app.index.backends import why_unavailable

            return not why_unavailable(
                cached_profile(self._w._store, self._w._settings.data_path))
        except Exception:                        # noqa: BLE001
            return False

    def _benchmarked(self, result: Any) -> None:
        """Report a benchmark in the numbers somebody can act on.

        **Reading, writing and the model, not only the model.** A run whose
        model is quick and whose disk is slow is bounded by the disk, and the
        three side by side are what say which.
        """
        if getattr(result, "error", ""):
            self._w.notify(
                f"The benchmark could not run: {result.error}", 15_000)
            return

        rates = dict(getattr(result, "embed_per_second", {}) or {})
        parts = [f"reading {result.extract_per_second:,.0f} files a second",
                 f"writing {result.write_per_second:,.0f} chunks a second"]
        parts += [f"meaning {rate:,.0f} a second on the "
                  f"{'graphics card' if device == 'gpu' else 'processor'}"
                  for device, rate in rates.items()]
        # Notes carry the things that make a number untrustworthy - a model
        # that would not load, a graphics card that declined. Saying the
        # number without them is how a figure nobody should act on gets quoted
        # for a year.
        said = "; ".join(parts) + ("  " + " ".join(result.notes)
                                   if result.notes else "")
        self._w.notify(f"This computer: {said}", 40_000)
        self._w._refresh_tuning_status()

    def _rescan_archives(self) -> None:
        """One full walk of every archival folder, now. Not a policy change."""
        self._w._start_indexing(recheck_archives=True)

    # -- a run belonging to another process ---------------------------------

    def _poll_external_run(self) -> None:
        r"""Is something else indexing, and how far has it got?

        **Two questions, and only one of them is authoritative.** `is_indexing`
        asks the mutex, which the operating system releases when a process dies;
        `active_run` reads the description that process last wrote. A record
        without a lock is a crash, not a run, and must never refuse Start.

        Off the UI thread, because both touch the store and this runs on a timer
        for as long as the window is open. Cheap - one mutex probe and one row -
        but "cheap" on the UI thread is how a window develops a stutter nobody
        can attribute.
        """
        if self._w.indexing_view.is_running() and self._w.indexing_view._worker is not None:
            return                       # our own run; the live signal is better
        if self._w._resolving_index:
            # A resolve dispatched by `_start_indexing` has no Pipeline yet,
            # so `is_running()` above cannot see it - the very thing this
            # method exists to catch (a run belonging to another process) is
            # indistinguishable, from here, from "nothing is running yet
            # because we are still resolving our own". Skipping the read
            # entirely is cheap and correct: the next tick, four seconds
            # later, sees the truth once resolution has actually finished.
            return

        worker = CallableWorker(_read_external_run, self._w._store,
                                component="ui.index.watch")
        worker.signals.finished.connect(self._w._show_external_run)
        run(QThreadPool.globalInstance(), worker)

    def _show_external_run(self, payload: dict) -> None:
        # The guard in `_poll_external_run` closes most of the window, but
        # this read was dispatched asynchronously - a resolve can begin
        # *after* the dispatch and still be in flight when this result comes
        # back. `IndexingView._go_idle` (via `paint_external`) has no notion
        # of `_resolving_index` and would otherwise re-enable Start here,
        # exactly the second-click invitation non-negotiable #5 and this
        # button's own disable-on-click logic exist to prevent. `_run_link`
        # is unrelated to the run display and still runs either way.
        if not self._w._resolving_index:
            self._w.indexing_view.show_external(
                payload.get("record"), locked=bool(payload.get("locked")))
        self._w._run_link(payload.get("link"))
        if payload.get("front_requested"):
            self._w._front_self()

    def _stop_external_run(self) -> None:
        """Ask the other process to stop. A request, not a kill.

        Terminating it would leave the vector store mid-write, which is the one
        thing the run lock exists to prevent - so this writes the flag and the
        runner honours it at its next checkpoint, keeping everything read so far.
        """
        from app.core.run_lock import request_stop

        try:
            request_stop(self._w._store)
        except Exception as exc:                 # noqa: BLE001
            _log.warning("could not ask the other run to stop: {}", exc)

    def _maybe_run_idle_bench(self) -> None:
        r"""Work order 0b §5e: "the first bench runs at the first idle
        moment and upgrades Defaults to Auto-tune quietly."

        The five rules of WORKORDER-space-report-and-idle-tune-ui-wiring §2,
        in order: never mid-run; never on confirmed battery (an unreadable
        battery must not block it); ask `should_bench` for a reason; run the
        bench off the UI thread with the same call "Benchmark now" makes;
        remember the result. The reason itself is asked on the worker too -
        `cached_profile` and `should_bench` both read the store, and this
        window never does that on its own thread (non-negotiable 5).
        """
        if self._w.indexing_view.is_running() or self._w._idle_bench_running:
            return
        if _on_battery():
            return
        settings, store = self._w._settings, self._w._store
        devices = ("cpu", "gpu") if self._w._can_use_gpu() else None

        def measure() -> Any:
            from app.core.compute_profile import cached_profile
            from app.core.measured import remember
            from app.index.autotune import should_bench
            from app.index.index_bench import run_index_bench

            profile = cached_profile(store, settings.data_path)
            reason = should_bench(store, profile)
            if not reason:
                return None
            _log.info("idle-moment bench starting: {}", reason)
            found = run_index_bench(settings, devices=devices)
            if not getattr(found, "error", ""):
                remember(store, found.as_measured(profile.fingerprint()))
            return found

        self._w._idle_bench_running = True
        worker = CallableWorker(measure, component="ui.tuning.idle")
        worker.signals.finished.connect(self._w._idle_bench_finished)
        worker.signals.failed.connect(
            lambda error: _log.debug("idle-moment bench failed quietly: {}", error))
        worker.signals.done.connect(lambda: setattr(self._w, "_idle_bench_running", False))
        run(QThreadPool.globalInstance(), worker)

    def _idle_bench_finished(self, result: Any) -> None:
        r"""§5e's own words: "upgrades Defaults to Auto-tune quietly" -
        never a dialog, never a question. **Only from Defaults**: a person
        who has since chosen Manual or Auto keeps that choice.
        """
        if result is None or getattr(result, "error", ""):
            return
        if self._w._settings.index_tuning_mode != "defaults":
            return
        self._w._settings_changed({"INDEX_TUNING_MODE": "auto"})
        self._w._settings = self._w._settings.model_copy(update={"index_tuning_mode": "auto"})
        self._w.indexing_view.tuning.load(self._w._settings)
        self._w._refresh_tuning_status()
        self._w.notify("Timed this computer while it was idle - tuned automatically "
                    "from now on. Change it any time in Index Tuning.", 10_000)

    def _run_idle_optimize(self) -> None:
        r"""§3c: refresh the query planner's statistics, off the UI thread.

        Fires once an hour for as long as the window is open (see
        `_optimize_timer` in `__init__`). Off the UI thread for the same
        reason `_poll_external_run` is: this touches the store, and "cheap"
        on the UI thread is still a stutter nobody can attribute. No signal
        connected to the result - there is nothing to show for a query-planner
        refresh succeeding, and `optimize_query_planner` already logs a
        warning on the way it can fail.
        """
        worker = CallableWorker(self._w._store.optimize_query_planner,
                                 component="ui.optimize")
        run(QThreadPool.globalInstance(), worker)

    def _scan_corpus(self) -> None:
        r"""Count the corpus so the progress bar has a real denominator.

        Answers the complaint behind the Scan button. `app.cli scan` was the
        only thing that had ever written a total, and nothing in the window
        could run one - so a GUI-started index always had `total_estimate == 0`
        and the bar was a busy indicator for its entire length. Correct by its
        own rules, and indistinguishable from broken.

        **Reads no file contents**, so it takes no run lock: it walks folders,
        adds up sizes, and opens only the tail of a sampled archive and a
        sampled PDF. Two of these at once would waste effort and nothing worse.
        """
        chosen = self._w.settings_view.current_roots()
        if not chosen:
            self._w._show(self._w.settings_view)
            self._w.notify(
                "Add at least one folder to index in Settings.", 8_000)
            return

        self._w.indexing_view.scan_button.setEnabled(False)
        self._w.notify("Counting files… the bar will show a real "
                                     "percentage once this finishes.", 0)

        # **The button, not the window, owns the re-enable.** A count of a large tree
        # takes a while, and the lambda that re-enables the button has no receiver of
        # its own: if the button's C++ half has gone by the time the scan lands, that
        # `setEnabled` raises inside a Qt slot where nothing catches it. `when_done`
        # parents the connection to the button, so it is dropped with it instead.
        # `finished`/`failed` are bound methods of the window and already safe.
        from app.ui.later import when_done

        scan_button = self._w.indexing_view.scan_button
        worker = CallableWorker(_scan_and_save, self._w._store, chosen,
                                component="ui.index.scan")
        worker.signals.finished.connect(self._w._scan_finished)
        worker.signals.failed.connect(self._w._show_error)
        when_done(scan_button, worker,
                  done=lambda: scan_button.setEnabled(True))
        run(QThreadPool.globalInstance(), worker)

    def _scan_finished(self, payload: dict) -> None:
        files = int(payload.get("files", 0) or 0)
        self._w.notify(
            f"{files:,} files to index. The progress bar can show a percentage "
            f"now.", 10_000)

    def _scan_total(self, roots: list[str]) -> int:
        """How many files `app.cli scan` counted, if it counted these folders.

        **Without it a week-long run has no percentage at all.** The bar grows
        its own denominator from what the walker has found so far, which is
        honest but reads as 97% within the first minute - the work queue is
        bounded, so `seen` is never far ahead of `done`. A scan is the only
        thing that knows the real total, and this is where it gets used.

        Zero for no scan or a scan of different folders, which `progress_for`
        already reads as "no estimate".
        """
        from app.index.scan import SCAN_STATE_KEY, saved_total

        try:
            return saved_total(self._w._store.get_state(SCAN_STATE_KEY, "") or "", roots)
        except Exception as exc:                     # noqa: BLE001 - a bar, not a run
            _log.debug("no scan total available: {}", exc)
            return 0

    def _convert_pst(self, archive: str, destination: str) -> None:
        """Export an archive to .eml, off the UI thread.

        Long-running and worth doing once: afterwards the mail is ordinary files
        that need neither Outlook nor libpff, and the folder can simply be added
        as an index root.
        """
        from app.extract import pst_libpff

        target = Path(destination) / Path(archive).stem
        self._w.notify(f"Converting {Path(archive).name}…")

        worker = CallableWorker(
            pst_libpff.export_to_eml, Path(archive), target, component="ui.convert",
        )
        worker.signals.finished.connect(
            lambda count: self._w._conversion_done(count, target)
        )
        worker.signals.failed.connect(self._w._show_error)
        run(QThreadPool.globalInstance(), worker)

    def _conversion_done(self, count: int, target: Path) -> None:
        self._w.notify(f"Wrote {count:,} messages to {target}", 15_000)
        roots = self._w.settings_view.current_roots()
        if str(target) not in roots:
            # Offer it as an index root immediately - converting and then having
            # to remember to add the folder is a step nobody should have to take.
            self._w.settings_view.add_root(str(target))

    def _start_indexing(self, *, roots: Optional[list[str]] = None,
                        recheck_archives: bool = False) -> None:
        r"""Resolve the tuning numbers off-thread, then hand off to `IndexingView`.

        **`resolve_for_run` used to run right here, inline.** On a warm compute-
        profile cache that is imperceptible - but `_profile` falls through to
        `compute_profile.detect()` on a cold or invalidated cache (a first run on
        this machine, a driver or hardware change, a cache write that failed
        last time), and `detect()` shells out to PowerShell for the disk kind and
        the display adapters with 10s and 15s timeouts. Both calls sat on the UI
        thread, at the exact moment somebody clicked Start - non-negotiable #5,
        broken by the one button people click to begin.
        """
        # Checked *before* anything is built. `IndexingView.start` already
        # refuses a second run, but it refused silently and only after this
        # method had constructed a Pipeline and an Embedder - which loads the
        # ONNX model - purely to throw them away. Six starts in seven seconds
        # appeared in the log from ordinary clicking, and each one paid that
        # cost. Saying so is also better than appearing to ignore the button.
        if self._w.indexing_view.is_running():
            self._w._show(self._w.indexing_view)
            # 2026-09-29: this used to be a five-second note and nothing else.
            # The owner pressed Start with "Index in a separate process" on
            # while a long PST run was going, and reported that the button
            # "did not work" - the note had come and gone. Now it asks, and
            # offers the one thing that would let the new Start happen.
            if self.confirm_stop_running(self._w):
                self._w.indexing_view.stop()
                self._w.notify(
                    "Stopping the current run after the file it is reading. "
                    "Press Start again once it has stopped.", 12_000)
            else:
                self._w.notify("An index run is already in progress.", 5_000)
            return

        chosen = roots or self._w.settings_view.current_roots()
        if not chosen:
            self._w._show(self._w.settings_view)
            self._w.notify(
                "Add at least one folder to index in Settings.", 8_000
            )
            return

        # A second click, or `F5`, or the scheduler firing while the first
        # resolve is still out on its worker - none of them go through the
        # disabled button (the scheduler and F5 do not touch it at all), and
        # `is_running()` above stays false until the Pipeline this resolve
        # will build actually exists. Without this flag, a burst of clicks
        # during a slow cold-cache detection would queue several resolves and
        # could hand `IndexingView.start` more than one Pipeline.
        if self._w._resolving_index:
            return
        self._w._resolving_index = True
        self._w.indexing_view.start_button.setEnabled(False)
        self._w.notify("Checking your hardware…", 30_000)
        # **The bar moves from the click, not from the first tick.** Nothing
        # exists yet to report progress, and a cold hardware check can take
        # the best part of a minute - a bar sitting still at zero over that
        # read as a Start button that had done nothing.
        self._show_preparing(True)

        # **The same resolution the tuning screen shows.** One function, so a
        # run started from the window and one started from the command line
        # cannot disagree about what `Auto (4)` means. Dispatched to a worker -
        # see the docstring above - with the result handed back to
        # `_index_resolved` by signal, on the GUI thread, exactly as if this
        # had returned in place.
        from app.index.resolve import resolve_for_run

        worker = CallableWorker(resolve_for_run, self._w._settings, self._w._store,
                                component="ui.index.resolve")
        worker.signals.finished.connect(
            lambda tuned: self._w._index_resolved(tuned, chosen, roots, recheck_archives))
        worker.signals.failed.connect(self._w._index_resolve_failed)
        run(QThreadPool.globalInstance(), worker)

    @staticmethod
    def confirm_stop_running(parent: Any) -> bool:
        """Start was pressed while a run is going. Say why nothing new began,
        and offer to stop the current run. True if the person chose to stop it.

        A static method so a test can stand in for the dialog.
        """
        box = QMessageBox(parent)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Indexing is already running")
        box.setText("An index run is already going, so Start cannot begin a second "
                    "one into the same index.")
        box.setInformativeText(
            'Settings you have changed since it began - "Index in a separate '
            'process", for one - apply from the next Start. Stop the current run '
            "now? Everything indexed so far is kept, and the next Start carries on "
            "where this one stopped.")
        stop = box.addButton("Stop the current run", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Keep it running", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is stop

    def _index_resolved(self, tuned: Any, chosen: list[str],
                        roots: Optional[list[str]], recheck_archives: bool) -> None:
        """Build the Pipeline and hand it to `IndexingView`. Back on the GUI thread.

        Everything `_start_indexing` did after calling `resolve_for_run`, moved
        here unchanged - only *when* it runs changed, not what it does.
        """
        from app.index.clip_embedder import ClipImageEmbedder
        from app.index.embedder import Embedder
        from app.extract.media import MediaConfig
        from app.index.pipeline import Pipeline, PipelineConfig
        from app.index.read_order import normalise_order
        from app.index.walker import WalkConfig

        self._w._resolving_index = False
        self._w.toast.clear()
        # A second Start click cannot get in *ahead* of this while the resolve
        # was in flight (the flag above stops it), but a run started from
        # elsewhere - the CLI, taking the run lock this window will also wait
        # on - could have begun in the meantime. IndexWorker still surfaces
        # that as a failure if it happens, but there is no reason to build a
        # second Pipeline and throw it away.
        if self._w.indexing_view.is_running():
            self._w.indexing_view.start_button.setEnabled(True)
            return

        # Work order 0x §2: the same run, in a child process, when the
        # "Index in a separate process" switch is on. Everything below this
        # line is the in-process path, unchanged.
        if bool(getattr(self._w._settings, "index_separate_process", False)):
            self._w.indexing_view.start(
                self._child_run(tuned, chosen, roots, recheck_archives),
                total_estimate=self._w._scan_total(chosen))
            repaint_totals(self._w.indexing_view)
            return

        limits = replace(limits_from_settings(self._w._settings),
                         workers=tuned.workers)

        # Work order 0h §1c's flagged gap, closed: the only real Pipeline(
        # construction site that had never been given image_embedder=/
        # image_vectors= (app.cli's cmd_index was fixed earlier this
        # session; grep -n "Pipeline(" app/ui/shell.py confirmed this is
        # the window's only one). H4: self._image_vectors is None on a
        # window built without one (an older caller, or a test stub), and
        # ClipImageEmbedder is lazy - nothing loads until the first image
        # is actually embedded, so building it unconditionally here costs
        # nothing on a run that never reaches an image file.
        image_embedder = (
            ClipImageEmbedder.from_settings(self._w._settings)
            if self._w._image_vectors is not None else None
        )
        pipeline = Pipeline(
            self._w._store, self._w._vectors,
            Embedder.from_settings(self._w._settings, threads=tuned.onnx_threads),
            PipelineConfig(
                walk=WalkConfig(
                    roots=[Path(root) for root in chosen],
                    # §2b: the master switch gates whether ANY folder's
                    # cloud content is eligible at all; the per-folder set
                    # says which ones, when it is. Off (the default) means
                    # names-only everywhere, whatever any row says.
                    cloud_content_roots=(
                        frozenset(self._w.settings_view.current_cloud_content_roots())
                        if self._w.settings_view.cloud.isChecked() else frozenset()
                    ),
                    cloud_content_cap_bytes=int(getattr(
                        self._w._settings, "cloud_content_cap_mb", 1024)) * 1024 * 1024,
                    name_only=bool(getattr(
                        self._w._settings, "index_name_only", True)),
                    # 2026-09-29: "Index this folder first", in order.
                    priority_roots=[Path(folder) for folder in self._first_folders()],
                ),
                # Memory, CPU, battery and disk ceilings, from .env. Without
                # these an index run competes with whatever the person is
                # actually doing, and gets switched off for good.
                limits=limits,
                min_free_gb=self._w._settings.min_free_gb,
                required_free_gb=int(getattr(self._w._settings, "required_free_gb", 0)),
                ocr_mode=self._w._ocr_mode_for_run(),
                junk_images=bool(getattr(
                    self._w._settings, "index_junk_image_filter", True)),
                embed_batch=tuned.embed_batch,
                dedup_chunks=bool(getattr(self._w._settings, "embed_dedup", True)),
                two_phase=bool(getattr(self._w._settings, "index_two_phase", True)),
                bulk_fts=str(getattr(self._w._settings, "index_bulk_fts", "auto")),
                # 0x §5b: "Read files in separate processes".
                read_processes=bool(getattr(
                    self._w._settings, "index_read_processes", False)),
                # 0z lane B: the time limits (`app/index/file_watch.py`).
                file_time_limit_s=int(getattr(
                    self._w._settings, "index_file_time_limit_s", 120)),
                stall_limit_s=int(getattr(
                    self._w._settings, "index_stall_limit_s", 600)),
                prune_missing=roots is None,     # a folder-scoped run must not prune the rest
                # A folder marked as an archive is walked once and then checked
                # with one `stat` - the largest single saving available on a
                # settled corpus. `recheck_archives` is the "Rescan archived
                # folders now" button, which walks them all in full this once.
                recheck_archives=recheck_archives,
                recheck_days=int(getattr(self._w._settings, "archive_recheck_days", 30)),
                caption_trickle_enabled=bool(
                    getattr(self._w._settings, "caption_trickle_enabled", False)),
                ollama_url=str(getattr(
                    self._w._settings, "ollama_url", "http://127.0.0.1:11434")),
                ollama_vision_model=str(
                    getattr(self._w._settings, "ollama_vision_model", "llava")),
                chat_engine=str(getattr(self._w._settings, "chat_engine", "onnx")),
                people_recognition_enabled=bool(getattr(
                    self._w._settings, "people_recognition_enabled", False)),
                # Work order 202626270515: video and audio, off unless the
                # two switches in Settings say otherwise.
                media=MediaConfig.from_settings(self._w._settings),
                # Work order 202626130120 (0t) section 6: resolved above, off
                # this thread, by the same resolve_for_run call that decided
                # tuned.workers - see its own docstring for why the notice
                # cannot be computed from the Pipeline's cached profile alone.
                gpu_regression_notice=tuned.gpu_regression_notice,
                # 2026-09-29: newest first, unless Tuning says "as found".
                read_order=normalise_order(
                    getattr(self._w._settings, "index_order", "")),
            ),
            image_embedder=image_embedder, image_vectors=self._w._image_vectors,
        )
        # **The window's run is a writer like any other**, so it names itself
        # on the published record and holds the same lock the CLI takes. The
        # lock itself is acquired by `IndexWorker`, on the worker thread, for
        # exactly as long as the run - taking it here would hold it across the
        # whole life of the window again, which is the bug being fixed.
        pipeline.run_owner = GUI
        # The window's own responsiveness, so the run can yield when it is late.
        # Absent in a test window that never installed one: no yielding then.
        monitor = getattr(self._w, "lag_monitor", None)
        if monitor is not None:
            pipeline.ui_lag = monitor.recent_lag_s
        self._w.indexing_view.start(pipeline, total_estimate=self._w._scan_total(chosen))
        # 0w 3a: this run is the carrying on, so "did not finish" comes down now.
        repaint_totals(self._w.indexing_view)

    def _child_run(self, tuned: Any, chosen: list[str], roots: Optional[list[str]],
                   recheck_archives: bool) -> Any:
        r"""A `ChildIndexRun` carrying what the in-process `Pipeline` would get.

        Each in-process choice above has its counterpart here, so switching
        between the two paths changes *where* the run happens and nothing
        about *what* it does:

        * the folders chosen, and `--no-prune` for a run over some of them;
        * the cloud-content folders, only when the master switch is on;
        * "Rescan archived folders now";
        * the worker count `resolve_for_run` just decided;
        * **every setting, from this window's live copy** (`settings_
          environment`), because the Tuning shelf changes that copy the moment
          a control moves and the in-process run has always read it;
        * the `.env` it came from, for anything else the child reads.

        Builds a command and an environment and touches nothing on disk, so
        it is safe here on the window's thread; the child is started by
        `IndexWorker`, on the page's own run thread.
        """
        import os

        from app.core.config import project_root
        from app.index.child_run import (
            CHILD_STDERR_NAME, ChildIndexRun, child_command, settings_environment,
        )

        settings = self._w._settings
        cloud = (self._w.settings_view.current_cloud_content_roots()
                 if self._w.settings_view.cloud.isChecked() else ())
        argv = child_command(
            chosen, env_file=getattr(settings, "env_file", None),
            prune=roots is None, recheck_archives=recheck_archives,
            workers=int(getattr(tuned, "workers", 0) or 0),
            cloud_content_keys=cloud, first=self._first_folders())
        env = dict(os.environ)
        env.update(settings_environment(settings))
        log_path = getattr(settings, "log_path", None)
        run = ChildIndexRun(
            argv, env=env, cwd=project_root(),
            stderr_path=(Path(log_path) / CHILD_STDERR_NAME) if log_path else None,
            low_priority=bool(getattr(settings, "index_low_priority", True)))
        # Order 0z A3: the page's status counts stay live during this run too.
        # An attribute, no I/O; the reads are a worker's (`StatusFunnel`).
        run.read_store = getattr(self._w, "_store", None)
        return run

    def _first_folders(self) -> list[str]:
        """2026-09-29: the folder list's "Index this folder first", in order,
        from the widget - no I/O, so safe on this thread. Empty for a window
        built without a settings page (a test stub)."""
        view = getattr(self._w, "settings_view", None)
        read = getattr(view, "current_first_folders", None)
        if read is None:
            return []
        try:
            return list(read())
        except Exception as exc:                     # noqa: BLE001 - never the run
            _log.debug("folders to index first not read: {}", exc)
            return []

    def _index_resolve_failed(self, error: Any) -> None:
        """`resolve_for_run` does not raise by contract - see its own docstring -
        so this is defence in depth, not the expected path. Restores the button
        and surfaces the error exactly as a synchronous failure would have."""
        self._w._resolving_index = False
        self._w.toast.clear()
        self._show_preparing(False)
        self._w.indexing_view.start_button.setEnabled(True)
        self._w._show_error(error)

    def _show_preparing(self, busy: bool) -> None:
        """A busy bar and a sentence while the run is being prepared, or neither.

        Here rather than in `IndexingView`, which is over its length guard; the
        view's own `start` and first progress tick take over from this.
        """
        view = self._w.indexing_view
        view.bar.setRange(0, 0 if busy else 1)
        view.bar.setValue(0)
        view.detail.setText(PREPARING_WORDS if busy else "")

    def _reset_index(self) -> None:
        """Delete everything indexed, after asking, and never the documents.

        **The confirmation says what is and is not at risk**, because "reset"
        is a word people have learned to fear from applications that mean
        something else by it. Nothing here touches a single document: the index
        is derived from them and is rebuilt by pointing the indexer at the same
        folders again. The only real cost is the time to do that.
        """
        if self._w.indexing_view.is_running():
            self._w.notify(
                "Stop the index run before resetting.", 6_000)
            return

        box = QMessageBox(self._w)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Reset the index?")
        box.setText("Delete everything that has been indexed and start over?")
        box.setInformativeText(
            "Your documents and emails are NOT touched - the index is built from "
            "them and can always be rebuilt.\n\n"
            "What it costs is the time to index again, and your saved folders, "
            "schedule and settings are kept."
        )
        box.setStandardButtons(
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Reset
        )
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Reset:
            return

        self._w.recorder.event("click", what="reset_index")
        self._w.notify("Clearing the index…")

        def clear() -> dict:
            # **Measured, because "it did nothing" was the report.** The size on
            # the Indexing page is the whole of DATA_PATH, which includes the
            # 130MB model cache and deliberately survives a reset - so on a
            # small index the number barely moves and there is nothing saying
            # why. Weighing the two things a reset actually removes, before and
            # after, turns that into a sentence.
            before = index_bytes(self._w._store, self._w._settings)
            removed = self._w._store.clear_index()
            self._w._vectors.drop()
            return {"removed": removed,
                    "freed": max(0, before - index_bytes(self._w._store, self._w._settings))}

        worker = CallableWorker(clear, component="ui.reset")
        worker.signals.finished.connect(self._w._index_cleared)
        worker.signals.failed.connect(self._w._show_error)
        run(QThreadPool.globalInstance(), worker)

    def _index_cleared(self, outcome: Any) -> None:
        self._w.notify(cleared_message(outcome), 20_000)
        self._w.indexing_view.refresh_totals(self._w._store, self._w._settings)
        self._w.files_view.refresh_summary()
        self._w._refresh_status()

    # -- Offline Media: order 202626270513 -----------------------------------
    #
    # 2a has no progress bar - a status line on the tab itself and a plain
    # `statusBar` sentence when it finishes, the same weight the order gives
    # the whole feature. Scan and Rescan both run a real `Pipeline`, so both
    # take the window's own run lock (`GUI`) exactly as `_start_indexing`
    # does - a Scan started while an ordinary index run is already using the
    # lock waits for it, on the worker thread, never on this one.

    def _offline_media_scan(self, root: str, name: str, description: str) -> None:
        r"""2b: the first Scan of a chosen folder.

        1a's offer runs first, on its own worker - `offline_media.check_
        renamed_source` is a directory listing and a store query, never the full
        walk `scan_new_source` itself pays for - so a renamed source can be
        offered *before* anything is catalogued a second time as a
        duplicate. Only when nothing matches (or the check itself fails,
        never fatal for a Scan) does this fall straight through to
        cataloguing as new, exactly as it did before this existed.
        """
        from app.index.offline_media import check_renamed_source

        self._w.offline_media_view.set_busy(f"Checking {root}\u2026")
        worker = CallableWorker(
            check_renamed_source, self._w._store, Path(root),
            component="ui.offline_media",
        )
        worker.signals.finished.connect(
            lambda suggestion: self._w._offline_media_scan_after_check(
                root, name, description, suggestion))
        worker.signals.failed.connect(
            lambda _error: self._w._offline_media_scan_confirmed(
                root, name, description, None))
        run(QThreadPool.globalInstance(), worker)

    def _offline_media_scan_after_check(self, root: str, name: str, description: str,
                                        suggestion: Any) -> None:
        """UI thread: 1a's dialog, only when the worker above found a
        structure match against a *different* catalogued source."""
        same_as = None
        if suggestion is not None:
            from app.ui.widgets.offline_media_dialogs import RenameSuggestionDialog

            dialog = RenameSuggestionDialog(suggestion["name"], self._w)
            if dialog.exec() == RenameSuggestionDialog.DialogCode.Accepted:
                same_as = suggestion["name"]
        self._w._offline_media_scan_confirmed(root, name, description, same_as)

    def _offline_media_scan_confirmed(self, root: str, name: str, description: str,
                                      same_as: Optional[str]) -> None:
        """Catalogues `root`, then runs a `Pipeline` scoped to it -
        `scan_new_source` does both, off this worker. `same_as` reattaches
        to an existing source instead (1a, accepted)."""
        from app.index.offline_media import scan_new_source

        self._w.offline_media_view.set_busy(f"Scanning {root}\u2026")
        worker = CallableWorker(
            scan_new_source, self._w._settings, self._w._store, Path(root),
            name=name, description=(description or None), run_lock_owner=GUI,
            same_as=same_as, component="ui.offline_media",
        )
        worker.signals.finished.connect(self._w._offline_media_run_done)
        worker.signals.failed.connect(self._w._offline_media_run_failed)
        run(QThreadPool.globalInstance(), worker)

    def _offline_media_rescan(self, volume_id: int) -> None:
        """2a's Rescan: 1e's move-repair pass, then a `Pipeline` for what
        actually changed."""
        from app.index.offline_media import rescan_source

        self._w.offline_media_view.set_busy("Rescanning\u2026")
        worker = CallableWorker(
            rescan_source, self._w._settings, self._w._store, volume_id,
            run_lock_owner=GUI, component="ui.offline_media",
        )
        worker.signals.finished.connect(self._w._offline_media_run_done)
        worker.signals.failed.connect(self._w._offline_media_run_failed)
        run(QThreadPool.globalInstance(), worker)

    def _offline_media_delete(self, volume_id: int) -> None:
        """2c: the product's one deliberate deletion - the full cascade,
        never the drive itself."""
        from app.index.offline_media import delete_volume

        self._w.offline_media_view.set_busy("Removing from the index\u2026")
        worker = CallableWorker(
            delete_volume, self._w._store, self._w._vectors, volume_id,
            component="ui.offline_media",
        )
        worker.signals.finished.connect(self._w._offline_media_run_done)
        worker.signals.failed.connect(self._w._offline_media_run_failed)
        run(QThreadPool.globalInstance(), worker)

    def _offline_media_run_done(self, result: Any) -> None:
        self._w.offline_media_view.set_busy("")
        self._w.offline_media_view.refresh()
        self._w.files_view.refresh_summary()
        self._w.notify(offline_media_run_summary(result), 20_000)

    def _offline_media_run_failed(self, error: Any) -> None:
        self._w.offline_media_view.set_busy("")
        self._w._show_error(error)
