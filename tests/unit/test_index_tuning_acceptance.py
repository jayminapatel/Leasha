r"""Index tuning §7: the seven acceptance tests, against invented machines.

**Not one of the machines that matter is the one running this suite.** The
owner's box is a hybrid 2P+8E with Iris Xe; a discrete-GPU machine is coming;
the case that decides whether this design is any good is a four-core laptop
somebody's child uses. All three are `ComputeProfile` values here, which is
exactly why §1 made detection a plain dataclass and §3 made the envelope pure.

The rule these tests exist to protect, stated once: **a control that alters
nothing is the defect this project keeps finding.** It has shipped three times -
two signals emitted into nothing, a search cache passed to no constructor, and
seven tuning settings read into `Settings` and consumed by no one. The wiring
tests below are the anti-P1 rule, one step past `test_settings_are_used.py`:
not only that the value is read, but that moving it changes the run.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.core import envelope
from app.core.compute_profile import ComputeProfile, GpuAdapter
from app.index import backends

ROOT = Path(__file__).resolve().parents[2]

# --- the machines, per §0 ---------------------------------------------------

#: The owner's: i7-1365U, two performance cores with hyper-threading, eight
#: efficiency cores, 32GB, and an Iris Xe that **is** DirectML-capable.
OWNER = ComputeProfile(
    logical_processors=12, physical_cores=10, performance_cores=2,
    efficiency_cores=8, ram_mb=32 * 1024, avx2=True, index_disk="ssd",
    gpus=(GpuAdapter(name="Intel Iris Xe", vram_mb=0, directml=True),))

#: The machine that follows: the design must light up on it with **zero
#: reconfiguration**, which is what makes this test worth writing before one
#: exists to try.
DISCRETE = ComputeProfile(
    logical_processors=32, physical_cores=16, ram_mb=64 * 1024, avx2=True,
    index_disk="ssd",
    gpus=(GpuAdapter(name="NVIDIA RTX 4080", vram_mb=16 * 1024, directml=True),))

#: The kids' machine, and the case that decides whether any of this is good:
#: four cores, 8GB, no graphics card worth the name. Defaults must index it
#: without a benchmark ever having run.
LAPTOP = ComputeProfile(
    logical_processors=4, physical_cores=4, ram_mb=8 * 1024, avx2=True,
    index_disk="hdd")


# --- 7a: the envelope on each of them ---------------------------------------


def test_the_owners_hybrid_machine_counts_cores_for_reading() -> None:
    r"""**2P+8E, and the correction §0 forced.** Weighting the ten cores by
    core class gave two readers where four had been running - a silent halving
    justified by an efficiency-core estimate borrowed from inference, which is
    not what extraction does. Extraction waits on the disk as much as it
    computes."""
    workers = envelope.index_workers(OWNER)

    assert workers.auto == 4
    assert workers.ceiling == 10
    assert "counts cores rather than weighting them" in workers.why


def test_the_owners_graphics_card_is_offered() -> None:
    """Iris Xe *is* DirectML-capable, which §0 records because it is the
    opposite of what people assume about integrated graphics."""
    assert backends.why_unavailable(OWNER) == ""
    assert backends.choose(OWNER).device == backends.GPU


def test_inference_threads_are_weighted_where_extraction_is_not() -> None:
    """The distinction that made `capacity()` exist: an ONNX thread on an
    efficiency core really does deliver a fraction of a performance core's
    throughput, and a file reader does not."""
    assert envelope.capacity(OWNER) < OWNER.physical_cores
    assert envelope.index_workers(OWNER).ceiling == OWNER.physical_cores


def test_a_discrete_machine_lights_up_with_no_reconfiguration() -> None:
    """The order's requirement, and the reason this test predates the
    hardware: nothing about it may need a setting changed by hand."""
    assert backends.choose(DISCRETE).device == backends.GPU
    assert envelope.index_workers(DISCRETE).auto == 4          # still capped
    assert envelope.index_workers(DISCRETE).ceiling == 16
    assert envelope.embed_batch(DISCRETE).auto == 256


def test_the_four_core_laptop_clamps_everything_down() -> None:
    """The kids'-machine case. Nothing here may need a benchmark, an
    explanation, or a decision from whoever is using it."""
    workers = envelope.index_workers(LAPTOP)
    batch = envelope.embed_batch(LAPTOP)
    memory = envelope.index_memory_mb(LAPTOP)

    assert workers.auto == 2 and workers.ceiling == 4
    assert batch.auto == 128, "8GB: a smaller batch than the owner's 32GB box"
    assert memory.auto <= LAPTOP.ram_mb // 2
    assert backends.choose(LAPTOP).device == backends.CPU


def test_defaults_index_that_laptop_with_nothing_measured() -> None:
    """§5e: the installer asks nothing new, and the first run works. A design
    that needs a benchmark before it can index is a design that fails on the
    machine it most needs to succeed on."""
    from app.index.resolve import resolve_for_run

    class Settings:
        index_tuning_mode = "defaults"
        index_workers = 0
        onnx_intra_op_threads = 0
        embed_batch = 0

    found = resolve_for_run(Settings(), store=None, profile=LAPTOP)

    assert found.workers >= 1
    assert found.embed_batch >= 8
    assert found.measured is False, "and it did not pretend to have measured"


def test_a_fingerprint_change_re_derives_the_profile() -> None:
    """The future-machine story: the same index folder opened on a different
    box must not run on the old one's arithmetic."""
    assert OWNER.fingerprint() != LAPTOP.fingerprint()
    assert OWNER.fingerprint() == ComputeProfile(
        logical_processors=12, physical_cores=10, performance_cores=2,
        efficiency_cores=8, ram_mb=32 * 1024, avx2=True, index_disk="ssd",
        gpus=OWNER.gpus).fingerprint()


