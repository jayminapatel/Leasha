r"""The tree an installed copy has, and the guard that keeps it honest.

Layer: L0

Asked for: *"organize a structure under Leasha which are the master files as if
they are installed in final environment.. i.e. production and run like that so
that can be tested too"*.

`scripts/stage.py` builds that tree. The reason it is a script rather than a
folder somebody maintains is drift: **a second copy of the source that has
quietly gone out of date is worse than none, because it is trusted.** These
tests are the other half of that argument - they assert that the manifest still
covers everything the application actually needs, so a package added next month
cannot be left out of the install and discovered by a user.

The one that earns its place is `test_every_folder_the_application_imports_is_shipped`:
it reads the imports rather than the manifest, so it fails when the *code*
changes, which is the moment the manifest is wrong.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import stage as staging  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def staged(tmp_path):
    dest = tmp_path / "Leasha"
    staging.stage(ROOT, dest, Path(sys.executable))
    return dest


# --- the manifest cannot drift ---------------------------------------------

def test_every_folder_the_application_imports_is_shipped():
    """**Reads the imports, not the manifest.**

    A test that checks the manifest against itself passes for ever. This one
    fails the day somebody adds `app/reports/` importing a new top-level
    package that ships nowhere - which is the moment the manifest became wrong,
    rather than the day a user finds out.
    """
    ours: set[str] = set()
    for path in ROOT.iterdir():
        # Guarded: a checkout collects folders nobody can stat - a `.pytest_tmp`
        # written by Windows and read from a container, for one. A repo-hygiene
        # check that falls over on somebody's stray directory is a check people
        # start skipping.
        try:
            if path.is_dir() and not path.name.startswith((".", "_")):
                ours.add(path.name)
        except OSError:
            continue
    ours -= set(staging.EXCLUDED) | {"scripts", "docs", "build", "dist"}

    needed: set[str] = set()
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                needed |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                needed.add(node.module.split(".")[0])

    missing = sorted((needed & ours) - set(staging.SHIPPED))

    assert not missing, (
        f"{missing} are imported by the application and are not staged - an "
        f"installed copy would fail to start")


def test_the_test_suite_is_never_imported_at_module_scope():
    """**Not "never imported" - never imported where it cannot fail safely.**

    `app.cli evaluate --builtin` loads a corpus that genuinely ships with the
    tests, and that is a reasonable thing for a source checkout to offer. It
    does it inside the function, in a `try`, and answers with an `AppError`
    saying the corpus is not installed and what to use instead.

    A module-scope import is the one that cannot do that: it fails at start-up,
    before any handler exists, and takes the whole application with it. So the
    check is about *where*, and the first version of this test - which banned
    the import outright - would have made the application worse by forcing a
    working feature out of the source build.
    """
    offenders: list[str] = []
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:                    # top level only
            text = ast.unparse(node) if isinstance(
                node, (ast.Import, ast.ImportFrom)) else ""
            if text.startswith(("import tests", "from tests")):
                offenders.append(str(path.relative_to(ROOT)))

    assert not offenders, (
        f"{offenders} import the test suite at module scope - an installed "
        f"copy would fail to start")


def test_a_guarded_import_of_the_corpus_says_what_to_do_instead():
    """The other half: it must fail *usefully*. An `ImportError` traceback for
    a corpus that was never installed is a bug report about a design decision.
    """
    import inspect

    from app import cli

    source = inspect.getsource(cli)
    guarded = source.split("from tests.fixtures.evaluation")[1][:600]

    assert "except ImportError" in guarded
    assert "--questions" in guarded, "it does not say what to use instead"


def test_files_the_application_opens_by_path_are_shipped():
    """`doctor.py` is shelled out to by path, `config/` and `assets/` are read
    at runtime. None of them is an import, so the check above cannot see them -
    and each one absent is a feature that fails only once installed."""
    for name in ("doctor.py", "config", "assets", "VERSION"):
        assert name in staging.SHIPPED, f"{name} is read at runtime and is not staged"


# --- what the tree contains -------------------------------------------------

def test_the_application_and_its_configuration_are_there(staged):
    assert (staged / "app" / "cli.py").is_file()
    assert (staged / "config" / "extractors.toml").is_file()
    assert (staged / "VERSION").is_file()
    assert (staged / "doctor.py").is_file()


def test_the_test_suite_and_its_fixtures_are_not(staged):
    """Not a product. Also several hundred megabytes of generated PDFs."""
    assert not (staged / "tests").exists()
    assert not (staged / "tests" / "fixtures").exists()


def test_no_compiled_bytecode_is_copied(staged):
    """`__pycache__` is compiled against a different absolute path. Copying it
    is at best wasted space and at worst a stale module that shadows the source
    beside it."""
    assert not list(staged.rglob("__pycache__"))


def test_the_owner_s_env_is_never_copied(staged, tmp_path):
    """**Non-negotiable 11: `.env` is written by the application, never by the
    user - and never by a copy.** It holds this machine's paths; staging it
    would give the installed tree somebody else's index location, which is the
    one setting that must be wrong loudly rather than quietly."""
    assert not (staged / ".env").exists()


def test_the_log_folder_is_empty_rather_than_the_development_one(staged):
    """An installed copy has no history. Copying `logs/` would ship the
    developer's run logs to a user - and one of those now contains the resolved
    settings of the machine it was built on."""
    assert (staged / "logs").is_dir()
    assert not list((staged / "logs").rglob("*.log"))


def test_it_says_it_is_generated(staged):
    """An edit made in a generated folder vanishes without warning, and makes
    the tree disagree with the source it came from. Worth saying where somebody
    will read it."""
    readme = (staged / "README.txt").read_text(encoding="utf-8")

    assert "generated" in readme.lower()
    assert "stage.py" in readme


# --- how it is run ----------------------------------------------------------

def test_the_launchers_start_in_their_own_folder(staged):
    """**`cd /d "%~dp0"`, and it is the whole point.**

    Everything the application finds - `.env`, `logs\\`, `config\\` - is
    resolved from its own location. A launcher that inherits whatever directory
    a shortcut happened to start in is how a staged tree quietly reads the
    development tree's configuration, which is exactly the confusion this
    exercise exists to remove.
    """
    for name in ("Leasha.cmd", "leasha-cli.cmd"):
        text = (staged / name).read_text(encoding="utf-8")
        assert 'cd /d "%~dp0"' in text, f"{name} runs from wherever it was called"


def test_the_window_and_the_command_line_have_separate_launchers(staged):
    assert "app.main" in (staged / "Leasha.cmd").read_text(encoding="utf-8")
    assert "app.cli" in (staged / "leasha-cli.cmd").read_text(encoding="utf-8")


# --- staging again ----------------------------------------------------------

def test_staging_twice_removes_what_the_first_run_left(tmp_path):
    """A stale file from a previous layout is the thing that makes a staged
    tree lie about what ships."""
    dest = tmp_path / "Leasha"
    staging.stage(ROOT, dest, Path(sys.executable))
    (dest / "left-over.txt").write_text("from a previous layout", encoding="utf-8")

    staging.stage(ROOT, dest, Path(sys.executable))

    assert not (dest / "left-over.txt").exists()


def test_it_refuses_to_stage_over_the_source():
    """It deletes the destination first. Pointed at the checkout, that is the
    whole project."""
    with pytest.raises(SystemExit):
        staging.stage(ROOT, ROOT, Path(sys.executable))


def test_a_name_in_the_manifest_that_does_not_exist_is_reported(tmp_path, monkeypatch):
    """Named but absent means the manifest and the repository have drifted -
    which is the failure staging exists to make visible, not to hide."""
    monkeypatch.setattr(staging, "SHIPPED", ("app", "no-such-file"))
    written = staging.stage(ROOT, tmp_path / "Leasha", Path(sys.executable))

    assert any("MISSING" in line and "no-such-file" in line for line in written)
