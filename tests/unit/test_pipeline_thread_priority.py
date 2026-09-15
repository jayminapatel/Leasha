"""Regression: every pipeline thread lowers its own priority, never the process's.

Layer: L3

**2026-09 trap.** `Pipeline.run()` used to call `governor.apply_priority()`
once, from whichever thread called `run()`, and that call reached a
*process-wide* Windows API (`psutil.Process().nice(BELOW_NORMAL_PRIORITY_
CLASS)`, i.e. `SetPriorityClass`). Non-negotiable #1 runs the window and the
indexer in one process, so this silently dropped the GUI thread - the one
painting the Start button and answering a click - to below-normal priority
for the life of every index run. Invisible on an idle machine; on a real one
under real competing load, measured directly, it produced Qt event-loop gaps
up to 7.3 seconds the instant an index run started. See
`app/index/resources.py::lower_current_thread_priority` for the fix (Windows'
`THREAD_MODE_BACKGROUND_BEGIN`, which is per-thread by construction) and
`tests/unit/test_resources.py` for the unit-level guards on that seam.

This file is the other half: the fix only works if *every* thread the
pipeline itself starts - the walker, every extraction worker, the feeder -
asks for it on its own, because a new OS thread does not inherit another
thread's priority. A regression that reverted `Pipeline._background` back to
a single call from `run()` would pass every existing acceptance test (the
run still completes, indexes everything, reports the same stats) and would
only be visible again as a frozen window under load - exactly how it was
missed the first time. This test makes that failure mode visible without
needing real contention: a fake governor records which thread name reached
`apply_priority()`, and the assertion is that every kind of pipeline thread
appears, not just the one that called `run()`.
"""

from __future__ import annotations

import math
import threading
from pathlib import Path

import pytest

from app.index.embedder import Embedder, l2_normalise
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.resources import ResourceGovernor, ResourceLimits, Snapshot
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore
from app.storage.vector_store import VectorStore

# A real store and a real run, same trade-off `test_layer3_acceptance.py`
# makes for the same reason: this needs actual threads actually starting.
pytestmark = pytest.mark.slow

DIM = 384

#: A machine with nothing else going on, so the governor's own pause logic
#: never enters into it - this file is about *which thread* calls
#: `apply_priority()`, not about the pause/resume behaviour `test_resources.py`
#: already covers.
_QUIET = Snapshot(rss_mb=50.0, system_cpu_percent=1.0, own_cpu_percent=1.0,
                   free_disk_gb=999.0, on_battery=False)


def fake_encoder(texts):
    """Deterministic vectors, no ONNX - see `test_layer3_acceptance.py`."""
    out = []
    for text in texts:
        seed = float(abs(hash(text)) % 1000)
        out.append(l2_normalise([math.sin(seed + i) for i in range(DIM)]))
    return out


class _RecordingGovernor(ResourceGovernor):
    """The real governor, plus a log of which OS thread asked to be lowered."""

    def __init__(self) -> None:
        super().__init__(ResourceLimits(low_priority=True), probe=lambda: _QUIET)
        self.threads: list[str] = []

    def apply_priority(self) -> bool:
        self.threads.append(threading.current_thread().name)
        return True


def test_every_pipeline_thread_lowers_its_own_priority(tmp_path: Path) -> None:
    root = tmp_path / "corpus"
    root.mkdir()
    for i in range(8):
        (root / f"doc{i}.txt").write_text(
            f"Document {i}. The northern pump station was commissioned in March.",
            encoding="utf-8",
        )

    store = SqliteStore(tmp_path / "index.db").connect()
    vectors = VectorStore(tmp_path / "vectors", dim=DIM).connect()
    try:
        config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=2, checkpoint_every=2)
        governor = _RecordingGovernor()
        pipeline = Pipeline(store, vectors, Embedder(dim=DIM, encoder=fake_encoder),
                            config, governor=governor)

        calling_thread = threading.current_thread().name
        stats = pipeline.run()

        assert stats.indexed == 8

        # The thread `run()` itself executes on - the GUI's dedicated worker
        # thread in the app, this test's own thread here - lowers its own
        # priority once, at the top of `run()`.
        assert calling_thread in governor.threads

        # And every thread the pipeline *started for itself* did the same,
        # each on its own thread, via `_background` - never inherited from
        # whichever thread happened to create them.
        names = set(governor.threads)
        assert "walker" in names
        assert any(name.startswith("extract-") for name in names), names
        assert "feeder" in names

        # The regression this guards against: a single process-wide call
        # would still make the first assertion pass (the calling thread is
        # in the list) while never touching the other three - so the count,
        # not just membership, is what catches it reverting.
        assert len(names) >= 4, (
            f"only {names!r} called apply_priority() - a pipeline thread is "
            "not lowering its own priority; see the module docstring"
        )
    finally:
        store.close()
        vectors.close()


def test_a_thread_called_directly_by_a_test_is_never_touched() -> None:
    """`_produce`/`_extract_worker`/`_feed_worker` are called directly by
    other tests, on the caller's own thread, without going through `run()` -
    see `test_layer3_acceptance.py`'s comment on `_produce` for one such
    case. `_background` must never leak onto that path: a source check, in
    the spirit of `tests/unit/test_ui_never_blocks.py`, is what catches a
    future edit that "helpfully" adds a priority call inside one of these
    methods directly - which would make the OS-level, thread-lifetime side
    effect (`THREAD_MODE_BACKGROUND_BEGIN` lasts until the thread exits)
    land on whichever thread a test happens to call it from.
    """
    import inspect

    from app.index.pipeline import Pipeline

    for name in ("_produce", "_extract_worker", "_feed_worker"):
        body = inspect.getsource(getattr(Pipeline, name))
        assert "apply_priority" not in body, (
            f"Pipeline.{name} calls apply_priority() directly - move it "
            f"behind _background() at the threading.Thread(...) call site "
            f"instead, or a direct test call would lower its own thread's "
            f"priority for the rest of the process's life"
        )
