r"""Index tuning §6a: where a run's time went, and why it is measured this way.

**This is the item that gates the rest of §6.** Every speed idea in that list
sounds plausible and at most two of them are worth building on any given
corpus: a feeder thread buys nothing on a run that waits for the disk, more
workers buy nothing on one that is embedding-bound. Without these numbers the
whole section is guesswork with a changelog entry.

The measurement decision worth defending is **what gets timed**. Extraction
runs on N threads, so adding up their seconds gives a "percentage" over 100.
What the consumer *waits* for is the honest wall-clock measure, and it is also
the useful one: it is exactly the thing that would go faster if more readers
were added. Worker-seconds are kept, separately named, because "extraction cost
40 worker-minutes" is a real fact - just not a fraction of anything.
"""

from __future__ import annotations

import math
import tempfile
import threading
from pathlib import Path

import pytest

from app.index.pipeline import Pipeline, PipelineConfig
from app.index.stages import WAITING, StageClock, advice
from app.index.walker import WalkConfig
from app.storage.sqlite_store import SqliteStore


# --- the clock --------------------------------------------------------------


def test_seconds_accumulate_per_stage() -> None:
    clock = StageClock()

    clock.add("embed", 2.0)
    clock.add("embed", 1.0)
    clock.add("write", 0.5)

    assert clock.seconds() == {"embed": 3.0, "write": 0.5}


def test_the_largest_stage_comes_first() -> None:
    """The one worth acting on should not need looking for."""
    clock = StageClock()
    clock.add("write", 1.0)
    clock.add("embed", 9.0)

    assert list(clock.seconds()) == ["embed", "write"]


def test_worker_seconds_are_kept_apart_from_wall_time() -> None:
    """Four workers busy for a minute is four worker-minutes and one wall
    minute. Mixing them gives a stage share over 100%, which is a report
    nobody can act on and everybody notices."""
    clock = StageClock()
    clock.add(WAITING, 1.0)
    clock.add_worker("extract", 40.0)

    assert clock.seconds() == {WAITING: 1.0}
    assert clock.worker_seconds() == {"extract": 40.0}
    assert clock.share(WAITING) == 1.0, "the worker time is not in the total"


def test_a_zero_or_negative_duration_is_ignored() -> None:
    """A clock the operating system stepped backwards over would otherwise
    subtract from a stage, and a negative percentage in a report is worse than
    a missing one."""
    clock = StageClock()
    clock.add("embed", 0.0)
    clock.add("embed", -3.0)

    assert clock.seconds() == {}


def test_a_share_of_nothing_is_zero_rather_than_an_error() -> None:
    """Asked on every run, including the ones too short to measure."""
    assert StageClock().share("embed") == 0.0
    assert StageClock().dominant() is None


def test_the_context_managers_measure_real_time() -> None:
    clock = StageClock()

    with clock.stage("embed"):
        pass
    with clock.worker("extract"):
        pass

    assert "embed" in clock.seconds()
    assert "extract" in clock.worker_seconds()