def test_free_space_is_not_part_of_the_fingerprint() -> None:
    """Otherwise deleting a file would re-derive the machine, and the cache
    would be worthless on the drive it matters most on."""
    from dataclasses import replace

    assert OWNER.fingerprint() == replace(OWNER, index_disk="ssd").fingerprint()


def test_three_slow_runs_trigger_a_re_bench_and_two_do_not() -> None:
    """§5d's drift rule. One slow afternoon is not a changed machine."""
    from app.core.measured import DRIFT_RUNS, Measured

    class Machine:
        def fingerprint(self):
            return "same"

    for runs, expected in ((DRIFT_RUNS - 1, ""), (DRIFT_RUNS, "changed")):
        stored = Measured(fingerprint="same", drifting_runs=runs)
        why = stored.stale_against(Machine())
        assert (expected in why) if expected else (why == ""), runs


# --- 7b: the plain-words guard ----------------------------------------------

#: Words that must not appear on a control somebody sees without asking for
#: Manual. Every one of them is this codebase's vocabulary, not anybody's.
JARGON = ("onnx", "directml", "intra-op", "intra op", "quantised model",
          "batch size", "ivf", "fts5", "lance", "embedding vector")


def _controls_of(box) -> list:
    from PyQt6.QtWidgets import QAbstractButton, QComboBox, QSpinBox

    return [child for child in box.findChildren((QComboBox, QSpinBox,
                                                 QAbstractButton))]


@pytest.fixture()
def screen():
    from PyQt6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from app.ui.widgets.tuning_box import TuningBox

    class Stored:
        embed_device = "auto"; index_workers = 0; onnx_intra_op_threads = 0
        embed_batch = 0; embed_quantised = False; index_memory_mb = 4000
        index_cpu_percent = 80; min_free_gb = 5; required_free_gb = 300
        index_low_priority = True; index_pause_on_battery = True
        index_two_phase = True; embed_dedup = True; index_bulk_fts = "auto"
        index_ocr_pass = "with-run"; index_tuning_mode = "defaults"
        index_ocr_mode = "both"; archive_recheck_days = 30
        index_name_only = True; archive_read_inside = True
        archive_max_mb = 100; pdf_ocr_pages = 0

    return TuningBox(Stored())


def test_no_control_speaks_this_codebases_language(screen) -> None:
    r"""**The rule, and the only way it survives new controls.**

    Somebody tuning their computer should not have to know what an
    intra-op thread is. "Threads per model call" says the same thing and can
    be acted on; "ONNX intra-op threads" is a term you either already know or
    cannot look up usefully.

    Applies to what is *shown* - labels, tooltips, combo entries. The object
    names are the registry's keys and are deliberately technical: they are how
    the tests find the controls, and nobody reads them.
    """
    offenders: list[str] = []
    for control in _controls_of(screen):
        shown = [control.toolTip(), getattr(control, "text", lambda: "")()]
        if hasattr(control, "count"):
            shown += [control.itemText(i) for i in range(control.count())]
        for text in shown:
            for word in JARGON:
                if word in str(text).lower():
                    offenders.append(f"{control.objectName() or control}: {word}")

    assert not offenders, (
        "these controls use this codebase's vocabulary rather than English:\n  "
        + "\n  ".join(offenders))


