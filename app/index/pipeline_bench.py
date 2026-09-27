r"""Index a synthetic corpus with the real pipeline, and report what it cost.

Layer: L3 (tooling - reached through `app.cli bench-pipeline`)

**Work order 0x item 5a, and the measuring tool 2d needs.** 5a asks for "a
repeatable benchmark ... figures recorded per stage"; 2d asks for the window's
longest stall and p99 while indexing, files per minute, and memory, measured
before and after the indexer moves into its own process. This module is both
halves:

* `run_pipeline_bench` indexes a corpus from `synthetic_corpus.py` into a
  **throwaway** data folder, through the same `Pipeline` class the window and
  `app.cli index` use, and returns a report.
* With `probe=True` it does that inside a Qt event loop with a heartbeat
  timer on the main thread and the pipeline on a worker thread - which is the
  shape of the window today - and reports how late the heartbeat ran. That is
  the "before" number for moving the indexer out of process.

**How this differs from `index_bench.py` (`app.cli bench-index`).** That one is
a one-minute calibration: it calls the extractor, the store and the model
*separately* on 240 small text files, and stores the rates for auto-tune. It
never runs the `Pipeline` itself, so it cannot see queueing, the consumer
thread, the resume bookkeeping, archives or mail. This one runs the whole
thing end to end and stores nothing anywhere except its own temporary folder.

**A number without its conditions is not a result** (non-negotiable 9, and the
lesson of four throughput claims in this project that were wrong). So every
report carries `conditions` - which corpus, which embedder (real or fake),
which machine, which settings - and `label`, one line that says all of it and
is printed above every table.

**The fake embedder is labelled, loudly.** The real model is 130MB and may not
be downloaded on the machine running this; without it, nothing else could be
measured. So the embedder can be swapped for one that returns a made-up but
correctly-shaped vector per passage. Its model name is
`FAKE_MODEL_NAME`, it is written into the report as `"embedder": "FAKE"`,
and the `embed` stage then measures only the plumbing around the model, not the
model. Numbers from a fake run must never be quoted as indexing speed.

**Cross-platform.** `pathlib` only, no Windows-only calls, and nothing here
touches the real index: the data folder is made under the system's temporary
folder (or one the caller names) and deleted afterwards unless `keep=True`.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
import tempfile
import threading
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.index.synthetic_corpus import (
    CORPUS_DIR,
    SIZES,
    CorpusManifest,
    CorpusSpec,
    generate,
    load_manifest,
)

__all__ = [
    "FAKE_MODEL_NAME",
    "REPORT_VERSION",
    "BenchOptions",
    "format_report",
    "machine_description",
    "run_pipeline_bench",
]

#: Bumped when the report's shape changes, so two JSON files can be told apart.
REPORT_VERSION = 1

#: What the stand-in embedder calls itself. Chosen so nobody can read it as a
#: real model name, in a log line, a report or a database row.
FAKE_MODEL_NAME = "FAKE-bench-embedder-not-a-model"

#: How often peak memory is sampled while the run goes. Fine enough to catch a
#: burst while one big archive is read; the sampling itself costs nothing
#: measurable (one system call per sample).
RSS_SAMPLE_S = 0.05

#: How long the heartbeat runs with nothing else happening, before indexing
#: starts. This is the baseline: a Qt event loop on an idle machine is never
#: exactly on time, and the load number means little without the idle one.
IDLE_BASELINE_S = 2.0


@dataclass
class BenchOptions:
    """Everything one benchmark run needs to know. Defaults are the usual case."""

    #: Where the corpus lives (or will be generated): `folder/corpus/` plus
    #: `folder/corpus-manifest.json`. Reused if it already holds a corpus made
    #: from the same spec; generated if it is empty or absent.
    corpus_folder: Path
    #: A size name from `synthetic_corpus.SIZES`, used when `spec` is None.
    size: str = "small"
    #: Explicit counts. Overrides `size` when given.
    spec: CorpusSpec | None = None
    #: "fake", "real", or "auto" (real if the model is already downloaded,
    #: fake otherwise - never a download).
    embedder: str = "auto"
    #: Run inside a Qt event loop with a heartbeat, and report its lateness.
    probe: bool = False
    #: With `probe`, let the pipeline slow itself when the heartbeat runs late,
    #: exactly as the window wires it (`pipeline.ui_lag`). Off measures the
    #: pipeline with no courtesy to the window at all.
    probe_yield: bool = True
    #: Extraction workers. None means whatever the app would choose here.
    workers: int | None = None
    #: The same switch as `app.cli index --full-speed`: no CPU ceiling, no
    #: pausing on battery, normal priority.
    full_speed: bool = False
    #: Work order 0x §2d, the "after" number: run the index as a child process
    #: (`app.cli index --events jsonl`, supervised by `app.index.child_run`,
    #: exactly as the window does with "Index in a separate process" on), and
    #: keep only the heartbeat - and the stand-in window - in this process.
    child_process: bool = False
    #: The person's own `.env`, read only to find the downloaded model and (with
    #: `my_settings`) their tuning. Never written to.
    env_file: Path | None = None
    #: Copy the person's tuning settings (workers, batch sizes, ...) into the
    #: throwaway configuration. Off by default so two machines are compared on
    #: the app's defaults rather than on two different `.env` files.
    my_settings: bool = False
    #: Where to make the throwaway data folder. None means a new temporary one.
    work_dir: Path | None = None
    #: Leave the throwaway data folder behind, for looking at afterwards.
    keep: bool = False
    #: Called with a short line of text as the run goes, e.g. to print
    #: progress. None means silence.
    on_note: Callable[[str], None] | None = None


# ---------------------------------------------------------------------------
# The machine
# ---------------------------------------------------------------------------

def machine_description() -> dict[str, Any]:
    """What this computer is: OS, processor count, memory, Python. Never raises.

    Every field is something that changes the numbers. `platform.processor()`
    is empty on some Linux systems; it is reported as it comes rather than
    guessed.
    """
    info: dict[str, Any] = {
        "os": platform.system(),
        "os_release": platform.release(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count_logical": os.cpu_count(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
    }
    try:
        import psutil

        info["cpu_count_physical"] = psutil.cpu_count(logical=False)
        info["memory_total_gb"] = round(psutil.virtual_memory().total / 1e9, 1)
    except Exception:                            # noqa: BLE001 - optional detail
        pass
    return info


def _app_build() -> dict[str, Any]:
    """The app version and, where git can say, the commit. Never raises."""
    try:
        from app.core.version import build_info

        return dict(build_info())
    except Exception as exc:                     # noqa: BLE001 - optional detail
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

class _PeakMemory:
    """Samples this process's resident memory on a background thread.

    Resident set size (RSS) is the memory the process actually holds in RAM,
    which is what makes a machine slow when it runs out. The peak is kept, not
    the average, because the question is "will this fit".

    Uses `psutil`, which works on Windows, macOS and Linux alike. Without it
    the numbers are simply absent from the report, never made up.
    """

    def __init__(self, pid: Callable[[], int | None] | None = None) -> None:
        """Prepare, but do not start, the sampler.

        `pid`, when given, is asked on every sample for the process to measure
        instead of this one - the indexer's child process in a
        `child_process` run, whose id is only known once it has started.
        Until it answers, nothing is sampled.
        """
        self.start_mb: float | None = None
        self.peak_mb: float | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pid = pid
        self._followed: Any = None
        try:
            import psutil

            self._process = psutil.Process()
        except Exception:                        # noqa: BLE001 - optional
            self._process = None

    def _target(self) -> Any:
        """The process to read: this one, or the followed child once known."""
        if self._pid is None or self._process is None:
            return self._process
        pid = self._pid()
        if pid is None:
            return None
        if self._followed is None or self._followed.pid != pid:
            try:
                import psutil

                self._followed = psutil.Process(pid)
            except Exception:                    # noqa: BLE001 - it may have ended
                return None
        return self._followed

    def _read_mb(self) -> float | None:
        """Current RSS in megabytes, or None if it cannot be read."""
        target = self._target()
        if target is None:
            return None
        try:
            return target.memory_info().rss / 1_048_576
        except Exception:                        # noqa: BLE001
            return None

    def _loop(self) -> None:
        """The sampling loop: read, keep the largest, sleep, repeat."""
        while not self._stop.wait(RSS_SAMPLE_S):
            value = self._read_mb()
            if value is not None and self.start_mb is None:
                self.start_mb = value
            if value is not None and (self.peak_mb is None or value > self.peak_mb):
                self.peak_mb = value

    def start(self) -> None:
        """Record the starting memory and begin sampling."""
        self.start_mb = self._read_mb()
        self.peak_mb = self.start_mb
        if self._process is not None:
            # (A followed child that has not started yet reads as None here;
            # its first sample in `_loop` then becomes its starting figure.)
            self._thread = threading.Thread(target=self._loop, name="bench-rss",
                                            daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop sampling, taking one last reading so a final spike is not missed."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        value = self._read_mb()
        if value is not None and (self.peak_mb is None or value > self.peak_mb):
            self.peak_mb = value


# ---------------------------------------------------------------------------
# The embedder
# ---------------------------------------------------------------------------

def _fake_encoder(dim: int) -> Callable[[Any], Any]:
    """A stand-in for the model: one unit-length vector per passage, instantly.

    The vector is drawn from a random stream seeded by a checksum of the text,
    so the same passage always gets the same vector (the pipeline's
    duplicate-passage saving still behaves as it would with a real model) and
    the vectors are spread out like real ones (so the vector store's own work
    is realistic). It is still meaningless - see the module docstring.
    """
    import numpy as np

    def encode(texts: Any) -> Any:
        """Turn a batch of passages into a `(len, dim)` float32 array."""
        texts = list(texts)
        out = np.empty((len(texts), dim), dtype=np.float32)
        for row, text in enumerate(texts):
            rng = np.random.default_rng(zlib.crc32(text.encode("utf-8", "replace")))
            vector = rng.standard_normal(dim).astype(np.float32)
            out[row] = vector / (np.linalg.norm(vector) or 1.0)
        return out

    return encode


def _model_is_cached(model_cache: Path | None, model_name: str) -> tuple[bool, str]:
    """Whether the real model's file is already on disk, and why not if not.

    Reuses `embed_bench.inspect_model`, the check `embed-bench` already makes,
    so the two tools cannot disagree about what "downloaded" means.
    """
    if model_cache is None:
        return False, "no settings file was found, so there is no model folder to look in"
    from app.index.embed_bench import BenchResult, inspect_model

    result = inspect_model(Path(model_cache), BenchResult(model_name=model_name))
    if result.model_file:
        return True, result.model_file
    return False, result.error or "the model file was not found"


# ---------------------------------------------------------------------------
# Settings for the throwaway run
# ---------------------------------------------------------------------------

#: `.env` keys that say *where* things are. Never copied from the person's
#: file: the whole point is that this run writes somewhere else.
_LOCATION_KEYS = frozenset({
    "DATA_PATH", "VECTOR_PATH", "FTS_DB", "CACHE_PATH", "MODEL_CACHE",
    "STATE_PATH", "PROJECT_PATH", "LOG_PATH",
})


def _person_settings(env_file: Path | None) -> tuple[Any, dict[str, str], str]:
    """The person's settings and raw `.env` values, read only. Never raises.

    Returns `(settings or None, raw key->value dict, where it came from)`.
    Used for two things only: finding the downloaded model, and - when asked -
    copying their tuning. `create_dirs=False` and `check_writable=False` so
    that reading it cannot create or touch anything.
    """
    try:
        from app.core.config import _read_env_file, find_env_file, load_settings

        path = find_env_file(env_file)
        settings = load_settings(path, create_dirs=False, check_writable=False)
        return settings, dict(_read_env_file(path)), str(path)
    except Exception as exc:                     # noqa: BLE001 - optional input
        return None, {}, f"none ({type(exc).__name__})"


def _write_throwaway_env(work: Path, *, model_cache: Path, model_name: str,
                         dim: int, extra: dict[str, str]) -> Path:
    """Write a `.env` whose every location is inside `work`, and return its path.

    The model folder is the one exception: pointing it at the person's
    existing download is what lets a real-embedder run happen without a
    130MB fetch. Nothing is written into it - the embedder only reads a model
    that is already there, and this module never asks for one that is not.

    The two free-space floors are zero because the resource governor would
    otherwise refuse to start on a nearly-full sandbox disk, which is a real
    check but not what is being measured.
    """
    data = work / "data"
    lines = [
        f"DATA_PATH={data.as_posix()}",
        f"VECTOR_PATH={(data / 'vectors').as_posix()}",
        f"FTS_DB={(data / 'fts' / 'knowledge.db').as_posix()}",
        f"CACHE_PATH={(data / 'cache').as_posix()}",
        f"MODEL_CACHE={Path(model_cache).as_posix()}",
        f"STATE_PATH={(data / 'state').as_posix()}",
        f"LOG_PATH={(work / 'logs').as_posix()}",
        f"EMBED_MODEL={model_name}",
        f"EMBED_DIM={dim}",
        "MIN_FREE_GB=0",
        "REQUIRED_FREE_GB=0",
    ]
    fixed = {line.split("=", 1)[0] for line in lines}
    for key, value in sorted(extra.items()):
        if key not in fixed and key not in _LOCATION_KEYS:
            lines.append(f"{key}={value}")
    path = work / "bench.env"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _pipeline_config(settings: Any, store: Any, corpus_root: Path,
                     options: BenchOptions) -> tuple[Any, Any]:
    """The `PipelineConfig` `app.cli index` would build for this corpus.

    **The same function `app.cli index` calls** (`build_pipeline_config`),
    since work order 0x §2: this used to be a copy of `cmd_index`'s inline
    construction, kept in step by a comment, and a benchmark that measures a
    configuration no real run uses is measuring the wrong thing. The command's
    flag defaults apply (hash checks on, pruning on, archives honoured); only
    `--workers` and `--full-speed` are passed through.

    Returns `(config, resolved tuning)`.
    """
    from app.cli.index import build_pipeline_config
    from app.index.resolve import resolve_for_run

    tuned = resolve_for_run(settings, store)
    config = build_pipeline_config(settings, [corpus_root], tuned=tuned,
                                   workers=options.workers,
                                   full_speed=options.full_speed)
    return config, tuned


# ---------------------------------------------------------------------------
# The corpus
# ---------------------------------------------------------------------------

def _corpus(options: BenchOptions) -> tuple[CorpusManifest, str]:
    """Reuse the corpus in `options.corpus_folder` if it matches, else make it.

    Returns `(manifest, "reused" | "generated")`. A folder holding a corpus
    made from *different* counts is refused rather than overwritten or
    silently used: the report would otherwise describe one corpus and measure
    another.
    """
    spec = options.spec or SIZES[options.size]
    size_name = "custom" if options.spec else options.size
    folder = Path(options.corpus_folder)
    existing = load_manifest(folder)
    if existing is not None and (folder / CORPUS_DIR).is_dir():
        if existing.spec == spec.as_dict():
            return existing, "reused"
        raise ValueError(
            f"{folder} already holds a corpus made from different counts "
            f"({existing.size_name}, seed {existing.spec.get('seed')}). Name an "
            "empty folder, or the same size and seed it was made with.")
    return generate(folder, spec, size_name=size_name), "generated"


# ---------------------------------------------------------------------------
# The responsiveness probe
# ---------------------------------------------------------------------------

def _ensure_qt_platform() -> None:
    """Pick Qt's off-screen platform when there is no screen to draw on.

    On Windows and macOS there is always a display, and the real platform is
    used - that is closer to what a person sees. A Linux machine with no
    `DISPLAY` or `WAYLAND_DISPLAY` (a server, a sandbox, CI) would make Qt
    refuse to start, so it gets `offscreen`, and the report says so.
    """
    if sys.platform.startswith("linux") and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _run_with_probe(run: Callable[[Callable[[Any], None]], Any],
                    pipeline: Any, options: BenchOptions) -> tuple[Any, dict[str, Any]]:
    """Run `run` on a worker thread while a Qt heartbeat runs on this one.

    This is the shape of the window today: the event loop on the main thread,
    the pipeline on a worker thread in the same process, sharing one Python
    interpreter lock. The heartbeat is `app.ui.lag_monitor` itself -
    `install()` is what `app/main.py` calls, with the same 50 ms beat, the same
    stall watcher and the same switch-interval tightening - so the numbers
    are the lag monitor's own numbers, not a second implementation of them.

    A small stand-in window (one label and a progress bar) is updated from
    each progress callback through a queued Qt signal, the way the Indexing
    page is, so the main thread has some painting to do. It is **not** the
    real Indexing page; the report says so.

    Two phases: `IDLE_BASELINE_S` with nothing running, then the run itself.
    Returns `(whatever run returned, probe report)`.
    """
    _ensure_qt_platform()
    from PyQt6.QtCore import QEventLoop, QObject, QTimer, pyqtSignal
    from PyQt6.QtWidgets import QApplication, QLabel, QProgressBar, QVBoxLayout, QWidget

    from app.ui import lag_monitor

    application = QApplication.instance() or QApplication([])
    before_switch = sys.getswitchinterval()
    switch = lag_monitor.tighten_switch_interval()

    class _Relay(QObject):
        """Carries progress from the worker thread to the main thread.

        A signal emitted on one thread and received by an object living on
        another is queued: the receiving slot runs later, on the receiver's
        thread. That is how the real window gets progress without touching a
        widget from a worker thread (which Qt forbids).
        """

        progress = pyqtSignal(int, int)

    window = QWidget()
    window.setWindowTitle("Leasha bench - stand-in window")
    label = QLabel("idle")
    bar = QProgressBar()
    bar.setRange(0, 0)
    layout = QVBoxLayout(window)
    layout.addWidget(label)
    layout.addWidget(bar)
    window.show()
    relay = _Relay()

    def paint(indexed: int, chunks: int) -> None:
        """Main thread: show the latest counts, as the Indexing page would."""
        label.setText(f"{indexed:,} documents, {chunks:,} chunks")

    relay.progress.connect(paint)

    def pump(seconds: float, until: Callable[[], bool] | None = None) -> None:
        """Run the event loop for `seconds`, or until `until()` turns true."""
        loop = QEventLoop()
        if until is None:
            QTimer.singleShot(int(seconds * 1000), loop.quit)
        else:
            poll = QTimer()
            poll.setInterval(100)
            poll.timeout.connect(lambda: until() and loop.quit())
            poll.start()
        loop.exec()

    # Phase 1: the idle baseline. A fresh monitor, so it holds idle beats only.
    idle = lag_monitor.install(application)
    pump(IDLE_BASELINE_S)
    idle.stop()
    idle_summary = idle.summary()
    _stop_beat_timers(application, idle)

    # Phase 2: the run, with a second fresh monitor.
    monitor = lag_monitor.install(application)
    if options.probe_yield:
        # Exactly the wiring in `ui/controllers/index_controller.py`: the
        # pipeline reads the window's recent lateness and yields when it is
        # high. Off, the pipeline shows the window no courtesy at all.
        pipeline.ui_lag = monitor.recent_lag_s
    outcome: dict[str, Any] = {}

    def work() -> None:
        """Worker thread: the whole index run."""
        try:
            outcome["value"] = run(
                lambda stats: relay.progress.emit(int(stats.indexed), int(stats.chunks)))
        except BaseException as exc:             # noqa: BLE001 - re-raised below
            outcome["error"] = exc

    worker = threading.Thread(target=work, name="bench-index-run", daemon=True)
    worker.start()
    pump(0, until=lambda: not worker.is_alive())
    worker.join()
    monitor.stop()
    load_summary = monitor.summary()
    load_summary["p90_ms"] = round(monitor.percentile(0.90) * 1000)
    idle_summary["p90_ms"] = round(idle.percentile(0.90) * 1000)
    _stop_beat_timers(application, monitor)
    window.close()
    sys.setswitchinterval(before_switch)

    if "error" in outcome:
        raise outcome["error"]
    report = {
        "method": ("app.ui.lag_monitor.install() on the main thread (50 ms beat, "
                   "stall watcher on), the Pipeline on one worker thread in the "
                   "same process, a stand-in window (label + busy bar) repainted "
                   "from progress - NOT the real Indexing page"),
        "qt_platform": application.platformName(),
        "beat_ms": lag_monitor.BEAT_MS,
        "stall_threshold_ms": round(lag_monitor.STALL_S * 1000),
        "switch_interval_ms": round(switch * 1000, 3),
        "pipeline_yields_to_window": bool(options.probe_yield),
        "idle": idle_summary,
        "indexing": load_summary,
        "lateness_means": ("how much later than its 50 ms interval each beat "
                           "arrived; what a person feels as a delay"),
    }
    return outcome.get("value"), report


def _stop_beat_timers(application: Any, monitor: Any) -> None:
    """Stop the heartbeat timer `lag_monitor.install` parented to the app.

    `install` keeps no handle to its timer (it lives as long as the
    application, which is right for the window). The bench installs two in
    turn, so the first is found among the application's children - the
    `QTimer`s whose `timeout` is connected to this monitor's `beat` - and
    stopped, or its beats would keep landing in a monitor nobody reads.
    """
    from PyQt6.QtCore import QTimer

    for timer in application.findChildren(QTimer):
        if timer.interval() == int(monitor._beat_s * 1000):  # noqa: SLF001
            try:
                timer.timeout.disconnect(monitor.beat)
            except (TypeError, RuntimeError):
                continue                          # not this monitor's timer
            timer.stop()
            timer.deleteLater()


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def run_pipeline_bench(options: BenchOptions) -> dict[str, Any]:
    """Make or reuse the corpus, index it into a throwaway folder, and report.

    Returns the report as a plain dictionary (see `format_report` for the
    table). Raises only for a bad request - an unknown size, a real embedder
    asked for when none is downloaded, a corpus folder holding a different
    corpus - because those are the caller's to fix. The throwaway folder is
    removed afterwards whatever happens, unless `keep` was asked for.
    """
    note = options.on_note or (lambda _text: None)
    if options.spec is None and options.size not in SIZES:
        raise ValueError(f"unknown size {options.size!r}; choose from "
                         f"{', '.join(SIZES)}")
    if options.embedder not in ("fake", "real", "auto"):
        raise ValueError("embedder must be fake, real or auto")

    started_corpus = time.perf_counter()
    manifest, corpus_how = _corpus(options)
    corpus_seconds = time.perf_counter() - started_corpus
    note(f"corpus {corpus_how}: {manifest.files:,} files, "
         f"{manifest.bytes / 1_048_576:,.1f} MB, "
         f"{manifest.expected_documents:,} documents ({corpus_seconds:.1f}s)")

    person, raw_env, env_source = _person_settings(options.env_file)
    model_name = str(getattr(person, "embed_model", "") or "BAAI/bge-small-en-v1.5")
    dim = int(getattr(person, "embed_dim", 384) or 384)
    model_cache = getattr(person, "model_cache", None)
    cached, cache_detail = _model_is_cached(model_cache, model_name)
    if options.embedder == "real" and not cached:
        raise ValueError(
            f"the real embedder was asked for, but {model_name} is not downloaded "
            f"({cache_detail}). Run a search or an index once so it downloads, "
            "or use --embedder fake.")
    use_real = options.embedder == "real" or (options.embedder == "auto" and cached)

    work = Path(options.work_dir) if options.work_dir else Path(
        tempfile.mkdtemp(prefix="leasha-pipeline-bench-"))
    work.mkdir(parents=True, exist_ok=True)
    try:
        return _index_and_report(
            options, manifest, corpus_how, work, use_real=use_real,
            model_name=model_name, dim=dim,
            model_cache=(Path(model_cache) if use_real else work / "models"),
            cache_detail=cache_detail, raw_env=raw_env, env_source=env_source,
            note=note)
    finally:
        if not options.keep:
            # `ignore_errors`: on Windows the vector store's files can stay
            # locked for a moment after closing, and failing to tidy a temp
            # folder must never turn a finished measurement into an error.
            shutil.rmtree(work, ignore_errors=True)


def _index_and_report(options: BenchOptions, manifest: CorpusManifest,
                      corpus_how: str, work: Path, *, use_real: bool,
                      model_name: str, dim: int, model_cache: Path,
                      cache_detail: str, raw_env: dict[str, str],
                      env_source: str, note: Callable[[str], None]) -> dict[str, Any]:
    """The part of `run_pipeline_bench` that runs inside the throwaway folder."""
    from app.core.config import load_settings
    from app.core.logging import setup_logging
    from app.index.embedder import Embedder
    from app.index.pipeline import Pipeline
    from app.storage.sqlite_store import SqliteStore
    from app.storage.vector_store import VectorStore

    extra = {k: v for k, v in raw_env.items() if k not in _LOCATION_KEYS} \
        if options.my_settings else {}
    env_path = _write_throwaway_env(work, model_cache=model_cache,
                                    model_name=model_name, dim=dim, extra=extra)
    settings = load_settings(env_path)
    # Log to the throwaway folder, and keep the console quiet: the lag
    # monitor's stall warnings would otherwise land in the middle of the table.
    setup_logging(settings.log_path, console_level="CRITICAL", force=True)

    corpus_root = Path(options.corpus_folder) / CORPUS_DIR
    if options.child_process:
        return _index_in_child_and_report(
            options, manifest, corpus_how, work, settings, env_path, corpus_root,
            use_real=use_real, model_name=model_name, cache_detail=cache_detail,
            env_source=env_source, note=note)
    memory = _PeakMemory()
    probe_report: dict[str, Any] | None = None
    load_seconds = 0.0

    with SqliteStore(settings.fts_db) as store, \
            VectorStore(settings.vector_path, dim=settings.embed_dim) as vectors:
        config, tuned = _pipeline_config(settings, store, corpus_root, options)
        if use_real:
            embedder = Embedder.from_settings(settings, threads=tuned.onnx_threads)
            # **The model load is timed apart from the run.** It happens once
            # per run and would otherwise smear into the first files' rate -
            # the mistake behind an earlier "59 passages a minute" figure.
            note("loading the model (not counted in the run time)...")
            started_load = time.perf_counter()
            embedder.warm_up()
            load_seconds = time.perf_counter() - started_load
        else:
            embedder = Embedder(FAKE_MODEL_NAME, dim=settings.embed_dim,
                                encoder=_fake_encoder(settings.embed_dim))
        pipeline = Pipeline(store, vectors, embedder, config)

        clock: dict[str, float] = {}

        def run(on_progress: Callable[[Any], None] | None) -> Any:
            """Index the corpus once, timing the whole call with a wall clock."""
            memory.start()
            clock["start"] = time.perf_counter()
            try:
                return pipeline.run(on_progress=on_progress)
            finally:
                clock["end"] = time.perf_counter()
                memory.stop()

        note("indexing" + (" under the responsiveness probe" if options.probe
                           else "") + "...")
        if options.probe:
            stats, probe_report = _run_with_probe(run, pipeline, options)
        else:
            stats = run(None)
        wall = clock["end"] - clock["start"]

        choice = getattr(embedder, "choice", None)
        ran_on = str(getattr(choice, "device", "") or ("n/a (fake)" if not use_real
                                                       else "unknown"))

    report = _build_report(
        options, manifest, corpus_how, stats, wall, memory, probe_report,
        use_real=use_real, model_name=model_name, ran_on=ran_on,
        load_seconds=load_seconds, cache_detail=cache_detail,
        env_source=env_source, config=config, tuned=tuned, settings=settings,
        work=work)
    return report


def _index_in_child_and_report(options: BenchOptions, manifest: CorpusManifest,
                               corpus_how: str, work: Path, settings: Any,
                               env_path: Path, corpus_root: Path, *, use_real: bool,
                               model_name: str, cache_detail: str, env_source: str,
                               note: Callable[[str], None]) -> dict[str, Any]:
    r"""Work order 0x §2d's "after": the same corpus, indexed by a child process.

    **Exactly the window's arrangement with "Index in a separate process" on.**
    `app.index.child_run.ChildIndexRun` starts `app.cli index --events jsonl`
    with the same interpreter, reads its events on a worker thread, and hands
    each rebuilt `IndexStats` to the stand-in window - while the heartbeat
    runs on this process's main thread, which now shares its interpreter lock
    with nothing but that reading thread. Same corpus, same throwaway `.env`,
    and the same `PipelineConfig` (the child builds it with
    `build_pipeline_config`, as `_pipeline_config` does here for the report).

    Three differences from the in-process run, all said in the report:

    * **Memory is the child's** (the indexer's), sampled by its process id.
    * **A real model loads inside the child**, so its load time is part of
      the wall time rather than timed apart (with the fake embedder there is
      nothing to load).
    * **The child takes the run lock** like any `app.cli index`. Its lock file
      is kept in the throwaway folder on macOS and Linux; on Windows the lock
      is a machine-wide name, so a real Leasha index running at the same time
      would refuse this run - which the report would show as an error.
    """
    import os

    from app.index.child_run import CHILD_STDERR_NAME, ChildIndexRun, child_command
    from app.storage.sqlite_store import SqliteStore

    # The configuration the child will build, for the report's conditions.
    # Closed again before the child starts, so the two never share a handle.
    with SqliteStore(settings.fts_db) as store:
        config, tuned = _pipeline_config(settings, store, corpus_root, options)

    extra = []
    if not use_real:
        extra.append("--fake-embedder-for-bench")
    if options.full_speed:
        extra.append("--full-speed")
    argv = child_command([corpus_root], env_file=env_path,
                         workers=int(options.workers or 0), extra=extra)
    env = dict(os.environ, TMPDIR=str(work))
    child = ChildIndexRun(argv, env=env, cwd=Path(__file__).resolve().parents[2],
                          stderr_path=Path(settings.log_path) / CHILD_STDERR_NAME,
                          low_priority=bool(config.limits.low_priority))
    memory = _PeakMemory(pid=lambda: child.pid)
    probe_report: dict[str, Any] | None = None
    clock: dict[str, float] = {}

    def run(on_progress: Callable[[Any], None] | None) -> Any:
        """Index the corpus once in the child, timing the whole call."""
        memory.start()
        clock["start"] = time.perf_counter()
        try:
            return child.run(on_progress=on_progress)
        finally:
            clock["end"] = time.perf_counter()
            memory.stop()

    note("indexing in a child process" + (" under the responsiveness probe"
                                           if options.probe else "") + "...")
    if options.probe:
        stats, probe_report = _run_with_probe(run, child, options)
    else:
        stats = run(None)
    wall = clock["end"] - clock["start"]
    ran_on = "n/a (fake)" if not use_real else str(
        (getattr(stats, "resolved", {}) or {}).get("device", "unknown"))

    report = _build_report(
        options, manifest, corpus_how, stats, wall, memory, probe_report,
        use_real=use_real, model_name=model_name, ran_on=ran_on,
        load_seconds=0.0, cache_detail=cache_detail,
        env_source=env_source, config=config, tuned=tuned, settings=settings,
        work=work)
    report["conditions"]["pipeline"]["entry"] = (
        "app.cli index --events jsonl in a CHILD process, supervised by "
        "app.index.child_run.ChildIndexRun (config from build_pipeline_config)")
    report["conditions"]["pipeline"]["memory_of"] = "the child (indexer) process"
    report["conditions"]["pipeline"]["image_lane"] = (
        "built by app.cli index (lazy; the corpus has no pictures)")
    if use_real:
        report["conditions"]["embedder"]["note"] = (
            "the model loads inside the child, so its load time is in wall_s")
    if report.get("responsiveness"):
        # Nothing to yield to: the child has its own interpreter lock, and the
        # heartbeat's lateness never reaches it. Said so the table is not read
        # as "the pipeline was being polite".
        report["responsiveness"]["pipeline_yields_to_window"] = False
        report["responsiveness"]["method"] = (
            "app.ui.lag_monitor.install() on the main thread (50 ms beat, stall "
            "watcher on); the Pipeline in a CHILD process, its events read on one "
            "worker thread here; a stand-in window (label + busy bar) repainted "
            "from progress - NOT the real Indexing page")
    return report


def _per_minute(count: float, seconds: float) -> float:
    """`count` per minute over `seconds`, or 0.0 for a zero-length run."""
    return round(count / seconds * 60, 1) if seconds > 0 else 0.0


def _build_report(options: BenchOptions, manifest: CorpusManifest, corpus_how: str,
                  stats: Any, wall: float, memory: _PeakMemory,
                  probe_report: dict[str, Any] | None, *, use_real: bool,
                  model_name: str, ran_on: str, load_seconds: float,
                  cache_detail: str, env_source: str, config: Any, tuned: Any,
                  settings: Any, work: Path) -> dict[str, Any]:
    """Assemble the report dictionary: conditions first, then results."""
    machine = machine_description()
    embedder_label = (f"REAL {model_name} on {ran_on}" if use_real
                      else "FAKE (no model - the embed stage is plumbing only)")
    label = (
        f"{manifest.size_name} corpus, seed {manifest.spec.get('seed')} "
        f"({manifest.files:,} files, {manifest.bytes / 1_048_576:,.1f} MB, "
        f"{manifest.expected_documents:,} documents) · embedder {embedder_label} · "
        f"{machine['os']} {machine['os_release']} {machine['machine']} · "
        f"{machine['cpu_count_logical']} logical CPUs · Python {machine['python']} · "
        f"{config.limits.workers or 'auto'} workers"
        + (" · full speed" if options.full_speed else "")
        + (" · your .env tuning" if options.my_settings else " · app defaults")
        + (" · CHILD PROCESS" if options.child_process else " · in process")
    )
    stages = dict(getattr(stats, "stages", {}) or {})
    documents = int(stats.indexed)
    results = {
        "wall_s": round(wall, 2),
        "files_seen": int(stats.seen),
        "documents": documents,
        "expected_documents": manifest.expected_documents,
        "chunks": int(stats.chunks),
        "vectors": int(stats.vectors),
        "files_per_min": _per_minute(stats.seen, wall),
        "documents_per_min": _per_minute(documents, wall),
        "chunks_per_min": _per_minute(stats.chunks, wall),
        "mb_per_min": round(stats.bytes_read / 1_048_576 / wall * 60, 2) if wall else 0.0,
        "bytes_read": int(stats.bytes_read),
        "skipped": int(stats.skipped),
        "skipped_by_code": dict(stats.skipped_by_code),
        "chunks_deduped": int(stats.chunks_deduped),
        "paused_s": round(stats.paused_seconds, 2),
        "pauses": int(stats.pauses),
        "stopped_early": stats.stopped_early.code if stats.stopped_early else None,
        # The pipeline's own clock (`app/index/stages.py`): seconds the
        # consumer thread spent in each stage. `waiting` means waiting for the
        # readers - extraction was the bottleneck for that long.
        "stages_s": {name: round(seconds, 3) for name, seconds in stages.items()},
        # Summed across all reader threads, so it can exceed the wall time.
        "worker_seconds": {name: round(seconds, 3) for name, seconds in
                           (getattr(stats, "worker_seconds", {}) or {}).items()},
        "model_load_s": round(load_seconds, 2),
        "memory_start_mb": _round(memory.start_mb),
        "memory_peak_mb": _round(memory.peak_mb),
        "memory_growth_mb": (_round(memory.peak_mb - memory.start_mb)
                             if memory.peak_mb is not None and memory.start_mb is not None
                             else None),
    }
    conditions = {
        "corpus": {**manifest.as_dict(), "folder": str(Path(options.corpus_folder)),
                   "how": corpus_how},
        "embedder": {
            "kind": "REAL" if use_real else "FAKE",
            "model": model_name if use_real else FAKE_MODEL_NAME,
            "ran_on": ran_on,
            "model_file": cache_detail if use_real else None,
            "note": (None if use_real else
                     "vectors are made-up; the embed stage measures the "
                     "pipeline's plumbing, not the model. Do not quote as "
                     "indexing speed."),
        },
        "machine": machine,
        "app": _app_build(),
        "settings": {
            "source": "your .env tuning" if options.my_settings else "app defaults",
            "read_model_location_from": env_source,
        },
        "pipeline": {
            "entry": "app.index.pipeline.Pipeline.run (in process, config mirrors app.cli index)",
            "workers": config.limits.workers,
            "cpu_percent": config.limits.cpu_percent,
            "low_priority": config.limits.low_priority,
            "embed_batch": config.embed_batch,
            "onnx_threads": tuned.onnx_threads,
            "dedup_chunks": config.dedup_chunks,
            "two_phase": config.two_phase,
            "bulk_fts": config.bulk_fts,
            "ocr_mode": config.ocr_mode,
            "full_speed": options.full_speed,
            "image_lane": "not built (the corpus has no pictures)",
        },
        "measured": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "throwaway_data_folder": str(work) if options.keep else "(deleted)",
    }
    report = {
        "report_version": REPORT_VERSION,
        "tool": "app.cli bench-pipeline",
        "label": label,
        "conditions": conditions,
        "results": results,
    }
    if probe_report is not None:
        report["responsiveness"] = probe_report
    return report


def _round(value: float | None) -> float | None:
    """Round a megabyte figure to one decimal, keeping None as None."""
    return None if value is None else round(value, 1)


# ---------------------------------------------------------------------------
# The table
# ---------------------------------------------------------------------------

def format_report(report: dict[str, Any]) -> str:
    """The report as a readable table, with its conditions printed first.

    The label goes on top, above every number, so that a screenshot or a
    pasted excerpt carries its conditions with it.
    """
    r = report["results"]
    c = report["conditions"]
    lines = [
        "Indexing benchmark (app.cli bench-pipeline)",
        "=" * 72,
        f"Measured under: {report['label']}",
        "",
    ]
    if c["embedder"]["kind"] == "FAKE":
        lines += ["  ! FAKE EMBEDDER: the 'embed' figures below are not the model.",
                  "    Quote the other stages; do not quote this as indexing speed.", ""]
    rows = [
        ("wall time", f"{r['wall_s']:,.2f} s"),
        ("files seen", f"{r['files_seen']:,}"),
        ("documents", f"{r['documents']:,}  (corpus holds {r['expected_documents']:,})"),
        ("chunks", f"{r['chunks']:,}  ({r['vectors']:,} vectors)"),
        ("files / min", f"{r['files_per_min']:,.1f}"),
        ("documents / min", f"{r['documents_per_min']:,.1f}"),
        ("chunks / min", f"{r['chunks_per_min']:,.1f}"),
        ("MB / min", f"{r['mb_per_min']:,.2f}"),
        ("peak memory", "unknown (psutil missing)" if r["memory_peak_mb"] is None
         else f"{r['memory_peak_mb']:,.1f} MB  (started at {r['memory_start_mb']:,.1f}, "
              f"+{r['memory_growth_mb']:,.1f})"),
    ]
    if r["model_load_s"]:
        rows.append(("model load", f"{r['model_load_s']:,.2f} s  (not in wall time)"))
    if r["pauses"]:
        rows.append(("paused", f"{r['paused_s']:,.1f} s over {r['pauses']} pause(s) - "
                               "the resource governor; try --full-speed"))
    if r["skipped"]:
        rows.append(("skipped", f"{r['skipped']:,}  {r['skipped_by_code']}"))
    if r["stopped_early"]:
        rows.append(("STOPPED EARLY", str(r["stopped_early"])))
    lines += [f"  {name:<18} {value}" for name, value in rows]

    if r["stages_s"]:
        lines += ["", "  Consumer-thread time per stage (app/index/stages.py):"]
        total = sum(r["stages_s"].values()) or 1.0
        for name, seconds in r["stages_s"].items():
            lines.append(f"    {name:<16} {seconds:>9.2f} s  {seconds / total:>5.0%}")
    if r["worker_seconds"]:
        lines += ["  Reader threads, summed (can exceed wall time):"]
        for name, seconds in r["worker_seconds"].items():
            lines.append(f"    {name:<16} {seconds:>9.2f} s")

    probe = report.get("responsiveness")
    if probe:
        lines += ["", f"  Window heartbeat lateness (beat every {probe['beat_ms']} ms, "
                      f"Qt '{probe['qt_platform']}', pipeline yields: "
                      f"{'yes' if probe['pipeline_yields_to_window'] else 'no'}):",
                  f"    {'':<10} {'beats':>7} {'p50':>7} {'p90':>7} {'p99':>7} "
                  f"{'max':>7} {'stalls':>7}"]
        for phase in ("idle", "indexing"):
            s = probe[phase]
            lines.append(f"    {phase:<10} {s['beats']:>7,} {s['p50_ms']:>5} ms "
                         f"{s['p90_ms']:>4} ms {s['p99_ms']:>4} ms {s['worst_ms']:>4} ms "
                         f"{s['stalls']:>7}")
        lines.append(f"    (a stall is a beat {probe['stall_threshold_ms']} ms or more late; "
                     "stand-in window, not the real Indexing page)")
    return "\n".join(lines)
