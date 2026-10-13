r"""Every test measured over a second is marked `slow` (order 1u section 4b).

Layer: L0 - repo tooling.

**Why.** On 2026-10-13, 698 tests took a second or more and only 93 were marked
`slow`; the other 605 (3,687 s) were not, so `run_suite.py --quick`
(`not gui and not slow and not qt`) was not quick, and the marker meant nothing.
They were marked in one pass. This keeps them from drifting again: a test that
`logs/suite/durations.json` lists at or over `run_suite.SLOW_SECONDS` must carry
the mark - on the function, its class, a `pytest.param` of it, or the file's
`pytestmark`.

**Read from the source, not from the measurement's `marked` flag.** The flag is
whatever the test was when it last ran; a mark added since then is real, so the
file itself is read. A test the file no longer has (renamed, deleted) is a stale
measurement and is passed over.

**Where the measurements live.** `logs/` is git-ignored: the numbers are the
machine's own, written by `scripts/run_suite.py` after a run. A fresh clone - CI
included - has none, and this test says so and skips. It bites on the machine
that runs the full suite, which is where a new slow test first shows up.
"""

from __future__ import annotations

import ast
import importlib.util
import warnings
from functools import cache
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _run_suite():
    spec = importlib.util.spec_from_file_location("run_suite", ROOT / "scripts" / "run_suite.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


run_suite = _run_suite()

#: Files being made faster by order 1u section 2 at the same time as this test was written
#: (2026-10-13), so marking their tests here would collide with that work and mark tests
#: that are about to stop being slow. Each is re-audited when its group's PR merges, and
#: comes off this list then.
_SECTION_2_REASON = "order 1u section 2 is speeding this file up; re-audited when its PR merges"
EXEMPT: dict[str, str] = {name: _SECTION_2_REASON for name in (
    "test_index_tuning_acceptance", "test_speed_work", "test_run_setup", "test_read_process",
    "test_grab_ui", "test_window_opens", "test_ui_redesign_scenarios", "test_ui_review_0x9",
    "test_diagnostics", "test_index_child_process", "test_timed_out_retry",
    "test_pst_folder_resume", "test_staging", "test_resolve_gpu_regression",
    "test_clip_lane_wiring", "test_ui_never_blocks", "test_query_plans", "test_converter",
    "test_layer4_acceptance", "test_layer3_acceptance",
)}


def _is_slow_mark(node: ast.AST) -> bool:
    """Anything spelt `<...>.mark.slow` - a decorator, a `pytestmark`, a `pytest.param` mark."""
    return any(isinstance(n, ast.Attribute) and n.attr == "slow"
               and isinstance(n.value, ast.Attribute) and n.value.attr == "mark"
               for n in ast.walk(node))


@cache
def _marks_in(path: Path) -> tuple[bool, dict[str, bool]]:
    """(the whole file is `slow`, {test name or Class::name: marked})."""
    with warnings.catch_warnings():              # a file's own `"D:\Data"` is not ours to warn on
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(path.read_text(encoding="utf-8"))
    whole = any(isinstance(node, ast.Assign) and _is_slow_mark(node.value)
                and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets)
                for node in tree.body)
    marks: dict[str, bool] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            marks[node.name] = any(_is_slow_mark(d) for d in node.decorator_list)
        elif isinstance(node, ast.ClassDef):
            on_class = any(_is_slow_mark(d) for d in node.decorator_list) or any(
                isinstance(s, ast.Assign) and _is_slow_mark(s.value)
                and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in s.targets)
                for s in node.body)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    marks[f"{node.name}::{sub.name}"] = (
                        on_class or any(_is_slow_mark(d) for d in sub.decorator_list))
    return whole, marks


def unmarked_over_threshold(measurements: dict) -> list[str]:
    """Measured tests at or over the threshold whose source carries no `slow` mark."""
    offenders = []
    for nodeid, entry in measurements.get("slow_tests", {}).items():
        if float(entry.get("seconds", 0.0)) < run_suite.SLOW_SECONDS or entry.get("marked"):
            continue
        path, _, rest = nodeid.partition("::")
        file = ROOT / path
        if Path(path).stem in EXEMPT or not file.is_file():
            continue
        whole, marks = _marks_in(file)
        name = rest.split("[", 1)[0]
        if whole or name not in marks or marks[name]:
            continue
        offenders.append(f"{nodeid} ({float(entry['seconds']):.1f} s)")
    return offenders