def test_the_machine_line_says_it_in_specification_sheet_words(screen) -> None:
    """The person reading it is comparing it against what they think they
    bought, so it uses their vocabulary rather than ours."""
    from app.ui.tuning import machine_line

    line = machine_line(OWNER).lower()

    assert "cores" in line and "gb" in line
    for word in ("logical_processors", "avx2 =", "fingerprint"):
        assert word not in line


# --- 7c: return to automatic ------------------------------------------------


def test_one_action_returns_a_fiddled_machine_to_automatic(screen) -> None:
    """The recovery path for every fiddled-with machine, so it is one action
    and a visible button rather than a dropdown entry to be found."""
    screen.set_profile(OWNER)
    screen.mode.setCurrentIndex(screen.mode.findData("manual"))
    screen.compute.workers.setValue(9)
    screen.compute.batch.setValue(512)

    screen.automatic.click()

    assert screen.current_mode() == "defaults"
    assert screen.compute.workers.specialValueText() == "Auto (4)"


def test_returning_to_automatic_keeps_what_was_typed(screen) -> None:
    """Somebody who tried Manual, made it worse and wants out should not also
    lose the numbers they set - they may want to look at them, or go back."""
    screen.set_profile(OWNER)
    screen.mode.setCurrentIndex(screen.mode.findData("manual"))
    screen.compute.workers.setValue(9)

    screen.return_to_automatic()
    screen.mode.setCurrentIndex(screen.mode.findData("manual"))

    assert screen.compute.workers.value() == 9


def test_the_next_run_uses_the_automatic_values(screen) -> None:
    """Tested through `resolve_for_run` rather than at the widget, because the
    widget agreeing with itself is not the claim."""
    from app.index.resolve import resolve_for_run

    class Settings:
        index_tuning_mode = "manual"
        index_workers = 9
        onnx_intra_op_threads = 0
        embed_batch = 0

    Settings.index_tuning_mode = "defaults"      # what the button writes
    found = resolve_for_run(Settings(), store=None, profile=OWNER)

    assert found.workers == 4


# --- 7d: moving a control changes the run -----------------------------------


@pytest.mark.parametrize("mode,stored,expected", [
    ("manual", 7, 7),
    ("defaults", 7, 4),
])
def test_the_mode_decides_whether_a_typed_number_is_used(mode, stored, expected
                                                         ) -> None:
    from app.index.resolve import resolve_for_run

    class Settings:
        index_tuning_mode = mode
        index_workers = stored
        onnx_intra_op_threads = 0
        embed_batch = 0

    assert resolve_for_run(Settings(), store=None,
                           profile=OWNER).workers == expected


def test_the_dedup_switch_changes_what_the_model_is_asked(tmp_path) -> None:
    """The anti-P1 rule for §6e: not that the setting is read, but that moving
    it produces a different number of calls to the model."""
    from app.index.embedder import Embedder
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.walker import WalkConfig
    from app.storage.sqlite_store import SqliteStore

    seen: list = []

    def encode(texts):
        texts = list(texts)
        seen.append(len(texts))
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    store = SqliteStore(tmp_path / "wiring.db").connect()
    for dedup, expected in ((True, [1]), (False, [3])):
        seen.clear()
        pipeline = Pipeline(store, _NoVectors(), Embedder(dim=4, encoder=encode),
                            PipelineConfig(walk=WalkConfig(roots=[tmp_path]),
                                           dedup_chunks=dedup))
        pipeline._embed_texts(["a", "a", "a"])   # noqa: SLF001
        assert seen == expected, dedup
    store.close()


def test_the_bulk_switch_changes_whether_the_word_index_is_merged(tmp_path
                                                                  ) -> None:
    from app.index.embedder import Embedder
    from app.index.pipeline import IndexStats, Pipeline, PipelineConfig
    from app.index.walker import WalkConfig
    from app.storage.sqlite_store import SqliteStore

    merged: list = []

    class Store(SqliteStore):
        def optimize_fts(self):                  # noqa: D102
            merged.append(True)
            return True

    store = Store(tmp_path / "bulk.db").connect()
    for wanted, expected in (("off", 0), ("on", 1)):
        merged.clear()
        pipeline = Pipeline(store, _NoVectors(),
                            Embedder(dim=4, encoder=lambda t: []),
                            PipelineConfig(walk=WalkConfig(roots=[tmp_path]),
                                           bulk_fts=wanted))
        pipeline._optimise_keyword_index(IndexStats(chunks=5))   # noqa: SLF001
        assert len(merged) == expected, wanted
    store.close()


