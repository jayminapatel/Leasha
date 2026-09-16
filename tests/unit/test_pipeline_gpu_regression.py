r"""Work order 202626130120 (0t) section 6: the notice reaches a run.

Pipeline._report_gpu_regression is the method run() calls first, before a
single file is read - "before the run starts" means before either of
those, not after. Tested the same way _report_root_problems is in
test_empty_run_says_why.py: a stub carrying only what the method reads,
because the whole point is that a real Pipeline is not needed to prove
this.
"""

from __future__ import annotations

from app.index.pipeline import IndexStats, Pipeline, PipelineConfig
from app.index.walker import WalkConfig


class _Log:
    def __init__(self):
        self.said = []

    def warning(self, template, *args):
        self.said.append(template.format(*args) if args else template)

    def debug(self, template, *args):
        pass


class _Pipeline:
    """Just enough of a pipeline for the method under test - the same shape
    test_empty_run_says_why.py's own stub uses."""

    def __init__(self, notice):
        self.config = PipelineConfig(
            walk=WalkConfig(roots=[]), gpu_regression_notice=notice)
        self._log = _Log()


def test_a_real_notice_lands_on_the_run() -> None:
    stats = IndexStats()
    pipeline = _Pipeline("this machine lost its graphics card provider")

    Pipeline._report_gpu_regression(pipeline, stats)

    assert stats.notices == ["this machine lost its graphics card provider"]
    assert pipeline._log.said, "a lost provider must be loud, not just visible"


def test_no_notice_means_no_notice() -> None:
    """The common case - nothing to say on every healthy machine, every
    run."""
    stats = IndexStats()
    pipeline = _Pipeline("")

    Pipeline._report_gpu_regression(pipeline, stats)

    assert stats.notices == []
    assert pipeline._log.said == []


def test_an_older_config_with_no_field_at_all_does_not_raise() -> None:
    """A stub or an older caller that never set the field - defaults to
    off, never a crash."""
    class _BareConfig:
        pass

    class _BarePipeline:
        def __init__(self):
            self.config = _BareConfig()
            self._log = _Log()

    stats = IndexStats()
    Pipeline._report_gpu_regression(_BarePipeline(), stats)   # must not raise

    assert stats.notices == []


def test_it_reaches_the_run_summary() -> None:
    """as_dict is what the run log and the Indexing page read - the same
    contract every other notice in this file already keeps."""
    stats = IndexStats()
    stats.notices.append("lost the graphics card")

    assert stats.as_dict()["notices"] == ["lost the graphics card"]


def test_the_field_defaults_to_empty_for_every_existing_caller() -> None:
    """A PipelineConfig built with none of this order's changes in mind -
    every existing test and call site - must behave exactly as before."""
    config = PipelineConfig(walk=WalkConfig(roots=[]))

    assert config.gpu_regression_notice == ""