def test_several_threads_may_write_at_once() -> None:
    """The extraction workers write while the consumer reads, and a dict
    update lost to a race is a number quietly too small."""
    clock = StageClock()

    def work() -> None:
        for _ in range(200):
            clock.add_worker("extract", 0.001)

    threads = [threading.Thread(target=work) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert math.isclose(clock.worker_seconds()["extract"], 0.8, rel_tol=0.01)


def test_a_run_that_measured_nothing_says_nothing() -> None:
    """Rather than a row of zeroes, which reads as "every stage was instant"
    instead of "this was not measured"."""
    assert StageClock().as_dict() == {}


# --- the advice, which is what a non-technical person is given --------------


def test_a_waiting_bound_run_is_told_to_read_more_at_once() -> None:
    said = advice({WAITING: 90.0, "embed": 10.0})

    assert "waiting for files to be read" in said
    assert "more of them at once" in said


def test_an_embed_bound_run_is_told_about_the_graphics_card() -> None:
    assert "graphics card" in advice({"embed": 80.0, WAITING: 20.0})


def test_a_run_already_on_the_graphics_card_is_told_something_else() -> None:
    """Recommending the graphics card to somebody already using it is how a
    tuning screen loses its credibility in one sentence."""
    said = advice({"embed": 80.0, WAITING: 20.0}, on_gpu=True)

    assert "larger batch" in said
    assert "A graphics card," not in said


def test_a_balanced_run_is_given_no_advice() -> None:
    """Inventing a recommendation for an even split sends somebody off to
    change a setting for no reason."""
    assert advice({"embed": 34.0, WAITING: 33.0, "write": 33.0}) == ""


def test_advice_never_raises_on_nonsense() -> None:
    for stages in (None, "", {"a": "x"}, {}, 7):
        assert isinstance(advice(stages), str)


# --- the run actually records them ------------------------------------------


class NoVectors:
    def ensure_table(self): pass
    def add(self, **kwargs): return len(kwargs.get("chunk_ids", []))
    def delete_by_file_ids(self, ids): return 0
    def maybe_create_index(self): pass
    def maybe_compact(self, force=False): pass
    def count(self): return 0


def _encode(texts):
    from app.index.embedder import l2_normalise

    return [l2_normalise([math.sin(abs(hash(text)) % 100 + i) for i in range(8)])
            for text in texts]


@pytest.fixture()
def corpus() -> Path:
    root = Path(tempfile.mkdtemp()) / "docs"
    root.mkdir(parents=True)
    for number in range(12):
        (root / f"f{number}.txt").write_text(
            "pump station commissioning report " * 80 + str(number),
            encoding="utf-8")
    return root


def test_a_run_records_where_its_time_went(corpus: Path, tmp_path: Path) -> None:
    from app.index.embedder import Embedder

    store = SqliteStore(tmp_path / "index.db").connect()
    config = PipelineConfig(walk=WalkConfig(roots=[corpus]), workers=2,
                            min_free_gb=0, required_free_gb=0)

    stats = Pipeline(store, NoVectors(),
                     Embedder(dim=8, encoder=_encode), config).run()

    assert stats.indexed == 12
    assert stats.stages, "a run that indexed twelve files measured nothing"
    assert set(stats.stages) <= {WAITING, "write", "embed", "vectors"}
    assert stats.worker_seconds.get("extract", 0) > 0
    store.close()


def test_the_summary_carries_the_stages(corpus: Path, tmp_path: Path) -> None:
    """It is read back by the tuning footer off `last_run_stats`, so it has to
    survive `as_dict`."""
    from app.index.embedder import Embedder

    store = SqliteStore(tmp_path / "index.db").connect()
    config = PipelineConfig(walk=WalkConfig(roots=[corpus]), workers=1,
                            min_free_gb=0, required_free_gb=0)

    summary = Pipeline(store, NoVectors(),
                       Embedder(dim=8, encoder=_encode), config).run().as_dict()

    assert "stages" in summary and summary["stages"]
    assert "worker_seconds" in summary
    store.close()


def test_a_second_run_reports_its_own_time_not_the_first_ones(
    corpus: Path, tmp_path: Path
) -> None:
    """A reused Pipeline would otherwise add the two together, and a total over
    two different corpora is the one number nobody can act on."""
    from app.index.embedder import Embedder

    store = SqliteStore(tmp_path / "index.db").connect()
    config = PipelineConfig(walk=WalkConfig(roots=[corpus]), workers=1,
                            min_free_gb=0, required_free_gb=0)
    pipeline = Pipeline(store, NoVectors(),
                        Embedder(dim=8, encoder=_encode), config)

    first = pipeline.run()
    second = pipeline.run()          # everything unchanged: almost no work

    assert sum(second.stages.values()) < sum(first.stages.values())
    store.close()


def test_extraction_is_still_lazy(tmp_path: Path) -> None:
    r"""**The regression this test exists for was mine.** The first version of
    the worker timer wrapped `_extract_stream` in a `list` - which times it
    perfectly well and buffers a 30GB archive's messages in memory, undoing the
    M16 fix for the sake of a stopwatch.

    Asserted structurally, because reproducing it needs an archive larger than
    a test may create.
    """
    import ast

    source = (Path(__file__).resolve().parents[2] / "app" / "index"
              / "pipeline.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "list"):
            continue
        for argument in node.args:
            called = getattr(argument, "func", None)
            name = getattr(called, "attr", "")
            assert name != "_extract_stream", (
                "extraction was collected into a list - that is M16 undone")
