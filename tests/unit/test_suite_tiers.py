r"""The test tiers say one thing everywhere (order 1u section 4a), and the input guard stays.

Layer: L0 - repo tooling, no app imports.

**The gap this closes.** Until 2026-10-13 the Windows CI job ran
`-m "not jvm and not e2e and not slow"` on every push and the weekly schedule ran
only the macOS job, so the 93 `slow` tests ran nowhere but a local full run - and
nothing said so except the YAML. The tiers are now written once, in
`scripts/run_suite.py`'s `TIERS`, and these tests read `.github/workflows/ci.yml`,
`pyproject.toml` and `docs/VERSIONING.md` and fail when one of them says otherwise.

| Tier | Where | Selection |
|---|---|---|
| quick | `run_suite.py --quick` | no Qt, no `gui`, no `slow` |
| affected | `run_suite.py --affected` | the project's deselection, on the files that can see a change |
| full | `run_suite.py` | everything but `e2e` and `jvm` |
| PR CI | the Windows job, every push | everything but `e2e` and `jvm` - `slow` included |
| weekly CI | the Windows job on the schedule | adds `jvm`, in its own process |
| release | the owner's laptop | `e2e` against the packaged app |

*2026-10-15: CI runs only when started by hand now, and `ci.yml` is three workflows -
Windows Run (the PR CI selection), Mac Run and Windows JVM Run (the `jvm` tests). The table's
"every push" and "the schedule" are the history of those two rows.*

**And section 4f's guard:** the autouse fixture in `tests/unit/conftest.py` that fails a
test leaving a key or mouse button held is checked to exist, to be active, and to see
and release the exact leak that #80 fixed by hand.
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
#: The workflows, each started by hand: file -> the name GitHub shows (2026-10-15).
RUNS = {"windows-run.yml": "Windows Run", "mac-run.yml": "Mac Run",
        "windows-jvm-run.yml": "Windows JVM Run"}


def _run_suite():
    spec = importlib.util.spec_from_file_location("run_suite", ROOT / "scripts" / "run_suite.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


run_suite = _run_suite()


def _workflow(file: str) -> dict:
    return yaml.safe_load((WORKFLOWS / file).read_text(encoding="utf-8"))


def _marker_expression(command: str) -> str | None:
    """The `-m "..."` of one pytest command line, or None when it passes none."""
    if "pytest" not in command:
        return None
    found = re.search(r'-m\s+"([^"]*)"', command.split("pytest", 1)[1])
    return found.group(1) if found else None


def _pytest_steps(job: dict) -> list[dict]:
    return [step for step in job["steps"] if "pytest" in step.get("run", "")]


# -- 4a: one list of tiers ------------------------------------------------------------


def test_the_tiers_are_the_six_the_order_names():
    assert set(run_suite.TIERS) == {"quick", "affected", "full", "pr_ci", "weekly_ci_adds",
                                    "release"}


def test_the_runner_deselection_is_the_projects_own():
    """`addopts` in pyproject.toml is what a plain `pytest` and every runner part use."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'-m \\"{run_suite.PROJECT_DESELECT}\\"' in pyproject
    assert run_suite.TIERS["full"] == run_suite.TIERS["affected"] == run_suite.PROJECT_DESELECT


def test_quick_leaves_out_qt_gui_and_slow_and_only_those():
    quick = run_suite.TIERS["quick"]
    assert quick == f"{run_suite.PROJECT_DESELECT} and {run_suite.QUICK_DESELECT}"
    for marker in ("gui", "qt", "slow", "jvm", "e2e"):
        assert f"not {marker}" in quick, f"--quick still runs `{marker}` tests"


def test_pr_ci_runs_slow_and_leaves_out_only_e2e_and_jvm():
    pr_ci = run_suite.TIERS["pr_ci"]
    assert "slow" not in pr_ci, "PR CI must include the slow tests (order 1u section 4a)"
    assert pr_ci == "not jvm and not e2e"
    assert pr_ci == run_suite.TIERS["full"], "a local full run and PR CI select the same tests"


# 2026-10-15, later, the owner: "set up clean jobs in CI i.e. Windows Run, Mac Run, etc so i
# can run them manual and only those should be there". `ci.yml` and its three boxes became
# three workflows of their own, each one Run button; the tests below read those files.


def test_the_only_workflows_are_the_three_runs():
    assert {path.name for path in WORKFLOWS.glob("*.y*ml")} == set(RUNS), \
        "only Windows Run, Mac Run and Windows JVM Run belong in .github/workflows"
    for file, name in RUNS.items():
        assert _workflow(file)["name"] == name


