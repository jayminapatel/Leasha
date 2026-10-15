r"""Every test measured over a second is marked `slow` (order 1u section 4b).

Layer: L0 - repo tooling.

**Why.** On 2026-10-13, 698 tests took a second or more and only 93 were marked
`slow`; the other 605 (3,687 s) were not, so `run_suite.py --quick`
(`not gui and not slow and not qt`) was not quick, and the marker meant nothing.
They were marked in one pass. This keeps them from drifting again: a test that
`logs/suite/durations.json` judges at or over `run_suite.SLOW_SECONDS` must carry
the mark - on the function, its class, a `pytest.param` of it, or the file's
`pytestmark`.

**Judged on the lowest of its last three runs (2026-10-15).** It used to be judged on
the last run alone, and which tests cross a second moves with how loaded the laptop is:
a quiet 4-part run listed 177, they were marked (#99), and the next 3-part run listed
108 different ones. Marking again would only have moved the line. Load only ever adds
time, so `durations.json` now keeps each test's last `run_suite.HISTORY_RUNS` times and
the test is judged on the lowest (`run_suite.judged_seconds`), once it has
`run_suite.JUDGED_AFTER_RUNS`: a quick test needs three slow runs in a row to be
flagged, and a new test that really is slow is caught after its second run.

**Read from the source, not from the measurement's `marked` flag.** The flag is
whatever the test was when it last ran; a mark added since then is real, so the
file itself is read. A test the file no longer has (renamed, deleted) is a stale
measurement and is passed over.

**Where the measurements live.** `logs/` is git-ignored: the numbers are the
machine's own, written by `scripts/run_suite.py` after a run. A fresh clone - CI
included - has none, and this test says so and skips; so does a machine whose file has
no test measured twice yet (the first full run after the history began). It bites on
the machine that runs the full suite, which is where a new slow test first shows up.
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
#: *2026-10-13:* every section 2 PR merged (#92), the files were re-audited on that run's times
#: and their slow tests marked, so no file is exempt now.
EXEMPT: dict[str, str] = {}


def _is_param(node: ast.AST) -> bool:
    """A `pytest.param(...)` call: its marks are one case's, not the test's."""
    return isinstance(node, ast.Call) and (
        (isinstance(node.func, ast.Attribute) and node.func.attr == "param")
        or (isinstance(node.func, ast.Name) and node.func.id == "param"))


def _is_slow_mark(node: ast.AST) -> bool:
    """`<...>.mark.slow` on a test, a class or a `pytestmark` - **not** one inside a `pytest.param`,
    which marks only that case (review of #85: one slow case would have excused its unmarked siblings)."""
    stack = [node]
    while stack:
        n = stack.pop()
        if _is_param(n):
            continue
        if (isinstance(n, ast.Attribute) and n.attr == "slow"
                and isinstance(n.value, ast.Attribute) and n.value.attr == "mark"):
            return True
        stack.extend(ast.iter_child_nodes(n))
    return False


def _slow_case_ids(decorators) -> set[str]:
    """The ids of the cases a `pytest.param(..., marks=pytest.mark.slow)` marks: its `id=`, else the
    id pytest gives a plain literal (`1`, `a-b` for a tuple). A case named some other way is not
    matched, so it is checked as unmarked - the guard errs towards asking, never towards excusing."""
    ids = set()
    for dec in decorators:
        for node in ast.walk(dec):
            if not (_is_param(node) and any(kw.arg == "marks" and _is_slow_mark_anywhere(kw.value)
                                             for kw in node.keywords)):
                continue
            given = next((kw.value for kw in node.keywords if kw.arg == "id"), None)
            try:
                if given is not None:
                    ids.add(str(ast.literal_eval(given)))
                else:
                    values = [ast.literal_eval(a) for a in node.args]
                    ids.add("-".join(str(v) for v in values))
            except (ValueError, SyntaxError):
                continue
    return ids


def _is_slow_mark_anywhere(node: ast.AST) -> bool:
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
            for case in _slow_case_ids(node.decorator_list):
                marks[f"{node.name}[{case}]"] = True
        elif isinstance(node, ast.ClassDef):
            on_class = any(_is_slow_mark(d) for d in node.decorator_list) or any(
                isinstance(s, ast.Assign) and _is_slow_mark(s.value)
                and any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in s.targets)
                for s in node.body)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    marks[f"{node.name}::{sub.name}"] = (
                        on_class or any(_is_slow_mark(d) for d in sub.decorator_list))
                    for case in _slow_case_ids(sub.decorator_list):
                        marks[f"{node.name}::{sub.name}[{case}]"] = True
    return whole, marks