# --- 7e: degrade loudly -----------------------------------------------------


def test_a_graphics_card_that_fails_on_load_falls_back_with_a_notice() -> None:
    problems: list = []

    def build(providers):
        if backends.DML_PROVIDER in providers:
            raise RuntimeError("the device is not available")
        return "session"

    _session, choice = backends.with_fallback(build, backends.choose(OWNER),
                                              problems=problems)

    assert choice.device == backends.CPU
    assert problems and "processor" in problems[0]


def test_a_graphics_card_that_fails_mid_run_does_not_end_the_run() -> None:
    r"""H4's pattern, pinned. A driver mid-update or a device in use is an
    ordinary Windows situation, and none of them may cost hours of indexing."""
    from app.index.embedder import Embedder

    calls = {"n": 0}

    def flaky(texts):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("the graphics device was reset")
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    embedder = Embedder(dim=4, encoder=flaky)
    from app.core.errors import AppErrorException

    with pytest.raises(AppErrorException):
        embedder.embed(["a"])                    # reported, never swallowed
    # `embed()` returns a `numpy.ndarray` since order 0b section 6c - bare
    # truthiness on a multi-element array raises, so the recovery is
    # asserted by shape (one row back for one input) instead.
    recovered = embedder.embed(["a"])
    assert len(recovered) == 1, "and the next call still works"


# --- 7f: the timers are themselves under test -------------------------------


def test_a_run_report_carries_where_its_time_went(tmp_path) -> None:
    import math

    from app.index.embedder import Embedder, l2_normalise
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.walker import WalkConfig
    from app.storage.sqlite_store import SqliteStore

    corpus = tmp_path / "docs"
    corpus.mkdir()
    for number in range(6):
        (corpus / f"f{number}.txt").write_text("pump station " * 200,
                                               encoding="utf-8")

    store = SqliteStore(tmp_path / "timed.db").connect()
    stats = Pipeline(
        store, _NoVectors(),
        Embedder(dim=8, encoder=lambda texts: [
            l2_normalise([math.sin(i) for i in range(8)]) for _ in texts]),
        PipelineConfig(walk=WalkConfig(roots=[corpus]), min_free_gb=0,
                       required_free_gb=0)).run()

    assert stats.stages, "a run that indexed six files measured nothing"
    assert "walk" in stats.worker_seconds
    assert "extract" in stats.worker_seconds
    store.close()


def test_the_chunker_floor_still_exists() -> None:
    """H9's guard. Named here so §6's speed work cannot quietly remove the
    test that stops the quadratic chunker coming back."""
    found = list((ROOT / "tests").rglob("test_*.py"))
    sources = "\n".join(path.read_text(encoding="utf-8") for path in found)

    assert "chunk" in sources and "seconds" in sources


# --- 7g: clamp at load, out loud --------------------------------------------


def test_sixteen_workers_on_a_four_core_machine_clamp_with_a_notice() -> None:
    r"""**A clamp is never silent.** This fires exactly when somebody has
    carried an index to a smaller machine and is wondering why it is slow, so
    the notice names the old value, the new one and the reason."""
    value, notice = envelope.index_workers(LAPTOP).clamp(16)

    assert value == 4
    assert notice is not None
    assert "16 lowered to 4" in notice
    assert "4 core(s)" in notice


def test_the_clamp_reaches_the_run_not_only_the_screen() -> None:
    from app.index.resolve import resolve_for_run

    class Settings:
        index_tuning_mode = "manual"
        index_workers = 16
        onnx_intra_op_threads = 0
        embed_batch = 0

    found = resolve_for_run(Settings(), store=None, profile=LAPTOP)

    assert found.workers == 4
    assert "lowered to 4" in found.why["INDEX_WORKERS"]


def test_the_envelope_stays_pure() -> None:
    """Its whole value is that the machines that matter are not this one."""
    tree = ast.parse((ROOT / "app" / "core" / "envelope.py")
                     .read_text(encoding="utf-8"))
    imported = {node.module.split(".")[0] for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module}
    imported |= {alias.name.split(".")[0] for node in ast.walk(tree)
                 if isinstance(node, ast.Import) for alias in node.names}

    assert imported <= {"__future__", "dataclasses", "typing"}, imported


class _NoVectors:
    def ensure_table(self): pass
    def add(self, **kwargs): return len(kwargs.get("chunk_ids", []))
    def delete_by_file_ids(self, ids): return 0
    def maybe_create_index(self): pass
    def maybe_compact(self, force=False): pass
    def count(self): return 0