def test_ci_runs_only_when_started_by_hand():
    """2026-10-15, the owner: "only this should run on demand for both". No push, pull
    request or schedule starts a run; the local suite is the gate (CLAUDE.md)."""
    for file in RUNS:
        workflow = _workflow(file)
        triggers = workflow.get("on", workflow.get(True))     # YAML 1.1 reads `on` as true
        assert triggers == {"workflow_dispatch": None}, f"{file} must start only by hand"


def test_every_run_may_only_read_the_repository():
    """CodeQL on PR #98: a workflow with no `permissions` gets the token's defaults."""
    for file in RUNS:
        assert _workflow(file).get("permissions") == {"contents": "read"}, file


def test_windows_run_runs_the_pr_ci_tier():
    (job,) = _workflow("windows-run.yml")["jobs"].values()
    assert job["runs-on"] == "windows-latest"
    (step,) = _pytest_steps(job)
    assert _marker_expression(step["run"]) == run_suite.TIERS["pr_ci"]


def test_windows_jvm_run_runs_the_jvm_tests_in_a_run_of_their_own():
    (job,) = _workflow("windows-jvm-run.yml")["jobs"].values()
    assert job["runs-on"] == "windows-latest"
    (step,) = _pytest_steps(job)
    assert _marker_expression(step["run"]) == run_suite.TIERS["weekly_ci_adds"] == "jvm"


def test_mac_run_runs_on_a_mac():
    (job,) = _workflow("mac-run.yml")["jobs"].values()
    assert job["runs-on"].startswith("macos")
    assert len(_pytest_steps(job)) == 1


def test_no_ci_command_leaves_out_slow_on_windows():
    for file in ("windows-run.yml", "windows-jvm-run.yml"):
        for job in _workflow(file)["jobs"].values():
            for step in _pytest_steps(job):
                assert "not slow" not in step["run"], step["name"]


def test_the_release_checklist_runs_e2e_on_the_packaged_app():
    text = (ROOT / "docs" / "VERSIONING.md").read_text(encoding="utf-8")
    checklist = text.split("## Release checklist", 1)[1].split("\n## ", 1)[0]
    assert f"-m {run_suite.TIERS['release']}" in checklist
    assert "packaged app" in checklist
    assert "`slow`" in checklist and "`jvm`" in checklist, "the checklist names what CI runs"


def test_the_runner_docstring_names_the_tiers():
    doc = run_suite.__doc__
    for flag in ("--quick", "--affected", "TIERS", "e2e", "jvm"):
        assert flag in doc


# -- 4f: no input left held -----------------------------------------------------------


def test_the_input_fixture_exists_and_is_autouse():
    tree = ast.parse((ROOT / "tests" / "unit" / "conftest.py").read_text(encoding="utf-8"))
    found = [node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name == "no_input_left_held"]
    assert found, "tests/unit/conftest.py lost `no_input_left_held` (order 1u section 4f)"
    decorator = ast.unparse(found[0].decorator_list[0])
    assert "autouse=True" in decorator, decorator


def test_the_input_fixture_is_active_on_this_test(request):
    assert "no_input_left_held" in request.fixturenames


def test_the_only_excused_input_leakers_are_files_section_2_is_rewriting():
    """A test that leaves input held is fixed, not excused - except in a file another
    thread of order 1u is rewriting at the same moment, until that lands."""
    from tests.unit import conftest
    from tests.unit.test_slow_markers_truthful import EXEMPT

    for path in conftest.KNOWN_INPUT_LEAKERS:
        assert (ROOT / path).is_file(), path
        assert Path(path).stem in EXEMPT, f"{path} leaves input held: fix it, do not excuse it"


@pytest.mark.qt
def test_a_held_ctrl_and_button_are_seen_and_released(qtbot):
    """The two leaks of 2026-10-13, reproduced, seen and let go of."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QLineEdit

    from tests.unit import conftest

    box = QLineEdit()
    qtbot.addWidget(box)
    box.show()
    QTest.keyClick(box, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
    assert conftest.held_input() == ["ControlModifier"]
    conftest.release_held_input()
    assert conftest.held_input() == []

    QTest.mouseDClick(box, Qt.MouseButton.LeftButton)
    assert conftest.held_input() == ["LeftButton"]
    conftest.release_held_input()
    assert conftest.held_input() == []