def test_every_test_measured_over_the_threshold_is_marked_slow():
    measurements = run_suite.load_durations(run_suite.DURATIONS_FILE)
    if not measurements.get("slow_tests"):
        pytest.skip(f"no measurements at {run_suite.DURATIONS_FILE} - a full "
                    f"`scripts/run_suite.py` run writes them")
    offenders = unmarked_over_threshold(measurements)
    assert not offenders, (
        f"{len(offenders)} test(s) took {run_suite.SLOW_SECONDS:.0f} s or more and are not "
        f"marked `slow` - mark them (`@pytest.mark.slow`) or make them faster:\n  "
        + "\n  ".join(offenders[:60]))


def test_the_guard_catches_an_unmarked_slow_test_and_passes_a_marked_one(tmp_path,
                                                                          monkeypatch):
    """The guard proved against a file of its own, so it cannot pass by reading nothing."""
    tests = tmp_path / "tests" / "unit"
    tests.mkdir(parents=True)
    (tests / "test_probe.py").write_text(
        "import pytest\n\n"
        "def test_unmarked():\n    pass\n\n"
        "@pytest.mark.slow\ndef test_marked():\n    pass\n\n"
        "@pytest.mark.parametrize('x', [pytest.param(1, marks=pytest.mark.slow)])\n"
        "def test_param(x):\n    pass\n\n"
        "class TestGroup:\n    @pytest.mark.slow\n    def test_method(self):\n        pass\n",
        encoding="utf-8")
    (tests / "test_whole.py").write_text(
        "import pytest\npytestmark = [pytest.mark.slow]\n\ndef test_any():\n    pass\n",
        encoding="utf-8")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    over = run_suite.SLOW_SECONDS
    measured = {"slow_tests": {
        "tests/unit/test_probe.py::test_unmarked": {"seconds": over, "marked": False},
        "tests/unit/test_probe.py::test_marked": {"seconds": over, "marked": False},
        "tests/unit/test_probe.py::test_param[1]": {"seconds": over, "marked": False},
        "tests/unit/test_probe.py::TestGroup::test_method": {"seconds": over, "marked": False},
        "tests/unit/test_whole.py::test_any": {"seconds": over, "marked": False},
        "tests/unit/test_probe.py::test_gone": {"seconds": over, "marked": False},
        "tests/unit/test_probe.py::test_fast": {"seconds": over / 2, "marked": False},
    }}
    assert unmarked_over_threshold(measured) == [
        f"tests/unit/test_probe.py::test_unmarked ({over:.1f} s)"]


def test_the_threshold_is_the_runners_own():
    """One number: `--audit-markers` and this guard cannot disagree about what is slow."""
    assert run_suite.SLOW_SECONDS == 1.0
    audited = run_suite.unmarked_slow({"slow_tests": {
        "a::t": {"seconds": run_suite.SLOW_SECONDS, "marked": False}}})
    assert audited == [("a::t", run_suite.SLOW_SECONDS)]


def test_every_exemption_names_a_real_file_and_a_reason():
    for name, reason in EXEMPT.items():
        assert list((ROOT / "tests").rglob(f"{name}.py")), f"{name} is not a test file"
        assert reason.strip()


@pytest.mark.slow
def test_a_slow_test_still_cannot_load_the_florence_model():
    """Found while marking (2026-10-13): `tests/conftest.py`'s Florence-2 guard used to let
    any `slow` test through, so marking 428 more tests `slow` would have let each of them
    download and load a gigabyte model - the hang that guard exists for. The opt-out is
    `real_florence` now, and a test that is merely slow is still guarded."""
    florence_tagger = pytest.importorskip("app.extract.florence_tagger")
    assert florence_tagger._load.__name__ == "<lambda>", "the guard did not patch `_load`"