def unmarked_over_threshold(measurements: dict) -> list[str]:
    """Tests judged at or over the threshold whose source carries no `slow` mark."""
    offenders = []
    for nodeid, entry in measurements.get("tests", {}).items():
        # The source decides, never the recorded `marked` flag: a mark removed since the last
        # measured run must be caught now, not after the next one (review of #85).
        seconds = run_suite.judged_seconds(entry)
        if seconds is None or seconds < run_suite.SLOW_SECONDS:
            continue                             # not measured often enough yet, or quick
        path, _, rest = nodeid.partition("::")
        file = ROOT / path
        if Path(path).stem in EXEMPT or not file.is_file():
            continue
        whole, marks = _marks_in(file)
        name = rest.split("[", 1)[0]
        if whole or name not in marks or marks[name] or marks.get(rest):
            continue                             # the file, the test, or this one case is marked
        offenders.append(f"{nodeid} ({seconds:.1f} s)")
    return offenders


def _judged_any(measurements: dict) -> bool:
    return any(run_suite.judged_seconds(entry) is not None
               for entry in measurements.get("tests", {}).values())


@pytest.mark.slow
def test_every_test_measured_over_the_threshold_is_marked_slow():
    measurements = run_suite.load_durations(run_suite.DURATIONS_FILE)
    if not _judged_any(measurements):
        pytest.skip(f"no test measured {run_suite.JUDGED_AFTER_RUNS} times at "
                    f"{run_suite.DURATIONS_FILE} - each full `scripts/run_suite.py` run adds "
                    f"one time per test")
    offenders = unmarked_over_threshold(measurements)
    assert not offenders, (
        f"{len(offenders)} test(s) took {run_suite.SLOW_SECONDS:.0f} s or more in each of "
        f"their last {run_suite.HISTORY_RUNS} measured runs (the lowest is shown) and are not "
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
    _marks_in.cache_clear()
    over = run_suite.SLOW_SECONDS
    slow_twice = {"runs": [over, over + 0.5], "marked": False}
    measured = {"tests": {
        "tests/unit/test_probe.py::test_unmarked": slow_twice,
        "tests/unit/test_probe.py::test_marked": slow_twice,
        "tests/unit/test_probe.py::test_param[1]": slow_twice,
        "tests/unit/test_probe.py::TestGroup::test_method": slow_twice,
        "tests/unit/test_whole.py::test_any": slow_twice,
        "tests/unit/test_probe.py::test_gone": slow_twice,
        "tests/unit/test_probe.py::test_fast": {"runs": [over / 2, over / 2], "marked": False},
    }}
    # Judged on the lowest of its runs, so the line says the lowest.
    assert unmarked_over_threshold(measured) == [
        f"tests/unit/test_probe.py::test_unmarked ({over:.1f} s)"]

    # 2026-10-15, the new rule: one quick run among its last three clears it; one slow run
    # alone is not judged at all; three slow runs in a row are.
    def judged(runs):
        return unmarked_over_threshold(
            {"tests": {"tests/unit/test_probe.py::test_unmarked": {"runs": runs}}})

    assert judged([over / 2, over * 3, over * 3]) == [], "a quiet run in the window clears it"
    assert judged([over * 3, over / 2, over * 3]) == []
    assert judged([over * 3]) == [], "one run, loaded or not, is not enough to judge"
    assert judged([over / 2, over * 2, over * 2, over * 2]) == [
        f"tests/unit/test_probe.py::test_unmarked ({over * 2:.1f} s)"], (
        "only the last three count: the quiet run has aged out")


def test_the_threshold_is_the_runners_own():
    """One number: `--audit-markers` and this guard cannot disagree about what is slow.
    2026-10-15: nor about how a test is judged - both go through `judged_seconds`."""
    assert run_suite.SLOW_SECONDS == 1.0
    at = run_suite.SLOW_SECONDS
    audited = run_suite.unmarked_slow({"tests": {
        "a::t": {"runs": [at, at + 1.0], "marked": False},
        "a::u": {"runs": [at - 0.01, at + 1.0], "marked": False}}})
    assert audited == [("a::t", at)]


def _source(root: Path, marked: int, unmarked: int) -> list[str]:
    """A test file of `marked` tests carrying `slow` and `unmarked` that do not."""
    tests = root / "tests" / "unit"
    tests.mkdir(parents=True, exist_ok=True)
    names = [f"test_{i:03d}" for i in range(marked + unmarked)]
    (tests / "test_load.py").write_text("import pytest\n\n" + "".join(
        ("@pytest.mark.slow\n" if i < marked else "") + f"def {name}():\n    pass\n\n"
        for i, name in enumerate(names)), encoding="utf-8")
    return [f"tests/unit/test_load.py::{name}" for name in names]


def _run(times: dict[str, float]) -> dict:
    """One run's part, as `scripts/suite_durations.py` writes it."""
    return {"files": {"tests/unit/test_load.py": sum(times.values())},
            "tests": {name: {"seconds": seconds, "marked": False}
                      for name, seconds in times.items()}}


def test_177_then_108_different_tests_over_a_second_is_not_a_red(tmp_path, monkeypatch):
    """The two runs of 2026-10-15, in shape: a quiet 4-part run put 177 tests over a second
    and they were marked (#99); the next, 3-part run put 108 *different* tests over, each
    of them well under a second the run before. Judged on the last run alone that was 108
    offenders; judged on the lowest of the last runs it is none - and stays none through a
    third run that is just as loaded."""
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    _marks_in.cache_clear()
    names = _source(tmp_path, marked=177, unmarked=108 + 200)
    first, second, rest = names[:177], names[177:285], names[285:]
    quiet = _run({**{n: 1.3 for n in first}, **{n: 0.6 for n in second},
                  **{n: 0.2 for n in rest}})
    loaded = _run({**{n: 0.8 for n in first}, **{n: 1.2 for n in second},
                   **{n: 0.4 for n in rest}})

    after_one = run_suite.merge_durations({}, [quiet])
    assert not _judged_any(after_one), "one run judges nothing - the guard skips"
    assert unmarked_over_threshold(after_one) == []

    after_two = run_suite.merge_durations(after_one, [loaded])
    assert len(run_suite.recent_runs(after_two["tests"][second[0]])) == 2
    latest_alone = [n for n, e in after_two["tests"].items()
                    if run_suite.recent_runs(e)[-1] >= run_suite.SLOW_SECONDS and n in second]
    assert len(latest_alone) == 108, "the old rule's red, reproduced"
    assert unmarked_over_threshold(after_two) == []
    assert run_suite.unmarked_slow(after_two) == []

    after_three = run_suite.merge_durations(after_two, [loaded])
    assert unmarked_over_threshold(after_three) == [], "the quiet run is still in the window"


def test_a_new_test_that_really_is_slow_is_caught_on_its_second_run(tmp_path, monkeypatch):
    """Order 1u section 4b's purpose kept: `--quick` stays quick because a genuinely slow
    unmarked test is still caught within two runs, however quiet the machine."""
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    _marks_in.cache_clear()
    old, new = _source(tmp_path, marked=0, unmarked=2)
    history = run_suite.merge_durations({}, [_run({old: 0.1})])
    history = run_suite.merge_durations(history, [_run({old: 0.1, new: 2.5})])
    assert unmarked_over_threshold(history) == [], "its first run: not judged yet"
    assert [name for name, _ in run_suite.awaiting_judgement(history)] == [new]
    history = run_suite.merge_durations(history, [_run({old: 0.1, new: 2.2})])
    assert unmarked_over_threshold(history) == [f"{new} (2.2 s)"]


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



def test_a_slow_case_inside_pytest_param_does_not_mark_the_whole_test(tmp_path):
    """Review of #85: one `pytest.param(..., marks=pytest.mark.slow)` marks that case only."""
    import textwrap

    f = tmp_path / "test_x.py"
    f.write_text(textwrap.dedent("""
        import pytest
        @pytest.mark.parametrize("n", [1, pytest.param(2, marks=pytest.mark.slow)])
        def test_some(n):
            pass
        @pytest.mark.slow
        def test_all():
            pass
        class TestGroup:
            @pytest.mark.parametrize("n", [1, pytest.param(2, marks=pytest.mark.slow)])
            def test_case(self, n):
                pass
    """), encoding="utf-8")
    _marks_in.cache_clear()
    whole, marks = _marks_in(f)
    assert not whole and marks == {"test_some": False, "test_some[2]": True, "test_all": True,
                                   "TestGroup::test_case": False, "TestGroup::test_case[2]": True}
