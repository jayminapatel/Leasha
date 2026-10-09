r"""The suite runner measures, balances, selects and does not hang (order `suite-speed`).

Layer: L0 - repo tooling, no app imports.

What these pin, and the numbers that made each worth pinning:

* **Measuring.** `scripts/suite_durations.py` writes what every test file cost and which
  tests over a second carry the `slow` mark. Before it nothing recorded a duration, so the
  parts could only be cut by file count.
* **Balancing.** `split_balanced` places files longest-first into the emptiest part. On
  2026-10-08 the alphabetical thirds finished at 17, 23 and 22 minutes; the wall time is
  the slowest, and balancing is what brings it to the average.
* **Selecting.** `scripts/suite_affected.py` maps a change to the test files that can see
  it through the import graph and the texts of the tests, and says "everything" when
  that is the honest answer.
* **Not hanging.** A part past `--part-timeout` is killed with its process tree. On
  2026-10-08 one sat seven hours in native code that pytest-timeout could not interrupt.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


run_suite = _load("run_suite")
affected = run_suite.suite_affected          # the very object the runner calls


# ---------------------------------------------------------------------------
# 1b: balancing
# ---------------------------------------------------------------------------

def _files(count: int) -> list[str]:
    return [f"tests/unit/test_{i:03d}.py" for i in range(count)]


def test_balanced_parts_differ_by_no_more_than_the_longest_file():
    files = _files(40)
    durations = {f: 1.0 + (i * 7) % 13 for i, f in enumerate(files)}
    groups = run_suite.split_balanced(files, 3, durations)
    totals = [sum(durations[f] for f in g) for g in groups]
    assert len(groups) == 3
    assert max(totals) - min(totals) <= max(durations.values())
    assert sorted(f for g in groups for f in g) == files, "every file exactly once"
    assert all(g == sorted(g) for g in groups), "alphabetical inside a part"
    assert run_suite.split_balanced(files, 3, durations) == groups, "deterministic"


def test_the_alphabetical_thirds_of_the_night_run_are_what_balancing_improves_on():
    """Three files of 17, 23 and 22 (minutes) and three light ones: contiguous thirds
    put the two heavy ones together; balanced parts never do."""
    files = _files(6)
    durations = dict(zip(files, [17.0, 1.0, 23.0, 1.0, 22.0, 1.0], strict=True))
    contiguous = run_suite.split(files, 3)
    balanced = run_suite.split_balanced(files, 3, durations)
    worst = max(sum(durations[f] for f in g) for g in contiguous)
    best = max(sum(durations[f] for f in g) for g in balanced)
    assert worst == 24.0 and best == 23.0


def test_a_file_never_measured_counts_as_the_median_of_those_that_were():
    files = _files(3)
    durations = {files[0]: 1.0, files[1]: 9.0}          # files[2] unknown -> median 5.0
    groups = run_suite.split_balanced(files, 2, durations)
    assert sorted(groups, key=len) == [[files[1]], [files[0], files[2]]]


def test_without_measurements_the_contiguous_split_is_unchanged():
    files = _files(10)
    assert run_suite.split_balanced(files, 3, {}) == run_suite.split(files, 3)


# ---------------------------------------------------------------------------
# 1a: measuring, and merging the parts
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_the_plugin_writes_what_each_file_cost_and_which_slow_tests_are_unmarked(tmp_path):
    (tmp_path / "test_plug.py").write_text(textwrap.dedent("""
        import time, pytest
        def test_fast():
            pass
        @pytest.mark.slow
        def test_marked():
            time.sleep(1.05)
        def test_unmarked():
            time.sleep(1.05)
        """), encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers = slow: s\n", encoding="utf-8")
    out = tmp_path / "out" / "durations.json"
    env = dict(os.environ, LEASHA_SUITE_DURATIONS=str(out), PYTHONPATH=str(ROOT))
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(tmp_path / "test_plug.py"), "-q",
         "-p", "scripts.suite_durations", "-p", "no:cacheprovider",
         "-c", str(tmp_path / "pytest.ini"), "--rootdir", str(tmp_path)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(out.read_text(encoding="utf-8"))
    assert set(data) >= {"written", "files", "slow_tests"}
    (name, seconds), = data["files"].items()
    assert name.endswith("test_plug.py") and seconds >= 2.0
    slow = {k.split("::")[1]: v for k, v in data["slow_tests"].items()}
    assert slow["test_marked"]["marked"] is True
    assert slow["test_unmarked"]["marked"] is False
    assert "test_fast" not in slow


def test_without_the_variable_the_plugin_registers_nothing(monkeypatch):
    plugin = _load("suite_durations")
    monkeypatch.delenv(plugin.ENV_VAR, raising=False)

    registered: list[str] = []

    class Manager:
        def register(self, obj, name):
            registered.append(name)

    class Config:
        pluginmanager = Manager()

    plugin.pytest_configure(Config())
    assert registered == []


def test_merging_keeps_a_file_that_did_not_run_and_replaces_one_that_did():
    previous = {"files": {"a.py": 10.0, "b.py": 20.0},
                "slow_tests": {"a.py::old": {"seconds": 3.0, "marked": False},
                               "b.py::kept": {"seconds": 2.0, "marked": True}}}
    part = {"files": {"a.py": 4.0}, "slow_tests": {"a.py::new": {"seconds": 1.5, "marked": False}}}
    merged = run_suite.merge_durations(previous, [part])
    assert merged["files"] == {"a.py": 4.0, "b.py": 20.0}
    assert set(merged["slow_tests"]) == {"a.py::new", "b.py::kept"}, (
        "a.py ran again, so its old slow test goes; b.py did not, so its stays")
    assert merged["written"]


def test_unmarked_slow_tests_are_listed_slowest_first_and_the_marked_are_not():
    measurements = {"files": {"x.py": 30.0, "y.py": 1.0},
                    "slow_tests": {"x.py::a": {"seconds": 2.0, "marked": False},
                                   "x.py::b": {"seconds": 5.0, "marked": False},
                                   "x.py::c": {"seconds": 9.0, "marked": True}}}
    assert run_suite.unmarked_slow(measurements) == [("x.py::b", 5.0), ("x.py::a", 2.0)]
    text = run_suite.describe_measurements(measurements)
    assert "slowest 2 of 2 test files" in text and "x.py" in text
    assert "2 test(s) over 1s not marked `slow`" in text
    assert run_suite.describe_measurements({}) == "no measurements yet"


def test_audit_markers_runs_nothing_and_is_honest_about_no_measurements(tmp_path, capsys):
    code = run_suite.main(["--audit-markers", "--durations", str(tmp_path / "none.json")])
    assert code == 0
    assert "no measurements yet" in capsys.readouterr().out


def test_an_unreadable_durations_file_is_treated_as_none(tmp_path):
    broken = tmp_path / "durations.json"
    broken.write_text("{not json", encoding="utf-8")
    assert run_suite.load_durations(broken) == {}
    assert run_suite.load_durations(tmp_path / "missing.json") == {}


# ---------------------------------------------------------------------------
# The parts: what each is started with, and what the runner keeps afterwards
# ---------------------------------------------------------------------------

def _part_class(log_text: str, exit_code: int, *, durations: dict | None = None):
    """A `subprocess.Popen` stand-in: writes `log_text`, exits with `exit_code`, and when
    `durations` is given writes it where `LEASHA_SUITE_DURATIONS` points, as the plugin
    would. Records every command and environment it was started with."""
    seen: list[tuple[list[str], dict]] = []

    class Part:
        pid = 4242

        def __init__(self, command, cwd, stdout, stderr, env=None, **kwargs):
            seen.append((command, env or {}))
            stdout.write(log_text)
            stdout.flush()
            if durations is not None and env and env.get("LEASHA_SUITE_DURATIONS"):
                target = Path(env["LEASHA_SUITE_DURATIONS"])
                target.write_text(json.dumps(durations), encoding="utf-8")

        def poll(self):
            return exit_code

        def wait(self, timeout=None):
            return exit_code

    return Part, seen


GREEN = ("tests/unit/test_ok.py::test_fine PASSED [100%]\n"
         "============================== 1 passed in 0.10s ==============================\n")


def _run_main(monkeypatch, tmp_path, part, argv, *, files=("tests/unit/test_ok.py",)):
    monkeypatch.setattr(subprocess, "Popen", part)
    monkeypatch.setattr(run_suite, "find_files", lambda explicit: explicit or list(files))
    monkeypatch.setattr(run_suite.tempfile, "mkdtemp", lambda prefix: str(tmp_path / "work"))
    (tmp_path / "work").mkdir(exist_ok=True)
    return run_suite.main([*argv, "--durations", str(tmp_path / "durations.json")])


def test_every_part_loads_the_durations_plugin_and_is_told_where_to_write(tmp_path, monkeypatch):
    part, seen = _part_class(GREEN, 0)
    assert _run_main(monkeypatch, tmp_path, part, ["-j", "1"]) == 0
    (command, env), = seen
    assert "scripts.suite_durations" in command and command[command.index("-p") + 1] in (
        "no:cacheprovider", "scripts.suite_durations")
    assert env["LEASHA_SUITE_DURATIONS"].endswith("durations0.json")


def test_what_the_parts_measured_is_merged_and_reported_after_the_run(tmp_path, monkeypatch,
                                                                     capsys):
    measured = {"files": {"tests/unit/test_ok.py": 12.5},
                "slow_tests": {"tests/unit/test_ok.py::test_fine": {"seconds": 1.2,
                                                                    "marked": False}}}
    part, _ = _part_class(GREEN, 0, durations=measured)
    assert _run_main(monkeypatch, tmp_path, part, ["-j", "1"]) == 0
    stored = json.loads((tmp_path / "durations.json").read_text(encoding="utf-8"))
    assert stored["files"] == {"tests/unit/test_ok.py": 12.5}
    out = capsys.readouterr().out
    assert "slowest 1 of 1 test files" in out
    assert "1 test(s) over 1s not marked `slow`" in out


def test_the_next_run_is_balanced_by_what_the_last_one_measured(tmp_path, monkeypatch, capsys):
    (tmp_path / "durations.json").write_text(json.dumps(
        {"files": {"tests/unit/test_a.py": 30.0, "tests/unit/test_b.py": 1.0,
                   "tests/unit/test_c.py": 29.0}}), encoding="utf-8")
    part, seen = _part_class(GREEN, 0)
    files = ("tests/unit/test_a.py", "tests/unit/test_b.py", "tests/unit/test_c.py")
    assert _run_main(monkeypatch, tmp_path, part, ["-j", "2"], files=files) == 0
    groups = [[arg for arg in command if arg.startswith("tests/")] for command, _ in seen]
    assert sorted(groups) == [["tests/unit/test_a.py"],
                              ["tests/unit/test_b.py", "tests/unit/test_c.py"]]
    assert "balanced by measured cost" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 1c: a part that never ends
# ---------------------------------------------------------------------------

def test_a_part_that_never_ends_is_killed_with_its_tree_and_reported(tmp_path, monkeypatch,
                                                                     capsys):
    killed = []

    class Hung:
        pid = 777

        def __init__(self, command, cwd, stdout, stderr, env=None, **kwargs):
            stdout.write("tests/unit/test_first.py::test_one PASSED [ 50%]\n"
                         "tests/unit/test_hang.py::test_forever ")
            stdout.flush()

        def poll(self):
            return None                       # never finishes on its own

        def wait(self, timeout=None):
            return -9 if killed else None

    monkeypatch.setattr(run_suite, "kill_tree", lambda process: killed.append(process.pid))
    monkeypatch.setattr(run_suite, "POLL_SECONDS", 0.01)
    code = _run_main(monkeypatch, tmp_path, Hung, ["-j", "1", "--part-timeout", "0.05"])
    assert code == 1
    assert killed == [777]
    out = capsys.readouterr().out
    assert "TIMED OUT" in out and "Last file it started: tests/unit/test_hang.py" in out
    assert "1 process(es) crashed" in out


def test_kill_tree_ends_a_real_process_and_the_child_it_started():
    """Not a stand-in: a Python process that starts another and sleeps. After
    `kill_tree` neither is alive - the child is what `taskkill /T` is for."""
    import psutil

    parent = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent("""
            import subprocess, sys, time
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            print(child.pid, flush=True)
            time.sleep(120)
            """)],
        stdout=subprocess.PIPE, text=True,
        **({} if sys.platform == "win32" else {"start_new_session": True}))
    child_pid = int(parent.stdout.readline().strip())
    assert psutil.pid_exists(child_pid)
    run_suite.kill_tree(parent)
    parent.wait(timeout=30)
    deadline = time.time() + 10
    while time.time() < deadline and psutil.pid_exists(child_pid):
        time.sleep(0.1)
    assert not psutil.pid_exists(child_pid), "the child survived its parent's killing"


# ---------------------------------------------------------------------------
# 2b: --quick
# ---------------------------------------------------------------------------

def test_quick_repeats_the_projects_own_deselection_rather_than_replacing_it(tmp_path,
                                                                           monkeypatch):
    part, seen = _part_class(GREEN, 0)
    assert _run_main(monkeypatch, tmp_path, part, ["-j", "1", "--quick"]) == 0
    (command, _), = seen
    options = command[command.index("pytest") + 1:]        # `python -m pytest` has a -m too
    expression = options[options.index("-m") + 1]
    assert run_suite.PROJECT_DESELECT in expression and run_suite.QUICK_DESELECT in expression
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'-m \\"{run_suite.PROJECT_DESELECT}\\"' in pyproject, (
        "the runner's copy of the project's `-m` has drifted from pyproject.toml")


def test_without_quick_no_marker_expression_is_passed(tmp_path, monkeypatch):
    part, seen = _part_class(GREEN, 0)
    assert _run_main(monkeypatch, tmp_path, part, ["-j", "1"]) == 0
    (command, _), = seen
    assert "-m" not in command[command.index("pytest") + 1:]


# ---------------------------------------------------------------------------
# 2a: --affected, the mapping
# ---------------------------------------------------------------------------

def _tree(root: Path) -> list[str]:
    """A small repository: a leaf in core, a module that imports it, a UI module that
    imports that through a relative import, a module nothing imports, and tests that
    reach them in the ways tests do."""
    files = {
        "app/__init__.py": "",
        "app/core/__init__.py": "",
        "app/core/leaf.py": "X = 1\n",
        "app/index/__init__.py": "",
        "app/index/mid.py": "from app.core import leaf\n",
        "app/ui/__init__.py": "",
        "app/ui/top.py": "from ..index import mid\n",
        "app/other.py": "import json\nthing = 1\n",
        "tests/unit/test_leaf.py": "from app.core.leaf import X\n",
        "tests/unit/test_mid.py": "def test_it():\n    import app.index.mid\n",
        "tests/unit/test_top.py": "from app.ui import top\n",
        "tests/unit/test_other.py": "def test_it(monkeypatch):\n"
                                   "    monkeypatch.setattr('app.other.thing', 2)\n",
        "tests/unit/test_none.py": "def test_nothing():\n    pass\n",
        "tests/unit/test_doc.py": "TEXT = open('docs/HANDOFF.md').read()\n",
        "tests/conftest.py": "",
        "docs/HANDOFF.md": "# Hand-off\n",
        "docs/WORKORDER-CONVENTIONS.md": "| Test | Where |\n|---|---|\n"
                                         "| `test_nothing` | `test_none.py` |\n",
        "assets/icon.png": "",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return sorted(f for f in files if f.startswith("tests/") and Path(f).name.startswith("test_"))


def _select(root: Path, changed: list[str], **kwargs):
    return affected.select(changed, root, _tree_tests(root), **kwargs)


def _tree_tests(root: Path) -> list[str]:
    return sorted(p.relative_to(root).as_posix() for p in (root / "tests").rglob("test_*.py"))


def test_a_changed_leaf_selects_the_tests_that_import_it_and_the_modules_above_it(tmp_path):
    _tree(tmp_path)
    chosen = _select(tmp_path, ["app/core/leaf.py"])
    assert not chosen.whole
    assert chosen.files == ["tests/unit/test_leaf.py", "tests/unit/test_mid.py",
                            "tests/unit/test_top.py"]
    assert chosen.reasons["tests/unit/test_top.py"] == ["imports app.ui.top"]


def test_a_relative_import_is_resolved_so_the_ui_module_is_reached(tmp_path):
    _tree(tmp_path)
    graph = affected.module_graph(tmp_path)
    assert "app.ui.top" in graph["app.index.mid"]
    assert affected.reverse_closure(graph, {"app.core.leaf"}) == {
        "app.core.leaf", "app.index.mid", "app.ui.top"}


def test_a_changed_top_module_selects_only_its_own_test(tmp_path):
    _tree(tmp_path)
    assert _select(tmp_path, ["app/ui/top.py"]).files == ["tests/unit/test_top.py"]


def test_a_dotted_string_in_a_test_counts_as_reaching_the_module(tmp_path):
    _tree(tmp_path)
    assert _select(tmp_path, ["app/other.py"]).files == ["tests/unit/test_other.py"]


def test_a_changed_test_file_runs_itself(tmp_path):
    _tree(tmp_path)
    chosen = _select(tmp_path, ["tests/unit/test_none.py"])
    assert chosen.files == ["tests/unit/test_none.py"]
    assert chosen.reasons["tests/unit/test_none.py"] == ["changed"]


def test_a_changed_document_selects_the_test_that_names_it(tmp_path):
    _tree(tmp_path)
    chosen = _select(tmp_path, ["docs/HANDOFF.md"])
    assert chosen.files == ["tests/unit/test_doc.py"]
    assert chosen.reasons["tests/unit/test_doc.py"] == ["names HANDOFF.md"]


def test_the_always_run_set_comes_from_the_conventions_table(tmp_path):
    _tree(tmp_path)
    assert affected.always_run(tmp_path) == ["tests/unit/test_none.py"]
    chosen = _select(tmp_path, ["app/ui/top.py"], always=affected.always_run(tmp_path))
    assert chosen.files == ["tests/unit/test_none.py", "tests/unit/test_top.py"]
    assert chosen.reasons["tests/unit/test_none.py"] == ["always runs"]


def test_the_shared_fixtures_changing_means_everything(tmp_path):
    _tree(tmp_path)
    chosen = _select(tmp_path, ["app/ui/top.py", "tests/conftest.py"])
    assert chosen.whole and "tests/conftest.py changed" in chosen.why_whole
    assert chosen.files == _tree_tests(tmp_path)


def test_most_of_the_suite_means_all_of_it_and_says_so(tmp_path):
    _tree(tmp_path)
    chosen = _select(tmp_path, ["app/core/leaf.py", "app/other.py"])     # 4 of 6
    assert chosen.whole and "4 of 6 test files" in chosen.why_whole


def test_a_change_no_test_names_is_said_so(tmp_path):
    _tree(tmp_path)
    chosen = _select(tmp_path, ["assets/icon.png"])
    assert chosen.files == [] and chosen.unmapped == ["assets/icon.png"]
    assert "no test names it: assets/icon.png" in chosen.describe(6)


def test_describe_says_what_was_chosen_and_why(tmp_path):
    _tree(tmp_path)
    text = _select(tmp_path, ["app/ui/top.py"]).describe(6)
    assert text.startswith("1 of 6 test files can see this change:")
    assert "tests/unit/test_top.py  <- imports app.ui.top" in text


def test_changed_paths_reads_this_repository():
    """Real git, this checkout: a list of existing repository paths, nothing more
    is asserted about what is in flight."""
    paths = affected.changed_paths(ROOT)
    assert isinstance(paths, list)
    assert all((ROOT / p).exists() for p in paths)
    assert not any(p.startswith((".claude/", "_Knowledge/")) for p in paths)


def test_on_this_repository_the_load_bearing_tests_always_run():
    """The §0 table and the fixed four, all real files."""
    always = affected.always_run(ROOT)
    assert set(always) >= set(affected.ALWAYS)
    assert "tests/unit/test_layering_below_the_ui.py" in always
    assert all((ROOT / f).is_file() for f in always)


# ---------------------------------------------------------------------------
# 2a: --affected, wired into the runner
# ---------------------------------------------------------------------------

def test_affected_runs_what_the_selection_chose_and_prints_why(tmp_path, monkeypatch, capsys):
    part, seen = _part_class(GREEN, 0)
    monkeypatch.setattr(affected, "changed_paths", lambda root, ref: ["app/x.py"])
    chosen = affected.Selection(files=["tests/unit/test_ok.py"],
                                reasons={"tests/unit/test_ok.py": ["imports app.x"]})
    monkeypatch.setattr(affected, "select", lambda *a, **k: chosen)
    files = ("tests/unit/test_ok.py", "tests/unit/test_other.py")
    assert _run_main(monkeypatch, tmp_path, part, ["--affected", "-j", "1"], files=files) == 0
    (command, _), = seen
    assert "tests/unit/test_ok.py" in command and "tests/unit/test_other.py" not in command
    out = capsys.readouterr().out
    assert "1 changed file(s) against HEAD" in out
    assert "tests/unit/test_ok.py  <- imports app.x" in out


def test_affected_with_nothing_to_run_says_so_and_starts_no_part(tmp_path, monkeypatch, capsys):
    part, seen = _part_class(GREEN, 0)
    monkeypatch.setattr(affected, "changed_paths", lambda root, ref: [])
    monkeypatch.setattr(affected, "select", lambda *a, **k: affected.Selection(files=[]))
    assert _run_main(monkeypatch, tmp_path, part, ["--affected", "origin/main"]) == 0
    assert seen == []
    assert "nothing to run" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 2c: the docs and the tasks describe the runner as it is
# ---------------------------------------------------------------------------

def test_the_docs_and_the_tasks_describe_the_runner_as_it_now_is():
    readme = (ROOT / "scripts" / "README.md").read_text(encoding="utf-8")
    vscode = (ROOT / "docs" / "VSCODE.md").read_text(encoding="utf-8")
    tasks = (ROOT / ".vscode" / "tasks.json").read_text(encoding="utf-8")
    for text, where in ((readme, "scripts/README.md"), (vscode, "docs/VSCODE.md")):
        assert "--affected" in text and "--quick" in text, f"{where} does not say"
    assert "Run the tests affected by your changes" in tasks
    assert "Quick check (no Qt, no slow tests)" in tasks
    assert "Run the tests affected by your changes" in vscode
