r"""Indexing that works: the measured causes of "55 days", each pinned.

**Where these numbers come from.** 2026-09-19, the owner's machine (2 P-cores +
8 E-cores, 32GB), 90 real documents, 1,274 chunks, identical settings but for
the one named:

    1 thread,  batch 256   1,055 s      1.2 chunks/s   (what the real run used)
    4 threads, various     222-587 s    2.2-5.7 chunks/s   (this machine varies ~2.5x)
    model call 32 vs 256, interleaved, 4 threads: about +10%, and a third of the memory

and a stored `embed_per_second` of 106,666,662, from timing a generator nobody
iterated, which the tuning arithmetic read as "fast enough to double the batch".
Every test below fails on the code as it was.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import envelope
from app.core.measured import (
    MAX_PLAUSIBLE_EMBED_PER_SECOND,
    Measured,
    plausible_embed_rates,
)

REPO = Path(__file__).resolve().parents[2]

#: The owner's machine, as `compute:profile` records it.
OWNER = SimpleNamespace(
    logical_processors=12, physical_cores=10,
    performance_cores=2, efficiency_cores=8, ram_mb=32452,
)


# --- a rate that cannot be true is not a rate -------------------------------

def test_the_stored_rate_from_the_owners_machine_is_refused():
    assert plausible_embed_rates({"cpu": 106_666_662.89377557}) == {}


@pytest.mark.parametrize("bad", [0, -3.0, float("nan"), float("inf"), "fast", None])
def test_nonsense_rates_are_dropped_and_real_ones_kept(bad):
    kept = plausible_embed_rates({"cpu": bad, "gpu": 5.7})
    assert kept == {"gpu": 5.7}


def test_the_ceiling_is_a_ceiling_not_a_target():
    assert plausible_embed_rates(
        {"cpu": MAX_PLAUSIBLE_EMBED_PER_SECOND}) == {"cpu": MAX_PLAUSIBLE_EMBED_PER_SECOND}
    assert plausible_embed_rates(
        {"cpu": MAX_PLAUSIBLE_EMBED_PER_SECOND + 1}) == {}


def test_a_stored_record_with_the_bad_rate_heals_when_it_is_read():
    """The bad value is already in a database. Fixing the writer is not enough."""
    stored = Measured.from_dict({
        "fingerprint": "74a2d323f439429d", "pipeline_version": 1, "source": "run",
        "embed_per_second": {"cpu": 106_666_662.89377557},
        "extract_per_second": 429.98,
    })
    assert stored is not None
    assert stored.embed_per_second == {}
    assert stored.extract_per_second == pytest.approx(429.98)   # the rest survives


def test_a_bogus_rate_no_longer_doubles_the_batch_to_512():
    """The chain that produced `batch 512, 4.4GB` on a machine that measured 3/s."""
    bogus = Measured.from_dict({"embed_per_second": {"cpu": 106_666_662.0}})
    assert envelope.embed_batch_from_rates(OWNER, bogus) is None

    # And what the caller then falls back to is the RAM heuristic, not 512.
    assert envelope.embed_batch(OWNER).auto == 256


def test_a_believable_slow_rate_still_leaves_the_heuristic_alone():
    honest = Measured.from_dict({"embed_per_second": {"cpu": 5.7}})
    found = envelope.embed_batch_from_rates(OWNER, honest)
    assert found is not None and found.auto == 256


# --- the benchmark must time the work, not the generator --------------------

def test_the_index_bench_consumes_the_embedding_it_times(monkeypatch):
    from app.index import embedder as embedder_module
    from app.index.index_bench import IndexBench, _time_embedding

    consumed = []

    class Fake:
        choice = SimpleNamespace(device="cpu", fell_back_from="", why="")

        def warm_up(self):
            pass

        def embed_all(self, texts):
            for text in texts:                 # a generator, like the real one
                consumed.append(text)
                # Half a millisecond each, as a model would take. Instant,
                # forty passages finish in ~8 microseconds - five million a
                # second, which a fast machine reads as the 'never iterated'
                # signature this test's ceiling exists to catch.
                time.sleep(0.0005)
                yield text

    monkeypatch.setattr(embedder_module.Embedder, "from_settings",
                        classmethod(lambda cls, settings, **kw: Fake()))

    result = IndexBench()
    sample = [f"passage {i}" for i in range(40)]
    _time_embedding(SimpleNamespace(embed_device="cpu"), sample, None, 40, result)

    assert len(consumed) == 40, "the benchmark timed a generator it never iterated"
    assert result.embed_per_second["cpu"] < MAX_PLAUSIBLE_EMBED_PER_SECOND * 1_000
    assert "cpu" in result.embed_per_second


# --- the model gets the cores the workers are not using ---------------------

def test_the_owners_machine_gives_the_model_four_threads_not_one():
    found = envelope.onnx_threads(OWNER, workers=4)
    assert found.auto == 4, (
        f"{found.auto} thread(s): the four workers were charged a whole core "
        "each, though they were busy 6% of the time")


def test_a_small_machine_is_still_not_oversubscribed():
    tiny = SimpleNamespace(logical_processors=2, physical_cores=2,
                           performance_cores=0, efficiency_cores=0, ram_mb=8000)
    assert envelope.onnx_threads(tiny, workers=1).auto == 1


def test_more_workers_still_mean_fewer_threads():
    few = envelope.onnx_threads(OWNER, workers=2).auto
    many = envelope.onnx_threads(OWNER, workers=16).auto
    assert few >= many >= 1


# --- the model's own batch size ---------------------------------------------

def _embedder(choice):
    from app.index.embedder import Embedder

    embedder = Embedder(encoder=lambda texts: [])
    embedder.choice = choice
    return embedder


def test_on_a_processor_the_model_call_is_capped_at_32():
    from app.index.embedder import CPU_INFER_BATCH

    options = _embedder(SimpleNamespace(is_gpu=False))._call_options()
    assert options == {"batch_size": CPU_INFER_BATCH} and CPU_INFER_BATCH == 32


def test_the_graphics_card_keeps_the_models_default():
    assert _embedder(SimpleNamespace(is_gpu=True))._call_options() == {}


def test_before_the_model_loads_it_is_treated_as_a_processor():
    assert _embedder(None)._call_options() == {"batch_size": 32}


# --- every .ppt was skipped --------------------------------------------------

def _rules():
    from app.core.formats import load_rules

    return load_rules(packaged=REPO / "config" / "extractors.toml")


def test_ppt_is_converted_to_pptx_not_to_text():
    rule = _rules().converter_for(".ppt")
    assert rule is not None
    assert "txt:Text" not in rule.command, (
        "`txt:Text` is a Writer filter; Impress has none, so LibreOffice exited 1 "
        "for all 79 of the owner's presentations")
    assert "pptx" in rule.command
    assert rule.produces == "{stem}.pptx"
    assert rule.then == "pptx"


def test_the_extractor_a_converted_ppt_is_handed_to_exists():
    import app.extract  # noqa: F401 - registers the readers
    from app.extract.base import extractor_by_name

    assert extractor_by_name(_rules().converter_for(".ppt").then) is not None


def test_doc_keeps_its_text_route():
    """`.doc` worked (69 of 94 indexed); it must not be swept up in the fix."""
    rule = _rules().converter_for(".doc")
    assert "txt:Text" in rule.command and rule.then == "plaintext"


# --- a closed window must not leave a process behind ------------------------

def test_the_watchdog_dumps_then_exits_and_the_exit_is_injectable():
    from app.ui import exit_watchdog

    dumped, exited = threading.Event(), threading.Event()
    codes: list[int] = []

    timers = exit_watchdog.arm(
        dump_after_s=0.05, exit_after_s=0.15,
        dump=dumped.set, exit=lambda code: (codes.append(code), exited.set()))

    assert dumped.wait(2), "no stack dump was taken"
    assert exited.wait(2), "the surviving process was not ended"
    assert codes == [0]
    assert all(timer.daemon for timer in timers), (
        "a timer that is not a daemon would itself keep a healthy process alive")


def test_a_process_that_exits_normally_never_sees_the_watchdog():
    from app.ui import exit_watchdog

    fired = threading.Event()
    timers = exit_watchdog.arm(dump_after_s=30, exit_after_s=60,
                               dump=fired.set, exit=lambda _c: fired.set())
    for timer in timers:
        timer.cancel()
    assert not fired.wait(0.2)


def test_the_stack_dump_names_the_threads_it_found():
    from app.ui import exit_watchdog

    started, release = threading.Event(), threading.Event()

    def parked():
        started.set()
        release.wait(5)

    worker = threading.Thread(target=parked, name="index-worker-under-test",
                              daemon=True)
    worker.start()
    started.wait(2)
    try:
        text = exit_watchdog.format_stacks()
    finally:
        release.set()
    assert "index-worker-under-test" in text
    assert "parked" in text, "the stack should name what the thread is waiting in"


def test_only_main_arms_the_watchdog_so_the_test_suite_cannot_be_ended_by_it():
    source = (REPO / "app" / "main.py").read_text(encoding="utf-8")
    shell = (REPO / "app" / "ui" / "shell.py").read_text(encoding="utf-8")
    assert "window.after_close = exit_watchdog.arm" in source
    assert "import exit_watchdog" not in shell and "exit_watchdog.arm" not in shell, (
        "shell.py must reach the watchdog only through the `after_close` hook")
    assert 'getattr(self, "after_close", None)' in shell
